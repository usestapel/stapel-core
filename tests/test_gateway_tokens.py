"""Scope tokens and the network-identity check.

Invariants: a token is opaque and stored only as a sha256 hash; it dies on
expiry, revocation and rotation; a token for project A never authorizes a
request about project B; the network binding pins it to the container's
address (exact IP or CIDR), and a malformed binding fails closed.
"""
from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from stapel_core.gateway import TokenInvalid, issue_token, revoke_token, rotate_token, verify_token
from stapel_core.gateway.network import default_verifier, verify_network
from stapel_core.gateway.tokens import TOKEN_PREFIX, purge_expired_tokens

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------
# issue / verify
# --------------------------------------------------------------------------

def test_issue_returns_plaintext_once_and_stores_hash_only():
    issued = issue_token("p1", container="c1", network="10.0.7.4")
    assert issued.token.startswith(TOKEN_PREFIX)
    assert len(issued.token) > 40  # 256-bit urlsafe secret

    from stapel_core.django.gateway.models import ScopeToken
    row = ScopeToken.objects.get(pk=issued.token_id)
    assert issued.token not in row.token_hash
    assert len(row.token_hash) == 64  # sha256 hex, not the secret
    assert row.project == "p1"
    assert row.container == "c1"
    assert row.network == "10.0.7.4"


def test_issue_requires_project():
    with pytest.raises(ValueError, match="project"):
        issue_token("")


def test_verify_roundtrip_and_reasons():
    issued = issue_token("p1")
    row = verify_token(issued.token)
    assert row.project == "p1"

    with pytest.raises(TokenInvalid) as exc:
        verify_token(None)
    assert exc.value.reason == "token_missing"

    with pytest.raises(TokenInvalid) as exc:
        verify_token("sgw_forged-token-that-was-never-issued")
    assert exc.value.reason == "token_unknown"


def test_foreign_project_is_refused():
    issued = issue_token("p1")
    with pytest.raises(TokenInvalid) as exc:
        verify_token(issued.token, project="p2")
    assert exc.value.reason == "token_project_mismatch"
    # ...while the right project passes.
    assert verify_token(issued.token, project="p1").project == "p1"


def test_expired_token_is_refused():
    issued = issue_token("p1", ttl=-1)
    with pytest.raises(TokenInvalid) as exc:
        verify_token(issued.token)
    assert exc.value.reason == "token_expired"


def test_default_ttl_is_short_lived():
    issued = issue_token("p1")
    assert issued.expires_at <= timezone.now() + timedelta(seconds=3601)
    with override_settings(STAPEL_GATEWAY={"TOKEN_TTL": 60}):
        quick = issue_token("p1")
        assert quick.expires_at <= timezone.now() + timedelta(seconds=61)


# --------------------------------------------------------------------------
# revoke / rotate / purge
# --------------------------------------------------------------------------

def test_revoke_by_plaintext_and_by_id():
    a = issue_token("p1")
    b = issue_token("p1")
    assert revoke_token(a.token) is True
    assert revoke_token(b.token_id) is True
    assert revoke_token(a.token) is False  # already dead
    for dead in (a, b):
        with pytest.raises(TokenInvalid) as exc:
            verify_token(dead.token)
        assert exc.value.reason == "token_revoked"


def test_rotation_keeps_bindings_and_kills_old():
    old = issue_token("p1", container="c1", network="10.0.0.0/24")
    fresh = rotate_token(old.token)
    assert fresh.token != old.token
    assert (fresh.project, fresh.container, fresh.network) == ("p1", "c1", "10.0.0.0/24")
    with pytest.raises(TokenInvalid):
        verify_token(old.token)
    assert verify_token(fresh.token).project == "p1"


def test_rotation_grace_keeps_old_briefly():
    old = issue_token("p1")
    rotate_token(old.token, grace=30)
    row = verify_token(old.token)  # still alive inside the grace window
    assert row.expires_at <= timezone.now() + timedelta(seconds=31)


def test_rotating_dead_token_is_refused():
    old = issue_token("p1")
    revoke_token(old.token)
    with pytest.raises(TokenInvalid):
        rotate_token(old.token)


def test_purge_removes_expired_and_revoked():
    from stapel_core.django.gateway.models import ScopeToken

    issue_token("p1", ttl=-3600)
    revoked = issue_token("p1")
    revoke_token(revoked.token)
    ScopeToken.objects.filter(revoked_at__isnull=False).update(
        revoked_at=timezone.now() - timedelta(hours=1))
    live = issue_token("p1")
    assert purge_expired_tokens() == 2
    assert ScopeToken.objects.filter(pk=live.token_id).exists()


# --------------------------------------------------------------------------
# network identity
# --------------------------------------------------------------------------

class _Tok:
    def __init__(self, network):
        self.network = network


def test_exact_ip_binding():
    assert default_verifier("10.0.7.4", _Tok("10.0.7.4")) is True
    assert default_verifier("10.0.7.5", _Tok("10.0.7.4")) is False


def test_cidr_binding():
    assert default_verifier("10.0.7.200", _Tok("10.0.7.0/24")) is True
    assert default_verifier("10.0.8.1", _Tok("10.0.7.0/24")) is False


def test_ipv6_binding():
    assert default_verifier("fd00::7:4", _Tok("fd00::/64")) is True
    assert default_verifier("fe80::1", _Tok("fd00::/64")) is False


def test_missing_or_garbage_ip_fails_closed():
    assert default_verifier(None, _Tok("10.0.7.4")) is False
    assert default_verifier("not-an-ip", _Tok("10.0.7.4")) is False


def test_malformed_binding_fails_closed():
    assert default_verifier("10.0.7.4", _Tok("10.0.7.4/999")) is False
    assert default_verifier("10.0.7.4", _Tok("nonsense")) is False


def test_unbound_token_follows_require_binding_setting():
    assert default_verifier("10.0.7.4", _Tok(None)) is True  # opt-out posture
    with override_settings(STAPEL_GATEWAY={"REQUIRE_NETWORK_BINDING": True}):
        assert default_verifier("10.0.7.4", _Tok(None)) is False


def test_verifier_seam_is_swappable():
    calls = []

    def custom(ip, token):
        calls.append((ip, token.network))
        return ip == "1.2.3.4"

    with override_settings(STAPEL_GATEWAY={"NETWORK_VERIFIER": custom}):
        assert verify_network("1.2.3.4", _Tok("ignored")) is True
        assert verify_network("9.9.9.9", _Tok("ignored")) is False
    assert calls == [("1.2.3.4", "ignored"), ("9.9.9.9", "ignored")]
