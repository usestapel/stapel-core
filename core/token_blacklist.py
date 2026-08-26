"""
Token blacklist backed by Django's cache framework.

Uses whatever cache backend is configured (LocMemCache in tests, Redis in
production), but through the SHARED revocation namespace
(``stapel_core.core.revocation_store``) rather than
``django.core.cache.cache`` — otherwise each service's own ``KEY_PREFIX``
isolates the entry and a token revoked in one service stays valid in every
other. See that module for the full account of the defect.

The two verbs that REMOVE (``remove_from_blacklist``, ``clear_all``) report a
:class:`~stapel_core.core.drop.DropReport` since 0.47.0, instead of ``True``
for "the call did not raise" — same key space, same namespace mismatch, and
the same reason: a delete that removed nothing must not be indistinguishable
from one that worked.
"""
import logging
from datetime import timedelta

from .drop import DropReport, drop_cache_key, measured_clear
from .revocation_store import revocation_cache, revocation_namespace

logger = logging.getLogger(__name__)


class TokenBlacklist:
    """Token blacklist manager using Django's cache framework."""

    def __init__(self, key_prefix: str = "jwt_blacklist"):
        self.key_prefix = key_prefix

    def _key(self, jti: str) -> str:
        return f"{self.key_prefix}:{jti}"

    def blacklist_token(self, jti: str, expires_in: timedelta) -> bool:
        try:
            cache = revocation_cache()
            cache.set(self._key(jti), "1", int(expires_in.total_seconds()))
            logger.info(f"Token {jti[:8]}... blacklisted for {expires_in.total_seconds()}s")
            return True
        except Exception as e:
            logger.error(f"Error blacklisting token: {e}")
            return False

    def is_blacklisted(self, jti: str) -> bool:
        try:
            cache = revocation_cache()
            return bool(cache.get(self._key(jti)))
        except Exception as e:
            # Fail CLOSED: with the blacklist store down, treating every
            # token as valid would resurrect revoked sessions exactly when
            # the system is degraded. Override only for availability-over-
            # security deployments.
            logger.error(f"Error checking blacklist: {e}")
            from django.conf import settings
            if getattr(settings, "STAPEL_BLACKLIST_FAIL_OPEN", False):
                return False
            return True

    def remove_from_blacklist(self, jti: str) -> DropReport:
        """Un-revoke one token; reports what that did to the store.

        Until 0.47.0 this returned ``True`` for "the call did not raise" — the
        same value whether the entry was removed, was never written under this
        deployment's revocation namespace, or is still readable afterwards.
        Un-revoking is the direction where that matters least often and costs
        most when it does: the operator believes a session was restored.

        Truthy only for ``DROPPED`` (see :mod:`stapel_core.core.drop`).
        ``NOT_FOUND`` here does not mean the token is now accepted — a peer
        holding the entry under a different namespace still refuses it.
        """
        return drop_cache_key(
            revocation_cache,
            self._key(jti),
            what="token revocation",
            namespace=revocation_namespace(),
            log=logger,
            hint=(
                "check STAPEL_JWT_REVOCATION_NAMESPACE/_CACHE agree with the "
                "service that revoked the token"
            ),
        )

    def clear_all(self) -> DropReport:
        """Empty the whole revocation connection. An operator/test primitive.

        This is the one verb in the package that genuinely **cannot read back
        what it removed**: there is no key to re-read and nothing enumerates
        what was there. Returning ``True`` for "did not raise" was therefore
        not merely uninformative, it was the most comforting lie of the six —
        so this measures the one thing that IS measurable, that the clear
        reached the namespace this library writes, with a probe key
        (:func:`stapel_core.core.drop.measured_clear`).

        ``DROPPED`` means the probe is gone, so ``clear()`` reached this
        namespace. ``STILL_PRESENT`` means it did not obey. ``UNAVAILABLE``
        means the store raised, or does not retain what it is given — a dummy
        backend, on which a clear can never be verified at all.

        It does **not** claim that only revocation keys were removed: a clear
        empties the connection, and on a backend where several key prefixes
        share one store (LocMemCache keys everything under one ``LOCATION``)
        that is more than revocation. Never call it in production to expire one
        session; :meth:`remove_from_blacklist` is that verb.
        """
        return measured_clear(
            revocation_cache,
            what="every revocation",
            namespace=revocation_namespace(),
            probe_prefix=f"{self.key_prefix}:__clear_probe__:",
            log=logger,
        )
