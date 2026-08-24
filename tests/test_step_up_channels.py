"""The admin step-up gate read a channel the admin browser cannot produce.

Two defects, one shape — a guard reading a credential channel the real client
cannot fill:

1. **A per-service namespace pretending to be fleet-wide.** Verification
   grants went through ``django.core.cache.cache``, which Django keys under
   the *deployment's* ``KEY_PREFIX``. So ``stepup.py``'s own claim that
   "completing step-up anywhere in the session satisfies the admin gate" held
   only inside one prefix: the auth service wrote
   ``auth:1:stapel:verification:grant:<uid>:sensitive`` and the profiles
   service looked for ``stapel_profiles:1:...``. Revocation had already met
   and fixed this exact defect (``tests/test_revocation_namespace.py``);
   grants were simply never moved. The two-cache technique below is that
   file's, applied where it should have been applied the first time.

2. **A gate no browser could satisfy.** ``has_fresh_step_up`` called
   ``has_grant(user, scope)`` with no ``token=``, so the
   ``X-Verification-Token`` fallback was unreachable from the admin gate — and
   a browser form POST cannot set a header anyway. With ``ENFORCE`` defaulting
   to ``True``, any process that registered a factor but did not mint the
   grant refused every HIGH operation with a 403 whose instructions could not
   be followed.

Every test here drives the gate the way a client actually reaches it: a peer
service's cache prefix, or a request carrying a session cookie and no header
at all (``_assert_browser_shaped`` asserts the header's absence). On 0.44.1
the cross-prefix tests fail on the miss and the browser tests fail because no
such channel exists.
"""
import time
import uuid

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.sessions.backends.cache import SessionStore
from django.test import RequestFactory, override_settings

from stapel_core.django.admin.base import StapelModelAdmin
from stapel_core.django.users.models import User
from stapel_core.verification.decorators import TOKEN_HEADER
from stapel_core.verification.factors import VerificationFactor, factor_registry
from stapel_core.verification.grants import (
    complete_challenge,
    create_challenge,
    grant_verification,
    has_grant,
    revoke_grants,
)

pytestmark = pytest.mark.django_db

MANDATE_BACKENDS = [
    "stapel_core.access.backend.MandateBackend",
    "stapel_core.access.backend.AuditedModelBackend",
]

SCOPE = "sensitive"          # the STEP_UP default
MAX_AGE = 900                # the STEP_UP default

LOCMEM = "django.core.cache.backends.locmem.LocMemCache"

#: One store, two services. LocMemCache keyed on LOCATION shares its backing
#: dict between instances, which is exactly the "one Redis" of the defect.
SHARED_LOCATION = "step-up-shared-store"


def _service(key_prefix):
    """CACHES as one service in the split deployment would configure it."""
    return {
        "default": {
            "BACKEND": LOCMEM,
            "LOCATION": SHARED_LOCATION,
            "KEY_PREFIX": key_prefix,
        }
    }


AUTH_SERVICE = _service("auth")            # mints the grant
PROFILES_SERVICE = _service("stapel_profiles")   # runs the admin that reads it


class _DummyFactor(VerificationFactor):
    id = "dummy"

    def verify(self, user, challenge, payload):  # pragma: no cover - unused
        return True


@pytest.fixture
def factor():
    """Register a factor so step-up is *capable* — otherwise it self-disables."""
    factor_registry.register(_DummyFactor())
    yield
    factor_registry.clear()


@pytest.fixture(autouse=True)
def _clean_shared_store():
    """The split-deployment store is not the one conftest clears (LOCATION "")."""
    from stapel_core.verification.grants import _cache

    with override_settings(CACHES=AUTH_SERVICE):
        _cache().clear()
    yield
    with override_settings(CACHES=AUTH_SERVICE):
        _cache().clear()


def make_staff(*, roles=("admin",), superuser=False):
    user = User.objects.create(
        username=f"u_{uuid.uuid4().hex[:10]}",
        is_staff=True,
        is_superuser=superuser,
    )
    if roles is not None:
        user.staff_roles = list(roles)
        user.save(update_fields=["staff_roles"])
    return user


def admin_for(model=User):
    return StapelModelAdmin(model, AdminSite())


def mint_grant(user, *, scope=SCOPE):
    grant_verification(user_id=str(user.pk), scope=scope, max_age=MAX_AGE)


def mint_token(user, *, scope=SCOPE):
    """A verification token, with its companion grant revoked.

    ``complete_challenge`` writes both. Revoking the grant leaves the token as
    the ONLY thing that can satisfy the gate, which is what these tests are
    about — otherwise the server-side grant would pass them either way.
    """
    challenge = create_challenge(user, scope, ["dummy"], MAX_AGE)
    token = complete_challenge(challenge)
    revoke_grants(str(user.pk), [scope])
    assert has_grant(user, scope) is False
    return token


# ---------------------------------------------------------------------------
# 1. The grant store is fleet-wide, not per-service.
# ---------------------------------------------------------------------------

class TestGrantsCrossServices:
    def test_a_grant_minted_in_auth_is_visible_in_profiles(self, factor):
        user = make_staff()

        with override_settings(CACHES=AUTH_SERVICE):
            mint_grant(user)

        with override_settings(CACHES=PROFILES_SERVICE):
            assert has_grant(user, SCOPE) is True, (
                "step-up completed in one service is invisible in the next"
            )

    def test_the_admin_gate_opens_on_a_grant_minted_elsewhere(self, factor):
        """The whole point: the gate's docstring promise, actually kept."""
        admin = admin_for()
        user = make_staff()

        with override_settings(CACHES=AUTH_SERVICE):
            mint_grant(user)

        with override_settings(
            CACHES=PROFILES_SERVICE, AUTHENTICATION_BACKENDS=MANDATE_BACKENDS
        ):
            request = RequestFactory().get("/admin/")
            request.user = user
            assert admin.has_delete_permission(request) is True

    def test_a_user_nobody_verified_is_still_refused(self, factor):
        """The gate has to be a gate, not a wall removed."""
        admin = admin_for()
        user = make_staff()
        with override_settings(
            CACHES=PROFILES_SERVICE, AUTHENTICATION_BACKENDS=MANDATE_BACKENDS
        ):
            request = RequestFactory().get("/admin/")
            request.user = user
            assert admin.has_delete_permission(request) is False

    def test_revocation_crosses_services_too(self, factor):
        user = make_staff()
        with override_settings(CACHES=AUTH_SERVICE):
            mint_grant(user)
        with override_settings(CACHES=PROFILES_SERVICE):
            revoke_grants(str(user.pk), [SCOPE])
        with override_settings(CACHES=AUTH_SERVICE):
            assert has_grant(user, SCOPE) is False

    def test_a_token_minted_in_auth_validates_in_profiles(self, factor):
        user = make_staff()
        with override_settings(CACHES=AUTH_SERVICE):
            token = mint_token(user)
        with override_settings(CACHES=PROFILES_SERVICE):
            assert has_grant(user, SCOPE, token=token) is True

    def test_the_grant_left_this_services_own_prefix(self, factor):
        """Pins WHERE it went, not only that it is readable.

        The migrated in-process tests below still open the gate by calling
        ``grant_verification`` directly; this is what stops that from proving
        the old, per-service behaviour all over again.
        """
        from django.core.cache import cache

        from stapel_core.verification.grants import GRANT_KEY

        user = make_staff()
        mint_grant(user)
        own_key = GRANT_KEY.format(user_id=str(user.pk), scope=SCOPE)

        assert cache.get(own_key) is None, (
            "the grant is still in this service's own cache namespace"
        )
        assert has_grant(user, SCOPE) is True


# ---------------------------------------------------------------------------
# 2. A browser-shaped request can satisfy the gate.
# ---------------------------------------------------------------------------

def browser_request(user, *, method="get", path="/admin/users/user/delete/", data=None,
                    session=None):
    """A request shaped like the admin browser's: session cookie, no header."""
    request = getattr(RequestFactory(), method)(path, data or {})
    request.user = user
    request.session = session if session is not None else SessionStore()
    _assert_browser_shaped(request)
    return request


def _assert_browser_shaped(request):
    """A browser form POST cannot set X-Verification-Token. Prove we never do."""
    assert TOKEN_HEADER not in request.headers


class TestBrowserCanSatisfyTheGate:
    def test_a_step_up_recorded_on_the_session_opens_the_gate(self, factor):
        from stapel_core.access.stepup import record_step_up_in_session

        admin = admin_for()
        user = make_staff()
        request = browser_request(user)

        with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
            assert admin.has_delete_permission(request) is False
            assert record_step_up_in_session(request) is True
            assert admin.has_delete_permission(request) is True

    def test_a_stale_session_record_does_not_open_the_gate(self, factor):
        from stapel_core.access.stepup import SESSION_KEY

        admin = admin_for()
        user = make_staff()
        request = browser_request(user)
        request.session[SESSION_KEY] = {SCOPE: int(time.time()) - MAX_AGE - 1}

        with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
            assert admin.has_delete_permission(request) is False

    def test_a_session_record_for_another_scope_does_not_open_the_gate(self, factor):
        from stapel_core.access.stepup import record_step_up_in_session

        admin = admin_for()
        user = make_staff()
        request = browser_request(user)
        record_step_up_in_session(request, scope="payout")

        with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
            assert admin.has_delete_permission(request) is False

    def test_a_token_in_the_query_string_is_accepted_and_pinned(self, factor):
        """The redirect back from an auth-service step-up, then the confirm POST.

        The browser arrives at the delete URL with ``?verification_token=...``.
        The form it then submits does NOT carry that query string, and cannot
        carry the header — so the token has to become something the session
        holds, or the second half of the flow refuses the operation the first
        half just authorised.
        """
        from stapel_core.access.stepup import TOKEN_PARAM

        admin = admin_for()
        user = make_staff()
        token = mint_token(user)

        landing = browser_request(
            user, path=f"/admin/users/user/delete/?{TOKEN_PARAM}={token}"
        )
        with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
            assert landing.GET.get(TOKEN_PARAM) == token
            assert admin._step_up_response(landing, "delete") is None

            confirm = browser_request(
                user, method="post", data={"post": "yes"}, session=landing.session
            )
            assert TOKEN_PARAM not in confirm.POST
            assert TOKEN_PARAM not in confirm.GET
            assert admin.has_delete_permission(confirm) is True

    def test_a_token_in_a_form_field_is_accepted(self, factor):
        from stapel_core.access.stepup import TOKEN_PARAM

        admin = admin_for()
        user = make_staff()
        token = mint_token(user)

        request = browser_request(user, method="post", data={TOKEN_PARAM: token})
        with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
            assert admin.has_delete_permission(request) is True

    def test_another_users_token_does_not_open_the_gate(self, factor):
        from stapel_core.access.stepup import TOKEN_PARAM

        admin = admin_for()
        someone_else = make_staff()
        user = make_staff()
        token = mint_token(someone_else)

        request = browser_request(user, method="post", data={TOKEN_PARAM: token})
        with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
            assert admin.has_delete_permission(request) is False
            assert admin._step_up_response(request, "delete") is not None

    def test_a_garbage_token_does_not_open_the_gate(self, factor):
        from stapel_core.access.stepup import TOKEN_PARAM

        admin = admin_for()
        user = make_staff()
        request = browser_request(user, method="post", data={TOKEN_PARAM: "vt_nope"})
        with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
            assert admin.has_delete_permission(request) is False

    def test_the_header_channel_still_serves_api_clients(self, factor):
        """Unreachable from this gate before 0.45.0 — now the third channel."""
        admin = admin_for()
        user = make_staff()
        token = mint_token(user)

        request = RequestFactory().post("/admin/", headers={"x-verification-token": token})
        request.user = user
        with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
            assert admin.has_delete_permission(request) is True

    def test_a_request_without_a_session_is_not_an_error(self, factor):
        """API-only deployments have no session; the grant store still decides."""
        from stapel_core.access.stepup import record_step_up_in_session

        admin = admin_for()
        user = make_staff()
        request = RequestFactory().get("/admin/")
        request.user = user

        with override_settings(AUTHENTICATION_BACKENDS=MANDATE_BACKENDS):
            assert record_step_up_in_session(request) is False
            assert admin.has_delete_permission(request) is False
            mint_grant(user)
            assert admin.has_delete_permission(request) is True


# ---------------------------------------------------------------------------
# 3. The educational 403 names channels that exist.
# ---------------------------------------------------------------------------

class TestDeniedMessageIsFollowable:
    def test_message_names_the_browser_carriable_channels(self, factor):
        from stapel_core.access.stepup import TOKEN_PARAM, step_up_denied_message

        message = step_up_denied_message(User, "delete")
        assert TOKEN_PARAM in message
        assert TOKEN_HEADER in message
        assert "fleet-wide" in message
        # the pre-0.45.0 assertions still hold
        assert "Step-up verification required" in message
        assert SCOPE in message


# ---------------------------------------------------------------------------
# 4. The check that fires when the namespace stops being fleet-wide.
# ---------------------------------------------------------------------------

class TestGrantNamespaceCheck:
    """Mirrors ``TestRevocationNamespaceCheck`` — same defect, same alarm."""

    def _run(self):
        from stapel_core.verification.checks import check_grant_namespace

        return check_grant_namespace()

    def test_silent_on_the_default(self):
        with override_settings(CACHES=AUTH_SERVICE):
            assert self._run() == []

    def test_custom_namespace_is_reported(self):
        with override_settings(
            CACHES=AUTH_SERVICE,
            STAPEL_VERIFICATION={"GRANT_NAMESPACE": "fleet-b"},
        ):
            ids = [f.id for f in self._run()]
        assert "stapel_core.verification.W001" in ids

    def test_unknown_alias_with_a_default_warns(self):
        with override_settings(
            CACHES=AUTH_SERVICE, STAPEL_VERIFICATION={"GRANT_CACHE": "nope"}
        ):
            ids = [f.id for f in self._run()]
        assert "stapel_core.verification.W002" in ids

    def test_unknown_alias_with_no_default_is_an_error(self):
        with override_settings(
            CACHES={"other": {"BACKEND": LOCMEM}},
            STAPEL_VERIFICATION={"GRANT_CACHE": "nope"},
        ):
            findings = self._run()
        assert [f.id for f in findings] == ["stapel_core.verification.E001"]
        assert findings[0].level >= 40  # ERROR

    def test_the_namespace_warning_cannot_be_silently_muted(self):
        from stapel_core.django.check_guard import is_security_critical

        assert is_security_critical("stapel_core.verification.W001")
