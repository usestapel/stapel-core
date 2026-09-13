"""The shadow user row, materialised from an EVENT instead of a token.

In a fleet, identities live in one service and every other service holds a
shadow copy of the rows it needs a foreign key to. That copy is written by
:func:`stapel_core.django.jwt.utils.get_or_create_user_from_jwt` the first
time the account presents a token HERE — which is the only moment an HTTP
request can supply one.

A bus consumer has no token, and it usually runs FIRST. The publisher emits
inside the registration request; the account's first call to this particular
service comes later, if it ever comes at all. So every handler that inserts a
row keyed on ``users.id`` in reaction to an event is racing a writer it does
not control, and losing is the common case, not the corner:

* ``iron-recordings`` 2026-09-13 — ``workspace.personal.created`` arrived
  before the shadow row and ``ZoomIngestSettings.objects.get_or_create`` died
  on ``ForeignKeyViolation ... Key (user_id)=(13484e5c-…) is not present in
  table "users"``, 3 events, 22 failed attempts, all three parked in the DLQ.
  The user's Zoom ingest default was never written and never retried.
* ``stapel_workspaces`` 0.30.3 — the same race, the other outcome: the
  consumer logged "user not found, skipping", committed the offset, and the
  account had no personal workspace for the rest of its life.
* ``stapel_billing`` / ``billing_ext`` (audit minor #11) — a charge for
  somebody who had never opened the billing screen was dropped with a
  warning, so everything they consumed in that window was free.

Three services, three independently written work-arounds, one seam. This is
that seam.

WHAT IT IS NOT. It does not authenticate anybody and it does not sync an
account. An event is not a token: it may not assert ``is_staff``,
``is_superuser``, ``staff_roles`` or ``is_active``, and this function refuses
to pass any of them on even when the payload carries them. Those fields
arrive from the right authority the first time the person presents a JWT.

WHY IT NEVER TOUCHES AN EXISTING ROW. ``get_or_create_user_from_jwt`` is a
SYNC as well as a get-or-create: in consumer mode it REPLACES ``is_staff``
and ``is_superuser`` from the claims on every call, because a token is the
issuer speaking about the account right now. Feed it an event payload, which
by the rule above carries no privileges, and it reads that silence as
"``False``" and demotes a staff shadow row — from a handler whose only
business was a foreign key. So the existing row is returned untouched and
the creating seam is reached only when there is no row at all.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

#: Claims an event may never speak for. See the module docstring.
_PRIVILEGE_FIELDS = ("is_staff", "is_superuser", "staff_roles", "is_active")

#: Identity fields an event MAY carry through to the created row.
_IDENTITY_FIELDS = ("email", "username", "phone", "auth_type")


def _create_users_from_token() -> bool:
    from stapel_core.django.jwt.utils import _create_users_from_token as _flag

    return _flag()


def _deterministic_username(uid: str, *, anonymous: bool) -> str:
    """A username derived from the id, not from ``uuid4()``.

    The seam generates ``user_<random hex>`` when no username is given, so
    two retries of the same event would propose two different names. Only one
    of them can win (the loser collides on the primary key and re-reads), but
    a name nobody can predict is also a name nobody can recognise in a table,
    and a deterministic one makes the whole call replayable. The ``anon_``
    prefix is the same one ``User.create_anonymous`` uses, so
    ``upgrade_username_from_anonymous`` still recognises the row it renames
    when the guest signs up.
    """
    suffix = uuid.UUID(uid).hex[:12] if _is_uuid(uid) else str(uid)[:12]
    return f"{'anon' if anonymous else 'user'}_{suffix}"


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def ensure_shadow_user(user_id, payload: Optional[Mapping[str, Any]] = None):
    """The local user row for *user_id*, created from *payload* if missing.

    Call this at the top of any event handler that is about to write a row
    with a foreign key to ``users``, passing the event payload as it came.

    *payload* is optional and may carry ``email``, ``username``, ``phone``,
    ``auth_type`` and ``is_anonymous``. Anything else — privileges above
    all — is ignored on purpose. An anonymous account's ``email`` is forced
    to ``NULL`` rather than an empty string, because the column is unique and
    two guests would collide on ``""``; its username defaults to
    ``anon_<id>``.

    Returns the ``User``, or ``None`` when there is genuinely nobody:

    * no id in the payload;
    * the account was deleted or deactivated at the issuer (consumer mode;
      the same fleet-wide facts ``get_or_create_user_from_jwt`` consults, so
      a handler cannot revive a tombstoned account by the back door);
    * ``JWT_CREATE_USERS_FROM_TOKEN`` is off — the deployment says its own
      user table is authoritative, and a background worker is the last place
      allowed to overrule that;
    * creation failed, which is logged with the exception.

    A ``None`` is a decision, not a crash: the caller skips its write and
    says so. It is idempotent — a second call returns the first call's row
    and writes nothing.
    """
    uid = str(user_id).strip() if user_id is not None else ""
    if not uid or uid.lower() in ("none", "null"):
        logger.error("ensure_shadow_user called with no user id (payload=%r)", payload)
        return None

    from stapel_core.django.jwt.utils import _deactivated, _tombstoned

    consumer_mode = _create_users_from_token()

    # Lifecycle first, before the row is consulted at all — the same order
    # and the same reason as the JWT seam: in consumer mode a row that is
    # present locally says nothing about whether the account still exists at
    # the issuer, so "deleted" has to be a fact carried from there.
    if consumer_mode:
        if _tombstoned(uid):
            logger.warning(
                "ensure_shadow_user refused: user %s was deleted at the issuer", uid
            )
            return None
        if _deactivated(uid):
            logger.warning(
                "ensure_shadow_user refused: user %s is deactivated at the issuer", uid
            )
            return None

    from django.contrib.auth import get_user_model

    User = get_user_model()

    try:
        existing = User.objects.filter(pk=uid).first()
    except Exception:
        logger.error("ensure_shadow_user: %r is not a usable user id", uid)
        return None
    if existing is not None:
        # Untouched on purpose — see the module docstring.
        return existing

    if not consumer_mode:
        logger.warning(
            "ensure_shadow_user: user %s is unknown here and "
            "JWT_CREATE_USERS_FROM_TOKEN is off — not creating a shadow row",
            uid,
        )
        return None

    data = dict(payload or {})
    for field in _PRIVILEGE_FIELDS:
        data.pop(field, None)

    identity: dict[str, Any] = {"user_id": uid}
    for field in _IDENTITY_FIELDS:
        value = data.get(field)
        if value:
            identity[field] = value

    anonymous = bool(data.get("is_anonymous"))
    if "is_anonymous" in data:
        identity["is_anonymous"] = anonymous
    if anonymous:
        # A guest has no email anchor. The column is unique, so "" would make
        # the second guest collide with the first.
        identity.pop("email", None)
        identity.setdefault("auth_type", "anonymous")
    identity.setdefault("username", _deterministic_username(uid, anonymous=anonymous))

    from stapel_core.django.jwt.utils import get_or_create_user_from_jwt

    user = get_or_create_user_from_jwt(identity)
    if user is None:
        logger.error(
            "ensure_shadow_user: could not materialise the shadow row for "
            "user %s from the event (payload=%r)",
            uid,
            payload,
        )
        return None

    logger.info("ensure_shadow_user: materialised the shadow row for user %s", uid)
    return user


__all__ = ["ensure_shadow_user"]
