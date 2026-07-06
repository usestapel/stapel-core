# Changelog

## [Unreleased]

### Added — `errors.json` codegen artifact + declarative remediation (error-remediation)

- **`errors.json` — the backend companion of `schema.json`/`flows.json`.** New
  management command `generate_error_keys --out docs/errors.json` emits the
  language-agnostic registry of every `error.<status>.<name>` key the instance
  can raise: a JSON array of `{code, status, params, remediation, en}`, sorted
  by code, byte-stable (a no-op regen is a no-op diff — drift-gate ready). The
  shape matches what the frontend `gen-errors.mjs` currently produces by parsing
  `errors.py` directly, so a pair can migrate onto the emitted artifact without
  a format change (frontend follow-up). The command populates the registry
  deterministically — `autodiscover_modules("errors")` for every INSTALLED_APP
  plus the cross-cutting core mechanisms (`stapel_core.verification.errors`,
  `stapel_core.django.captcha`) and any `settings.STAPEL_ERROR_MODULES` — rather
  than relying on whichever view/serializer happened to be imported.

- **Declarative `remediation` on the error registry
  (`stapel_core.django.api.errors`).** `register_service_errors(errors,
  remediation=None)` gains an optional `code -> remediation` map — a
  machine-readable "what to do" hint from the finite `REMEDIATION_VOCAB`
  (`retry`, `wait_and_retry`, `reauthenticate`, `verify`, `fix_input`,
  `contact_support`, `bug`). It is validated at registration (every key must be
  in the accompanying `errors` map and carry a vocabulary value). Undeclared
  keys fall back to `default_remediation(code, status, params)`, a status+name
  heuristic ported byte-for-byte from the frontend, so the artifact carries a
  remediation for every key by construction. `build_error_registry()` projects
  the global registry into the `errors.json` structure. The `verification` and
  `captcha` mechanisms now declare their own remediation (e.g. a lost
  verification challenge → `verify`, a network block → `contact_support`).

- **Captcha error text aligned to the canonical (fuller) copy.**
  `stapel_core.django.captcha` now registers `error.400.captcha_invalid` /
  `error.400.captcha_required` with the same wording consumers use
  (`"Captcha verification failed. Please try again."` / `"Captcha token is
  required."`), so a service that re-declares these keys produces an
  order-independent `errors.json`.

### Added — hardened prod-guard for generated-project settings (SEC-4/SEC-6)

- **`stapel_core.django.prodguard`**: `guard_secret(name, value, min_length=50)`
  and `guard_db_password(password)` — the prod-only startup checks
  `stapel-tools` templates now call from `core/settings/prod.py` (monolith /
  microservices) and the minimal preset's `DJANGO_ENV=prod` branch
  (docs/security-programme.md gaps B2/B6). The old inline guard only rejected
  an empty `SECRET_KEY` or one starting with `django-insecure-`; a shipped
  `.env.example` placeholder (`change_me_to_a_long_random_string`) or the
  default `POSTGRES_PASSWORD=stapel`/`change_me` sailed straight through into
  a live deployment. `guard_secret` now also rejects any `change_me*`-prefixed
  value and anything shorter than 50 characters (raised or lowered per call
  via `min_length`); `guard_db_password` rejects the library's dev-only
  Postgres default and the placeholder value, case-insensitively. Both raise
  `django.core.exceptions.ImproperlyConfigured` (fail-closed, same shape as
  the existing DEBUG/JWT-secret checks). Pairs with SEC-6 in `stapel-tools`,
  which now writes freshly generated secrets into `.env` at project creation
  so these guards only ever fire on the "deployed as downloaded" mistake, not
  on a normally-configured project.

## [0.8.0] - 2026-07-06

### Changed — taskstore Django label renamed (frees `stapel_tasks` for the tasks module)

- **`stapel_core.django.taskstore` app label: `stapel_tasks` → `stapel_taskstore`.**
  The internal comm-**Task** persistence app (records for async named
  background operations — module-communication §2.1) historically claimed the
  Django label `stapel_tasks`. The new generic user-facing task/kanban module
  **stapel-tasks** (0.1.0) owns that canonical label, and two apps cannot share
  a label in one `INSTALLED_APPS` (`ImproperlyConfigured: Application labels
  aren't unique`). Core vacates to `stapel_taskstore` so both coexist
  (docs/tasks-module.md §2/§11). The two are unrelated: "comm Task" = a
  background function; "stapel-tasks" = boards/cards/kanban. Renaming a label
  is part of the public app contract, hence a **minor** bump.

- **The physical table name is unchanged.** `TaskRecord` now pins
  `Meta.db_table = "stapel_tasks_taskrecord"` (its historical auto-derived
  name). This makes the rename **label-only**: no `ALTER TABLE`, no data
  movement, lowest risk for existing deployments. Table names are internal
  (not a contract); the label is what collided. `makemigrations --check` is
  clean — no new migration is generated.

- **Migration note for existing projects.** Django keys applied migrations and
  content types by app *label*. After upgrading, relabel the history so Django
  recognizes the app as already migrated (nothing physical changes):

  ```sql
  UPDATE django_migrations   SET app       = 'stapel_taskstore' WHERE app       = 'stapel_tasks';
  UPDATE django_content_type SET app_label = 'stapel_taskstore' WHERE app_label = 'stapel_tasks';  -- if contenttypes is installed
  ```

  Alternative (no SQL): `python manage.py migrate stapel_taskstore --fake`
  (leaves harmless stale `stapel_tasks` rows in `django_migrations`).
  Projects that key `MIGRATION_MODULES`/`DATABASE_ROUTERS` by the old label
  must update the key `stapel_tasks` → `stapel_taskstore`. Fresh installs need
  nothing — they create `stapel_tasks_taskrecord` under the new label directly.

## [0.7.0] - 2026-07-06

### Added — `stapel_core.gateway`: privilege gateway mechanism (Studio SN-4)

- **The security primitive behind "capability, not credentials"**
  (system-design §5.9; studio-design §2.3): untrusted code in a project
  container calls declared **verbs** through one known endpoint; every
  key/password/script stays behind the gateway in the control plane (S1).
  This module is the OSS mechanism only — concrete verbs and policies are
  the deployment's (Studio's) business.
- **Verb declaration** — name + mandatory JSON schema for arguments +
  policy `{tiers, rate_limit, require_confirmation, audit_stream}` +
  handler (dotted path or callable): `register_verb()` / `@gateway.verb`
  in `AppConfig.ready()`. **Merge-registry** with
  `STAPEL_GATEWAY["VERBS"]`: settings entries patch a code-declared verb
  per key (policy merges per field), declare settings-only verbs, or
  disable a verb with `None`. **Deny-by-default**: an undeclared verb
  does not exist (404, no capability enumeration).
- **Scope tokens** (`issue_token` / `verify_token` / `rotate_token` /
  `revoke_token` / `purge_expired_tokens`) — project-scoped, short-lived
  (`TOKEN_TTL`, 1h). Contract decision: **opaque, stored as sha256 only**
  (per the flow-mcp trade-off — tokens are few, verification is one
  indexed lookup, and instant revocation beats saving it; a signed token
  needs a revocation table anyway). `sgw_` prefix for secret scanners;
  optional bindings to a `container` and a `network` (exact IP or CIDR).
  Rotation keeps bindings, kills the old token (optional grace window).
- **Network identity check** — three-factor authorization on the HTTP
  door (project id = addressing, token = right to speak, network = the
  physical caller): `STAPEL_GATEWAY["NETWORK_VERIFIER"]` seam;
  the default enforces the token's bound IP/CIDR from `REMOTE_ADDR`
  (never a forwarded header — proxy trust belongs in a custom verifier);
  `REQUIRE_NETWORK_BINDING` makes unbound tokens unusable over HTTP.
- **Two call surfaces** — HTTP for containers
  (`gateway.get_gateway_urls()` → `POST api/_gateway/<verb>/`,
  `Authorization: Bearer sgw_…`, statuses 200/202/400/401/403/404/429/
  502/500) and comm Functions for control-plane callers
  (`gateway.invoke`, `gateway.confirm` — registered by the
  `stapel_core.django.gateway` app, which is opt-in, not in
  `COMMON_INSTALLED_APPS`: a privilege surface is mounted deliberately).
- **Audit without holes (S6)** — exactly one line per invocation outcome
  (executed ok/failed, denied by any check incl. token/network/config
  errors, parked pending, confirmed, rejected, expired) with who/what/
  when/channel/ip/token/args (fingerprinted over `AUDIT_ARGS_MAXLEN`).
  Sink is a dotted-path seam (`AUDIT_SINK`), default appends to
  `stapel_core.eventstore` stream `audit` (per-verb `policy.audit_stream`
  override). Sink failure is fail-closed and fail-noisy (`AuditFailure`).
- **Policy engine** — `STAPEL_GATEWAY["POLICY_ENGINE"]` seam; the default
  checks tiers (unresolvable tier on a restricted verb **denies**;
  `TIER_RESOLVER` seam) and rate limits (`"30/m"`-style, fixed window,
  counted per `(verb, project)`; `RATE_LIMITER` seam, cache-backed
  default; malformed limit = config error, never "unlimited").
- **Two-phase confirmation** — `require_confirmation` parks the validated
  call as a `PendingAction` row (TTL `CONFIRMATION_TTL`, 15 min) and
  returns `202 {confirmation_id}`; execution takes `gateway.confirm(id,
  approved_by=…)` — comm/Python only, deliberately absent from the
  container surface (a hijacked agent must not confirm its own
  destructive action). The confirmed leg re-runs schema + policy, is
  claimed atomically (no double-execute), and stamps `confirmed_by` into
  context and audit.
- Optional extra `stapel-core[gateway]` (jsonschema) — verb-args
  validation is mandatory and fails **closed** when the validator is
  unavailable (S5).
- Root export `stapel_core.gateway` (lazy). 86 new tests (1221 total).

## [0.6.0] - 2026-07-06

### Added — `stapel_core.eventstore`: append-only stream primitive (Studio SN-3′)

- **One seam for the many high-volume append streams** (LLM-call ledger,
  gateway audit, analytics, delivery logs) — written often, read as
  aggregates, grow without bound, out of band with business transactions
  (docs/data-storage-and-observability.md §1; studio-design §3, three storage
  contours). Modules write through the facade, never a backend.
- **`EventStore` ABC + backend seam** — `STAPEL_EVENTSTORE["BACKEND"]`
  (dotted path); default `PostgresEventStore`. Per-stream override via
  `STAPEL_EVENTSTORE["ROUTES"]` (merge-routing by stream name, like
  bus-routing). ClickHouse is the documented scale-out evolution point — the
  ABC already permits it; not implemented here.
- **Facade API** — `append(stream, payload, *, ts, project, task, container)`,
  `append_batch`, `query(stream, *, after, limit, time_range, filters)` →
  `EventPage` (cursor read, `(ts, id)` tie-break so bursts never skip/repeat a
  row), `rollup(stream, *, group_by, sum_fields, into=…)` → `RollupRow`s,
  `purge(stream, *, older_than)`, `flush()`.
- **Append-only rows** — `{stream (indexed), ts (indexed), payload jsonb,
  project/task/container (generic, nullable, indexed)}`. Identity columns are
  promoted out of the payload for cheap slicing; the framework does not ascribe
  meaning to them.
- **Write buffer** — batch-flush by size or interval (`BUFFER_SIZE` /
  `BUFFER_INTERVAL`); flush runs the DB I/O outside the lock. `BUFFER_SYNC`
  write-through fallback for tests/low-volume; reads flush first
  (read-your-writes). `atexit` flush so buffered events are not lost.
- **Generic rollup helper** — group-by (identity columns or payload keys) +
  sum-fields, aggregated in Python so it is identical on every engine
  (bool/non-numeric values skipped from sums). Optional `into=` upserts the
  buckets into a rollup table with replace (recompute) semantics; concrete
  rollups are the consumer's business.
- **Per-stream retention** — `STAPEL_EVENTSTORE["RETENTION"]` /
  `["RETENTION_ROLLUP"]` (raw ≠ rollup), applied by
  `manage.py sweep_eventstore` (cron/beat).
- **PostgreSQL time-partitioning** — `django/eventstore/partitions.py` SQL
  generators (`parent_ddl` range-partitioned parent, `ensure_partitions_sql`,
  `create/drop_partition_sql`) driven by `manage.py eventstore_partition`
  (idempotent, `--dry-run`). **SQLite minimal profile degrades to one plain
  table, no partitions** (documented); the partition command reports skipped
  rather than erroring.
- App `stapel_core.django.eventstore` added to `COMMON_INSTALLED_APPS`
  (models `EventRecord`/`EventRollup`, migration `0001_initial`).

Tests: append/cursor paging + tie-break, identity/payload filters, half-open
time ranges, buffer (size/interval/sync/flush), rollup (group/sum/into/replace),
retention purge + sweep command, stream routing, cursor token round-trip,
partition SQL generation (structural — Postgres not available locally) and
SQLite plain-table degradation. Base 1101 → 1133.

## [0.5.1] - 2026-07-05

### Fixed — RevisionMixin: phantom revision on `save(update_fields=...)` + duplicate issuance under concurrency (review H-3)

- **Phantom revision.** `save(update_fields=["draft"])` used to bump the
  in-memory `revision` (and every post_save receiver / emitted event carried
  it) while the DB kept the old number — the next content change reused the
  phantom number and a sync client that had stored it from the event skipped
  that change forever. New contract: `update_fields` **without** `"revision"`
  means a scoped non-synced write — **no bump**; DB row, instance and
  post_save events stay consistent on the current revision. Passing
  `update_fields=[..., "revision"]` is the explicit opt-in to bump-and-persist
  (that path already worked and is unchanged). Plain `save()` is unchanged.
- **Duplicate issuance.** The docstring promised `select_for_update`, but no
  lock existed: two concurrent saves read the same `MAX(revision)` and shared
  a number, so `get_changes_since` lost one of them. Issuance is now
  serialized: PostgreSQL — `pg_advisory_xact_lock` keyed on the table, held
  to COMMIT (unique **and** commit-ordered numbers across processes); other
  backends (SQLite minimal profile, where `SELECT ... FOR UPDATE` is
  unavailable) — a process-local mutex per (db alias, table) around
  issue+commit. Documented caveat: outside PostgreSQL the mutex releases
  before an *outer* `transaction.atomic` commits — multi-threaded writers
  with long outer transactions should use PostgreSQL (or SQLite
  `"transaction_mode": "IMMEDIATE"`).
- `save()` now respects `using=` / the DB router when issuing revisions
  (`transaction.atomic(using=...)` + `.using(...)` aggregate).

Tests: update_fields persist/event consistency, H-3 sync-loss repro, one-shot
iterable `update_fields`, nested-atomic regression, threaded uniqueness
(8 threads × 5 saves — doubles as the sqlite-compatibility check).

## [0.5.0] - 2026-07-05

### Added — flow SA-document renderer: mermaid + endpoint tables + bilingual trees (flow-system.md §4)

`generate_flow_docs` now renders a **pretty SA-document** through the new
`STAPEL_FLOWS["FLOW_DOC_RENDERER"]` seam (dotted path; default
`DefaultFlowDocRenderer`). Per flow: a GitHub-native `mermaid` step diagram
(human = stadium node, HTTP = rectangle, action/function/task = subroutine;
sequential edges), the numbered steps, and an **Endpoints** table carrying
request/response serializers and the step-up **verification contract**
(`scope` + factors). A module swaps the whole look by pointing the seam at
its own class — no fork.

- **Renderer chrome is localized** (`## Steps` / `## Шаги`, `Actors` /
  `Актор(ы)`, table columns, `User action`) via a `language` argument, while
  the *content* still resolves from i18n keys. Unknown languages fall back to
  English chrome. This closes the piece deferred from 0.4.0 (§2 left the
  chrome hardcoded). `render_flow_markdown` / `render_index_markdown` gained
  an optional `language` parameter (default English — literal-only callers
  are unaffected in wording except the scaffolding is now English, matching
  `DOC_SOURCE_LANGUAGE`).
- **`generate_project_docs`** — new management command: one **byte-stable
  doc tree per `STAPEL_FLOWS["DOC_LANGUAGES"]`** language (`["en", "ru"]` by
  default) from the single language-agnostic `flows.json`. Layout
  `docs/flows/{flows.json, README.md, en/…, ru/…}`; the root README links
  every language tree. Deterministic output makes the release-gate drift
  check (`generate_project_docs` + `git diff --exit-code`) meaningful —
  regeneration without source changes = zero diff.
- New settings: `FLOW_DOC_RENDERER`, `DOC_LANGUAGES`. New public API:
  `DefaultFlowDocRenderer`, `get_flow_doc_renderer`, `render_flow_markdown` /
  `render_index_markdown` (now re-exported from `stapel_core.flows`).

Additive: existing `generate_flow_docs`, `flows.json` schema and literal-only
flows are unchanged.

### Fixed — deterministic endpoint enumeration (docs + check_flows)

`iter_api_endpoints` now skips the framework-auto `HEAD`/`OPTIONS` verbs on
DRF ViewSets. DRF binds an auto `HEAD` (mirroring `GET`) into the view's
`actions` mapping at *request* time, so whether an endpoint had been hit at
runtime leaked into the rendered docs and the endpoint-coverage check — a
byte-stable render (and the release-gate drift check) cannot depend on that.
HEAD/OPTIONS are never business steps.

## [0.4.1] - 2026-07-05

### Fixed — netintel circuit-breaker concurrency + log hygiene (defensive)

- `netintel._breaker_*` now take `_provider_lock` for all breaker state access.
  The failure counter is a shared read-modify-write (`state[0] += 1`); under a
  concurrent fail-open flood on N threads the unlocked increment dropped counts,
  so the breaker opened *later* than its threshold. The module comment claiming
  the state was "guarded by `_provider_lock`" is now true. Behaviour change is
  strictly a faster, exact trip on the Nth real failure; no API change.
- `_reset_state` now clears `_warned_providers` too. Previously the
  once-per-provider fail-open warning stayed suppressed for the whole process
  even across `setting_changed` / `override_settings` reconfiguration — a config
  change now re-warns. Logging only.

## [0.4.0] - 2026-07-05

### Added — flow i18n: keys instead of literals (flow-system.md §2, first-instance)

Flow texts are now i18n keys; the in-code literal stays the canonical
English source text and the render fallback, so **existing literal-only
flows keep working unchanged** (the keys are derived implicitly).

- `Flow` carries `title_key`/`description_key` (implicit
  `flow.<id>.title` / `flow.<id>.description`; explicit kwargs override);
  every step carries `note_key` (implicit `flow.<id>.step.<order>.note`;
  `note_key=` kwarg on `@flow_step` and `Flow.action/.function/.task/
  .human` overrides). `_stapel_flows` memberships include `note_key`.
- `flows.json` (`export_json`) now includes `title_key`,
  `description_key` and per-step `note_key` alongside the literals —
  the artifact is language-agnostic: keys + structure + API bindings are
  one contract, language lives on the presentation layer. Additive for
  existing consumers.
- New `stapel_core.flows.i18n` — the resolution engine
  (`resolve_flow_texts(flows, language, ...)`), chain:
  1. committed per-app catalogs `<app>/translations/flows.<lang>.json`
     (merge over INSTALLED_APPS, later apps win);
  2. `translate.resolve` comm Function (best-effort, host DB values, only
     keys the catalogs don't cover);
  3. `STAPEL_FLOWS["DOC_TRANSLATOR"]` dotted-path seam (opt-in `llm=True`)
     — default `CommDocTranslator` calls `llm.translate` by comm name
     (core stays L0-clean); guarded by a content-hash cache
     (`DocTranslationCache`, committed file): regeneration without source
     changes = zero LLM calls and zero diff (byte-stable, like
     `dump_translations`);
  4. the source literal — rendering never breaks.
  `STAPEL_FLOWS["DOC_SOURCE_LANGUAGE"]` (default `"en"`) declares the
  literal language. Public exports: `resolve_flow_texts`,
  `flow_source_texts`, `load_app_catalogs`.
- `generate_flow_docs` gained `--lang X`, `--llm`, `--llm-cache FILE`:
  markdown is rendered with resolved texts; `flows.json` stays
  language-agnostic. (`render_flow_markdown` / `render_index_markdown`
  accept an optional `texts` mapping.)
- `check_flows`: new error when several steps of one flow share an i18n
  note key (colliding implicit keys — same `order` twice — would silently
  share one catalog entry).

Reference migration: the three stapel-auth flows (en literals + en/ru
catalogs) — the pattern every module copies. Full bilingual doc trees,
README links and the release gate are flow-system.md §4 (next step).

## [0.3.3] - 2026-07-05

### Added — outbox atomicity as a seam (docs/module-extension-gaps.md §"Системный паттерн")

Two module repos independently broke the outbox guarantee ("the event
leaves iff the surrounding transaction commits") the same two ways
(categories C1: swallowed emit failure; listings L2: save and emit in
separate transactions). This release turns the discipline into mechanism:

- `stapel_core.comm.mutate_and_emit(using=None, savepoint=True)` — context
  manager for the canonical mutation+emit pattern: everything in the block
  (ORM writes and outbox rows) commits or rolls back as one unit. Yields an
  emit callable with the exact `emit()` signature (0..N calls; refuses to
  run after the block exits); plain `emit()` / `emit_*` helpers inside the
  block get the same protection, so `with mutate_and_emit():` without `as`
  is a valid form. Root lazy export `stapel_core.mutate_and_emit`.
- Runtime guards in `emit()` (outbox mode):
  - emit *outside* `transaction.atomic()` now warns by default (the outbox
    row would commit detached from the mutation; also fires for emit inside
    `on_commit` callbacks). New `STAPEL_COMM["EMIT_OUTSIDE_ATOMIC"]`:
    `"warn"` (default) | `"error"` (raises new `EmitOutsideAtomicError`) |
    `"allow"`. Set `"error"` in module test settings to make it a gate.
  - a failed emit inside an atomic block marks the transaction
    rollback-only before propagating — even a caller that swallows the
    exception (the C1 anti-pattern) cannot commit the mutation without its
    event.
- `stapel_core.lint.emit_check` — AST-based CI gate
  (`python -m stapel_core.lint.emit_check [paths]`, also runnable as a
  standalone file): EMIT001 emit in `except` handler, EMIT002 emit
  swallowed by broad except (C1), EMIT003 mutation+emit in one function
  without a shared atomic construct (L2), EMIT004 emit in an `on_commit`
  lambda. Suppression: `# emit-check: ok — <reason>`. Purely lexical by
  design — see the module docstring for limitations; the runtime guards
  cover what the static pass cannot. Wired into this repo's pre-commit /
  pre-push hooks and CI.

### Fixed

- The emit-check gate flagged five instances of the L2 bug class in core
  itself; all now go through `mutate_and_emit()`:
  - `comm.tasks.start()` — task record and `task.requested` event were in
    separate transactions when the caller held no atomic block (a crash
    between them left a PENDING task that was never announced);
  - `comm.tasks.execute()` — DONE state + `task.completed` event;
  - `comm.tasks` retry path (new `_requeue()` helper) — PENDING reset +
    re-announce, previously emitted inside the except handler;
  - `comm.tasks._park()` — FAILED state + `task.failed` event (signature
    change: internal helper, no longer takes `emit`);
  - `manage.py sweep_tasks` — per-record FAILED state + `task.failed`.

## [0.3.2] - 2026-07-05

### Added
- `stapel_core.netintel` — IP intelligence seam (docs/geo-network-trust.md
  §0): `classify_ip(ip) -> IpProfile{kind, asn, asn_org, country,
  confidence}`, `country_of(ip)`, `client_ip(request)`. Provider is a
  dotted-path replace seam (`STAPEL_NETINTEL["PROVIDER"]`, default
  `NullProvider` — always `unknown`); built-ins: `MaxMindProvider` (offline
  GeoLite2/GeoIP2 mmdb, new optional extra `stapel-core[netintel-maxmind]`)
  and `HttpJsonProvider` (generic ipinfo/IPQS-style HTTP lookup with a
  response-mapper seam). Results cached in the Django cache
  (`CACHE_ALIAS`/`CACHE_TTL`, key prefix `stapel-netintel:`); fail-open —
  provider errors log once per provider class and return `unknown`, never
  5xx. W-level system checks on the provider path. Root lazy exports:
  `classify_ip`, `country_of`, `IpProfile`.
- Tiered captcha challenge policy (docs/geo-network-trust.md §2):
  `stapel_core.captcha.policy` with ordered levels `none < invisible <
  interactive < interactive+ratelimit < block`, `ChallengePolicy` ABC and
  the default `MatrixChallengePolicy` (netintel ip-kind →
  `STAPEL_CAPTCHA["CHALLENGE_MATRIX"]` merged over builtin defaults →
  `ACTION_OVERRIDES` `{action: {kind: level} | "+1"}`). Policy swappable via
  `STAPEL_CAPTCHA["CHALLENGE_POLICY"]` (dotted path).
- `@captcha_protected(action=...)` view decorator (`django/captcha.py`):
  `none` passes, `block` → 403 with new registered key
  `error.403.network_blocked`, other levels verify the captcha token; the
  challenge level is passed to backends that opt into an optional `level`
  keyword on `verify()` (legacy backends unchanged). Sets
  `request.stapel_challenge_level` for rate-limit middleware (captcha does
  not rate-limit) and logs every decision at INFO
  (`ip_kind, action, level, allowed`).
- `STAPEL_CAPTCHA` settings namespace (`captcha/conf.py`) with legacy
  fallback: flat `CAPTCHA_BACKEND` / `CAPTCHA_SECRET` keep working;
  `error.400.captcha_invalid` / `error.400.captcha_required` are now
  registered error keys.

### Compatibility
- No behavior change without configuration: with the default `NullProvider`
  every request classifies as `unknown` → challenge level `invisible`, which
  reproduces the historical binary captcha exactly (pass when no secret is
  configured, verify the token when a backend is configured). `CaptchaMixin`
  and existing `CaptchaVerifier` subclasses are untouched.

## 0.3.1 — 2026-07-04
### Added
- `notifications/schemas/emits/notification.requested.json` — the
  `request_notification` payload is now a declared contract, including the
  optional `content_html` / `content_text` raw-content escape hatch.
  Validation is split across the seam (documented in the schema): core
  validates payload shape at the edge, the notifications module validates
  type-registry membership in its consumer and `check_notifications` lint.
- `request_notification(..., content_html=, content_text=)` — raw body
  threaded through the event payload for ad-hoc notifications without a
  registered type/template. The function now raises `ValueError` early on
  a malformed request (empty `notification_type`, non-string content).

### Deprecated
- `kafka.topics.TOPIC_TRANSLATIONS_CHANGED` and
  `kafka.events.EventType.TRANSLATIONS_CHANGED` — the
  translate→notifications sync moved to the comm Action
  `translations.changed` (thin invalidation) + the `translate.resolve`
  Function (pull). No stapel module uses the legacy Kafka contract anymore;
  the constants stay for deployments that pin it.


## 0.3.0 — 2026-07-03

### Added
- `stapel_core.verification` — step-up verification framework:
  `@requires_verification(scope, factors, max_age)`, structured 403
  challenge envelope, server-side grants per user+scope (or stateless
  X-Verification-Token), `STAPEL_VERIFICATION` policy overrides, factor
  registry, OpenAPI annotation.
- `bus/backends/routing.py` — per-topic transport routing behind the
  BusBackend facade (e.g. some topics to NATS, others to Kafka/memory).

### Changed
- comm task dispatch and action/config refinements.


## 0.2.2 — 2026-07-02

### Fixed
- Flows/verification OpenAPI postprocessing hook resolves ViewSet action
  handlers (`x-stapel-flows` / `x-stapel-verification` now annotate
  @action endpoints, not only plain http-verb handlers).

All notable changes to stapel-core. Versioning: semver; 0.x may break
minor-to-minor, breaking changes are always listed here.

## [0.2.1] - 2026-07-02

### Fixed
- Declare `django-cors-headers` as a dependency — COMMON_INSTALLED_APPS /
  COMMON_MIDDLEWARE require it, but pip installs of the wheel did not pull
  it in (worked only in vendored checkouts whose requirements listed it
  explicitly).

## [0.2.0] - 2026-07-02

### Added
- comm layer: Action (`emit`/`on_action`) with transactional outbox,
  Function (`call`/`@function`) with in-process / NATS request-reply /
  HTTP / dotted-path transports, Task (`start`/`status`/`@task_handler`)
  for async named operations with persistent state, retries, deadlines
  and completion events.
- NATS JetStream bus backend (`STAPEL_BUS_BACKEND=nats`) with DLQ,
  publish dedup (`Nats-Msg-Id`) and durable pull consumers.
- `stapel_core.conf.AppSettings` — per-package settings namespaces with
  dotted-path import strings.
- `stapel_core.signals` — business-milestone Django signals.
- `AbstractStapelUser` — subclass to customize the user model without
  forking; feature modules reference `settings.AUTH_USER_MODEL`.
- Schema autoload: JSON Schemas from app `schemas/` dirs are registered
  with the comm registries (enforced when `VALIDATE_SCHEMAS` is on).
- `manage.py serve_functions`, `dispatch_outbox`, `sweep_tasks`.

### Changed
- `STAPEL_HOST` replaces `IRON_HOST` (legacy env still honored).
- Auth-service prefix is configurable: `STAPEL_AUTH_SERVICE_PREFIX`
  (admin login redirect, JWKS discovery).
- `STAPEL_SERVICES` admin catalog is overridable via Django setting.
- CSRF: `/api/` requests are exempt only for header-token/service-key
  clients; JWT-cookie browser sessions require the CSRF token or
  `X-Requested-With: XMLHttpRequest`.
- Token blacklist fails CLOSED when the cache is unavailable
  (`STAPEL_BLACKLIST_FAIL_OPEN=True` restores the old behavior).
- Kafka consumer: poison messages go to the DLQ instead of wedging the
  partition; offsets commit only after handler/DLQ success.
- HS256 JWT refuses to start outside DEBUG with a missing/default secret.
- Django floor raised to 5.1.

### Fixed
- Timing-safe service API key comparison.
- `OAuthUserData.email_verified` for safe merge-by-email.
