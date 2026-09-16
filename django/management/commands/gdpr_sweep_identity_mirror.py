"""Anonymise mirror rows for people whose erasure already completed.

WHY THIS IS NEEDED AT ALL
-------------------------
The identity mirror (``stapel_core.gdpr.identity``) is claimed from the
release that shipped it. Anyone erased BEFORE that release got a complete
receipt set from every owner that existed at the time, and their email
survived in every service that mirrored them — the orchestrator will not ask
again, because as far as it is concerned that erasure is finished.

So the fix closes the future and this closes the past. Without it, "we erased
you" stays false for everybody already erased, and no amount of correct
behaviour afterwards repairs it.

HOW IT FINDS THEM
-----------------
By the tombstone, not by a list. stapel-gdpr's ``erase_identity`` anonymises
the primary row to ``deleted-<hex>@deleted.invalid``, so the identity owner is
the register of who has been erased, and this asks it for that register — over
the comm bus by default (``auth.user_projection`` is not suitable; the tombstone
query is), or from an explicit list of ids when a deployment would rather not
have services querying each other.

Because a mirror row for an erased person is by definition one whose local
copy still carries an address while the owner's does not, the safe input is
always the id list: ``--user-ids-file`` takes it, one per line. That is the
mode a deployment should use, and the one the docs show::

    # on the identity owner
    manage.py shell -c "from django.contrib.auth import get_user_model as g; \\
        print('\\n'.join(str(i) for i in g().objects.filter(
            email__endswith='@deleted.invalid').values_list('id', flat=True)))" \\
        > /tmp/erased.txt

    # on every service that mirrors identities
    manage.py gdpr_sweep_identity_mirror --user-ids-file /tmp/erased.txt --dry-run
    manage.py gdpr_sweep_identity_mirror --user-ids-file /tmp/erased.txt

WHAT IT WILL NOT DO
-------------------
It refuses to run where this process is not a mirror
(``JWT_CREATE_USERS_FROM_TOKEN`` off), because there the local user table is
the authoritative identity and anonymising it from a list would erase accounts
nobody asked about. That refusal is the whole safety property: the command
cannot be pointed at the identity owner by mistake.
"""
import sys

from django.core.management.base import BaseCommand, CommandError

from stapel_core.gdpr.identity import (
    erase_subject,
    is_tombstoned,
    mirrors_identities,
)


class Command(BaseCommand):
    help = "Anonymise identity-mirror rows for users already erased upstream."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user-ids-file",
            required=True,
            help="Ids of already-erased users, one per line ('-' for stdin).",
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if not mirrors_identities():
            raise CommandError(
                "This process does not mirror identities "
                "(JWT_CREATE_USERS_FROM_TOKEN is off), so its user table is "
                "the authoritative identity, not a mirror. Refusing: "
                "anonymising it from a list would erase accounts nobody "
                "asked about. Run this on the services that CONSUME the "
                "identity."
            )

        from django.contrib.auth import get_user_model

        User = get_user_model()
        path = options["user_ids_file"]
        stream = sys.stdin if path == "-" else open(path, encoding="utf-8")
        try:
            ids = [
                line.strip() for line in stream
                if line.strip() and not line.strip().startswith("#")
            ]
        finally:
            if stream is not sys.stdin:
                stream.close()

        present = User.objects.filter(pk__in=ids)
        # A row already tombstoned is not work. Decided by RECOGNISING the
        # tombstone, not by testing whether the identity fields are empty:
        # after an anonymisation they are not empty, they hold the tombstone,
        # so an emptiness test counts every already-swept row as outstanding.
        #
        # That is exactly the bug `erase_subject` had and this command
        # inherited — caught by running the sweep twice on a live fleet, where
        # the second run reported one row still to do and would have claimed
        # to anonymise it while `erase_subject` correctly did nothing. A
        # command that reports work it did not do is worse than one that
        # refuses, because the number is what somebody signs off against.
        dirty = [u for u in present if not is_tombstoned(u)]

        self.stdout.write(
            f"erased ids supplied : {len(ids)}\n"
            f"mirrored here       : {present.count()}\n"
            f"still identifying   : {len(dirty)}"
        )

        if not dirty:
            self.stdout.write(self.style.SUCCESS("nothing to sweep"))
            return

        if options["dry_run"]:
            for u in dirty[:20]:
                self.stdout.write(f"  would anonymise: {u.pk}")
            if len(dirty) > 20:
                self.stdout.write(f"  … and {len(dirty) - 20} more")
            self.stdout.write(
                self.style.WARNING(f"DRY RUN — {len(dirty)} row(s) NOT changed")
            )
            return

        swept = 0
        for user in dirty:
            # Through the owner callable, not a second implementation: the
            # sweep and the live erasure must leave rows in the same state or
            # "erased" means two things.
            if erase_subject("account", str(user.pk)):
                swept += 1
        self.stdout.write(self.style.SUCCESS(f"anonymised {swept} mirror row(s)"))
