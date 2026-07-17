# CONFIG.MD — stapel-core

Config registry for **stapel-core** (`static-scaffold-and-config.md` §2). One
row per configuration key core reads, its **source** (`env` = the process
environment; `vault` = the secret-provider seam, `stapel_core.secrets`), what
it is for, whether it is required, and its default.

`get_config(key)` (`stapel_core.config`) routes each read here: `env` keys are
read from `os.environ`; `vault` keys are delegated to `get_secret` (which is
the environment too, until a project points `STAPEL_SECRETS_PROVIDER` at
OpenBao/Vault). This file is a **library registry**: at scaffold time
`assemble_scaffold` aggregates it (under the `## stapel-core` owner heading
below) with the CONFIG.MD of every other selected lib into the project's own
root CONFIG.MD, and the `config-lint` gate (CFG001–CFG003) checks the result.

The keys below are read *inside* stapel-core (its `django/settings.py`,
`secrets`, `bus`, `kafka`, monitoring), so they are owned by core and exempt
from CFG003 in a consuming project — the project need not read them itself.

**Required is enforced at boot, not on first use** (0.11.1+): a `Required:
yes` row with no value and no default used to only raise
(`ConfigUnavailable`) the first time some code path called `get_config(key)`
— possibly never, for a key nothing reads through the config seam yet. The
`stapel_config` system check (`stapel_core.config.checks`) now walks every
required key at `manage.py check` / boot-smoke time and fails loud
(`E001: config 'X' обязателен — нужен для: ...`) before the service is
allowed to start.

**Metadata can come from code, not just this hand-written table**:
`get_config(key, purpose=..., required=...)` (or the standalone
`declare_config(key, source=, purpose=, required=, default=)`, called once at
import time) records the same purpose/required/default a regenerator would
otherwise have to trust this file to have kept in sync with the code — the
declaration never overrides a real row here (this table wins on overlap),
it only fills in keys this file does not know about yet. See
`declared_config_entries()`.

## stapel-core

### Secrets (source: vault)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| SECRET_KEY | vault | Django secret key (signing, sessions, CSRF). Prod boot refuses a placeholder. | yes | |
| JWT_SECRET_KEY | vault | HS256 JWT signing/verify secret. | no | falls back to SECRET_KEY |
| JWT_PRIVATE_KEY | vault | RS256 JWT signing key, base64 PEM (auth service only). | no | |
| SERVICE_API_KEY | vault | Shared service-to-service API key. | no | |
| POSTGRES_PASSWORD | vault | Database password. | no | stapel |
| KAFKA_CLIENT_PASSWORD | vault | Kafka SASL password. | no | |
| TELEGRAM_BOT_TOKEN | vault | Telegram bot token for the alert log handler. | no | |

### Runtime configuration (source: env)

| Key | Source | Purpose | Required | Default |
|-----|--------|---------|----------|---------|
| DJANGO_ENV | env | Deploy profile: `local` \| `prod` (turns on prod hardening). | no | local |
| DEBUG | env | Django debug mode. | no | derived from DJANGO_ENV |
| LOG_LEVEL | env | Root logger level. | no | INFO |
| SERVICE_NAME | env | Service name stamped into logs/events/alerts. | no | |
| STAPEL_HOST | env | Primary public host (ALLOWED_HOSTS / issuer / origins derive from it). | no | localhost |
| STAPEL_AUTH_SERVICE_PREFIX | env | Mount prefix of the auth service; empty = no dedicated auth service. | no | auth |
| ALLOWED_HOSTS | env | Comma-separated host allowlist; empty = derive from STAPEL_HOST. | no | |
| CSRF_TRUSTED_ORIGINS | env | Comma-separated CSRF trusted origins; empty = derive from STAPEL_HOST. | no | |
| CORS_ALLOWED_ORIGINS | env | Comma-separated CORS origin allowlist. | no | |
| CORS_ALLOW_ALL_ORIGINS | env | Allow any CORS origin (dev only). | no | False |
| REDIS_URL | env | Redis cache / session store URL. | no | redis://redis:6379/0 |
| POSTGRES_DB | env | Database name. | no | (per-service) |
| POSTGRES_USER | env | Database user. | no | stapel |
| POSTGRES_HOST | env | Database host. | no | db |
| POSTGRES_PORT | env | Database port. | no | 5432 |
| CONN_MAX_AGE | env | Django persistent-connection age (seconds); 0 = per-request. | no | 0 |
| STAPEL_BUS_BACKEND | env | Message-bus backend dotted path; kafka/nats/redis_streams are explicit opt-in (0.11.0+). | no | stapel_core.bus.backends.memory.MemoryBus |
| KAFKA_BOOTSTRAP_SERVERS | env | Kafka bootstrap servers. | no | kafka:9092 |
| KAFKA_SECURITY_PROTOCOL | env | Kafka security protocol. | no | PLAINTEXT |
| KAFKA_SASL_MECHANISM | env | Kafka SASL mechanism. | no | |
| KAFKA_CLIENT_USER | env | Kafka SASL username. | no | |
| STAPEL_REDIS_BUS_URL | env | Redis Streams bus connection URL. | no | REDIS_URL, else redis://redis:6379/0 |
| STAPEL_REDIS_BUS_CLAIM_IDLE_MS | env | XAUTOCLAIM idle threshold (ms) before a pending entry is reclaimed from a presumed-dead consumer. | no | 60000 |
| STAPEL_REDIS_BUS_STREAM_MAXLEN | env | Approximate XADD stream cap (MAXLEN ~); 0 disables trimming. | no | 100000 |
| JWT_PUBLIC_KEY | env | RS256 JWT verify key, base64 PEM (non-signing services). | no | |
| JWT_ALGORITHM | env | JWT algorithm; auto RS256 if a key is present, else HS256. | no | HS256 |
| JWT_ISSUER | env | JWT `iss` claim. | no | https://STAPEL_HOST |
| JWT_AUDIENCE | env | JWT `aud` claim. | no | stapel |
| JWT_ACCESS_TOKEN_LIFETIME | env | Access-token lifetime (seconds). | no | 3600 |
| JWT_REFRESH_TOKEN_LIFETIME | env | Refresh-token lifetime (seconds). | no | 604800 |
| JWT_COOKIE_NAME | env | Access-token cookie name. | no | stapel_jwt |
| JWT_REFRESH_COOKIE_NAME | env | Refresh-token cookie name. | no | stapel_refresh_jwt |
| JWT_COOKIE_DOMAIN | env | Cookie domain; empty = host-only. | no | |
| JWT_COOKIE_SECURE | env | Send auth cookies only over HTTPS. | no | False |
| JWT_COOKIE_SAMESITE | env | Auth cookie SameSite policy. | no | Lax |
| SENTRY_DSN | env | Sentry DSN; empty disables Sentry. | no | |
| SENTRY_ENVIRONMENT | env | Sentry environment tag. | no | dev |
| STAPEL_SECRETS_PROVIDER | env | Bootstrap dotted path of the secret provider (before django.setup). | no | env provider |
| TELEGRAM_ALERT_CHAT_ID | env | Telegram chat id for alerts. | no | |
| TELEGRAM_ALERT_THREAD_ID | env | Telegram topic/thread id for alerts. | no | |
