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
    assert contract == {"scope": "payout", "factors": ["test_code"], "max_age": 120}


@pytest.mark.django_db
def test_dotted_path_factor_registration(user):
    factor_registry.clear()
    register_factor("tests.test_verification.CodeFactor")
    assert factor_registry.names() == ["test_code"]
