# Changelog

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
