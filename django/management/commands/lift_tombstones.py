"""Undo deletion tombstones written for accounts that were never deleted.

Why this exists
---------------
A deletion tombstone is a fact the fleet publishes about an account, and
``get_or_create_user_from_jwt`` consults it before any claim, in every
consumer-mode service. That is exactly right when the account really was
deleted — and unrecoverable when it was not.

Up to 0.60.0 it could be written for an account that was perfectly alive. The
shadow-row re-key path in ``stapel_core.django.jwt.utils`` compared the
issuer id (a ``str`` off the JWT claim) with the local row's primary key (a
``uuid.UUID`` for a UUID key), found them "different", and deleted the row to
re-create it under an id it already had. The ``post_delete`` receiver did its
job and tombstoned a live person, fleet-wide, for the tombstone's TTL — a
week by default. Measured on a deployed stand, 2026-09-04: 8 sellers burned
in 24 h, every one of them logging ``updating PK from X to X`` with the two
ids identical, and then answering 401 from every service in the fleet while
their session cookie stayed valid.

0.60.1 closes the hole three ways (text comparison, `shadow_rekey` around the
re-key's delete, and creating before repairing so ordinary concurrency never
reaches the repair). This command cleans up after the versions that had it.

What it does
------------
Runs **at the issuer** — the service whose database IS the account store
(``JWT_CREATE_USERS_FROM_TOKEN=False``; svc-auth in a split fleet). Only the
issuer can answer "is this account actually alive?", and because the
revocation namespace is fleet-wide, lifting the tombstone once there heals
every peer. No cross-service call is needed and none is made.

Consumers re-mirror the row themselves: a lifted uid's next request is an
ordinary first contact, and the shadow row is created from the token exactly
as it would be for a new account.

Usage::

    # what WOULD be lifted (default: changes nothing)
    python manage.py lift_tombstones --issuer-check

    # do it
    python manage.py lift_tombstones --issuer-check --apply

    # only these, e.g. the uids an incident report named
    python manage.py lift_tombstones --issuer-check --apply \\
        --uid 1fefe21a-e3a1-410f-8ff4-b1bd6d2337b2

``--issuer-check`` is not optional dressing: without an issuer verdict a lift
is a way for a token to undo a deletion, which is the whole thing tombstones
exist to prevent. The flag is required, and the command refuses to run
anywhere but the issuer.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Lift deletion tombstones for uids the issuer still holds as active "
        "(cleans up after the pre-0.60.1 shadow-row re-key defect)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--issuer-check",
            action="store_true",
            help=(
                "Required. Verify each uid against the issuer's own user "
                "store before lifting; only active accounts are lifted."
            ),
        )
        parser.add_argument(
            "--uid",
            action="append",
            default=[],
            metavar="UID",
            help=(
                "Check only this uid (repeatable). Without it the command "
                "enumerates the tombstones in the revocation namespace."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually lift. Without it the command only reports.",
        )

    def handle(self, *_args, **options):
        from django.conf import settings

        from stapel_core.django.jwt.tombstone import (
            TOMBSTONE_PREFIX,
            is_user_tombstoned,
            lift_tombstone,
        )
        from stapel_core.django.jwt.utils import _get_user_model

        if not options["issuer_check"]:
            raise CommandError(
                "--issuer-check is required. Lifting a tombstone without an "
                "issuer verdict lets a still-valid token undo a deletion, "
                "which is the one thing tombstones exist to prevent."
            )

        if bool(getattr(settings, "JWT_CREATE_USERS_FROM_TOKEN", False)):
            raise CommandError(
                "This service is a CONSUMER (JWT_CREATE_USERS_FROM_TOKEN=True): "
                "its user rows are shadow copies and cannot answer whether an "
                "account is alive. Run this at the issuer (the service whose "
                "database is the account store). The revocation namespace is "
                "fleet-wide, so one lift there heals every peer, and consumers "
                "re-mirror the row on the uid's next request."
            )

        uids = list(options["uid"])
        enumerated = False
        if not uids:
            uids = self._enumerate(TOMBSTONE_PREFIX)
            enumerated = True
            if uids is None:
                raise CommandError(
                    "This cache backend cannot enumerate keys, so the "
                    "tombstones cannot be listed. Name them explicitly with "
                    "--uid (repeatable) — an incident report's uid list, or "
                    "the 'Deletion tombstone written for user <uid>' lines in "
                    "the services' logs."
                )

        if not uids:
            self.stdout.write("No tombstones found in the revocation namespace.")
            return

        User = _get_user_model()
        lifted, kept, missing = [], [], []
        for uid in uids:
            if enumerated is False and not is_user_tombstoned(uid):
                missing.append(uid)
                continue
            row = User.objects.filter(pk=uid).first()
            if row is None or not getattr(row, "is_active", True):
                kept.append(uid)
                continue
            if options["apply"]:
                report = lift_tombstone(uid)
                lifted.append((uid, report.outcome, bool(report)))
            else:
                lifted.append((uid, "WOULD_LIFT", True))

        for uid, outcome, ok in lifted:
            mark = "+" if ok else "!"
            self.stdout.write(f"  {mark} {uid}  {outcome}  (issuer: active)")
        for uid in kept:
            self.stdout.write(
                f"  . {uid}  KEPT  (issuer: no active account — a real deletion)"
            )
        for uid in missing:
            self.stdout.write(f"  . {uid}  NOT TOMBSTONED  (nothing to lift)")

        verb = "lifted" if options["apply"] else "would lift"
        self.stdout.write(
            f"\n{len(lifted)} {verb}, {len(kept)} kept, {len(missing)} not tombstoned."
        )
        if not options["apply"]:
            self.stdout.write("Nothing was changed — re-run with --apply.")

    @staticmethod
    def _enumerate(prefix: str):
        """The tombstoned uids, or ``None`` when the backend cannot list keys."""
        from stapel_core.core.revocation_store import revocation_cache

        try:
            keys = revocation_cache().keys(f"{prefix}*")
        except Exception:
            return None
        uids = []
        for key in keys or []:
            text = key.decode() if isinstance(key, bytes) else str(key)
            # django-redis hands back the app-level key; a raw client would
            # hand back the fully namespaced one. Take everything after the
            # LAST occurrence of the prefix either way.
            if prefix in text:
                uids.append(text.rsplit(prefix, 1)[1])
        return uids
