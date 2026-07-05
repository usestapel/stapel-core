"""RevisionMixin contract: update_fields semantics + concurrent issuance.

Covers the review finding H-3 (catalog feature editor review):
- phantom revision: ``save(update_fields=["draft"])`` used to bump the
  in-memory revision (and the post_save event) without persisting it — the
  number was then reused by the next change and sync clients lost it forever;
- duplicate issuance: the docstring promised ``select_for_update`` but there
  was no locking, so two concurrent saves could read the same MAX(revision).

The suite runs on the SQLite minimal profile — the concurrency test doubles
as the sqlite-compatibility check for the process-local mutex path.
"""

import threading

import pytest
from django.db import connection, connections, models, transaction
from django.db.models.signals import post_save

from stapel_core.django.models import RevisionMixin


class RevContractItem(RevisionMixin):
    name = models.CharField(max_length=50, default="")
    draft = models.TextField(default="")

    class Meta:
        app_label = "users"


@pytest.fixture
def rev_table(transactional_db):
    with connection.schema_editor() as editor:
        editor.create_model(RevContractItem)
    yield
    with connection.schema_editor() as editor:
        editor.delete_model(RevContractItem)


@pytest.fixture
def captured_events():
    """Collect (pk, revision) as post_save receivers observe them."""
    events = []

    def receiver(sender, instance, **kwargs):
        events.append((instance.pk, instance.revision))

    post_save.connect(receiver, sender=RevContractItem, weak=False)
    yield events
    post_save.disconnect(receiver, sender=RevContractItem)


# ---------------------------------------------------------------------------
# update_fields contract
# ---------------------------------------------------------------------------


def test_update_fields_without_revision_does_not_bump(rev_table, captured_events):
    """A save scoped to non-synced fields is not a content change: DB row,
    in-memory instance and the post_save event all keep the old revision."""
    item = RevContractItem(name="a")
    item.save()  # revision 1
    assert item.revision == 1

    item.draft = "wip"
    item.save(update_fields=["draft"])

    assert item.revision == 1  # instance not phantom-bumped
    fresh = RevContractItem.objects.get(pk=item.pk)
    assert fresh.revision == 1  # DB unchanged
    assert fresh.draft == "wip"  # the scoped write itself persisted
    assert captured_events[-1] == (item.pk, 1)  # event matches persisted state


def test_update_fields_with_revision_opts_into_bump(rev_table, captured_events):
    """Explicit opt-in: 'revision' in update_fields bumps AND persists —
    the event and the DB row carry the same new number."""
    item = RevContractItem(name="a")
    item.save()  # revision 1

    item.name = "b"
    item.save(update_fields=["name", "revision"])

    assert item.revision == 2
    fresh = RevContractItem.objects.get(pk=item.pk)
    assert fresh.revision == 2
    assert fresh.name == "b"
    assert captured_events[-1] == (item.pk, 2)


def test_update_fields_accepts_one_shot_iterable(rev_table):
    """The revision-membership check must not consume a generator before
    Django sees it (fields must still be persisted)."""
    item = RevContractItem(name="a")
    item.save()
    item.draft = "gen"
    item.save(update_fields=iter(["draft"]))
    assert RevContractItem.objects.get(pk=item.pk).draft == "gen"


def test_phantom_number_is_not_lost_for_sync_clients(rev_table, captured_events):
    """H-3 repro: draft-only save no longer emits a revision that the next
    content change silently reuses — a sync client acting on every event's
    revision never skips a change."""
    item = RevContractItem(name="a")
    item.save()  # revision 1

    item.draft = "autosave"
    item.save(update_fields=["draft"])  # no bump, event carries 1

    client_max = max(rev for _, rev in captured_events)  # client stored 1
    item.name = "real change"
    item.save()  # revision 2 — the next content change

    changes = RevContractItem.get_changes_since(min_revision=client_max)
    assert [obj.pk for obj in changes] == [item.pk]  # change is visible
    assert captured_events[-1] == (item.pk, 2)


def test_full_save_and_nested_atomic_still_bump(rev_table):
    """Regression: plain save() bumps as before, including inside a caller's
    outer transaction.atomic (the categories mutate_and_emit pattern)."""
    item = RevContractItem(name="a")
    item.save()
    with transaction.atomic():
        item.name = "b"
        item.save()
    assert item.revision == 2
    assert RevContractItem.objects.get(pk=item.pk).revision == 2


# ---------------------------------------------------------------------------
# concurrent issuance (also the sqlite-compatibility check)
# ---------------------------------------------------------------------------


def test_concurrent_saves_issue_unique_revisions(rev_table):
    """Two saves must never share a revision number — a duplicate makes
    get_changes_since lose one of them forever. Each of THREADS threads
    re-saves its own row SAVES times; every issued number must be unique
    and the final MAX must equal the total number of bumps."""
    THREADS = 8
    SAVES = 5

    rows = []
    for i in range(THREADS):
        row = RevContractItem(name=f"row-{i}")
        row.save()  # revisions 1..THREADS, sequential
        rows.append(row)

    barrier = threading.Barrier(THREADS)
    issued = []
    issued_lock = threading.Lock()
    errors = []

    def worker(row):
        try:
            barrier.wait()  # maximize contention on MAX(revision) issuance
            for _ in range(SAVES):
                row.save()
                with issued_lock:
                    issued.append(row.revision)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)
        finally:
            connections.close_all()  # this thread's own connections

    threads = [threading.Thread(target=worker, args=(row,)) for row in rows]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(issued) == THREADS * SAVES
    assert len(set(issued)) == len(issued), "duplicate revision issued"
    assert RevContractItem.get_max_revision() == THREADS + THREADS * SAVES
