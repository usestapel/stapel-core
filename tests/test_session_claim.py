"""The session claim (``sid``): minted by the issuer, carried across refresh,
read off a request by any service holding the verification key.

The reader is what lets a brick in another process (push device registry,
audit, analytics) bind a row to the caller's session without trusting a
client-sent id and without importing stapel-auth.
"""
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import RequestFactory, override_settings

from stapel_core.django.jwt.claims import (
    SESSION_CLAIM,
    request_session_id,
    request_token_claims,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def provider():
    from stapel_core.core.revocation_store import reset_revocation_cache
    from stapel_core.django.jwt.provider import jwt_provider

    jwt_provider.reset()
    reset_revocation_cache()
    yield jwt_provider
    jwt_provider.reset()
    reset_revocation_cache()


def _data(uid=None, **extra):
    data = {
        "user_id": str(uid or uuid.uuid4()),
        "email": f"{uuid.uuid4().hex[:8]}@example.com",
        "username": f"u{uuid.uuid4().hex[:8]}",
        "is_staff": False,
        "is_superuser": False,
        "is_active": True,
    }
    data.update(extra)
    return data


def _user():
    return get_user_model().objects.create(
        username=f"u{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@example.com"
    )


def _authenticated(token, user=None):
    """A request as the DRF class leaves it: bearer header, request.user set
    by JWTCookieAuthentication from that same token."""
    from stapel_core.django.jwt.authentication import JWTCookieAuthentication

    request = RequestFactory().get("/x/", HTTP_AUTHORIZATION=f"Bearer {token}")
    result = JWTCookieAuthentication().authenticate(request)
    request.user = user if user is not None else (result[0] if result else None)
    return request


def _serialized(user, **extra):
    from stapel_core.django.jwt.utils import serialize_user_to_jwt_data

    return {**serialize_user_to_jwt_data(user), **extra}


class TestMint:
    def test_pair_carries_the_session_claim(self, provider):
        sid = str(uuid.uuid4())
        access, refresh = provider.create_tokens_from_data(_data(**{SESSION_CLAIM: sid}))
        assert provider.handler.decode_token(access)[SESSION_CLAIM] == sid
        assert provider.handler.decode_token(refresh)[SESSION_CLAIM] == sid
        assert provider.validate_token(access)[SESSION_CLAIM] == sid

    def test_refresh_keeps_the_session_of_the_refresh_token(self, provider):
        user = _user()
        sid = str(uuid.uuid4())
        _, refresh = provider.create_tokens_from_data(_serialized(user, **{SESSION_CLAIM: sid}))
        # Default loader: claims re-read from the database, which knows no session.
        minted = provider.refresh_access_token(refresh)
        assert provider.validate_token(minted)[SESSION_CLAIM] == sid

    def test_loader_cannot_name_the_session(self, provider):
        user = _user()
        sid = str(uuid.uuid4())
        _, refresh = provider.create_tokens_from_data(_serialized(user, **{SESSION_CLAIM: sid}))
        minted = provider.refresh_access_token(
            refresh, lambda uid: _serialized(user, **{SESSION_CLAIM: "other"})
        )
        assert provider.validate_token(minted)[SESSION_CLAIM] == sid

    def test_refresh_token_without_claim_mints_without_one(self, provider):
        """Tokens issued before the claim existed keep refreshing."""
        user = _user()
        _, refresh = provider.create_tokens_from_data(_serialized(user))
        minted = provider.refresh_access_token(
            refresh, lambda uid: _serialized(user, **{SESSION_CLAIM: "forged"})
        )
        assert minted
        assert SESSION_CLAIM not in provider.validate_token(minted)


class TestRequestReader:
    def test_reads_the_verified_claim(self, provider):
        user = _user()
        sid = str(uuid.uuid4())
        access, _ = provider.create_tokens_from_data(_serialized(user, **{SESSION_CLAIM: sid}))
        request = _authenticated(access)
        assert request.user.pk == user.pk
        assert request_session_id(request) == sid
        assert request_token_claims(request)["user_id"] == str(user.pk)

    def test_mirrored_user_on_a_service_without_the_auth_database(self, provider):
        """Consumer mode: the user exists only in the token; the service still
        reads the session from the claim."""
        uid = uuid.uuid4()
        sid = str(uuid.uuid4())
        access, _ = provider.create_tokens_from_data(_data(uid, **{SESSION_CLAIM: sid}))
        assert not get_user_model().objects.filter(pk=uid).exists()
        with override_settings(JWT_CREATE_USERS_FROM_TOKEN=True):
            request = _authenticated(access)
        assert str(request.user.pk) == str(uid)
        assert request_session_id(request) == sid

    def test_token_without_claim_reads_none(self, provider):
        user = _user()
        access, _ = provider.create_tokens_from_data(_serialized(user))
        request = _authenticated(access)
        assert request.user.pk == user.pk
        assert request_token_claims(request) is not None
        assert request_session_id(request) is None

    def test_unauthenticated_request_reads_none(self, provider):
        from django.contrib.auth.models import AnonymousUser

        user = _user()
        access, _ = provider.create_tokens_from_data(_serialized(user, **{SESSION_CLAIM: "s1"}))
        request = RequestFactory().get("/x/", HTTP_AUTHORIZATION=f"Bearer {access}")
        request.user = AnonymousUser()
        assert request_session_id(request) is None

    def test_tampered_claim_reads_none(self, provider):
        """A sid edited into a signed token breaks the signature."""
        import base64
        import json

        user = _user()
        access, _ = provider.create_tokens_from_data(_serialized(user, **{SESSION_CLAIM: "mine"}))
        head, body, sig = access.split(".")
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        payload[SESSION_CLAIM] = "theirs"
        forged_body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        request = RequestFactory().get(
            "/x/", HTTP_AUTHORIZATION=f"Bearer {head}.{forged_body}.{sig}"
        )
        request.user = user  # as if some other mechanism authenticated the user
        assert request_session_id(request) is None

    def test_token_of_another_user_reads_none(self, provider):
        me, other = _user(), _user()
        access, _ = provider.create_tokens_from_data(_serialized(other, **{SESSION_CLAIM: "theirs"}))
        request = RequestFactory().get("/x/", HTTP_AUTHORIZATION=f"Bearer {access}")
        request.user = me  # e.g. a Django session login with a stray bearer
        assert request_session_id(request) is None

    def test_revoked_token_reads_none(self, provider):
        user = _user()
        access, _ = provider.create_tokens_from_data(_serialized(user, **{SESSION_CLAIM: "s1"}))
        request = RequestFactory().get("/x/", HTTP_AUTHORIZATION=f"Bearer {access}")
        request.user = user
        assert provider.blacklist_token(access)
        assert request_session_id(request) is None

    def test_refresh_token_as_bearer_reads_none(self, provider):
        user = _user()
        _, refresh = provider.create_tokens_from_data(_serialized(user, **{SESSION_CLAIM: "s1"}))
        request = RequestFactory().get("/x/", HTTP_AUTHORIZATION=f"Bearer {refresh}")
        request.user = user
        assert request_session_id(request) is None

    @pytest.mark.parametrize("value", [123, "", "   ", "x" * 65, ["s"]])
    def test_malformed_claim_reads_none(self, provider, value):
        user = _user()
        access, _ = provider.create_tokens_from_data(_serialized(user, **{SESSION_CLAIM: value}))
        request = _authenticated(access)
        assert request_session_id(request) is None

    def test_middleware_reminted_token_is_read(self, provider):
        """An expired access cookie re-minted from the refresh token on this
        request: the new token is the credential, and it keeps the session."""
        user = _user()
        sid = str(uuid.uuid4())
        _, refresh = provider.create_tokens_from_data(_serialized(user, **{SESSION_CLAIM: sid}))
        request = RequestFactory().get("/x/")
        request._new_access_token = provider.refresh_access_token(refresh)
        request.user = user
        assert request_session_id(request) == sid

    def test_drf_request_is_unwrapped(self, provider):
        from rest_framework.request import Request

        user = _user()
        access, _ = provider.create_tokens_from_data(_serialized(user, **{SESSION_CLAIM: "s9"}))
        raw = RequestFactory().get("/x/", HTTP_AUTHORIZATION=f"Bearer {access}")
        drf = Request(raw)
        drf.user = user
        assert request_session_id(drf) == "s9"
