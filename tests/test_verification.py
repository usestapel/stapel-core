"""Step-up verification: challenge/grant lifecycle, decorator, factors."""
import pytest
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from stapel_core.verification import (
    VerificationFactor,
    complete_challenge,
    create_challenge,
    factor_registry,
    get_challenge,
    has_grant,
    register_factor,
    requires_verification,
)
from stapel_core.verification.grants import (
    record_failed_attempt,
    revoke_grants,
)


class CodeFactor(VerificationFactor):
    id = "test_code"

    def initiate(self, user, challenge):
        return {"hint": "code is 42"}

    def verify(self, user, challenge, payload):
        return payload.get("code") == "42"


class UnavailableFactor(VerificationFactor):
    id = "never"

    def available_for(self, user):
        return False

    def verify(self, user, challenge, payload):  # pragma: no cover
        return False


@pytest.fixture(autouse=True)
def factors():
    factor_registry.clear()
    register_factor(CodeFactor())
    register_factor(UnavailableFactor())
    yield
    factor_registry.clear()


@pytest.fixture
def user(db):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.create(email="v@example.com", username="v")


class ProtectedView(APIView):
    @requires_verification(scope="payout", factors=["test_code"], max_age=120)
    def post(self, request):
        from rest_framework.response import Response

        return Response({"ok": True})


def _call(view_cls, user, headers=None):
    factory = APIRequestFactory()
    request = factory.post("/x/", **(headers or {}))
    force_authenticate(request, user=user)
    return view_cls.as_view()(request)


@pytest.mark.django_db
def test_missing_grant_returns_challenge_envelope(user):
    response = _call(ProtectedView, user)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    body = response.data
    assert body["localizable_error"] == "error.403.verification_required"
    v = body["verification"]
    assert v["scope"] == "payout"
    assert v["factors"] == ["test_code"]
    assert get_challenge(v["challenge_id"])["user_id"] == str(user.pk)


@pytest.mark.django_db
def test_factor_completion_grants_and_request_passes(user):
    challenge = create_challenge(user, "payout", ["test_code"], max_age=120)
    factor = factor_registry.get("test_code")
    assert factor.verify(user, challenge, {"code": "42"})
    token = complete_challenge(challenge)

    # server-side grant: retry succeeds without any token plumbing
    assert has_grant(user, "payout")
    assert _call(ProtectedView, user).status_code == 200
    # challenge is consumed
    assert get_challenge(challenge["challenge_id"]) is None
    # stateless clients: token satisfies the check for the same user+scope
    revoke_grants(str(user.pk), ["payout"])
    assert not has_grant(user, "payout")
    assert has_grant(user, "payout", token=token)
    response = _call(ProtectedView, user, {"HTTP_X_VERIFICATION_TOKEN": token})
    assert response.status_code == 200


@pytest.mark.django_db
def test_unavailable_factor_filtered_from_challenge(user):
    challenge = create_challenge(user, "s", ["test_code", "never"], max_age=60)
    assert challenge["factors"] == ["test_code"]


@pytest.mark.django_db
def test_failed_attempts_kill_challenge(user):
    with override_settings(STAPEL_VERIFICATION={"MAX_ATTEMPTS": 2}):
        from stapel_core.verification.conf import verification_settings

        verification_settings.reload()
        challenge = create_challenge(user, "s", ["test_code"], max_age=60)
        assert record_failed_attempt(challenge) is True
        assert record_failed_attempt(challenge) is False
        assert get_challenge(challenge["challenge_id"]) is None
        verification_settings.reload()


@pytest.mark.django_db
def test_grant_is_scope_isolated(user):
    challenge = create_challenge(user, "payout", ["test_code"], max_age=60)
    complete_challenge(challenge)
    assert has_grant(user, "payout")
    assert not has_grant(user, "erasure")


def test_decorator_annotates_contract():
    contract = ProtectedView.post._stapel_verification
    assert contract == {
        "scope": "payout",
        "factors": ["test_code"],
        "max_age": 120,
        "level": "strict",
    }


@pytest.mark.django_db
def test_dotted_path_factor_registration(user):
    factor_registry.clear()
    register_factor("tests.test_verification.CodeFactor")
    assert factor_registry.names() == ["test_code"]


# ─────────────────────────────────────────────────────────────────────────────
# Policy levels (strict / default_on / opt_in) + auth.verification.policy
# ─────────────────────────────────────────────────────────────────────────────

from stapel_core.comm import register_function  # noqa: E402
from stapel_core.comm.registry import function_registry as comm_functions  # noqa: E402
from stapel_core.verification import (  # noqa: E402
    get_user_policy,
    invalidate_policy_cache,
)
from stapel_core.verification.policy import POLICY_FUNCTION  # noqa: E402

ENROLLMENT_KEY = "error.403.verification_enrollment_required"


class StrictView(APIView):
    @requires_verification(scope="payout", factors=["test_code"], level="strict")
    def post(self, request):
        from rest_framework.response import Response

        return Response({"ok": True})


class StrictNoFactorsView(APIView):
    @requires_verification(scope="payout", factors=["never"], level="strict")
    def post(self, request):
        from rest_framework.response import Response

        return Response({"ok": True})


class DefaultOnView(APIView):
    @requires_verification(scope="wallet", factors=["test_code"], level="default_on")
    def post(self, request):
        from rest_framework.response import Response

        return Response({"ok": True})


class DefaultOnNoFactorsView(APIView):
    @requires_verification(scope="wallet", factors=["never"], level="default_on")
    def post(self, request):
        from rest_framework.response import Response

        return Response({"ok": True})


class OptInView(APIView):
    @requires_verification(scope="export", factors=["test_code"], level="opt_in")
    def post(self, request):
        from rest_framework.response import Response

        return Response({"ok": True})


@pytest.fixture
def policy_functions():
    """Isolated comm function registry per test."""
    comm_functions.clear()
    yield
    comm_functions.clear()


def _policy(disabled=(), enabled=()):
    def handler(payload):
        return {
            "disabled_scopes": list(disabled),
            "enabled_scopes": list(enabled),
        }

    register_function(POLICY_FUNCTION, handler)
    return handler


@pytest.mark.django_db
def test_strict_no_usable_factors_enrollment_envelope(user, policy_functions):
    from django.core.cache import cache

    response = _call(StrictNoFactorsView, user)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    body = response.data
    assert body["localizable_error"] == ENROLLMENT_KEY
    # Same envelope shape, but enrollment-flavored: the endpoint's factor
    # list (what the user could enroll) and no challenge to complete.
    assert body["verification"] == {
        "scope": "payout",
        "factors": ["never"],
        "enroll": True,
    }
    # Nothing to verify yet — no challenge was stored.
    assert not any(
        "stapel:verification:challenge:" in key for key in cache._cache
    )


@pytest.mark.django_db
def test_strict_ignores_user_policy_and_never_calls_function(user, policy_functions):
    calls = []

    def handler(payload):
        calls.append(payload)
        return {"disabled_scopes": ["payout"], "enabled_scopes": []}

    register_function(POLICY_FUNCTION, handler)
    response = _call(StrictView, user)
    # Disabled in preferences, still enforced — and the policy Function is
    # never even consulted for strict endpoints.
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.data["localizable_error"] == "error.403.verification_required"
    assert calls == []


@pytest.mark.django_db
def test_default_on_enforced_without_preference(user, policy_functions):
    _policy()
    response = _call(DefaultOnView, user)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.data["verification"]["scope"] == "wallet"


@pytest.mark.django_db
def test_default_on_disabled_scope_passes_through(user, policy_functions):
    _policy(disabled=["wallet"])
    response = _call(DefaultOnView, user)
    assert response.status_code == 200
    assert response.data == {"ok": True}


@pytest.mark.django_db
def test_default_on_no_usable_factors_passes_through(user, policy_functions):
    # No policy Function registered either — factor check comes first.
    response = _call(DefaultOnNoFactorsView, user)
    assert response.status_code == 200


@pytest.mark.django_db
def test_default_on_grant_still_honored(user, policy_functions):
    _policy()
    challenge = create_challenge(user, "wallet", ["test_code"], max_age=60)
    complete_challenge(challenge)
    assert _call(DefaultOnView, user).status_code == 200


@pytest.mark.django_db
def test_opt_in_enabled_scope_enforced(user, policy_functions):
    _policy(enabled=["export"])
    response = _call(OptInView, user)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.data["verification"]["scope"] == "export"


@pytest.mark.django_db
def test_opt_in_without_preference_passes_through(user, policy_functions):
    _policy()
    assert _call(OptInView, user).status_code == 200


@pytest.mark.django_db
def test_fail_safe_function_not_registered(user, policy_functions):
    # default_on: protection stays ON; opt_in: stays OFF.
    assert _call(DefaultOnView, user).status_code == status.HTTP_403_FORBIDDEN
    assert _call(OptInView, user).status_code == 200


@pytest.mark.django_db
def test_fail_safe_function_call_error(user, policy_functions):
    def handler(payload):
        raise RuntimeError("policy backend down")

    register_function(POLICY_FUNCTION, handler)
    assert _call(DefaultOnView, user).status_code == status.HTTP_403_FORBIDDEN
    assert _call(OptInView, user).status_code == 200


@pytest.mark.django_db
def test_policy_failure_is_not_cached(user, policy_functions):
    assert get_user_policy(user) is None
    _policy(disabled=["wallet"])
    # The failed lookup was not cached — the next call resolves.
    assert get_user_policy(user)["disabled_scopes"] == ["wallet"]


@pytest.mark.django_db
def test_policy_cache_hit_and_invalidate(user, policy_functions):
    calls = []

    def handler(payload):
        calls.append(payload)
        return {"disabled_scopes": [], "enabled_scopes": ["export"]}

    register_function(POLICY_FUNCTION, handler)
    first = get_user_policy(user)
    second = get_user_policy(user)
    assert first == second == {"disabled_scopes": [], "enabled_scopes": ["export"]}
    assert calls == [{"user_id": str(user.pk)}]

    invalidate_policy_cache(user.pk)
    get_user_policy(user)
    assert len(calls) == 2


@pytest.mark.django_db
def test_level_none_defers_to_settings_default(user, policy_functions):
    class SettingsLevelView(APIView):
        @requires_verification(scope="export", factors=["test_code"], level=None)
        def post(self, request):
            from rest_framework.response import Response

            return Response({"ok": True})

    assert SettingsLevelView.post._stapel_verification["level"] is None
    # DEFAULT_LEVEL defaults to "strict": enforced even with no preference.
    assert _call(SettingsLevelView, user).status_code == status.HTTP_403_FORBIDDEN

    from stapel_core.verification.conf import verification_settings

    with override_settings(STAPEL_VERIFICATION={"DEFAULT_LEVEL": "opt_in"}):
        verification_settings.reload()
        # opt_in + unavailable policy backend → pass through.
        assert _call(SettingsLevelView, user).status_code == 200
    verification_settings.reload()


def test_unknown_level_rejected_at_decoration_time():
    with pytest.raises(ValueError, match="unknown verification level"):
        requires_verification(scope="x", level="sometimes")


# ─────────────────────────────────────────────────────────────────────────────
# OpenAPI postprocessing hook exposes the level
# ─────────────────────────────────────────────────────────────────────────────


def test_openapi_hook_exposes_level():
    import sys
    import types

    from django.test import override_settings as _override

    from stapel_core.django.openapi.extensions import stapel_postprocessing_hook

    module = types.ModuleType("_stapel_verification_hook_urls")
    from django.urls import path

    module.urlpatterns = [path("wallet/", DefaultOnView.as_view())]
    sys.modules["_stapel_verification_hook_urls"] = module

    result = {
        "paths": {"/wallet/": {"post": {"operationId": "wallet_create"}}}
    }
    try:
        with _override(ROOT_URLCONF="_stapel_verification_hook_urls"):
            out = stapel_postprocessing_hook(result, None, None, True)
    finally:
        del sys.modules["_stapel_verification_hook_urls"]

    operation = out["paths"]["/wallet/"]["post"]
    assert operation["x-stapel-verification"] == {
        "scope": "wallet",
        "factors": ["test_code"],
        "max_age": None,
        "level": "default_on",
    }
    assert "403" in operation["responses"]
