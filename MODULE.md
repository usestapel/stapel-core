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
- `i18n` — domain-agnostic shipping of localized content: per-app
  `translations/<domain>.<lang>.json` catalogs (later-wins, fork-free host
  override), a `.state.json` provenance sidecar, write-time
  `translate_catalogs` (seed → translator seam, byte-stable) and the
  `check_translation_catalogs` gate. `STAPEL_I18N["LOCALES"]` is the single
  project-languages knob.
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
- `eventstore` — append-only stream seam for high-volume streams (LLM-call
  ledger, gateway audit, analytics, delivery logs): `append` / `query`
  (cursor read) / `rollup` / `purge`; buffered batch writes, generic nullable
  identity columns (`project`/`task`/`container`), pluggable backend
  (`PostgresEventStore` default with PG time-partitions / SQLite plain-table
  degradation; ClickHouse the documented scale-out point), per-stream
  retention.
- `gateway` — privilege gateway mechanism: declared **verbs** (name + JSON
  schema + policy `{tiers, rate_limit, require_confirmation, audit_stream}`
  + handler) in a deny-by-default merge-registry; project-scoped opaque
  scope tokens (issue/verify/rotate/revoke) with network-identity binding;
  HTTP door for containers + comm Functions (`gateway.invoke` /
  `gateway.confirm`) for the control plane; two-phase confirmation; one
  audit line per outcome into the eventstore. Capability without
  credentials (system-design §5.9).
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

### i18n catalogs — domains, provenance, gate (`i18n/`, `STAPEL_I18N`)

`stapel_core.i18n` generalizes the flow-i18n contour to arbitrary content
**domains** (i18n-shipping.md). A domain `D` (`"flows"`, `"errors"`, …) ships
per-app catalogs `<app>/translations/D.<lang>.json` — flat `{key: text}` —
discovered over INSTALLED_APPS and merged **later-wins** (the host app, last,
overrides module texts without a fork; the same merge-over-builtins semantics
as every other registry). `flows.i18n` is the `"flows"` domain over this
(`load_app_catalogs`, `CommDocTranslator`, `DocTranslationCache` re-export from
here). `docs/errors.json` stays language-agnostic (en canon); localized error
texts live in `translations/errors.<lang>.json`, gen-errors reads them per
locale.

Localized texts are a **static, reviewed-as-code artifact**, generated
write-time:

- `manage.py translate_catalogs --domain errors --lang ru [--seed FILE] [--llm]
  [--approve KEY… | --approve-all]` materializes `<out>/errors.ru.json`
  (byte-stable) + a `.state.json` **provenance sidecar** keyed `<domain>.<lang>`
  → `{key: {hash: h(source_en), origin}}`. Per key: keep (source hash still
  matches) → seed from a curated corpus (`origin: seed:<label>`) → the
  `STAPEL_I18N["TRANSLATOR"]` seam (`--llm`, content-hash cached, `origin: llm`
  = machine/unreviewed) → left missing (fails the gate). `--approve` flips
  reviewed keys to `origin: human` without retranslating. Editing the en canon
  auto-staleness-marks exactly that one key (the hash no longer matches).
- The **first ru is not machine-translated**: `stapel-i18n-seed` (stapel-tools)
  exports the already-curated stapel-translate builtin fixtures (155 `error.*`
  × ru) into a seed the command applies — requirement "clients don't spend
  tokens" is met by copying, not re-running an LLM.
- `manage.py check_translation_catalogs --domain errors` is the CI gate
  (module pytest wraps `check_translation_catalogs(...)`, like `check_flows`):
  **E** on a missing key, a stale one (en changed, translation didn't), a
  `{param}` mismatch vs the canon, or a non-byte-stable file; **W** counts
  unreviewed (`origin: llm`/unknown) values (`--strict` makes them fatal —
  after the first review pass).
- `manage.py generate_error_docs [--lang ru]` writes the human-readable
  `docs/errors.<lang>.md` reference (i18n-shipping.md §4); README links both
  languages (lint rule `R100` in `stapel_tools.lint`).

`STAPEL_I18N` (`i18n/conf.py`): `LOCALES` (default `["en","ru"]`) — the single
"project languages" knob; `STAPEL_FLOWS["DOC_LANGUAGES"]` delegates to it
(`project_languages()`) unless a host sets it explicitly (doc languages may
differ from product languages). `EXTRA_CATALOG_DIRS` adds catalog roots outside
the apps. `TRANSLATOR` / `SOURCE_LANGUAGE` are the domain-agnostic
machine-translation seam (the `llm.translate` comm Function by name, default).

### Error registry (`django/api/errors.py`)

`register_service_errors({key: template}, remediation={key: hint})` adds
service-specific error keys to the global registry used by
`StapelErrorResponse(status, key, params)`. Raise
`StapelValidationError(key, params)` from serializers or
`StapelServiceError(status, key, params)` from services — both are converted
by `stapel_exception_handler` (wired as DRF's `EXCEPTION_HANDLER` in the
common settings). Subclass `ErrorKeysView` and override
`get_service_errors()` to serve a service's key dictionary.

The optional `remediation` map declares a machine-readable "what to do" hint
per key from the finite `REMEDIATION_VOCAB` (`retry`, `wait_and_retry`,
`reauthenticate`, `verify`, `fix_input`, `contact_support`, `bug`); undeclared
keys fall back to a status+name heuristic. `generate_error_keys --out
docs/errors.json` emits the backend codegen artifact — a byte-stable JSON array
of `{code, status, params, remediation, en}` (the companion of
`schema.json`/`flows.json`) that the frontend error bundle is generated from.
Commit it and gate drift with a regenerate-and-diff test (see stapel-auth's
`tests/test_error_keys.py`).

**Override semantics — the registry is `dict.update`, last-wins (a contract,
not an accident).** A host app's `errors` module (autodiscovered *after* the
framework modules) may re-word any shipped en text by registering the same key
— `register_service_errors({"error.423.locked": "…"})` — and both the artifact
and the raise-time render take the host value, no fork. This is the en tier of
the fork-free override seam (i18n-shipping.md §3); it is pinned by
`tests/test_error_i18n_contract.py` so it is never "fixed" into a duplicate
check. A localized override lives in a catalog instead (see i18n below); either
kind MUST preserve the canon `{placeholders}` — the gate enforces it.

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

### Secret provider — `STAPEL_SECRETS` (`secrets/`)

`get_secret(name, default=…) -> str | None` resolves a secret through a
dotted-path provider seam. The pattern: **settings read secrets through
`get_secret`, not `os.getenv`**, so a project moves its production secrets off
the environment into Vault by pointing one setting at a different provider —
no change to the settings that consume the secret.

```python
from stapel_core.secrets import get_secret

SECRET_KEY = get_secret("SECRET_KEY", "…dev fallback…")   # env by default
DATABASES["default"]["PASSWORD"] = get_secret("POSTGRES_PASSWORD")  # fail-closed in prod
```

| Key | Default | Semantics | What it customizes |
|---|---|---|---|
| `PROVIDER` | `stapel_core.secrets.EnvSecretProvider` | replace (dotted path/class/instance) | The secret source (duck type: `get(name) -> str | None`, optional `fail_closed`). Point at `stapel_vault.VaultSecretProvider` for OpenBao/Vault |
| `CACHE_TTL` | `300` | replace | Per-process value cache TTL (s); also the rotation re-read window. `0` disables caching |

- **Env default, zero deps.** `EnvSecretProvider` reads `os.environ`; local
  dev, the `minimal` preset and any unconfigured project are unchanged.
- **Fail-closed.** A provider returning `None` with no caller `default` raises
  `SecretUnavailable` — a missing prod secret is a loud boot failure. The env
  provider is `fail_closed = False` (missing var + no default → `None`,
  matching `os.environ.get`).
- **Bootstrap.** Prod settings resolve `SECRET_KEY` before `django.setup()`;
  the provider is then taken from the explicit `STAPEL_SECRETS_PROVIDER` env
  var (the generic `PROVIDER` key stays `no_env`). `django/settings.py`
  resolves `SECRET_KEY`/`JWT_SECRET_KEY` through this seam.
- **Cache/rotation.** Values are memoized for `CACHE_TTL`; `invalidate_secret()`
  forces an eager re-read (stapel-vault's rotation hook). Misses are never
  cached.
- **prodguard.** Guards run over the resolved value —
  `guard_secret("SECRET_KEY", get_secret("SECRET_KEY"))` — so a
  placeholder/short/empty secret is caught regardless of provider.
- System checks (W-level, `stapel_secrets`): W001 unimportable provider, W002
  not a provider. The env default never trips them.

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

### Event store — `STAPEL_EVENTSTORE` (`eventstore/`)

Append-only sink for high-volume streams that are written often, read as
aggregates, grow without bound, and stay out of band with business
transactions (LLM-call ledger, gateway audit, analytics, delivery logs — one
core primitive, not N bespoke tables). Facade API (root export
`stapel_core.eventstore`, lazy):

- `append(stream, payload, *, ts=None, project=None, task=None, container=None)`
  — buffered write. `append_batch(events)`, `flush()`.
- `query(stream, *, after=None, limit=100, time_range=None, filters=None)
  -> EventPage` — cursor read in `(ts, id)` order (id tie-break, so bursts
  never skip/repeat); `EventPage.cursor` (opaque `Cursor` token) feeds the
  next `after=`. `filters` match identity columns or payload keys.
- `rollup(stream, *, group_by, sum_fields, time_range=None, filters=None,
  into=None) -> list[RollupRow]` — group-by (identity columns or payload
  keys) + sum-fields; `into=` upserts buckets into a rollup table (replace /
  recompute semantics). Concrete rollups are the consumer's business.
- `purge(stream, *, older_than) -> int` — retention mechanism.

| Key | Default | Semantics | What it customizes |
|---|---|---|---|
| `BACKEND` | `…backends.postgres.PostgresEventStore` | replace (dotted path/class/instance) | The `EventStore` ABC impl (`append_batch`/`query`/`rollup`/`purge`) |
| `ROUTES` | `{}` | **merge**-routing by stream name | Per-stream backend override (`{"analytics": "…ClickHouseEventStore"}`); unlisted streams use `BACKEND` |
| `BUFFER_SIZE` | `500` | replace | Flush when the write buffer reaches N rows |
| `BUFFER_INTERVAL` | `5.0` | replace | Flush when the oldest buffered event is ≥ N seconds old |
| `BUFFER_SYNC` | `False` | replace | Write-through every append (tests/low-volume); reads always flush first |
| `RETENTION` | `{}` | replace | Per-stream raw retention in days, applied by `manage.py sweep_eventstore` |
| `RETENTION_ROLLUP` | `{}` | replace | Per-stream rollup retention in days (raw ≠ rollup) |
| `PARTITION_PERIOD` | `"month"` | replace | PG time-partition granularity (`month`/`day`); structural only off PostgreSQL |

`BACKEND`/`ROUTES` decide which store code runs and where a stream lands —
generic names, so `AppSettings(no_env=…)` blocks a stray same-named env var
from silently rerouting a stream (same guard as netintel `PROVIDER`).

Default `PostgresEventStore` (`stapel_core.django.eventstore`, in
`COMMON_INSTALLED_APPS`): append-only `EventRecord` `{stream, ts, payload
jsonb, project/task/container nullable}` + `EventRollup`. On PostgreSQL the
raw table is time-partitioned by `ts` (`django/eventstore/partitions.py` SQL
generators; `manage.py eventstore_partition [--dry-run] [--periods-ahead N]`
creates upcoming partitions idempotently; the parent-table conversion —
`partitions.parent_ddl` — is a one-time ops/RunSQL step). **On the SQLite
minimal profile it degrades to one plain table with no partitions** — same
rows, same API; the partition command reports skipped rather than erroring.
Rollup aggregation runs in Python so it is identical on every engine (pushing
the GROUP BY into SQL / ClickHouse is the scale-out optimization). ClickHouse
is the documented evolution point — the ABC already permits it; it is **not**
implemented here (add a backend, flip `BACKEND`/`ROUTES`). Consumers (Studio
steel thread): LLM-call ledger with the five-component usage split, gateway
audit (SN-4), delivery logs.

### Privilege gateway — `STAPEL_GATEWAY` (`gateway/`)

The mechanism behind "the agent gets the *capability*, never the
*credentials*" (system-design §5.9). A **verb** = name + mandatory JSON
schema for its arguments + policy + handler; untrusted code in a project
container reaches one endpoint with the declared verbs and nothing else —
keys, passwords and scripts stay behind the gateway. Root export
`stapel_core.gateway` (lazy).

**Threat model (short).** The container is hostile (prompt-injected agent,
malicious dependency — S5). It cannot: call an undeclared verb
(deny-by-default registry; 404 without enumeration), pass unvalidated
input (schema check is mandatory and fails closed without a validator),
speak without a live project-scoped token (opaque, sha256-at-rest,
short-lived, instantly revocable), speak *about* another project (token
scope + optional body cross-check + network identity), outrun its quota
(per-`(verb, project)` rate limit), execute a destructive verb alone
(two-phase confirmation resolves only via the control-plane comm/Python
surface — never the container door), or act invisibly (every outcome —
executed/denied/pending/confirmed/rejected/expired — is one audit line;
sink failure fails closed and noisy). Residual risk: the default audit
sink buffers through the eventstore `WriteBuffer` — a strict deployment
sets `STAPEL_EVENTSTORE["BUFFER_SYNC"]` or plugs a synchronous
`AUDIT_SINK`. Confirmation and token issuance are control-plane APIs; the
`stapel_core.django.gateway` app is **opt-in** (not in
`COMMON_INSTALLED_APPS`) — mount the privilege surface deliberately.

Declaring and calling:

```python
from stapel_core import gateway

@gateway.verb("send_email", schema={...}, policy={
    "tiers": ["starter", "business"], "rate_limit": "30/h",
    "require_confirmation": False, "audit_stream": "audit"})
def send_email(args: dict, caller: gateway.CallerContext): ...

issued = gateway.issue_token("proj-1", container="c-1", network="10.0.7.4")
# containers: urls.py += gateway.get_gateway_urls()
#   POST api/_gateway/send_email/  Authorization: Bearer sgw_…  {"args": {...}}
# control plane: call("gateway.invoke", {...}) / call("gateway.confirm", {...})
# tokens: verify_token / rotate_token(grace=…) / revoke_token / purge_expired_tokens
```

| Key | Default | Semantics | What it customizes |
|---|---|---|---|
| `VERBS` | `{}` | **merge** over `register_verb()` | Per-verb patch (policy merges per key, schema/handler replace), settings-only verbs, or `None` to disable a verb (deny-by-default again) |
| `POLICY_ENGINE` | `…policy.DefaultPolicyEngine` | replace (dotted path) | Allow/deny brain; subclass, `super().check()`, add rules (budgets, freeze windows). Unresolvable tier on a restricted verb **denies** |
| `RATE_LIMITER` | `…ratelimit.CacheRateLimiter` | replace (dotted path) | Quota store; default Django-cache fixed window per `(verb, project)` |
| `AUDIT_SINK` | `…audit.eventstore_sink` | replace (dotted path) | `callable(stream, payload, *, project, container)`; failures propagate as `AuditFailure` |
| `AUDIT_STREAM` | `"audit"` | replace | Default eventstore stream (per-verb: `policy.audit_stream`) |
| `AUDIT_ARGS_MAXLEN` | `2048` | replace | Args longer than this (canonical JSON) become a sha256 fingerprint on the audit line |
| `TOKEN_TTL` | `3600` | replace | Scope-token lifetime (seconds) |
| `NETWORK_VERIFIER` | `…network.default_verifier` | replace (dotted path) | `callable(ip, token) -> bool`; default enforces the token's bound IP/CIDR from `REMOTE_ADDR` only (proxy trust = custom verifier) |
| `REQUIRE_NETWORK_BINDING` | `False` | replace | `True` refuses HTTP calls with tokens that carry no network binding (strict fleet posture) |
| `TIER_RESOLVER` | `None` | replace (dotted path) | `callable(project) -> tier` when the caller carries none |
| `CONFIRMATION_TTL` | `900` | replace | Pending (`require_confirmation`) actions expire after N seconds |

All trust-deciding keys are `no_env` — a stray same-named env var can never
swap the policy engine, the audit sink, or the network verifier.

### Staff mandate — `STAPEL_ACCESS` (`access/`)

Mandatory access control for staff/admin (docs/admin-suite.md §3, AS-1):
staff rights are a *computed function* of (model declaration × role
clearance), never rows accumulated in `auth_permission`. Clearances
`LOW < MID < HIGH`; superuser is outside the mandate (Django semantics),
non-staff never receives mandate grants.

```python
from stapel_core.access import access, Level

@access.standard      # business; view=LOW, add/change=MID, delete=HIGH — the
class Listing(...): … # implicit default of every undecorated model
@access.sensitive     # view=MID, mutations HIGH (PII, money)
@access.ops           # ops journal: view=HIGH, add/change/delete forbidden
@access.secret        # superuser-only, all operations
@access(view="mid", delete="high", category="business")   # full form

AUTHENTICATION_BACKENDS = [
    "stapel_core.access.backend.MandateBackend",        # MAC: declaration × clearance
    "stapel_core.access.backend.AuditedModelBackend",   # DAC overlay: manual grants,
]                                                       # escalation audited / STRICT-capped
```

| Key | Default | Semantics | What it customizes |
|---|---|---|---|
| `ROLES` | `{}` | **merge** over builtins `viewer`(LOW)/`editor`(MID)/`admin`(HIGH) | Role definitions: `{"accountant": {"clearance": "low", "apps": {"stapel_billing": "high"}}}`; `None` disables a builtin; `apps` = per-app clearance scope |
| `MODELS` | `{}` | **merge** over `@access` decorators | Host override per `"app_label.Model"`: patch dict (`{"delete": "mid"}`) or `None` (back to implicit standard) |
| `ROLE_SOURCES` | claim → user-field → `role:*` groups | replace (list of dotted paths/callables) | Where a user's roles come from: `(user) -> list[str] \| None`; first non-`None` is authoritative (empty list terminates — sync-down replace) |
| `STRICT` | `False` | replace | `False`: DAC grant above mandate allowed but logged + `dac_escalation` signal + `access_report` line (A4). `True`: mandate is a ceiling, escalation denied |
| `RUNTIME_ROLE_DEFINITIONS` | `False` | reserved | Runtime-editable role definitions (mini-design in `access/roles.py`, not implemented; W-check if set) |

All keys are `no_env`. Feature is opt-in by the first role: with no roles
resolvable the backends behave like today's Django (checks
`stapel_core.access.E00x/W00x` flag misconfiguration). Audit surface:
`manage.py access_report [--json]` — role × model × operation matrix, DAC
grants above mandate, undeclared models. Role *assignment* transport (JWT
claim `staff_roles`, `StaffRole` in stapel-auth) is AS-2; admin visibility
built on the same declarations is AS-3.

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
| `generate_error_keys --out FILE` | Emit `errors.json` (the error-key registry: `{code, status, params, remediation, en}`) — the backend codegen artifact the frontend error bundle is generated from |
| `generate_project_docs --out DIR [--languages …] [--llm]` | Bilingual flow doc trees, one per project language (`STAPEL_I18N["LOCALES"]`) |
| `translate_catalogs --domain D --lang X [--seed F] [--llm] [--approve … \| --approve-all]` | Generate/refresh `translations/D.X.json` + `.state.json` provenance (seed → translator seam, byte-stable, content-hash cached) |
| `check_translation_catalogs --domain D [--languages …] [--strict]` | CI gate: catalogs cover the canon, are fresh, preserve `{params}` (E); counts unreviewed (W) |
| `generate_error_docs [--lang X] [--out docs]` | Human-readable `docs/errors.<lang>.md` reference (i18n-shipping.md §4) |
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
