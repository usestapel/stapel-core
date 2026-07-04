# stapel-core — MODULE.md

Agent-facing map of this package: what it provides, its fork-free extension
points, and anti-patterns. Use it to classify a desired change: business- or
deployment-specific behavior belongs in the **app layer** via the extension
points below; a generic fix or gap belongs **upstream** (see
[CONTRIBUTING.md](CONTRIBUTING.md)). Never fork this package to customize it.

## What this module provides

- `comm` — inter-module communication primitives: **Action** (`emit` /
  `@on_action`, transactional fire-and-forget via the outbox), **Function**
  (`call` / `@function`, synchronous RPC by name), **Task** (`start` /
  `status` / `@task_handler`, long-running work with persistent state).
  Modules never import each other — both sides know only a string name and a
  payload schema; transports are deployment configuration, not code.
- `bus` — transport-agnostic message bus (`publish`, `get_bus`, `Event`,
  `BusBackend`, `BaseBusConsumerCommand`) with in-memory, Kafka, NATS
  JetStream and per-topic routing backends.
- `conf.AppSettings` — per-app settings namespaces (the DRF `api_settings`
  pattern, generalized) with dotted-path `import_strings` seams.
- `verification` — step-up verification on any endpoint
  (`@requires_verification`), pluggable factor registry, per-user policy
  resolved via the `auth.verification.policy` comm Function.
- `flows` — self-documenting business scenarios (`Flow`, `@flow_step`,
  `flow_registry`) with doc generation and a CI completeness gate.
- `django` — service conventions: transactional outbox, task store,
  `StapelResponse` / `StapelErrorResponse` / error-key registry, OpenAPI
  helpers and postprocessing hooks, JWT auth middleware, common settings,
  management commands.
- `signals` — in-process Django signals for business milestones
  (`user_registered`, `user_logged_in`, `payment_completed`,
  `subscription_changed`, `media_processed`, `profile_updated`,
  `workspace_member_changed`).
- `gdpr` — `GDPRProvider` ABC, in-process `gdpr_registry`,
  `GDPRServiceConsumerCommand` for microservices mode.
- `captcha` — `CaptchaVerifier` ABC with turnstile / recaptcha / hcaptcha /
  noop backends, plus the tiered challenge policy (`captcha/policy.py`,
  `@captcha_protected`) driven by the client's network class.
- `netintel` — IP intelligence seam: `classify_ip(ip) -> IpProfile`,
  `country_of(ip)`, `client_ip(request)`; pluggable provider (MaxMind mmdb /
  generic HTTP JSON / null), Django-cache-backed, fail-open.
- `core` — framework-agnostic JWT primitives (`JWTHandler`, `TokenManager`,
  `TokenBlacklist`, `JWTConfig`).

All public root exports are lazy (PEP 562, see `__init__.py`); importing
`stapel_core` never touches Django until a Django-dependent attribute is used.

## Extension points (fork-free)

### Settings namespaces (`stapel_core.conf.AppSettings`)

`AppSettings(namespace, defaults, import_strings=...)` resolves each key in
order: `settings.<NAMESPACE>` dict → flat Django setting of the same name →
environment variable → default. Keys in `import_strings` are dotted paths
resolved with `import_string` — the standard seam for swapping behavior
without forking. Host apps and other stapel modules create their own
instances (e.g. `verification_settings` below).

### comm transports — `STAPEL_COMM` dict (`comm/config.py`)

| Key | Default | What it customizes |
|---|---|---|
| `ACTION_TRANSPORT` | `"inprocess"` | Action delivery: `inprocess` \| `bus` \| `memory` (`bus` delegates to `stapel_core.bus`) |
| `OUTBOX_ENABLED` | `True` | Route every `emit()` through the transactional outbox (disable only in tests) |
| `FUNCTION_TRANSPORT` | `"inprocess"` | Function RPC: `inprocess` \| `nats` \| `http` \| dotted path to `transport(name, payload, timeout=None)` (e.g. gRPC) |
| `FUNCTION_ROUTES` | `{}` | http transport: longest-prefix map of function name → base URL, e.g. `{"cdn.": "http://svc-cdn:8000/cdn"}` |
| `FUNCTION_TIMEOUT` | `5.0` | Default Function call timeout (seconds) |
| `NATS_URL` | `"nats://nats:4222"` | NATS transport broker address |
| `NATS_SUBJECT_PREFIX` | `"stapel.fn"` | NATS Function subject prefix |
| `VALIDATE_SCHEMAS` | `None` | Validate payloads against schemas from `@function` / `@on_action`; `None` = follow `settings.DEBUG` |
| `TASK_EXECUTOR` | `"inline"` | How a worker runs a claimed task: `inline` \| `celery` \| dotted path to `callable(task_id)` |
| `TASK_DISPATCH` | `"action"` | How `task.requested` reaches the worker: `action` (rides `ACTION_TRANSPORT`) \| `bus` (task.\* events go straight to the bus) \| `inline` (synchronous, tests only) |
| `SERVICE` | `None` | Service name stamped into emitted events; falls back to `SERVICE_NAME` |

`comm_setting()` also reads `HTTP_CONNECT_RETRIES` (2), `HTTP_POOL_CONNECTIONS`
(10), `HTTP_POOL_MAXSIZE` (50) for the pooled http transport session.

Registration seams: `@on_action(name, schema=...)` / `subscribe_action()`
(0..N subscribers per Action), `@function(name, schema=...)` /
`register_function()` (exactly one provider per Function),
`@task_handler(kind)` / `register_task()` (one executor per Task kind).
Registries: `action_registry`, `function_registry` (`comm/registry.py`).

### Bus backends — `STAPEL_BUS_BACKEND` (`bus/router.py`)

Resolution: env var first (12-factor), Django setting second, default
`"kafka"`. Value is a shorthand or any dotted path to a `BusBackend`
subclass — a custom broker needs zero core changes:

| Shorthand | Backend dotted path |
|---|---|
| `memory` | `stapel_core.bus.backends.memory.MemoryBus` |
| `kafka` | `stapel_core.bus.backends.kafka.KafkaBus` |
| `nats` | `stapel_core.bus.backends.nats.NatsJetStreamBus` |
| `routing` | `stapel_core.bus.backends.routing.RoutingBus` |

`routing` splits topics across brokers via `STAPEL_BUS_ROUTES` (env JSON or
Django dict) mapping topic prefix → shorthand/dotted path;
longest-prefix-wins, `""` is the default route (e.g.
`{"task.": "kafka", "": "nats"}`). Connection settings (`bus/_config.py`,
env-first then Django setting): `KAFKA_BOOTSTRAP_SERVERS`,
`KAFKA_SECURITY_PROTOCOL`, `KAFKA_SASL_MECHANISM` / `_USERNAME` / `_PASSWORD`;
`NATS_URL`, `STAPEL_NATS_STREAM` (`stapel-events`), `STAPEL_NATS_EVENT_PREFIX`
(`stapel.evt`). Consumers subclass `BaseBusConsumerCommand`.

### Verification factors & policy — `STAPEL_VERIFICATION` (`verification/conf.py`)

| Key | Default | What it customizes |
|---|---|---|
| `DEFAULT_FACTORS` | `["otp_email", "totp", "passkey"]` | Factors offered when a view doesn't pass its own list |
| `DEFAULT_MAX_AGE` | `300` | Grant lifetime (s) when a view doesn't pass `max_age` |
| `CHALLENGE_TTL` | `600` | Challenge lifetime (s) |
| `MAX_ATTEMPTS` | `5` | Failed attempts before a challenge is invalidated |
| `EXTRA_FACTORS` | `[]` | Dotted paths of custom factor classes, applied by `load_configured_factors()` |
| `DEFAULT_LEVEL` | `"strict"` | Level used when a view passes `level=None`: `strict` \| `default_on` \| `opt_in` |
| `POLICY_CACHE_TTL` | `60` | Cache TTL (s) for the resolved per-user policy |

Custom factors: subclass `VerificationFactor` (define `id`, implement
`verify`, optionally `available_for` / `initiate`) and call
`register_factor(instance_or_dotted_path)` from an `AppConfig.ready()`, or
list the dotted path in `EXTRA_FACTORS`. `@requires_verification(scope=...,
factors=..., max_age=..., level=...)` protects any DRF view method; the
per-user policy for `default_on` / `opt_in` levels is owned by the auth
service and resolved via the `auth.verification.policy` comm Function —
overriding policy storage means providing that Function, not patching core.

### Flows (`flows/registry.py`)

`Flow(flow_id, title=..., description=..., actors=...)` +
`@flow_step(flow, order=..., note=...)` on view methods; non-HTTP steps via
`Flow.action/.function/.task/.human`. `autodiscover_flows()` imports
`<app>.flows` from every installed app — a host project adds flows by
creating a `flows.py`, no registration wiring needed. `manage.py
generate_flow_docs --out docs/flows` renders markdown + `flows.json`;
`manage.py check_flows [--allow SUBSTRING]` is the CI completeness gate.

### Error registry (`django/api/errors.py`)

`register_service_errors({key: template})` adds service-specific error keys
to the global registry used by `StapelErrorResponse(status, key, params)`.
Raise `StapelValidationError(key, params)` from serializers or
`StapelServiceError(status, key, params)` from services — both are converted
by `stapel_exception_handler` (wired as DRF's `EXCEPTION_HANDLER` in the
common settings). Subclass `ErrorKeysView` and override
`get_service_errors()` to serve a service's key dictionary.

### OpenAPI hooks (`django/openapi/`)

`get_spectacular_settings(title, description, version, **extra)` merges
service settings over the common `SPECTACULAR_SETTINGS` and auto-appends
`stapel_core.django.openapi.extensions.stapel_postprocessing_hook`, which
annotates every operation with `x-stapel-flows` and `x-stapel-verification`
(plus a documented 403 challenge response). Extend via `**extra_settings`
or standard drf-spectacular `PREPROCESSING_HOOKS` / `POSTPROCESSING_HOOKS`
lists — hooks are dotted paths, so a host app adds its own without touching
core. `get_swagger_urls()` / `get_dev_urls()` provide the URL patterns; the
DRF defaults (`DEFAULT_SCHEMA_CLASS = PermissionAwareAutoSchema`,
`EXCEPTION_HANDLER`) are plain settings a project may override.

### Captcha backends & challenge policy — `STAPEL_CAPTCHA` (`captcha/`)

| Key | Default | Semantics | What it customizes |
|---|---|---|---|
| `BACKEND` | `None` (→ flat `CAPTCHA_BACKEND`, then `noop`) | replace | Verifier: `turnstile` \| `recaptcha` \| `hcaptcha` \| `noop` \| dotted path to a `CaptchaVerifier` subclass |
| `SECRET` | `None` (→ flat `CAPTCHA_SECRET`) | replace | Backend secret; empty → `NoopVerifier` (captcha disabled) |
| `CHALLENGE_MATRIX` | `{}` | **merge** over `DEFAULT_CHALLENGE_MATRIX` | ip-kind → level: residential/unknown → `invisible`, datacenter/vpn → `interactive`, tor → `interactive+ratelimit` |
| `ACTION_OVERRIDES` | `{}` | merge (per action) | `{action: {kind: level} \| "+1"}`; `"+1"` bumps one level (saturates at `block`) |
| `CHALLENGE_POLICY` | `stapel_core.captcha.policy.MatrixChallengePolicy` | replace (dotted path) | The whole `ChallengePolicy` (`level_for(request, action) -> level`) |

The legacy flat `CAPTCHA_BACKEND` / `CAPTCHA_SECRET` settings keep working;
`BACKEND`/`SECRET` are read from the `STAPEL_CAPTCHA` dict only (no env
fallback — a stray generic `SECRET` env var must not enable captcha).

Levels are ordered `none < invisible < interactive < interactive+ratelimit <
block` (`CHALLENGE_LEVELS`, `bump_level`, `level_gte`). Serializers use
`CaptchaMixin`; views use `@captcha_protected(action="register")`
(`django/captcha.py`): `none` passes, `block` → 403
`error.403.network_blocked`, other levels verify the token via the backend.
Backends MAY accept an optional `level` keyword
(`verify(token, ip=None, *, level=None)`) to force interactive challenges —
the decorator passes it only to backends that declare it, so legacy
two-argument backends work unchanged. Rate limiting is NOT performed by
captcha: the decorator sets `request.stapel_challenge_level` and rate-limit
middleware/hosts consume it (`interactive+ratelimit`). Every decision is
logged at INFO (`ip_kind, action, level, allowed`) — the input of host-side
antifraud scoring. With no netintel provider configured, every request
classifies as `unknown` → `invisible`, i.e. exactly the historical binary
behavior.

### NetIntel providers — `STAPEL_NETINTEL` (`netintel/`)

`classify_ip(ip) -> IpProfile{kind: residential|datacenter|vpn|tor|unknown,
asn, asn_org, country, confidence}`, `country_of(ip)`, `client_ip(request)`.
Fail-open by contract: provider errors log a warning once per provider class
and return the unknown profile — `classify_ip` never raises. Root exports:
`stapel_core.classify_ip` / `country_of` / `IpProfile` (lazy).

| Key | Default | Semantics | What it customizes |
|---|---|---|---|
| `PROVIDER` | `stapel_core.netintel.providers.NullProvider` | replace (dotted path/class/instance) | The IP intelligence source (`NetIntelProvider` ABC: `classify(ip)`, optional `country(ip)`) |
| `CACHE_ALIAS` | `"default"` | replace | Django cache used for results (key prefix `stapel-netintel:`) |
| `CACHE_TTL` | `86400` | replace | Result TTL (s) |
| `MAXMIND_ASN_DB` / `MAXMIND_COUNTRY_DB` / `MAXMIND_ANONYMOUS_DB` | `None` | replace | mmdb paths for `MaxMindProvider` (extra `stapel-core[netintel-maxmind]`); unset databases are skipped |
| `EXTRA_DATACENTER_ASNS` | `[]` | **merge** over builtin `HOSTING_ASNS` | Extra ASNs treated as hosting/datacenter |
| `HTTP_URL_TEMPLATE` / `HTTP_API_KEY` | `None` | replace | `HttpJsonProvider` endpoint (`{ip}` placeholder) and bearer key |
| `HTTP_RESPONSE_MAPPER` | `None` (builtin mapper) | replace (dotted path/callable) | `mapper(data, ip) -> IpProfile` — adapts any ipinfo/IPQS-style JSON |
| `TRUSTED_PROXY_HEADER` | `None` | replace | META key of the proxy-set client-IP header for `client_ip()`; default trusts `REMOTE_ADDR` only (proxy headers are spoofable) |

System checks (W-level, registered by `stapel_core.django` app):
`stapel_core.netintel.W001` (PROVIDER unimportable), `W002` (not a
`NetIntelProvider`) — a broken provider degrades, it never blocks a deploy.
MaxMind kind derivation: Anonymous-IP flags (tor > vpn > hosting) → ASN
hosting list → org-name keyword heuristic → residential (ASN known) →
unknown. `manage.py download_geolite` is a TODO (netintel package
docstring). Consumers: captcha challenge policy, OAuth region resolution
(stapel-auth), rate limits, analytics.

### GDPR providers (`gdpr.py`)

Subclass `GDPRProvider` (define `section`, implement `export` / `delete` /
`anonymize`) and either register with `gdpr_registry` (monolith) or ship a
management command subclassing `GDPRServiceConsumerCommand` with
`gdpr_service_name` matching an entry in `GDPR_COLLECTING_SERVICES`
(microservices).

### Signals (`signals.py`)

In-process seams for host projects (analytics, cache warm-up,
denormalization) — connect receivers, never fork. Same-process only, no
delivery guarantees; cross-module facts still go through comm Actions.

### Management commands (`django/**/management/commands/`)

| Command | Purpose |
|---|---|
| `dispatch_outbox [--once] [--interval] [--batch]` | Outbox relay: deliver pending Action events (loop or cron pass) |
| `consume_actions [--topics ...] [--group ...]` | Bus→registry bridge: consume remote Actions into local `@on_action` handlers |
| `serve_functions` | NATS Function server: expose this service's registered Functions (queue group = service name) |
| `sweep_tasks` | Fail comm Tasks past their deadline (cron / celery beat) |
| `generate_flow_docs --out DIR` | Render flow markdown + `flows.json` |
| `check_flows [--allow SUBSTRING]` | CI gate: flow documentation completeness |
| `staff_group`, `reset_sequences` | Staff group fixture management; DB sequence reset |

### Common Django settings (`django/settings.py`)

`from stapel_core.django.settings import *` gives the shared baseline
(`REST_FRAMEWORK`, `COMMON_INSTALLED_APPS`, `COMMON_MIDDLEWARE`, `LOGGING`,
JWT/CORS/session env-driven config, `get_default_database()`,
`get_common_templates()`, `get_staticfiles_dirs()`, `setup_sentry()`).
Everything is a plain module-level name — a service overrides by assignment
after the star-import; env vars drive deployment differences.

## Anti-patterns

- **Do not import other stapel modules from core** (or from each other).
  Cross-module communication is comm Actions/Functions/Tasks by string name
  only. Core cannot even validate another module's registry (see
  `notifications/publish.py` — payload shape only, by design).
- **Do not bypass the outbox for side effects.** `emit()` inside
  `transaction.atomic()` guarantees the event exists iff the transaction
  committed. Calling `bus.publish()` directly from request code, or setting
  `OUTBOX_ENABLED = False` outside tests, breaks that guarantee.
- **Do not hardcode transports in module code.** `ACTION_TRANSPORT`,
  `FUNCTION_TRANSPORT`, `TASK_DISPATCH`, `STAPEL_BUS_BACKEND` are deployment
  configuration; module code must work identically in monolith (inprocess)
  and microservices (bus/nats/http) modes.
- **Do not monkey-patch registries or core internals.** Every registry
  (`factor_registry`, `flow_registry`, `action_registry`,
  `function_registry`, error registry, `gdpr_registry`) has a public
  registration function — use it. `clear()` methods are tests-only.
- **Do not return bare DRF `Response` from views or invent error shapes.**
  Use `StapelResponse` / `StapelErrorResponse` and registered error keys —
  linters and clients depend on the envelope.
- **Do not swallow Function failures into fail-open defaults on
  security-relevant paths** (`comm.call` docstring); the verification policy
  module shows the correct fail-safe pattern.
- **Do not read `getattr(settings, ...)` ad hoc in a stapel package** — expose
  an `AppSettings` namespace so keys, defaults and dotted-path seams stay
  discoverable.
- **Action subscribers must be idempotent** — delivery is at-least-once
  (outbox retries, broker redelivery).

## App-layer override vs upstream contribution

Rule of thumb:

- **Business/deployment-specific** → override in the app layer via the
  points above: settings namespaces and `import_strings` dotted paths,
  custom bus backend / comm transport / task executor dotted paths,
  `register_factor`, `register_service_errors`, `GDPRProvider`, captcha
  backend, signal receivers, spectacular hooks, `flows.py` in your app.
  If a behavior can only be changed by editing this package, that missing
  seam is itself an upstream issue.
- **Generic fix or gap** (bug, missing extension point, a backend/factor
  useful to every deployment) → upstream contribution to this repository:
  see [CONTRIBUTING.md](CONTRIBUTING.md). Keep the diff inside this module,
  free of business identifiers.
