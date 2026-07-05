"""Tests for the emit-check static gate (stapel_core.lint.emit_check)."""
import textwrap
from pathlib import Path

from stapel_core.lint.emit_check import check_source, iter_python_files, main


def _check(src: str):
    return check_source(textwrap.dedent(src), Path("mod.py"))


def _codes(src: str):
    return [f.code for f in _check(src)]


# ---------------------------------------------------------------------------
# EMIT001 — emit inside except handler
# ---------------------------------------------------------------------------


def test_emit_in_except_handler_flagged():
    codes = _codes("""
        def f(obj):
            try:
                obj.save()
            except Exception:
                emit("thing.failed", {})
    """)
    assert codes == ["EMIT001"]


def test_emit_attribute_call_in_except_flagged():
    codes = _codes("""
        def f(obj):
            try:
                obj.save()
            except ValueError:
                events.emit_listing_removed(obj)
    """)
    assert codes == ["EMIT001"]


# ---------------------------------------------------------------------------
# EMIT002 — swallowed emit (the categories C1 shape)
# ---------------------------------------------------------------------------


def test_swallowed_emit_flagged():
    codes = _codes("""
        def publish(category_id):
            try:
                emit("category.changed", {"category_id": category_id})
            except Exception:
                logger.exception("failed")
    """)
    assert codes == ["EMIT002"]


def test_bare_except_swallow_flagged():
    codes = _codes("""
        def publish():
            try:
                emit("x.y", {})
            except:
                pass
    """)
    assert codes == ["EMIT002"]


def test_narrow_except_not_flagged():
    codes = _codes("""
        def publish():
            try:
                emit("x.y", {})
            except KeyError:
                raise ValueError("bad payload")
    """)
    assert codes == []


def test_broad_except_that_reraises_not_flagged():
    codes = _codes("""
        def publish():
            try:
                emit("x.y", {})
            except Exception as exc:
                logger.exception("failed")
                raise
    """)
    assert codes == []


def test_try_in_outer_function_does_not_swallow_nested_def():
    codes = _codes("""
        def outer():
            try:
                def inner():
                    emit("x.y", {})
                return inner
            except Exception:
                pass
    """)
    assert codes == []


# ---------------------------------------------------------------------------
# EMIT003 — mutation + emit without a shared atomic (the listings L2 shape)
# ---------------------------------------------------------------------------


def test_save_then_emit_without_atomic_flagged():
    codes = _codes("""
        def publish_listing(listing):
            listing.status = "published"
            listing.save()
            events.emit_listing_published(listing)
    """)
    assert codes == ["EMIT003"]


def test_save_and_emit_inside_transaction_atomic_ok():
    codes = _codes("""
        def publish_listing(listing):
            with transaction.atomic():
                listing.save()
                events.emit_listing_published(listing)
    """)
    assert codes == []


def test_save_and_emit_inside_mutate_and_emit_ok():
    codes = _codes("""
        def publish_listing(listing):
            with mutate_and_emit() as emit_event:
                listing.save()
                emit_event("listing.published", {})
    """)
    assert codes == []


def test_atomic_decorator_ok():
    codes = _codes("""
        @transaction.atomic
        def finalize(recording):
            recording.save(update_fields=["status"])
            events.emit_uploaded(recording)
    """)
    assert codes == []


def test_atomic_decorator_with_using_ok():
    codes = _codes("""
        @transaction.atomic(using="other")
        def finalize(recording):
            recording.save()
            emit("recording.uploaded", {})
    """)
    assert codes == []


def test_emit_only_helper_without_orm_writes_not_flagged():
    # Leaf emit helpers (recordings events.py style) — the caller holds the
    # transaction; nothing to couple with locally.
    codes = _codes("""
        def emit_uploaded(recording):
            emit("recording.uploaded", {"id": str(recording.id)})
    """)
    assert codes == []


def test_emit_under_atomic_but_save_outside_is_a_known_gap():
    # Documented limitation: EMIT003 only requires the *emit* to sit under an
    # atomic construct.
    codes = _codes("""
        def f(obj):
            obj.save()
            with transaction.atomic():
                emit("x.y", {})
    """)
    assert codes == []


# ---------------------------------------------------------------------------
# EMIT004 — emit inside on_commit callback
# ---------------------------------------------------------------------------


def test_emit_in_on_commit_lambda_flagged():
    codes = _codes("""
        def f(obj):
            with transaction.atomic():
                obj.save()
                transaction.on_commit(lambda: emit("x.y", {}))
    """)
    assert codes == ["EMIT004"]


# ---------------------------------------------------------------------------
# Suppression pragma + file iteration + CLI
# ---------------------------------------------------------------------------


def test_pragma_suppresses():
    codes = _codes("""
        def f(obj):
            obj.save()
            emit("x.y", {})  # emit-check: ok — caller wraps in atomic
    """)
    assert codes == []


def test_syntax_error_reported_not_crashing():
    findings = _check("def f(:\n")
    assert [f.code for f in findings] == ["EMIT000"]


def test_iter_python_files_skips_tests_and_migrations(tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text("x = 1\n")
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "0001_initial.py").write_text("x = 1\n")
    (tmp_path / "conftest.py").write_text("x = 1\n")
    files = [p.name for p in iter_python_files([tmp_path])]
    assert files == ["mod.py"]


def test_main_exit_codes(tmp_path, capsys):
    clean = tmp_path / "clean.py"
    clean.write_text("def emit_x():\n    emit('a.b', {})\n")
    assert main([str(tmp_path)]) == 0

    dirty = tmp_path / "dirty.py"
    dirty.write_text(
        "def f(obj):\n"
        "    obj.save()\n"
        "    emit('a.b', {})\n"
    )
    assert main([str(tmp_path)]) == 1
    out = capsys.readouterr()
    assert "EMIT003" in out.out
