# stapel_core

[![CI](https://github.com/usestapel/stapel-core/actions/workflows/ci.yml/badge.svg)](https://github.com/usestapel/stapel-core/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/usestapel/stapel-core/graph/badge.svg)](https://codecov.io/gh/usestapel/stapel-core)
[![PyPI](https://img.shields.io/pypi/v/stapel-core.svg)](https://pypi.org/project/stapel-core/)

Shared Python library for Stapel services. Provides JWT authentication, captcha
verification, event bus, notifications, and Django utilities used across all
backend services.

Part of the [Stapel framework](https://github.com/usestapel).

## Quick start for a new Django service

```bash
pip install -e ../iron-common-python
```

Add to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    ...
    'stapel_core.django',
    'stapel_core.django.users',   # if using the shared User model
]
```

## Modules

### `stapel_core.captcha` — Pluggable captcha verification

Backend-agnostic captcha interface. Supports Cloudflare Turnstile, Google
reCAPTCHA v2, hCaptcha, and custom backends.

**Settings** (per service, in `settings/base.py`):

```python
STAPEL_CAPTCHA = {
    'BACKEND': env.str('CAPTCHA_BACKEND', 'turnstile'),
    'SECRET': env.str('CAPTCHA_SECRET', None),  # absent → disabled
}
```

**Auto-disable**: if the secret is `None` or empty, `build_verifier`
returns `NoopVerifier` regardless of backend. No separate toggle needed.

**DRF integration** (add mixin to any serializer):

```python
from stapel_core.django.captcha import CaptchaMixin

class MySerializer(CaptchaMixin, serializers.Serializer):
    captcha_token = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        self._require_captcha_if_configured(attrs)
        return attrs
```

**Custom backend** — subclass `CaptchaVerifier` and point to it via a dotted
import path:

```python
from stapel_core.captcha import CaptchaVerifier

class MyCaptchaVerifier(CaptchaVerifier):
    def verify(self, token: str, ip: str | None = None, *, level: str | None = None) -> bool:
        return my_service.check(token, self.secret)
```

```python
# settings.py
STAPEL_CAPTCHA = {'BACKEND': 'myapp.captcha.MyCaptchaVerifier', 'SECRET': 'my-secret'}
```

**Tiered challenge policy** — instead of a binary on/off, protect a view with
a strictness level derived from the client's network class (via
`stapel_core.netintel`):

```python
from stapel_core.django.captcha import captcha_protected

class RegisterView(APIView):
    @captcha_protected(action="register")
    def post(self, request): ...
```

Levels: `none < invisible < interactive < interactive+ratelimit < block`.
The default matrix (overridable via `STAPEL_CAPTCHA["CHALLENGE_MATRIX"]`,
merged over the defaults) maps residential/unknown → invisible,
datacenter/vpn → interactive, tor → interactive+ratelimit. Per-action
overrides: `STAPEL_CAPTCHA["ACTION_OVERRIDES"] = {"register": "+1"}` (bump one
level) or `{"payout": {"vpn": "block"}}`. The whole policy is swappable via
`STAPEL_CAPTCHA["CHALLENGE_POLICY"]` (dotted path to a `ChallengePolicy`).
`block` returns 403 `error.403.network_blocked`; rate limiting is not done
here — middleware reads `request.stapel_challenge_level`. With no netintel
provider configured every request classifies as `unknown` → behavior is
identical to the classic binary captcha.

---

### `stapel_core.netintel` — IP intelligence (network class + geo)

`classify_ip(ip) -> IpProfile{kind, asn, asn_org, country, confidence}` and
`country_of(ip)`. Kind vocabulary: `residential | datacenter | vpn | tor |
unknown`. Results are cached in the Django cache; provider errors fail open
to `unknown` and never raise.

```python
STAPEL_NETINTEL = {
    # dotted path / class / instance of a NetIntelProvider (replace seam)
    "PROVIDER": "stapel_core.netintel.providers.MaxMindProvider",
    "MAXMIND_ASN_DB": "/var/geoip/GeoLite2-ASN.mmdb",
    "MAXMIND_COUNTRY_DB": "/var/geoip/GeoLite2-Country.mmdb",
    "MAXMIND_ANONYMOUS_DB": "/var/geoip/GeoIP2-Anonymous-IP.mmdb",
}
```

Built-in providers: `NullProvider` (default — always `unknown`),
`MaxMindProvider` (offline mmdb, `pip install stapel-core[netintel-maxmind]`),
`HttpJsonProvider` (ipinfo/IPQS-style HTTP APIs via `HTTP_URL_TEMPLATE` /
`HTTP_API_KEY` / `HTTP_RESPONSE_MAPPER`). `client_ip(request)` honors
`TRUSTED_PROXY_HEADER` (default: `REMOTE_ADDR` only — proxy headers are
spoofable unless your edge overwrites them).

---

### `stapel_core.django.jwt` — JWT authentication

Unified JWT provider (singleton). Supports HS256 and RS256.

```python
from stapel_core.django.jwt.provider import jwt_provider

access, refresh = jwt_provider.create_tokens(user)
payload = jwt_provider.validate_token(access_token)
```

**Settings**:

```python
JWT_ALGORITHM    = 'HS256'           # or 'RS256'
JWT_SECRET_KEY   = 'your-secret'     # HS256
JWT_PRIVATE_KEY  = '...'             # RS256
JWT_PUBLIC_KEY   = '...'             # RS256
JWT_ISSUER       = 'https://yourapp.com'
JWT_AUDIENCE     = None
JWT_ACCESS_TOKEN_LIFETIME  = 900     # seconds
JWT_REFRESH_TOKEN_LIFETIME = 604800  # seconds
```

---

### `stapel_core.django.jwt.authentication` — JWT cookie auth

`JWTCookieAuthentication` reads JWT from `access_token` cookie or
`Authorization: Bearer <token>` header.

```python
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'stapel_core.django.jwt.authentication.JWTCookieAuthentication',
    ],
}
```

---

### `stapel_core.django.api` — DRF utilities

| Symbol | Purpose |
|---|---|
| `StapelDataclassSerializer` | Serializer that maps `@dataclass` fields |
| `StapelResponse(serializer)` | Wraps `.data` automatically |
| `StapelErrorResponse(status, ERR_KEY)` | Structured error response |
| `StapelValidationError(ERR_KEY)` | Raises DRF validation error with error key |
| `register_service_errors(dict)` | Registers error messages for a service |
| `AnchorPagination` / `CreatedAtAnchorPagination` | Cursor-style paginators |

---

### `stapel_core.bus` — Event bus

Transport-agnostic event bus: in-memory backend for tests/dev, Kafka, NATS
JetStream, or Redis Streams for production — pick one via
`STAPEL_BUS_BACKEND` (or bring your own `BusBackend` subclass).

**Publish** (sync, fire-and-forget):

```python
from stapel_core.bus import publish, Event

publish('user.created', Event(
    event_type='user.created',
    service='auth',
    payload={'user_id': '...'},
))
```

**Consume** by subclassing the management-command base:

```python
from stapel_core.bus import BaseBusConsumerCommand, Event

class ConsumeUsers(BaseBusConsumerCommand):
    topics = ['user.created']
    consumer_group = 'notifications'

    def handle_event(self, event: Event) -> None:
        ...
```

Backend is selected via the `STAPEL_BUS_BACKEND` env var or Django setting
(shorthand `memory` / `kafka` / `nats` / `redis_streams`, or any dotted
path). Default is `memory` (`stapel_core.bus.backends.memory.MemoryBus`);
production picks one of `stapel_core.bus.backends.kafka.KafkaBus`,
`stapel_core.bus.backends.nats.NatsJetStreamBus`, or
`stapel_core.bus.backends.redis_streams.RedisStreamsBus` (needs
`pip install 'stapel-core[kafka]'` / `[nats]` / `[redis-bus]` respectively —
see `MODULE.md` for connection settings and delivery semantics).

---

### `stapel_core.notifications` — Push notifications

```python
from stapel_core.notifications import request_notification

request_notification(
    notification_type='welcome',
    user_id=str(user.id),
    email=user.email,
    variables={'name': user.username},
    source_service='auth',
)
```

---

### `stapel_core.oauth` — OAuth provider registry

Provider classes (`GoogleProvider`, `GitHubProvider`, etc.) and registry for
OAuth consumer flows (when your service accepts OAuth logins from external
providers).

---

### `stapel_core.gdpr` — GDPR utilities

Account closure requests, data export, re-registration hashes.

---

## Running tests

```bash
cd iron-common-python
pip install -e '.[dev]'
pytest stapel_core/tests/ -v
```
