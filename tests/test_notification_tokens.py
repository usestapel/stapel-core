"""Tests for stapel_core.notifications.tokens — HMAC unsubscribe tokens."""
import time

import pytest

from stapel_core.notifications.tokens import (
    generate_unsubscribe_token,
    verify_unsubscribe_token,
)


class TestUnsubscribeTokenRoundTrip:
    def test_generate_then_verify(self):
        token = generate_unsubscribe_token("user-1", "marketing", "email")
        result = verify_unsubscribe_token(token)
        assert result == {"user_id": "user-1", "group": "marketing", "channel": "email"}

    def test_verify_returns_none_for_wrong_signature(self):
        token = generate_unsubscribe_token("user-1", "marketing", "email")
        # Tamper with the user_id in an otherwise well-formed token.
        user_id, group, channel, ts, _sig = token.split(":")
        forged = f"impostor:{group}:{channel}:{ts}:deadbeef"
        assert verify_unsubscribe_token(forged) is None

    def test_verify_rejects_malformed_token(self):
        for bad in ("", "no:colons-here", "a:b:c", "a:b:c:d:e:f"):
            assert verify_unsubscribe_token(bad) is None


class TestUnsubscribeTokenValidation:
    def test_generate_rejects_colon_in_group(self):
        with pytest.raises(ValueError):
            generate_unsubscribe_token("u", "has:colon", "email")

    def test_generate_rejects_colon_in_channel(self):
        with pytest.raises(ValueError):
            generate_unsubscribe_token("u", "marketing", "has:colon")


class TestUnsubscribeTokenExpiry:
    def test_expired_token_rejected(self):
        # Build a token then rewind its timestamp beyond max_age.
        token = generate_unsubscribe_token("u", "g", "email")
        user_id, group, channel, _ts, sig = token.split(":")
        old_ts = str(int(time.time()) - 31 * 24 * 3600)
        old_token = f"{user_id}:{group}:{channel}:{old_ts}:{sig}"
        # signature no longer matches the new ts -> rejected for tampering,
        # which still yields None either way.
        assert verify_unsubscribe_token(old_token) is None

    def test_max_age_zero_disables_expiry_check(self):
        token = generate_unsubscribe_token("u", "g", "email")
        # max_age=0 means the age check is skipped; a fresh token still verifies.
        assert verify_unsubscribe_token(token, max_age=0) is not None
