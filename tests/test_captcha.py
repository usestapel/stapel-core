"""Tests for the captcha verification backends."""
from unittest import TestCase, mock

from stapel_core.captcha import (
    HcaptchaVerifier,
    NoopVerifier,
    RecaptchaVerifier,
    TurnstileVerifier,
    build_verifier,
)


# ---------------------------------------------------------------------------
# build_verifier
# ---------------------------------------------------------------------------

class BuildVerifierTests(TestCase):

    def test_no_secret_returns_noop(self):
        result = build_verifier('turnstile', None)
        self.assertIsInstance(result, NoopVerifier)

    def test_empty_secret_returns_noop(self):
        result = build_verifier('turnstile', '')
        self.assertIsInstance(result, NoopVerifier)

    def test_builtin_turnstile(self):
        result = build_verifier('turnstile', 'secret')
        self.assertIsInstance(result, TurnstileVerifier)
        self.assertEqual(result.secret, 'secret')

    def test_builtin_recaptcha(self):
        self.assertIsInstance(build_verifier('recaptcha', 'secret'), RecaptchaVerifier)

    def test_builtin_hcaptcha(self):
        self.assertIsInstance(build_verifier('hcaptcha', 'secret'), HcaptchaVerifier)

    def test_builtin_noop_with_secret(self):
        result = build_verifier('noop', 'secret')
        self.assertIsInstance(result, NoopVerifier)

    def test_dotted_path_custom_backend(self):
        # Use NoopVerifier itself as the "custom" class via dotted path
        result = build_verifier('stapel_core.captcha.backends.NoopVerifier', 'secret')
        self.assertIsInstance(result, NoopVerifier)

    def test_dotted_path_not_subclass_raises(self):
        with self.assertRaises(TypeError):
            build_verifier('stapel_core.captcha.backends.logger', 'secret')

    def test_unknown_short_name_with_secret_raises(self):
        with self.assertRaises((ImportError, AttributeError, ValueError)):
            build_verifier('nonexistent_backend', 'secret')


# ---------------------------------------------------------------------------
# NoopVerifier
# ---------------------------------------------------------------------------

class NoopVerifierTests(TestCase):

    def test_always_true_empty_token(self):
        self.assertTrue(NoopVerifier().verify(''))

    def test_always_true_any_token(self):
        self.assertTrue(NoopVerifier().verify('bad-token', ip='1.2.3.4'))

    def test_always_true_no_ip(self):
        self.assertTrue(NoopVerifier().verify('token', ip=None))


# ---------------------------------------------------------------------------
# TurnstileVerifier
# ---------------------------------------------------------------------------

class TurnstileVerifierTests(TestCase):

    def _make(self, secret='test-secret'):
        return TurnstileVerifier(secret)

    @mock.patch('requests.post')
    def test_success_response(self, mock_post):
        mock_post.return_value.json.return_value = {'success': True}
        self.assertTrue(self._make().verify('token', ip='1.2.3.4'))

    @mock.patch('requests.post')
    def test_failure_response(self, mock_post):
        mock_post.return_value.json.return_value = {'success': False}
        self.assertFalse(self._make().verify('bad-token'))

    @mock.patch('requests.post', side_effect=Exception('network error'))
    def test_network_error_returns_false(self, _):
        self.assertFalse(self._make().verify('token'))

    @mock.patch('requests.post')
    def test_ip_forwarded_in_payload(self, mock_post):
        mock_post.return_value.json.return_value = {'success': True}
        self._make().verify('token', ip='5.5.5.5')
        call_kwargs = mock_post.call_args
        payload = call_kwargs[1].get('data') or call_kwargs[0][1]
        self.assertEqual(payload.get('remoteip'), '5.5.5.5')


# ---------------------------------------------------------------------------
# RecaptchaVerifier
# ---------------------------------------------------------------------------

class RecaptchaVerifierTests(TestCase):

    @mock.patch('requests.post')
    def test_success(self, mock_post):
        mock_post.return_value.json.return_value = {'success': True}
        self.assertTrue(RecaptchaVerifier('secret').verify('token'))

    @mock.patch('requests.post')
    def test_failure(self, mock_post):
        mock_post.return_value.json.return_value = {'success': False}
        self.assertFalse(RecaptchaVerifier('secret').verify('bad'))

    @mock.patch('requests.post', side_effect=ConnectionError())
    def test_network_error_returns_false(self, _):
        self.assertFalse(RecaptchaVerifier('secret').verify('token'))


# ---------------------------------------------------------------------------
# HcaptchaVerifier
# ---------------------------------------------------------------------------

class HcaptchaVerifierTests(TestCase):

    @mock.patch('requests.post')
    def test_success(self, mock_post):
        mock_post.return_value.json.return_value = {'success': True}
        self.assertTrue(HcaptchaVerifier('secret').verify('token'))

    @mock.patch('requests.post')
    def test_failure(self, mock_post):
        mock_post.return_value.json.return_value = {'success': False}
        self.assertFalse(HcaptchaVerifier('secret').verify('bad'))
