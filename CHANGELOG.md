# Changelog

## [Unreleased]

### Added — admin visibility by access category (admin-suite AS-3)

Builds on AS-1 (`stapel_core.access`): the `@access` category of a model now
drives what the Django admin shows. Enforcement is the **backend, not app-list
filtering** — `MandateBackend.has_perm` answers every admin permission check,
so a direct `/admin/app/model/` URL is closed exactly like the index entry
(§1.3). How each category lands:

| category | backend (AS-1, unchanged) | admin layer (new) |
|---|---|---|
| `business` | view/add/change/delete per declared levels | nothing — plain ModelAdmin behavior |
| `ops` | `view=HIGH` → invisible below clearance HIGH; mutations FORBIDDEN | read-only **even for superusers** (A5 bypasses the mandate, so the journal contract is re-imposed here); `SHOW_OPS_MODELS` reveals to any staff, still read-only |
| `secret` | every operation SUPERUSER-only | secret fields masked: excluded from forms, rendered as a placeholder — plaintext never reaches the response, even for the superuser |

- **`StapelModelAdmin`** (`stapel_core.django.admin`) — declaration-aware base
  ModelAdmin: ops read-only, secret-field masking (name-pattern autodetection
  on `secret` models, or an explicit `secret_fields` tuple that masks on any
  category), masked fields stripped from forms/`list_display`/`search_fields`
  (icontains probing is an oracle). A bare `admin.ModelAdmin` keeps working —
  the backend still enforces visibility, only the cosmetics are lost.
- **`STAPEL_ADMIN` conf namespace** — merge-registry `MODELS` (dict patches,
  `None` unregisters the admin entirely — direct URL 404; `admin_class` swaps
  the registered admin) and `SHOW_OPS_MODELS` (env-readable dev toggle). The
  access-shaped keys of a `MODELS` entry feed `effective_access` — **one
  resolution** with `STAPEL_ACCESS["MODELS"]` (§3.7), so
  `{"category": "business"}` on an ops journal is real visibility through the
  backend, not cosmetics. A `category` key re-bases the declaration on that
  category's preset (that is what "show to every staff" means), remaining
  level keys patch on top.
- **Ops admins for the core tables, out of the box** (§1.3 — outbox debugging
  no longer needs dbshell): `OutboxEvent`, `TaskRecord`, `EventRecord`,
  `EventRollup`, `PendingAction` are declared `@access.ops` and registered
  read-only; `ScopeToken` is `@access.secret` with `token_hash` masked.
  Attribute-only declarations — **no migrations**.
- **Q9 — django.contrib service tables are ops by convention:**
  `CONTRIB_OPS_LABELS` (auth.Group, auth.Permission, sessions.Session,
  contenttypes.ContentType, admin.LogEntry) default to the ops category while
  undecorated. `auth.Group` is re-registered under a declaration-aware admin
  (groups are the DAC surface — read-only in the admin now; the classic
  editable Group is one override away:
  `STAPEL_ADMIN = {"MODELS": {"auth.Group": {"category": "business"}}}`),
  `sessions.Session` gets a masked read-only admin. Both hidden by default,
  revealed read-only by `SHOW_OPS_MODELS`.
- **Registration hooks** run from `CommonDjangoConfig.ready()` (list
  `stapel_core.django` after `django.contrib.admin`); exotic layouts can call
  `stapel_core.django.admin.registration.setup_admin_visibility()` directly.
- **System checks** (tag `stapel_admin`): E-level for a malformed
  `STAPEL_ADMIN["MODELS"]` registry or unimportable `admin_class`, W-level for
  cross-service labels and for a settings overlay downgrading a declared
  `secret` model (§1.4 — honored, never silent).

### Changed / Security — staff shadow-sync is now REPLACE, not upgrade-only (admin-suite AS-2)

Consumer half of the staff-role transport (producer lives in stapel-auth
[Unreleased], AS-2 — wording aligned). Auth is the single source of truth for
staff status; the sync-down in `get_or_create_user_from_jwt` switches from
**upgrade-only** to **REPLACE from the claim** (в.3).

- **`staff_roles` field on `AbstractStapelUser`** (`JSONField(default=list)`,
  migration `users/0006`): the shadow copy of the `staff_roles` JWT claim.
  Auth is the single writer (A2); consumers only mirror it.
- **`serialize_user_to_jwt_data` emits the `staff_roles` claim** on
  staff/superuser tokens only, sorted for a stable ordering. Present-but-empty
  is authoritative "zero roles"; absence means the model has no field (pre-AS-2)
  and consumers must not touch local state.
- **`get_or_create_user_from_jwt` sync-down REPLACE (в.3, breaking on the
  consumer side):** `is_staff` / `is_superuser` are now REPLACED from the token
  (a cleared flag DOWNGRADES a local staff/superuser — revocation finally
  lands, A3). `staff_roles` is REPLACED **only when the claim is present**;
  absence = no information (never grant, never revoke from silence). The old
  "upgrade-only" rule is gone. Migration path for services relying on *locally
  assigned* staff flags on shadow users: recreate those staffs in the auth
  service before upgrading; after the upgrade a fresh-token login overwrites
  local `is_staff`/`is_superuser` with the auth-side values. Old tokens without
  the claim change nothing, so mixed fleets degrade safely during rollout.
- **Security — re-elevation hole closed.** On the auth service / monolith
  (`JWT_CREATE_USERS_FROM_TOKEN=False`) a token now writes **no** staff
  attributes into the canonical store at all. The pre-AS-2 upgrade-only rule
  wrote into that store, so a replayed stale staff token could re-elevate a
  demoted admin; that is gone.
- **Bridge to AS-1 (`stapel_core.access`):** the validated claim is stamped
  onto the request user as the transient `CLAIM_ATTR`, so `MandateBackend`'s
  `claim_roles` source reads the fresh token, not a stored field.
- **Resurrection window closed at refresh:** the JWT middleware's proactive and
  fallback refresh now re-mint via `load_user_by_uid` (fresh DB) instead of the
  refresh token's own up-to-7-day-stale claims, so a revoked role/flag cannot
  resurrect on refresh under REPLACE.

### Added — `stapel_core.i18n`: bilingual content shipping (i18n-shipping wave 0)

- **`stapel_core.i18n`** — the flow-i18n contour generalized to arbitrary
  content **domains** (i18n-shipping.md §1). A domain `D` (`"flows"`,
  `"errors"`, …) ships per-app catalogs `<app>/translations/D.<lang>.json`
  (flat `{key: text}`), discovered over INSTALLED_APPS and merged
  **later-wins** — a host app (last) overrides any module text **without a
  fork**, the same merge-over-builtins semantics as every other registry.
  `load_app_catalogs(domain, language)`, `CommDocTranslator` and
  `DocTranslationCache` moved here; `flows.i18n` is now the `"flows"` domain
  over it and re-exports them (backward compatible).

- **`register_service_errors` override contract pinned** — the global error
  registry is `dict.update`, so a later (host) registration overriding an
  earlier en text is the *en tier of the fork-free override seam* (§3), not an
  accident. Fixed by `tests/test_error_i18n_contract.py` so it is never
  "hardened" into a duplicate check. `docs/errors.json` stays en-only /
  language-agnostic; localized error texts live in
  `translations/errors.<lang>.json`.

- **`STAPEL_I18N`** (`i18n/conf.py`) — a thin cross-domain namespace:
  `LOCALES` (default `["en","ru"]`), the single "project languages" knob that
  `STAPEL_FLOWS["DOC_LANGUAGES"]` now delegates to (`project_languages()`,
  soft — an explicit `DOC_LANGUAGES` still wins); `EXTRA_CATALOG_DIRS` (catalog
  roots outside the apps); `TRANSLATOR` / `SOURCE_LANGUAGE` (the
  domain-agnostic machine-translation seam, the `llm.translate` comm Function
  by name — core never imports the agent package).

- **`translate_catalogs --domain D --lang X`** — write-time catalog generation
  with a `.state.json` **provenance sidecar** (`{key: {hash: h(source_en),
  origin}}`). Per key: keep (source hash unchanged) → seed from a curated
  corpus (`--seed`, `origin: seed:<label>`) → the translator seam (`--llm`,
  content-hash cached, byte-stable, `origin: llm` = machine/unreviewed) → left
  missing (fails the gate). `--approve KEY… | --approve-all` flips reviewed
  keys to `origin: human` without retranslating. Editing the en canon
  auto-staleness-marks exactly the affected key.

- **`check_translation_catalogs --domain D [--strict]`** — CI gate (module
  pytest wraps `check_translation_catalogs(...)` like `check_flows`): **E** on
  a missing key, a stale one (en changed, translation didn't), a `{param}`
  mismatch vs the canon (a client override MUST preserve the placeholders), or
  a non-byte-stable file; **W** counts unreviewed (`origin: llm`/unknown)
  values (`--strict` makes them fatal, for after the first review pass).

- **`generate_error_docs [--lang X]`** — the human-readable
  `docs/errors.<lang>.md` reference (i18n-shipping.md §4), a byte-stable table
  joining the error registry with the language catalog (uncovered keys marked
  `_(en)_`). Gate it with the same regenerate-and-diff pattern as the flow
  docs.

### Added — `stapel_core.secrets`: secret-provider seam (arch-stapel-vault Part 1)

- **`stapel_core.secrets`** — secret resolution as a core *mechanism*, not a
  backend. `get_secret(name, default=…)` resolves a secret through a
  dotted-path provider seam `STAPEL_SECRETS["PROVIDER"]` (like `AUDIT_SINK` /
  `ROLE_SOURCES`). Provider duck type: `get(name) -> str | None`. The default
  is `EnvSecretProvider` (`os.environ`) — local dev, the `minimal` preset and
  every unconfigured project behave exactly as before, with **zero new
  dependencies**. Pointing `PROVIDER` at `stapel-vault`'s
  `VaultSecretProvider` (separate OSS module) is what moves production secret
  storage off the environment into OpenBao / HashiCorp Vault. Decision
  2026-07-06: env for prod secrets is unacceptable — this is the seam that
  closes it.

- **Per-process cache with TTL** — a resolved value is memoized for
  `STAPEL_SECRETS["CACHE_TTL"]` seconds (default 300) so the hot path never
  re-hits a remote store per request. The TTL doubles as the rotation re-read
  window; `invalidate_secret(name=None)` forces an eager re-read after a
  rotation (stapel-vault's rotation hook). Positive-only cache — a miss is
  never cached, so a just-added secret is visible immediately. `CACHE_TTL=0`
  disables caching.

- **Fail-closed** — a provider returning `None` with no caller `default`
  raises `SecretUnavailable` (a missing production secret is a loud boot
  failure, never a silent `None`). The env provider is the deliberate
  exception (`fail_closed = False`): missing env var + no default → `None`,
  preserving the `os.environ.get` semantics existing settings modules rely on.

- **Bootstrap-tolerant** — production settings modules resolve `SECRET_KEY`
  before `django.setup()`, so provider selection cannot depend on
  `django.conf.settings`. When settings are unreadable, the provider is taken
  from the explicit `STAPEL_SECRETS_PROVIDER` env var (the generic `PROVIDER`
  key stays `no_env`). `stapel_core.django.settings` now resolves `SECRET_KEY`
  and `JWT_SECRET_KEY` through `get_secret(...)` with their existing defaults —
  transparent under the env provider, Vault-backed when configured, no config
  change required.

- **prodguard compatibility** — the SEC-4 guards operate on the *resolved*
  value: `guard_secret("SECRET_KEY", get_secret("SECRET_KEY"))` catches a
  placeholder/short/empty secret identically whether it came from env or
  Vault; the guard needs no provider knowledge (documented in
  `django/prodguard.py`).

- **System checks** (tag `stapel_secrets`) — W-level (the env default always
  works): W001 provider not importable, W002 resolved value is not a
  provider. Deliberately does not probe connectivity.

### Added — `stapel_core.access`: staff mandate — computed admin rights (admin-suite AS-1)

- **`stapel_core.access`** — mandatory access control for staff/admin
  (docs/admin-suite.md §3): staff permissions are a *computed function* of
  (model declaration × role clearance), not accumulated `auth_permission`
  rows. Declarations: `@access(view=…, add=…, change=…, delete=…,
  category=…)` with presets `@access.standard` (business; view=LOW,
  add/change=MID, delete=HIGH — also the implicit default of every
  undecorated model), `@access.sensitive`, `@access.ops` (read-only journal,
  view=HIGH, mutations forbidden), `@access.secret` (superuser-only). The
  declaration is a plain class attribute — no `Meta.permissions`, no
  migrations; a decorator change takes effect on deploy (A1, no drift by
  construction). Admin category (business/ops/secret) lives in the same
  declaration, ready for the AS-3 visibility layer.

- **`MandateBackend`** (auth backend): `has_perm("app.change_model")` is
  evaluated at call time — parse codename → effective declaration
  (decorator merged with the `STAPEL_ACCESS["MODELS"]` overlay) → max
  clearance of the user's roles (with per-app scopes) → level comparison.
  Superuser is outside the mandate (A5); non-staff and inactive users are
  never granted; custom (non-CRUD) codenames are left to DAC. Roles resolve
  through the **`ROLE_SOURCES` seam** — an ordered chain `(user) ->
  list[str] | None`, default: JWT-claim attribute (AS-2 transport stamps
  `_stapel_staff_roles_claim`) → local `staff_roles` field → Django groups
  named `role:<name>`; the first non-`None` answer is authoritative, even
  when empty (a revocation synced down must not be resurrected by stale
  groups). With no roles resolvable the mandate disengages — existing
  projects keep today's behavior until the first role is assigned (opt-in).

- **Role registry `STAPEL_ACCESS["ROLES"]`** — merge-registry over builtins
  `viewer`(LOW) / `editor`(MID) / `admin`(HIGH): patch per key, define new
  roles (`clearance` required), `None` disables. App scopes shipped in v1
  (Q7): `{"accountant": {"clearance": "low", "apps": {"stapel_billing":
  "high"}}}` — the scope entry replaces the base clearance inside that
  app_label. Definitions are deploy config, assignments belong to the auth
  service (A2); a runtime-definitions mode is *reserved* behind
  `RUNTIME_ROLE_DEFINITIONS` with a written mini-design (`access/roles.py`
  docstring), not implemented.

- **DAC overlay with audit (A4)** — `AuditedModelBackend`, a drop-in
  `ModelBackend`: manual point-grants keep working; a grant used *above*
  the user's mandate is logged (`stapel_core.access` logger) and emits the
  `dac_escalation` signal — allowed by default, never silent.
  `STAPEL_ACCESS["STRICT"] = True` makes the mandate a ceiling (escalation
  denied for staff; superuser and custom codenames unaffected).

- **`access_report` management command** (`--json`) — the audit surface:
  role × model × operation matrix, every DAC grant above the mandate (incl.
  grants of role-less staff), models without an `@access` declaration.

- **System checks** (tag `stapel_access`): E001/E002 malformed
  ROLES/MODELS policy, E003 STRICT requested but unenforceable (plain
  `ModelBackend` in the chain), W-level hints for a configured-but-not-
  installed backend, unaudited DAC, unknown model labels (legal in shared
  microservice deploy configs), and the reserved runtime-roles flag.

- Out of AS-1 scope, staying on the roadmap: JWT `staff_roles` claim +
  sync-down + `StaffRole` assignments in stapel-auth (AS-2), admin
  visibility / `StapelModelAdmin` / secret-field masking (AS-3), step-up on
  HIGH operations (AS-6). `ensure_staff_group_permissions` (`groups.py`)
  remains the documented non-mandate legacy path.

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

### Added — `stapel_core.django.mounts`: canonical URL mounting (arch-monolith-mounting)

- **`stapel_core.django.mounts`** — the mount registry: where modules live in
  *this* deployment, merge-over-builtins like every other Stapel registry
  (`STAPEL_MOUNTS` setting; builtins: local `admin` with URL namespace
  `admin`, external `auth` at `f"{STAPEL_AUTH_SERVICE_PREFIX}/"` when that
  setting is non-empty). Local mounts resolve with `reverse()` (correct under
  include-prefix mounting *and* `SCRIPT_NAME`/`FORCE_SCRIPT_NAME`); external
  mounts (sibling services behind the same proxy) are script-prefix +
  declared prefix. API: `get_mounts` / `get_mount` / `mount_path` /
  `mount_reverse` / `admin_login_url` / `admin_index_url` +
  `lazy_admin_login_url` / `lazy_admin_index_url` for settings modules.
  Root cause fixed (found live on a sub-path-mounted project): `LOGIN_URL`
  and every cross-module target were hardcoded root-relative, so a project
  mounted whole under a prefix redirected anonymous users to
  `/admin/login/` → `/auth/admin/login/` → 404.

- **`LOGIN_URL` / `LOGOUT_REDIRECT_URL` defaults are now lazily derived**
  from the registry instead of the hardcoded `"/auth/admin/login/"`.
  Backward compatible: with default settings the derivation evaluates to
  exactly the old value; a monolith sets `STAPEL_AUTH_SERVICE_PREFIX = ""`
  and gets `reverse("admin:login")`, which follows any mount prefix. The
  same derivation now feeds `AdminLoginRedirectMiddleware`,
  `JWTCookieLoginView`'s post-login fallback (was hardcoded
  `/auth/admin/`), `setup_centralized_admin_login()` /
  `get_admin_logout_urlpattern()` (now script-prefix aware), and the
  admin/swagger cross-service navigation (`django/admin/context.py`,
  `django/openapi/swagger.py` — URLs built through `get_script_prefix()`).

- **System checks (tag `stapel_mounts`, `django/checks.py`)** —
  `stapel_core.mounts.E001/E002`: `LOGIN_URL` / `LOGOUT_REDIRECT_URL` /
  `LOGIN_REDIRECT_URL` pointing at a path this URLconf cannot `resolve()`
  (and matching no declared external mount) is a **deploy-time error**, not a
  user-facing 404 after redirect; URL-name values are `reverse()`-verified.
  `E003`: malformed `STAPEL_MOUNTS`. `W001`: Django's untouched stock
  defaults (`/accounts/login/`, `/accounts/profile/`) that don't resolve —
  warning only, a pure-API service that never redirects there should not be
  blocked.

- **Module convention pinned (MODULE.md)**: a stapel module never emits an
  absolute URL path — only `reverse()` / URL names / the mount registry.
  URL-target settings should be URL names (`LOGIN_REDIRECT_URL =
  "admin:index"`) or lazy derivations.

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
