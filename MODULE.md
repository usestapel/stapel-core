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
| `EMIT_OUTSIDE_ATOMIC` | `"warn"` | `emit()` with the outbox on but outside `transaction.atomic()`: `warn` (log with stack) \| `error` (raise `EmitOutsideAtomicError`) \| `allow`. Set `error` in module test settings to gate on it |
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

### Outbox atomicity — `mutate_and_emit()` + emit-check (`comm/actions.py`, `lint/emit_check.py`)

The outbox guarantee — *the event leaves iff the surrounding transaction
commits* — is a seam, not a discipline. The canonical mutation+emit pattern:

```python
from stapel_core.comm import mutate_and_emit

with mutate_and_emit() as emit_event:
    listing.status = ListingStatus.PUBLISHED
    listing.save(update_fields=["status"])
    emit_event("listing.published", {"listing_id": str(listing.pk)},
               key=str(listing.pk))
```

Everything in the block commits or rolls back as one unit; the yielded
callable has the exact `emit()` signature (0..N calls per block, refuses to
run once the block exits). `with mutate_and_emit():` without `as` is valid
when emits happen through `emit_*` helper functions inside the block.
Nesting inside a wider `transaction.atomic()` joins the outer transaction.

Mechanical guards behind it (they also protect plain `emit()`):

- a failed emit inside an atomic block marks the transaction rollback-only
  before propagating — swallowing the exception cannot commit the mutation
  without its event (the categories C1 bug class);
- `emit()` outside any atomic block (mutation and outbox row in separate
  transactions — the listings L2 bug class; also emit inside `on_commit`
  callbacks) is flagged per `EMIT_OUTSIDE_ATOMIC` above;
- `python -m stapel_core.lint.emit_check .` — static CI gate for the same
  classes (EMIT001 emit in except, EMIT002 swallowed emit, EMIT003
  mutation+emit without shared atomic, EMIT004 emit in on_commit). Lexical
  only; suppress a proven false positive with `# emit-check: ok — <reason>`.
  Module repos run it in pre-commit/CI next to ruff.

Review checklist for data-holding modules: every emit is atomic with its
mutation, and a `test_failing_emit_rolls_back`-class test exists (see
`tests/test_emit_atomicity.py` here for the reference shapes).

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

### Flow SA-document renderer (`flows/docs.py`, flow-system.md §4)

`STAPEL_FLOWS["FLOW_DOC_RENDERER"]` (dotted path; default
`DefaultFlowDocRenderer`) turns a `Flow` into a markdown SA-document: a
GitHub-native `mermaid` step diagram, the numbered steps, and an
**Endpoints** table with request/response serializers and the step-up
verification contract (`scope` + factors). Point the seam at your own class
to swap the whole look — no fork. Protocol: `render_flow(flow, index, texts,
language)` / `render_index(flows, index, texts, language)`.

The renderer's own scaffolding (headings, table columns, "User action") is
localized by the `language` argument (`en`/`ru` built in; unknown → English)
while the content resolves from i18n keys — so a module shipping only en/ru
catalogs still renders any language with English chrome around translated
content.

`manage.py generate_project_docs --out docs/flows [--llm]` writes one
**byte-stable tree per `STAPEL_FLOWS["DOC_LANGUAGES"]`** language (`["en",
"ru"]` by default) from the single language-agnostic `flows.json`:
`docs/flows/{flows.json, README.md, en/…, ru/…}`, the root README links each
tree. The determinism is the point — regenerate + `git diff --exit-code` is
the release-gate drift check (library-standard §4); a no-op regen is a no-op
diff. The module README tags both trees, e.g.
`[Flows (EN)](docs/flows/en/README.md) · [Флоу (RU)](docs/flows/ru/README.md)`.

### Flow i18n (`flows/i18n.py`, flow-system.md §2)

Flow texts are i18n keys, not literals: each flow/step derives an implicit
key (`flow.<id>.title` / `flow.<id>.description` /
`flow.<id>.step.<order>.note`; explicit `title_key`/`description_key`/
`note_key` parameters override) while the in-code literal stays the
canonical English source text and the render fallback — literal-only flows
work unchanged. `flows.json` carries keys + literals + API bindings and is
language-agnostic.

Rendering in language X (`resolve_flow_texts(flows, lang)`, or `manage.py
generate_flow_docs --lang X [--llm]`) resolves each key through:

1. committed per-app catalogs `<app>/translations/flows.<lang>.json`
   (merged over INSTALLED_APPS, later apps win — modules ship en/ru,
   reviewed as code; stapel-auth is the reference);
2. the `translate.resolve` comm Function (host-project values, best-effort,
   fills only keys the catalogs do not cover);
3. the **`STAPEL_FLOWS["DOC_TRANSLATOR"]` seam** (opt-in via `--llm` /
   `llm=True`) — dotted path; the default `CommDocTranslator` calls the
   `llm.translate` comm Function *by name* (core never imports the agent
   package). Output goes through a content-hash cache file (commit it):
   regeneration without source changes = zero LLM calls, zero diff — the
   same byte-stable discipline as `dump_translations`;
4. the source literal.

`STAPEL_FLOWS["DOC_SOURCE_LANGUAGE"]` (default `"en"`) declares the literal
language passed to the translator. A custom translator is any class with
`translate(entries: dict[key, source_text], source_language,
target_language) -> dict[key, text]`.

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
behavior. `remoteip` sent to siteverify and the IP in logs use
`netintel.client_ip` — the *same* trust model as classification (REMOTE_ADDR
unless `TRUSTED_PROXY_HEADER` is set), not a separate `X-Forwarded-For` read.

**Tiering is currently nominal for the builtin backends.** The three builtin
verifiers (`Turnstile`/`Recaptcha`/`Hcaptcha`) do not declare the `level`
kwarg, so every level above `none`/`block` verifies the token *identically* —
the *effect* of the tier is carried by `request.stapel_challenge_level`
(rate-limit middleware) and by the 403 at `block`, not by a stricter token
check. Genuine per-level verification needs a custom backend that (a) accepts
`level` and (b) is paired with a frontend channel that renders the matching
widget strictness (Turnstile interactive vs managed) and/or enforces a
reCAPTCHA-v3 score threshold. The `level` kwarg seam exists precisely so such
a backend drops in without touching the builtins. See the M2 note in the
change log for the proposed future contract.

`ACTION_OVERRIDES` bumping (`"+1"`) **saturates at `block`**: applied to an
already-strict kind (e.g. `tor` → `interactive+ratelimit`, or `vpn` on a
matrix that raised it) a single `"+1"` can reach `block` and 403 the request.
`block` is otherwise never produced by the default matrix — blocking a
network class is always an explicit host decision, so audit `"+1"` overrides
against the strict rows of the matrix.

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
| `CACHE_TTL` | `86400` | replace | Positive result TTL (s) |
| `NEGATIVE_CACHE_TTL` | `60` | replace | Fail-open (unknown) result TTL (s) — short, so a provider outage self-heals but does not hammer an unhealthy provider on every miss |
| `MAXMIND_ASN_DB` / `MAXMIND_COUNTRY_DB` / `MAXMIND_ANONYMOUS_DB` | `None` | replace | mmdb paths for `MaxMindProvider` (extra `stapel-core[netintel-maxmind]`); unset databases are skipped |
| `EXTRA_DATACENTER_ASNS` | `[]` | **merge** over builtin `HOSTING_ASNS` | Extra ASNs treated as hosting/datacenter |
| `HTTP_URL_TEMPLATE` / `HTTP_API_KEY` | `None` | replace | `HttpJsonProvider` endpoint (`{ip}` placeholder) and bearer key |
| `HTTP_RESPONSE_MAPPER` | `None` (builtin mapper) | replace (dotted path/callable) | `mapper(data, ip) -> IpProfile` — adapts any ipinfo/IPQS-style JSON |
| `TRUSTED_PROXY_HEADER` | `None` | replace | META key of the proxy-set client-IP header for `client_ip()`; default trusts `REMOTE_ADDR` only (proxy headers are spoofable) |

`PROVIDER`, `HTTP_URL_TEMPLATE`, `HTTP_API_KEY` and `TRUSTED_PROXY_HEADER`
carry trust/security weight and have generic names, so they are **never**
sourced from a same-named environment variable (an `AppSettings(no_env=…)`
guard) — they resolve only from the `STAPEL_NETINTEL` dict, a flat Django
setting, or the default. This mirrors captcha's `BACKEND`/`SECRET`: a stray
env var must not silently change which header is trusted or which provider
runs.

Resilience under load: `classify_ip` memoizes the provider instance
module-level (so `MaxMindProvider`'s per-instance mmdb `Reader`s — mmap + fd —
are opened once, not per request; lazy open is `threading.Lock`-guarded for
the shared singleton). Provider errors fail open, are cached for
`NEGATIVE_CACHE_TTL` (per-IP), and advance a small consecutive-failure
**circuit breaker**: after 5 straight failures the provider is skipped for a
short window (local unknown) so a flood of *distinct* IPs against an
unhealthy provider cannot pin every request on it. `HttpJsonProvider` does a
**blocking** `requests.get` on the request path and is **not** intended for a
production hot path — use the offline `MaxMindProvider` there; reserve
`HttpJsonProvider` for low-volume/offline enrichment.

System checks (W-level, registered by `stapel_core.django` app):
`stapel_core.netintel.W001` (PROVIDER unimportable), `W002` (not a
`NetIntelProvider`) — a broken provider degrades, it never blocks a deploy.
MaxMind kind derivation: Anonymous-IP flags (tor > vpn > hosting) → ASN
hosting list → org-name keyword heuristic → residential (ASN known) →
unknown. The builtin `HOSTING_ASNS` list (AWS/Azure/GCP/Cloudflare/Fastly/…)
is a **heuristic fallback only and intentionally incomplete**; the accurate
source of truth is the offline MaxMind **Anonymous-IP** database
(`MAXMIND_ANONYMOUS_DB`, `is_hosting_provider`), consulted first. Without that
mmdb the ASN heuristic under-detects datacenter/VPN egress. AS15169 (Google's
main ASN) is deliberately excluded — it also carries consumer traffic;
AS396982 (Google Cloud) is the datacenter-only ASN. `manage.py
download_geolite` is a TODO (netintel package docstring). Consumers: captcha
challenge policy, OAuth region resolution (stapel-auth), rate limits,
analytics.

### GDPR providers (`gdpr.py`)

Subclass `GDPRProvider` (define `section`, implement `export` / `delete` /
`anonymize`) and either register with `gdpr_registry` (monolith) or ship a
management command subclassing `GDPRServiceConsumerCommand` with
`gdpr_service_name` matching an entry in `GDPR_COLLECTING_SERVICES`
(microservices).

### Revision sync contract — `RevisionMixin` (`django/models.py`, `django/api/revision.py`)

Every model that participates in client sync inherits `RevisionMixin`
(`revision` + `deleted`, `get_changes_since`, DRF plumbing in
`django/api/revision.py`). The save contract (0.5.1):

- `save()` — content change: revision bumps to `MAX(revision)+1`.
- `save(update_fields=[...])` **without** `"revision"` — scoped non-synced
  write (drafts, counters): **no bump**. DB row, instance and post_save
  receivers all keep the current revision — never a phantom number.
- `save(update_fields=[..., "revision"])` — explicit opt-in: bump is issued
  and persisted with the listed fields.

Issuance is concurrency-safe: on PostgreSQL a transaction-scoped advisory
lock (`pg_advisory_xact_lock` keyed on the table) serializes issue→COMMIT
across processes (numbers are unique and commit-ordered — `get_changes_since`
never skips); on other backends (SQLite minimal profile) a process-local
mutex per (alias, table) serializes issue+commit. Caveat: outside PostgreSQL,
when the save is nested in a long outer `transaction.atomic` the mutex
releases before the outer COMMIT — multi-threaded writers there should use
PostgreSQL or SQLite `"transaction_mode": "IMMEDIATE"`.

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
| `generate_flow_docs --out DIR [--lang X] [--llm] [--llm-cache FILE]` | Render flow markdown + `flows.json`; `--lang` resolves i18n keys, `--llm` machine-translates missing keys (content-hash cached) |
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
- **Do not emit outside the mutating transaction, and never swallow an emit
  failure.** Use `mutate_and_emit()` (above); `save()`-then-`emit()` without
  a shared atomic block, `try/except` around `emit`, and `emit` inside
  `on_commit` callbacks are all flagged by the emit-check gate and the
  `EMIT_OUTSIDE_ATOMIC` runtime guard.
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
