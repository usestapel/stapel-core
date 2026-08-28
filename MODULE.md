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
  `status` / `@task_handler`, long-running work with persistent state),
  **Signal** (`signal`, at-most-once ephemeral delivery to a live observer's
  screen — silent no-op without a delivery transport) and **Projection**
  (`Projection` subclass, event-carried read-models over Action with
  generated idempotency and first-class rebuild).
  Modules never import each other — both sides know only a string name and a
  payload schema; transports are deployment configuration, not code.
- `bus` — transport-agnostic message bus (`publish`, `get_bus`, `Event`,
  `BusBackend`, `BaseBusConsumerCommand`) with in-memory, Kafka, NATS
  JetStream and per-topic routing backends.
- `conf.AppSettings` — per-app settings namespaces (the DRF `api_settings`
  pattern, generalized) with dotted-path `import_strings` seams.
- `verification` — step-up verification on any endpoint
  (`@requires_verification`), pluggable factor registry, per-user policy
  resolved via the `auth.verification.policy` comm Function, and a **fleet-wide**
  challenge/grant store (`core/fleet_cache.py`) so a factor completed in one
  service counts in the peer that demanded it. Every record it writes has a
  public verb that removes it again — `drop_challenge`,
  `drop_verification_token`, `revoke_grants` — each returning a `DropReport`
  rather than `None` (0.46.0). Since 0.47.0 **every removal in the package**
  speaks that one vocabulary (`core/drop.py`): a delete measures what it did
  instead of claiming it.
- `flows` — self-documenting business scenarios (`Flow`, `@flow_step`,
  `flow_registry`) with doc generation and a CI completeness gate.
- `i18n` — domain-agnostic shipping of localized content: per-app
  `translations/<domain>.<lang>.json` catalogs (later-wins, fork-free host
  override), a `.state.json` provenance sidecar where only `origin: human`
  counts as reviewed, write-time `translate_catalogs` (seed → translator seam,
  byte-stable, writing only where the loader reads) and the
  `check_translation_catalogs` gate. `STAPEL_I18N["LOCALES"]` is the single
  project-languages knob.
- `django` — service conventions: transactional outbox, task store,
  `StapelResponse` / `StapelErrorResponse` / error-key registry, the
  serializer seam and thin-view base (`SerializerSeamMixin` /
  `StapelAPIView`), OpenAPI helpers and postprocessing hooks, JWT auth
  middleware, common settings, management commands.
- `media` — one media interface over swappable storage backends
  (images-and-cdn.md §1): `media.describe(ref)` returns the render-metadata
  snapshot (`{mime, bytes, width, height, aspect, duration_ms, preview_b64,
  square, variants[]}`) from either the PIL/ImageField path (default,
  zero infrastructure — `generate_variants` writes `<stem>__<tier><branch>.webp`
  siblings) or the stapel-cdn service (`STAPEL_MEDIA_BACKEND="cdn"` →
  `cdn.describe` comm Function). Ladder core (min-side thumbnails 16/32/64/120,
  w/h preview branches 160–1080 incl. 560, square dedup, no upscaling) is
  reusable plan math in `media.variants`.
- `signals` — in-process Django signals for business milestones
  (`user_registered`, `user_logged_in`, `payment_completed`,
  `subscription_changed`, `media_processed`, `profile_updated`,
  `workspace_member_changed`).
- `gdpr` — `GDPRProvider` ABC, in-process `gdpr_registry`,
  `GDPRServiceConsumerCommand` for microservices mode, and the data-owner
  erasure subscriber (`register_gdpr_owner`, `pseudonymize`).
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
- `observability` — signals out, vendors swappable: structured JSON logging
  (`logging_config` / `configure_logging`, mandatory field set, secrets
  redacted at the formatter), a metrics facade
  (`metrics.counter/gauge/histogram/timer`) over a `METRICS_BACKEND` seam
  (Prometheus default, statsd/logging/no-op/your own), an `ERROR_REPORTER`
  seam with a Sentry-shaped interface and a no-op default, and **trace
  correlation carried in the comm envelope** — one `trace_id` follows a
  business operation from the HTTP request through every module and service it
  touches. Health/readiness are re-exported from `django.monitoring.health`,
  not re-implemented.
- `core` — framework-agnostic JWT primitives (`JWTHandler`, `TokenManager`,
  `TokenBlacklist`, `JWTConfig`).

All public root exports are lazy (PEP 562, see `__init__.py`); importing
`stapel_core` never touches Django until a Django-dependent attribute is used.

### The contract document — `docs/capabilities.json` (`make contract`)

The machine-readable answer to *"does stapel already have a mechanism for X?"*.
Until 0.17 this repository — the most reused one in the fleet — was the only
significant library with no such document, and the reason was structural, not
neglectful: the format could describe the **configuration** surface (`axes`,
"what can be switched on") and the **substitution** surface
(`extension_points`, "what can be replaced"), and the core has no feature axes
and no OpenAPI operations. It had nothing to say in the format.

What the core does have is the **usage** surface — the symbols a product is
meant to *call* — and that is the third section, `surface`: one entry per
permission class, factory, predicate and template, each with a curated line
saying when to reach for it, and (where it applies) `instead_of`, naming the
outside symbol it displaces. Two rules make it stay true:

- the entry set is **derived** by AST from the `surface_roots` declared in
  `docs/capabilities.meta.json` — scopes, not symbols, so it cannot be a stale
  hand-written list;
- a selected export with **no intent line fails emission**, naming the symbol.
  A library that exports a mechanism it cannot explain in one line has just
  built the next mechanism nobody adopts.

Run `make contract` after touching anything under a declared root and commit
the result; `tests/test_contract.py` is the drift gate.

## Extension points (fork-free)

### Settings namespaces (`stapel_core.conf.AppSettings`)

`AppSettings(namespace, defaults, import_strings=...)` resolves each key in
order: `settings.<NAMESPACE>` dict → flat Django setting of the same name →
environment variable → default. Keys in `import_strings` are dotted paths
resolved with `import_string` — the standard seam for swapping behavior
without forking. Host apps and other stapel modules create their own
instances (e.g. `verification_settings` below).

**Keys in `import_strings` skip the environment step** — they are implicitly
`no_env`. Such a key does not carry a value, it names the class the process
imports and runs, so a same-named env var in a shared pod would choose the
implementation of a provider, backend or policy. The project's settings dict
and the flat setting still select it; the environment does not. A deployment
that genuinely selects an implementation per environment opts out by name:
`AppSettings(..., import_strings=("PROVIDER",), env_overridable=("PROVIDER",))`.
Declaring the same key in both `no_env` and `env_overridable` raises at
construction rather than picking a winner.

**`resolvers=` — the import_strings family with a custom string→object step.**
A value that is legally "registry short name OR dotted path" cannot go through
the base class's eager `import_string`, so packages used to subclass
`__getattr__` or keep the key out of `import_strings` with a comment — the
second of which silently drops the key out of W001's scope.
`AppSettings(..., resolvers={"PROVIDER": callable_or_dotted_path})` delegates
only the resolution, at the same lazy, cached point where `import_string`
runs today; a dotted-path resolver is itself imported at first use so a
`conf.py` never drags a channel module into every import. Policy is
unchanged from `import_strings`: implicitly env-closed, reopened only by
`env_overridable`, reported by W001 with the class wording. Resolver
exceptions pass through with their own type and message (a registry's
`ImproperlyConfigured` naming the short names it knows survives instead of
degrading to a bare `ImportError`). Declaring a key in both `import_strings`
and `resolvers` raises at construction.

Ignoring a variable is silent, so it is announced: `manage.py check` emits
`stapel_core.conf.W001` (warning, tag `stapel_conf`) for every environment
variable that is set while the namespace refuses to read it, naming the
variable, the namespace and the key. Nobody has to grep a deploy manifest to
learn that the process is running an implementation other than the one the
environment asks for. **Scope is every key the namespace closes** — the
implicit closure over `import_strings`/`resolvers` *and* `no_env` — because
`no_env` was invented for the same threat model, and a set-but-ignored env
var on a policy key is exactly the "the operator believes X is configured and
it is not" silence W001 exists to end. The message branches on which family
closed the key, so it never tells an operator that a policy toggle names a
class. A key reached through a namespace's own alias route (media's
`STAPEL_MEDIA_BACKEND`) is correctly not reported: that variable IS read.

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
| `VALIDATE_SCHEMAS` | `True` | Validate payloads against schemas from `@function` / `@on_action`. On everywhere, `DEBUG` included and excluded; set `False` to opt out explicitly |
| `TASK_EXECUTOR` | `"inline"` | How a worker runs a claimed task: `inline` \| `celery` \| dotted path to `callable(task_id)` |
| `TASK_DISPATCH` | `"action"` | How `task.requested` reaches the worker: `action` (rides `ACTION_TRANSPORT`) \| `bus` (task.\* events go straight to the bus) \| `inline` (synchronous, tests only) |
| `SIGNAL_TRANSPORT` | `"none"` | Signal delivery: `none` (silent no-op — the right setting for every HTTP-only host) \| a name registered with `register_signal_transport()` (`"channels"`, from `stapel-realtime`) \| dotted path to `transport(stream_key, frame)`. Boot-gated by `stapel_core.comm.E003` |
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
  mutation+emit without shared atomic, EMIT004 emit in on_commit, EMIT005 a
  module-level `emit_*` helper with zero call sites anywhere in the scanned
  package — declared-but-unwired, the `stapel_listings.events.
  emit_listing_updated` defect). Lexical only; suppress a proven false
  positive with `# emit-check: ok — <reason>`. Module repos run it in
  pre-commit/CI next to ruff.

Review checklist for data-holding modules: every emit is atomic with its
mutation, and a `test_failing_emit_rolls_back`-class test exists (see
`tests/test_emit_atomicity.py` here for the reference shapes).

### Signal — the observer-facing primitive (`comm/signals.py`)

Action, Function and Task all address **code**. Signal addresses a **human's
screen**: *"show this to a live observer, if one is watching."*

```python
from stapel_core.comm import mutate_and_emit, signal

with mutate_and_emit() as emit_event:
    recording.status = "ready"
    recording.save(update_fields=["status"])
    emit_event("recording.completed", {...})           # the fact — durable
    signal(f"recordings:ws:{workspace_id}",             # the screen — ephemeral
           "recording.status",
           {"recording_id": str(recording.pk), "status": recording.status})
```

Guaranteed: delivery only to subscribers connected at the moment of the emit;
ordering within one stream key; never ahead of the transaction it describes
(scheduled through `transaction.on_commit`, no outbox row). NOT guaranteed:
delivery at all, ordering across streams, redelivery, any history. Losing a
frame is correct behaviour — the truth stays in the DB behind REST, and a
signal is a reason to refetch, never the state itself. Durability, where a
module needs it (chat), belongs to that module's own model.

- **Addressing** — `<mod>:<scope_type>:<scope_id>[:<topic>]`, built with
  `stream_key("recordings", "ws", workspace_id)` and validated on every emit
  (`InvalidStreamKey`). The scope is part of the name so a group physically
  cannot cross a workspace; the name is not a secret, subscription is
  authorized separately and fail-closed by the substrate.
- **Envelope (wire v1)** — `{"v": 1, "type", "stream", "payload"}`. `stream`
  is optional in the schema (a v1 socket serves one stream) but always
  populated: it is what makes a multiplexed socket possible later without a
  v2 envelope. There is deliberately no `seq` — frame kind is structural, so
  an ephemeral frame can never be mistaken for, or persisted as, journal
  state. Frame types reserved by the wire protocol (`hello`, `welcome`,
  `ping`, `kick`, …) are refused (`InvalidSignalType`).
- **Transport seam** — `STAPEL_COMM["SIGNAL_TRANSPORT"]`, closed by default.
  Core carries the emitter only (stdlib; no channels, no redis, no ASGI):
  emitting has to be free for the libraries that never serve a socket, or
  modules quietly stop signalling. Delivery — consumers, per-stream
  authorization, revoke/kick — is the separate `stapel-realtime` library,
  which calls `register_signal_transport("channels", …)` from its
  `AppConfig.ready()`. Contract: `transport(stream_key, frame)`, called after
  commit, may raise (the frame is dropped and logged; a courtesy to an
  observer must never break the caller).
- **Not an Action in the browser.** Signals never ride the outbox:
  at-least-once with 300s of retries would deliver "typing…" five minutes
  late into a table with no retention, and an Action's subscriber is a module
  obliged to handle it, while a Signal's subscribers are 0..N browsers whose
  absence is normal. The canonical bridge is the opposite direction and is
  encouraged: an `@on_action` handler turns a committed fact into a
  `signal()`.

### Realtime border — realtime-check (`lint/realtime_check.py`)

Four independent realtime implementations already exist in the fleet (video
lobby, chat, studio-dialog, runner-protocol) and three of them re-invent the
same 80% — none mounted by any host, all green in isolation. `python -m
stapel_core.lint.realtime_check .` is the border that stops the fifth, drawn
by asking who is on the other side of the socket: **a human in a browser →
`stapel-realtime`, emitting through `comm.signal()`; our own process → a
named application protocol (`stapel-runner-protocol`) that owes an answer to
"why not a Function/Task"**.

RT001 Channels consumer, RT002 hand-rolled socket auth middleware (the one
home is `stapel_core.django.jwt.channels`, G14), RT003 raw
`websockets.serve()` — errors; RT004 hand-rolled SSE endpoint, RT005 direct
channel-layer fan-out instead of `comm.signal()` — warnings. Suppress a
genuine one-off with `# realtime-check: ok — <reason>`; the four existing
implementations are grandfathered by an in-code allowlist where every entry
names the migration phase that deletes it. That list is a debt register: it
shrinks, never grows.

### Projections — event-carried read-models (`comm/projections.py`, `django/projections/`)

A cross-domain read (a catalog listing showing its like count owned by an
engagement module) is served by a **local read-model table** that a consumer
fills from the owner's Action events — no synchronous call on the read path.
The pattern was re-invented per table (idempotency hand-rolled as a unique
constraint, backfill as a one-off script, counters drifting when a bulk
`update()` skipped a `post_save` signal). `Projection` formalises it. Declare
which topic(s) feed which table and how each event upserts a row; subclass the
read-model from `ProjectionModel` (it carries the source key + sequence +
event-id bookkeeping):

```python
from django.db import models
from stapel_core.comm import Projection
from stapel_core.django.projections.models import ProjectionModel

class ListingLikes(ProjectionModel):          # the read-model table
    likes_count = models.PositiveIntegerField(default=0)
    class Meta:
        app_label = "catalog"

class ListingLikesProjection(Projection):     # the declaration
    name = "catalog.listing_likes"
    consumes = "engagement.likes_changed"     # Action topic(s)
    model = "catalog.ListingLikes"
    source_key = "listing_id"                 # payload field = row identity
    source_of_truth = "engagement.likes_export"  # Function for rebuild
    sequence_field = "revision"               # ordering token (else event ts)
    def apply(self, event):
        return {"likes_count": event.payload["likes_count"]}
```

The framework gives, once: **idempotency + ordering** — an event applies only
if its position (`sequence_field`, else event timestamp) is strictly newer
than the row's, so a redelivered duplicate is a no-op and a reordered/stale
event never overwrites fresher state (idempotency by event id + unique source
key, §10); a **consumer runner** wired through the ordinary action registry
(same in-process `on_commit` delivery in a monolith, same bus consumer across
services — the projection code does not change on split); **first-class
rebuild** — `manage.py rebuild_projection <name>` (or `comm.rebuild(name)`)
re-derives the whole table from the owner's `source_of_truth` Function,
batched, all-or-nothing, with progress, and `--check` (`comm.drift_check`)
compares row counts without writing; **`comm.projection_status(name)`** for
row count / last sequence / lag.

**Two modes, one declaration** (projections-and-composition §1). The mode is
a property of TOPOLOGY at process start, never of business code.
`resolve_mode(proj)` auto-detects it from the owner's app label — the first
`consumes` topic prefix (`"engagement.likes_changed"` → label `engagement`;
convention: a module's `app_label` == its bus namespace prefix; looked up
via `apps.get_app_config(label)`, NOT `is_installed()` which compares dotted
module paths). Optional `force_mode = "local" | "remote"` overrides.

- **remote** (owner not installed — separate services): everything above; a
  materialised table fed from the bus, `model` required.
- **local** (owner installed in the same process): no table, no bus
  subscription (`wire_projections()` skips it); reads go first-hand through
  the owner's **`live_query`** Function — a keyed batch lookup, contract
  `{"keys": [<str>, ...]}` → `{key: {..fields..}}`. `live_query` required
  in local mode; `model` optional.

Business code never branches on the mode — it calls the ONE accessor
**`comm.projections.read(name, keys)`** (never the ProjectionModel ORM
directly — that hard-wires remote mode into the caller): returns
`{key: fields}` with stringified keys, identical shape in both modes; absent
keys are simply missing.

Loud config validation at app ready (`validate_registry`, raises
`ProjectionConfigError`), branched by resolved mode: local requires
`live_query` (model/table checks skipped); remote requires `model` and the
existing checks — **one table = one source** (no two projections target the
same model), the model must derive from `ProjectionModel`, required
attributes present. Rules the primitive encodes (review/lint
matters): projections are read-only for business code; one projection owns one
source domain and its table (projected fields never mixed with locally
computed aggregates); the data *owner* computes each aggregate and publishes
it as a fact via `emit()` in its transaction — one-directional fact streams,
never `post_save` recompute loops. The owner's `source_of_truth` Function
pages with `{"cursor", "limit"}` → `{"rows": [{source_key, "seq", **fields}],
"cursor", "total"}`. Install the `stapel_core.django.projections` app to get
the model base and the management command.
`rebuild`/`drift_check`/`projection_status` remain remote-only (local data is
live by construction). Composite libs (stapel-shop &c.) are the canonical
home for cross-domain Projection declarations ("glue" between domain-blind
engines — reviews may not know about listings; the composite may).

### Bus backends — `STAPEL_BUS_BACKEND` (`bus/router.py`)

Resolution: env var first (12-factor), Django setting second, default
`"memory"` (0.11.0+ — kafka/nats/redis are explicit opt-in; see
`bus/router.py`'s docstring for why). Value is a shorthand or any dotted
path to a `BusBackend` subclass — a custom broker needs zero core changes:

| Shorthand | Backend dotted path |
|---|---|
| `memory` | `stapel_core.bus.backends.memory.MemoryBus` |
| `kafka` | `stapel_core.bus.backends.kafka.KafkaBus` |
| `nats` | `stapel_core.bus.backends.nats.NatsJetStreamBus` |
| `redis_streams` (alias `redis`) | `stapel_core.bus.backends.redis_streams.RedisStreamsBus` |
| `routing` | `stapel_core.bus.backends.routing.RoutingBus` |

`routing` splits topics across brokers via `STAPEL_BUS_ROUTES` (env JSON or
Django dict) mapping topic prefix → shorthand/dotted path;
longest-prefix-wins, `""` is the default route (e.g.
`{"task.": "kafka", "": "nats"}`). Connection settings (`bus/_config.py`,
env-first then Django setting): `KAFKA_BOOTSTRAP_SERVERS`,
`KAFKA_SECURITY_PROTOCOL`, `KAFKA_SASL_MECHANISM` / `_USERNAME` / `_PASSWORD`;
`NATS_URL`, `STAPEL_NATS_STREAM` (`stapel-events`), `STAPEL_NATS_EVENT_PREFIX`
(`stapel.evt`); `STAPEL_REDIS_BUS_URL` (falls back to `REDIS_URL`),
`STAPEL_REDIS_BUS_CLAIM_IDLE_MS` (`60000` — XAUTOCLAIM staleness threshold),
`STAPEL_REDIS_BUS_STREAM_MAXLEN` (`100000`, approximate XADD trim; `0`
disables). `redis_streams` maps one topic to one Redis stream (XADD), one
consumer group per subscriber (XREADGROUP+XACK, group name = the `group`
passed to `consume()` — same convention as Kafka's `group.id` / NATS's
durable name), and reclaims entries abandoned by a dead consumer via
XAUTOCLAIM once idle past the claim threshold. Consumers subclass
`BaseBusConsumerCommand`.

Every broker backend wraps each handler attempt in
`stapel_core.django.db.worker_db_lifecycle()` — a custom backend must do the
same. A consumer loop has no request lifecycle, so without it the connection
the first event opened is reused for days, and the first idle drop by the
database server turns into a permanent outage (every later event fails with
`InterfaceError: connection already closed`, retries included, because they
all reuse the same dead socket). `close_stale_connections()` probes with
`is_usable()` rather than trusting `close_old_connections()`, which by design
only closes what already errored or aged out.

**NATS durables are reconciled on every boot.** A JetStream durable outlives
the process that made it, and `js.pull_subscribe(durable=…)` binds to an
existing consumer while discarding the `ConsumerConfig` it is handed — so
without this, a service that gained a topic kept its pre-deploy
`filter_subjects` server-side and went deaf on the new subject with no error
anywhere. `NatsJetStreamBus` therefore compares the live consumer against the
declared one before binding: mutable drift (`RECONCILABLE_FIELDS` — subjects,
`ack_wait`, `max_deliver`, …) is updated **in place**, which preserves the ack
floor, and is logged with the before/after subject sets; immutable drift
(`IMMUTABLE_FIELDS` — `ack_policy`, `replay_policy`, `deliver_subject`, …)
raises `ConsumerConfigConflict` at startup rather than running deaf. Start
position (`deliver_policy`, `opt_start_*`) and fields the backend never
declares are left alone. The match is verified against the server after setup,
so the log line naming the subjects is a fact, not an intention.

### Verification factors & policy — `STAPEL_VERIFICATION` (`verification/conf.py`)

| Key | Default | What it customizes |
|---|---|---|
| `DEFAULT_FACTORS` | `["otp_email", "totp", "passkey"]` | Factors offered when a view doesn't pass its own list |
| `DEFAULT_MAX_AGE` | `300` | Grant lifetime (s) when a view doesn't pass `max_age` |
| `CHALLENGE_TTL` | `600` | Challenge lifetime (s) |
| `MAX_ATTEMPTS` | `5` | Failed attempts before a challenge is invalidated |
| `EXTRA_FACTORS` | `[]` | Dotted paths of custom factor classes, applied at boot by `CommonDjangoConfig.ready()` |
| `DEFAULT_LEVEL` | `"strict"` | Level used when a view passes `level=None`: `strict` \| `default_on` \| `opt_in` |
| `POLICY_CACHE_TTL` | `60` | Cache TTL (s) for the resolved per-user policy |
| `GRANT_NAMESPACE` | `"stapel_verification"` | The **fleet-wide** cache key prefix challenges/grants/tokens are written under (0.45.0). A wire format between peers — change it only to run two fleets against one store, and then change it in EVERY peer (`stapel_core.verification.W001`, security-critical) |
| `GRANT_CACHE` | `"default"` | `CACHES` alias the shared connection is borrowed from (`stapel_core.verification.E001` when it names an alias that does not exist and there is no `default` — grants written nowhere means step-up is permanently refused) |

**Grants are fleet-shared state, not per-service** (0.45.0). Until 0.44.2
challenges, grants and verification tokens went through
`django.core.cache.cache`, which Django namespaces under the *deployment's*
`KEY_PREFIX` — a value every service in a split deployment sets differently on
purpose. A step-up completed in the auth service was therefore invisible to the
peer that demanded it (`auth:1:stapel:verification:grant:<uid>:sensitive`
versus `stapel_profiles:1:…`). Grants now use `stapel_core.core.fleet_cache`,
the same mechanism revocation has used since 0.39.0. The key is fleet-stable
because the identity is: `user.pk` is the UUID the JWT `user_id` claim carries,
and consumer-mode services materialise the local row under that same pk.

**Dropping a record is a reported fact, not a gesture** (0.46.0). Until 0.46.0
this module could create a challenge, read it and complete it, and had no
public way to *remove* one. A consumer testing the expired-challenge path had
to reach into the private `grants._cache()` — or compute the key itself against
`django.core.cache.cache`, which is what stapel-auth 0.28.0 did. Once 0.45.0
moved the store onto the fleet namespace that plain-cache delete computed a
different key, removed nothing, and was indistinguishable from a delete that
worked: the test's setup silently did nothing and its assertion "verified" an
emptiness that had never been created. The release died on it.

| Verb | Removes | Returns |
|---|---|---|
| `drop_challenge(challenge_id)` | the challenge | `DropReport` |
| `drop_verification_token(token)` | one stateless `X-Verification-Token` | `DropReport` |
| `revoke_grants(user_id, scopes)` | the user's grants for those scopes | `list[DropReport]`, one per scope, in order |

Each reads the key **this module** computes, deletes it, reads it **back**, and
reports one of three outcomes that must never be folded into one another
(`DropOutcome`): `DROPPED` (it was there, a read-back confirms it is gone),
`NOT_FOUND` (nothing was stored under that key — already spent, aged out, or
*the writer computed a different key*), `STILL_PRESENT` (the delete ran and the
record is still readable; never success). `DropReport` is falsy unless the
outcome is `DROPPED`, so `assert drop_challenge(cid)` is a real assertion — a
bare `str`-Enum would be truthy on every member, including the one the
primitive exists to expose. `NOT_FOUND` logs a warning and `STILL_PRESENT` an
error, both naming the namespace, so a caller who ignores the return value
still cannot obtain a quiet no-op.

These are deliberately operational primitives that tests also use, in the
ordinary public API rather than a `testing` sidecar: a step-up nobody
requested, an erasure, a session known to be compromised are the same removal
`record_failed_attempt` already performs when the attempt budget runs out. A
"for tests only" function operations would reach for anyway is better named
honestly than quarantined where the contract does not describe it — the
private version of this seam is exactly what cost a consumer a release.

**One vocabulary for every removal in the package** (`core/drop.py`, 0.47.0).
The 0.46.0 audit named six more calls of the same shape and left them; they are
fixed here, and `DropOutcome` / `DropReport` moved out of `verification.grants`
so they are not re-invented per module. The shape is *not* "the function
returned nothing": Django's `cache.delete` returns `False`, not `None` — **the
return was a truthful answer about a key the module never writes, which is no
evidence about the record the caller meant.** A truthful answer to the wrong
question is worse than silence, because it looks like information. Four of the
six said even less: `True` for "the call did not raise".

| Verb | Was | Now |
|---|---|---|
| `jwt.tombstone.lift_tombstone(uid)` | `True` if it did not raise | `DropReport` — the operator is restoring a **wrongly deleted user**; a `True` that lifted nothing left that person locked out fleet-wide with no signal |
| `jwt.authentication.unblacklist_user(uid)` | `True` if it did not raise | `DropReport` — the concern `blacklist_user` documented, finally carried to the delete path |
| `TokenBlacklist.remove_from_blacklist(jti)` | `True` if it did not raise | `DropReport` |
| `TokenBlacklist.clear_all()` | `True` if it did not raise | `DropReport`, measured with a **probe** (see below) |
| `OneTimeCodeStore.discard()` / `.unblock()` | `None`, and `discard` swallowed `StoreUnavailable` | `DropReport`, with `UNAVAILABLE` as its own outcome |
| `mandate.invalidate_mandate_cache(user_id)` | `None` | `DropReport`; the report names the per-service scope |
| `verification.policy.invalidate_policy_cache(user_id)` | `None` | `DropReport` (the namespace **move** is deferred — see below) |
| `workspaces.invalidate_membership_cache(ws, user)` | `None` | `DropReport` |

`DropOutcome` gained a fourth member, `UNAVAILABLE`: the store could not be
reached, so nothing is known about the record — not a removal, and not evidence
of absence either. All four are logged bar `DROPPED`.

**Where a verb genuinely cannot read back, it says so.** `clear_all()` empties
a whole connection and nothing enumerates what was there, so `True` for "did not
raise" was the most comforting of the six lies. It now measures the one thing
that *is* measurable — that `clear()` reached the namespace this library writes
— by writing a probe first and reading it back after, and it documents that it
does **not** claim only revocation keys were removed.

**Two of these caches are deliberately service-local, and say so in the
report.** `OneTimeCodeStore` stays on `django.core.cache.cache` because the
issue and the check of a one-time code happen in one service; sharing it would
publish a bearer credential to every peer and make the attempt budget, block
and send-window shared state. The mandate cache stays local because the
*invalidation* is the fleet-wide part — each peer drops its own copy from the
same broadcast. Their reports carry `namespace="service-local:<KEY_PREFIX>"`,
so a `DROPPED` is visibly a measurement about this process only.

**Deferred, deliberately: the policy cache's namespace.**
`verification.policy` is the last part of `stapel_core.verification` still on
`django.core.cache.cache`, so invalidating a policy in auth does not invalidate
it in the peer that enforces it and the peer serves the stale answer for
`POLICY_CACHE_TTL`. Moving it is a wire format between peers, exactly as
`GRANT_NAMESPACE` was, so it gets its own release rather than riding an
additive one. What 0.47.0 fixes is that the reach is now visible in the return
value instead of invisible in a `None`.

`revoke_grants` does **not** reach a minted verification token: `TOKEN_KEY` is
keyed by the token itself, so it cannot be enumerated from a user id and a
holder still satisfies `has_grant(user, scope, token=...)` for the token's full
`max_age`. Use `drop_verification_token` when you hold the value; when you do
not, its lifetime is the only bound.

Custom factors: subclass `VerificationFactor` (define `id`, implement
`verify`, optionally `available_for` / `initiate`) and call
`register_factor(instance_or_dotted_path)` from an `AppConfig.ready()`, or
list the dotted path in `EXTRA_FACTORS` — **declaring it is enough**:
`stapel_core.django.apps.CommonDjangoConfig.ready()` calls
`load_configured_factors()` at boot (0.16.1; before that the loader had no
caller anywhere in the framework and the setting was inert). Entries are
registered *pinned*: a factor id the host claims in `EXTRA_FACTORS` beats any
library registration of the same id whatever the `INSTALLED_APPS` order is, so
overriding e.g. stapel-auth's `otp_phone` no longer depends on where the
host's app sits in the list. A dotted path that cannot be imported (or is not
a valid factor) raises `ImproperlyConfigured` at boot.
`@requires_verification(scope=...,
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

### Flow Gherkin projection (`flows/gherkin.py`, flow-system.md §3)

The flow is the source, the `.feature` is a projection. `manage.py
generate_flow_features --out features [--languages en,ru] [--llm]` writes
one **self-consistent bundle per project language**: `<flow_id>.feature`
(localized Gherkin — positional Given/When/Then over the resolved step
notes, `# language:` header + localized keywords for non-en) plus
`steps/flows.steps.ts` + `steps/fixtures.ts`, a **playwright-bdd** step
library. HTTP steps drive the codegen typed client (`@stapel/core`
`createStapelClient`); human/UI steps are honest `TODO(testid)` stubs
(no testid plan on the flow model yet — system-design §7.20); comm-effect
steps are pending side-effect assertions. Byte-stable — the same
regenerate-and-diff drift gate as the SA-doc trees.

Programmatic surface: `render_feature` / `render_step_defs` /
`render_fixtures` / `write_language_bundle`, and `load_flows_json` — rebuild
`(flows, endpoint index)` from a committed `flows.json` to generate without
booting the producing Django instance (never touches the registry). The
committed reference (3 stapel-auth flows, en+ru) lives in
`docs/examples/auth-flow-features/` gated by
`tests/test_flow_feature_reference.py`.

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
(`CommDocTranslator`, `DocTranslationCache` live in `stapel_core.i18n`).
`docs/errors.json` stays language-agnostic (en canon); localized error
texts live in `translations/errors.<lang>.json`, gen-errors reads them per
locale.

Localized texts are a **static, reviewed-as-code artifact**, generated
write-time:

- `manage.py translate_catalogs --domain errors --lang ru [--seed FILE] [--llm]
  [--app LABEL | --out DIR] [--approve KEY… | --approve-all]` materializes
  `<out>/errors.ru.json` (byte-stable) + a `.state.json` **provenance sidecar**
  keyed `<domain>.<lang>` → `{key: {hash: h(source_en), origin}}`. Per key: keep
  (source hash still matches) → seed from a curated corpus
  (`origin: seed:<label>`) → the `STAPEL_I18N["TRANSLATOR"]` seam (`--llm`,
  content-hash cached, `origin: llm`) → left missing (fails the gate).
  `--approve` flips keys to `origin: human` without retranslating. Editing the
  en canon auto-staleness-marks exactly that one key (the hash no longer
  matches).
- **Provenance says where a value came from; only `human` means reviewed.**
  `llm` (machine), `seed:<label>` (curated corpus — cheap, still machine-made)
  and `imported` (already in the catalog, authorship unknown) are all
  UNREVIEWED, and `--approve` is the only thing that clears the counter.
  Seeding is meant to be the obvious path, so it must never double as review.
  Two predicates, two questions: `is_reviewed(origin)` (did a human sign off →
  the gate's W-counter) and `is_curated(origin)` (was it placed deliberately →
  never silently re-derived, so a stale seed stays put and is reported stale).
- **The output directory is resolved against the loader, not the shell.**
  Catalogs are found by walking the *package* directories of INSTALLED_APPS, so
  a relative `--out` against a service root wrote a file nothing would ever
  read. The default is now the app package the command runs from; `--app LABEL`
  names one; an explicit `--out` that is not `<app package>/translations` (or an
  `EXTRA_CATALOG_DIRS` root) is refused, naming the places the loader looks.
  `resolve_catalog_dir()` and `load_app_catalogs()` share `catalog_search_dirs()`
  so writable and readable cannot drift apart.
- The **first ru is not machine-translated**: `stapel-i18n-seed` (stapel-tools)
  exports the already-curated stapel-translate builtin fixtures (155 `error.*`
  × ru) into a seed the command applies — requirement "clients don't spend
  tokens" is met by copying, not re-running an LLM.
- `manage.py check_translation_catalogs --domain errors` is the CI gate
  (module pytest wraps `check_translation_catalogs(...)`, like `check_flows`):
  **E** on a missing key, a stale one (en changed, translation didn't), a
  `{param}` mismatch vs the canon, or a non-byte-stable file; **W** counts
  unreviewed values — everything no human approved (`llm`, `seed:<label>`,
  `imported`, unknown) — with `--strict` making them fatal, after the first
  review pass. It resolves `--out`/`--app` the same way `translate_catalogs`
  does: gating a directory the loader cannot read is as useless as writing one.
- `manage.py generate_error_docs [--lang ru]` writes the human-readable
  `docs/errors.<lang>.md` reference (i18n-shipping.md §4); README links both
  languages (lint rule `R100` in `stapel_tools.lint`).

**Core ships its own catalogs, and a module translates only what it owns.**
`django/translations/errors.{ru,es}.json` (the `stapel_core.django` app
package — what `CommonDjangoConfig.path` is, so the loader already walks it)
carries the 41 cross-cutting keys core registers: `COMMON_ERRORS`, the
verification step-up keys, the captcha/network keys. Before it existed the
canon `check_translation_catalogs` demanded was the **whole** in-process
registry, so every module that localized its own errors had to re-translate
core's 41 to go green: five libraries × two languages = 410 byte-identical
duplicated entries, none of them an intentional reword, every one of them a
future stale shadow of a text core owns.

So keys carry an **owner** and the canon is scoped by it:

- `register_service_errors(errors, remediation=None, owner=None)` records who
  answers for a key. `owner` defaults to the caller's top-level package and is
  recorded only for a key nobody owns yet — **first registrant wins**, so
  re-registering somebody else's key still overrides its en text (the §3
  fork-free seam is untouched) without taking over its catalog duty. Core
  passes `owner="stapel_core"` explicitly where import order would otherwise
  decide. `error_owners()` / `error_owner(code)` read it back.
- The **loader does not change**: `load_app_catalogs` stays a flat later-wins
  merge over INSTALLED_APPS, so precedence is still position — core's catalog,
  then a module's declared override, then the host app last. Ownership is
  enforced at write and gate time, never inside the merge.
- `check_translation_catalogs` requires only the **owned** keys and raises
  **E `foreign`** for a catalog entry belonging to another package that
  already ships that language — with the gap-filling carve-out: covering a key
  its owner does *not* translate in that language (a host generating Turkish
  for the whole fleet) is legal and silent until the owner ships it.
- A deliberate reword says so: `translate_catalogs --declare-override KEY…`
  writes `override: <owner>` into the `.state.json` row. The catalog file
  stays a flat `{key: text}` map — the runtime merge, `gen-errors.mjs` and
  human readers all depend on that, so the declaration lives in the sidecar
  that is already tooling-only and already per-key. A declaration that repeats
  the owner's text verbatim is **W `vacuous_override`**.
- `translate_catalogs` will not emit a key the target package does not own, so
  the command that manufactured the 410 duplicates cannot manufacture more.
- **The reader follows ownership too.** `generate_error_docs --translations
  <dir>` reads one module's catalog directory, and a module's reference covers
  the whole registry — so the moment a module stopped duplicating core's keys,
  those 41 rows fell back to `_(en)_` and the Russian reference silently became
  English. `module_catalog(domain, lang, dir)` is the read seam that closes it:
  the module's own text wins (that is what a declared override is), a key it
  does not own is read from `owner_catalog()`, and a key it *does* own is never
  back-filled — an owner's own gap must stay the coverage error it is. Pruning
  is therefore byte-neutral for `docs/errors.<lang>.md`, which is what makes
  the sweep safe to run across the fleet.

**The two halves of the contract are gated against each other.** The registry
export (`docs/errors.json`, instance-scoped: every code the deployment can
emit, like schema.json) and the catalogs (ownership-scoped) are produced by
different commands; the seam between them is where translated keys used to
ship unreachable (gdpr) or registries declared codes no catalog carried:

- every `errors.json` entry carries **`owner`** (`build_error_registry`) — the
  package whose catalogs hold its translations, so a consumer pairs the two
  without knowing the mount graph;
- `generate_error_keys` runs `check_registry_catalog_pairing(entries)` before
  writing: **E `untranslated`** (an owner ships the language but not the
  declared code — emission refuses), **W `unshipped`** (an owner with declared
  codes ships no catalogs in an otherwise translated instance);
- `check_translation_catalogs` checks the reverse: **E `no_registry_export`**
  (catalogs for owned keys and no export at all) and **E `unexported`** (a
  translated owned key the export does not declare). Injectable via
  `export_resolver=` for unit tests;
- **where the export lives follows what the package is**
  (`DOMAIN_EXPORTS["errors"]`). A distributable carries it in its wheel, at
  `<top-level package>/docs/errors.json` — the only place a consumer who
  installed it can look. A project's own app (a monolith's `accounts`,
  `rooms`, …) is not a wheel and has no `docs/` to put anything in, so its
  codes are declared by the project's export: `<BASE_DIR>/docs/errors.json`, or
  `STAPEL_I18N["REGISTRY_EXPORT"]`. Two conditions keep that from becoming a
  way out of the gate — the package must have no installed distribution
  (something that ships as a wheel must carry its own export; a project export
  never stands in for it) and must live inside the project root — and the
  project export answers for a code only where it attributes it to that app,
  so it cannot vouch for a neighbour's keys;
- core ships its own export (`docs/errors.json`, 41 keys), drift-gated by
  `tests/test_error_registry_artifact.py`.

`STAPEL_I18N` (`i18n/conf.py`): `LOCALES` (default `["en","ru"]`) — the single
"project languages" knob; `STAPEL_FLOWS["DOC_LANGUAGES"]` delegates to it
(`project_languages()`) unless a host sets it explicitly (doc languages may
differ from product languages). `EXTRA_CATALOG_DIRS` adds catalog roots outside
the apps. `TRANSLATOR` / `SOURCE_LANGUAGE` are the domain-agnostic
machine-translation seam (the `llm.translate` comm Function by name, default).
`REGISTRY_EXPORT` names the project's own registry export when it is not at the
`<BASE_DIR>/docs/errors.json` default (env-closed — it decides which file the
pairing gate accepts). `UNDECLARED_OVERRIDES` (`"error"` default, `"warn"`) is
the one policy switch:
the escape hatch for a host onboarding a legacy catalog it did not write.
Fleet libraries run the default.

### Serializer seam & thin views (`django/api/views.py`)

The layered stance (system-design §8.1) asks a view to be *thin*: validate with
a serializer, hand a DTO to the service layer, render through a serializer,
return `StapelResponse`. Nothing in that shape is module-specific — and yet
twenty-three stapel modules hand-wrote the same `SerializerSeamMixin` — twenty-four
definitions counting the stapel-tools library template — because the
core did not ship one (`docs/reference/module-extension-gaps.md`, meettoday-gap
item 2). 0.37.0 ships it once.

```python
from stapel_core.django.api.views import SerializerSeamMixin, StapelAPIView

class CheckoutView(StapelAPIView):
    request_serializer_class = CheckoutRequestSerializer
    response_serializer_class = CheckoutResponseSerializer

    def post(self, request):
        data = self.validated_request_data(request)
        url, sid = services.create_checkout_session(**data)
        return self.serialized_response(CheckoutResponse(url, sid))
```

**`SerializerSeamMixin`** — `request_serializer_class` /
`response_serializer_class` class attributes, read only through
`get_request_serializer_class()` / `get_response_serializer_class()`. A host
swaps a direction by subclassing and setting the attribute, or overrides the
getter for a per-request decision; it never copies an HTTP method body and
never forks the library. `None` on either side is a *value*, not an error — it
says "this direction carries no serialized payload" (raw `request.FILES`, a
204). Views with several serializers per direction keep the suffix and add a
purpose prefix: `list_response_serializer_class` +
`get_list_response_serializer_class()`.

**`StapelAPIView`** — `APIView` + the seam + the two moves the hand-written
bodies were already making, three hundred call sites over:
`validated_request_data(request, partial=False)` (raises DRF 400 on invalid
input, `ImproperlyConfigured` when the view declares no request serializer —
a view bug is not a client error) and `serialized_response(payload,
status=200, many=False)` (renders through the response seam; passes the
payload through untouched when the seam is `None`). Neither helper is
mandatory — a branchy body keeps calling the getters directly.

**What it deliberately does not do:** it never defines DRF's own
`get_serializer_class()`. `GenericAPIView` and every `ViewSet` already define
that method, and shadowing it from a mixin placed first in the MRO would
silently disable per-action serializer selection. ViewSet-based modules
(stapel-listings) keep their own per-action seam.

#### Migration recipe for a consumer library

Per-lib, one release each — do not batch across libraries:

1. `from stapel_core.django.api.views import SerializerSeamMixin` (add
   `StapelAPIView` if you also want the two helpers).
2. Delete the local `class SerializerSeamMixin` / `SerializerSeamsMixin`
   definition. Nothing else changes: the class attributes and getter names are
   identical, so every `class FooView(SerializerSeamMixin, APIView)` and every
   `self.get_response_serializer_class()` call site stays as written.
3. Modules that named it `SerializerSeamsMixin` (plural — stapel-profiles,
   stapel-workspaces) import under the old name rather than touching every
   view header: `from stapel_core.django.api.views import SerializerSeamMixin
   as SerializerSeamsMixin`.
4. Modules that folded the seam into a base view (stapel-gdpr's `GDPRAPIView`)
   keep the base view and re-base it: `class GDPRAPIView(StapelAPIView)`.
5. Floor-bump the `stapel-core` pin to `>=0.37.0` in `pyproject.toml`, run the
   suite, release the library (minor — the deletion is not a behaviour change,
   but the dependency floor moved).
6. `stapel-tools`' library template (`_library_templates.py`) emits the copy
   into every *new* library — fix the template in the same sweep, or new
   libraries keep being born with the duplicate.

Three copies are **deliberate divergences and stay local** — do not "unify" them:

- **stapel-auth** (`utils.py:44`) — a `__getattr__` that *synthesizes*
  `get_<purpose>_serializer_class()` for any `*_serializer_class` attribute a
  view declares, so a view with a dozen serializers writes no getters at all.
  A different mechanism, not a drifted copy; the canonical mixin stays explicit
  (two named attributes, two named getters — greppable, typeable, and it does
  not swallow attribute errors). If the fleet wants the dynamic form, it comes
  upstream as its own opt-in mixin, not by folding it into this one.
- **stapel-listings** (`views.py:80`) — a ViewSet module. Its mixin defines
  `get_serializer_class()` (DRF's own hook) as a fallback, and
  `ListingViewSet` overrides it for per-action selection. Different seam,
  different method, on purpose.
- **stapel-mailtrap** (`views.py:30`) — response direction only. Adopting the
  canonical mixin is safe (it merely adds an unused `request_serializer_class
  = None`), but the narrowing was intentional; migrate it only when the module
  grows a request-validating endpoint.

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

The document is staff-only: `SERVE_PERMISSIONS` defaults to
`IsStaffUserForSwagger` (it was `AllowAny`), and `AppConfig.ready()` forces the
choice onto drf-spectacular's settings singleton *and* its view classes, both
of which snapshot it at import time — without that the setting is decorative
for any project that star-imports `stapel_core.django.settings`. A genuinely
public API sets `STAPEL_PUBLIC_API_SCHEMA=True`.

### Captcha backends & challenge policy — `STAPEL_CAPTCHA` (`captcha/`)

| Key | Default | Semantics | What it customizes |
|---|---|---|---|
| `BACKEND` | `None` (→ `noop`) | replace | Verifier: `turnstile` \| `recaptcha` \| `hcaptcha` \| `noop` \| dotted path to a `CaptchaVerifier` subclass |
| `SECRET` | `None` | replace | Backend secret; **required** whenever `BACKEND` names a real verifier — a named backend without a secret raises `CaptchaConfigurationError` (boot check `stapel_captcha`), it does not silently disable captcha. Leave `BACKEND` unset (or `noop`) to disable |
| `CHALLENGE_MATRIX` | `{}` | **merge** over `DEFAULT_CHALLENGE_MATRIX` | ip-kind → level: residential/unknown → `invisible`, datacenter/vpn → `interactive`, tor → `interactive+ratelimit` |
| `ACTION_OVERRIDES` | `{}` | merge (per action) | `{action: {kind: level} \| "+1"}`; `"+1"` bumps one level (saturates at `block`) |
| `CHALLENGE_POLICY` | `stapel_core.captcha.policy.MatrixChallengePolicy` | replace (dotted path) | The whole `ChallengePolicy` (`level_for(request, action) -> level`) |

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
- **prodguard, transport.** `guard_cookie_security(globals())` in the prod
  settings tier refuses to boot with a cleartext session/CSRF/JWT cookie, no
  HTTPS redirect or HSTS, or a trusted `SECURE_PROXY_SSL_HEADER` the
  deployment never vouched for. An edge that already redirects and sends
  HSTS says so with `STAPEL_TLS_TERMINATED_UPSTREAM = True`; one that
  overwrites `X-Forwarded-Proto` says so with
  `STAPEL_TRUST_PROXY_SSL_HEADER = True`.
- **prodguard, automatic.** The two functions above are also a system check
  (`stapel_prodguard`, E001/E002), registered from `CommonDjangoConfig.ready()`
  and on the boot-gate roster. They were opt-in for years — imported only by
  the prod tier stapel-tools GENERATES, so a project scaffolded elsewhere (or
  before the template grew the call) had no guard and no way to notice, which
  is how a six-character `SECRET_KEY` boots. The check CALLS the guards; it
  does not restate them, and no finding carries a value. `STAPEL_PRODGUARD` =
  `auto` (default: enforce unless `DEBUG` or a test run) | `enforce` | `off`
  (W001, reported at every boot). `STAPEL_PRODGUARD_SECRETS` adds settings
  names; the DB password is asked for only where the engine has one.
- System checks (W-level, `stapel_secrets`): W001 unimportable provider, W002
  not a provider. The env default never trips them.

### The third principal state — `django/mandate.py`

Anonymous / **guest** / mandated, as three distinguishable answers plus a
fourth outcome that is not an answer. A registered account with no active
membership anywhere is not "authenticated enough": it is stapel-workspaces'
guest (`permissions.is_guest`), a predicate that had zero consumers outside
that package and that no sibling could reach in a split deployment — the
workspaces comm surface publishes only workspace-scoped questions.

| Name | What it is |
|---|---|
| `MandateState` | `ANONYMOUS` / `GUEST` / `MANDATED` — a `str` enum |
| `mandate_state(user)` / `has_mandate(user)` | The predicate. Raises `MandateLookupUnavailable` rather than answering `GUEST` for a question it could not ask |
| `HasWorkspaceMandate` (`api.permissions`) | The DRF gate. Mandated → allow; anonymous/guest → 403; could-not-ask → `MandateUnavailable`, 503 `error.503.mandate_unavailable` |
| `MANDATE_FUNCTION` = `workspaces.check_mandate` | The seam. `MANDATE_SCHEMA` / `MANDATE_RESULT_KEY` are the contract the **stapel-workspaces** provider implements — it reads workspace tables, so it belongs there, not here |
| `mandate_seam_unreachable_reason()` | Settings-and-registry only, never a liveness probe. `None` when this deployment can ask |
| `stapel_core.mandate.E001` | Security-critical Error: views gate on a mandate and nothing here can answer. Not a boot gate (it resolves the URLconf); `stapel_preflight` lifts it |

`IsNotAnonymousUser` is unchanged and still means "a real account" — widening
it was the option not taken, because a name that reads as "is a real user"
must not quietly start meaning "holds a mandate".

Resolution order: the comm Function when `function_unreachable_reason` says it
is reachable, then the in-process `stapel_workspaces` predicate (the monolith
path, which is what makes this usable before the provider ships), then a loud
refusal. Never an admission.

**Cache.** Per user, `STAPEL_MANDATE_CACHE_SECONDS` (default 30, `0`
disables). `workspace.member_removed` / `workspace.member_suspended` drop the
entry as they arrive (subscribed from `ready()`), so the TTL bounds the bus
failing rather than the normal path. A grant may lag by up to the TTL — that
direction fails toward refusal. A non-answer is never cached.
`invalidate_mandate_cache()` returns a `DropReport` (0.47.0) — an invalidation
over an authorization answer is the last place a no-op may be silent — and the
broadcast subscriber passes `absence_is_normal=True`, because a peer that never
cached that user is the ordinary case there and a warning per event teaches the
reader to ignore the one that matters.

**The workspace client answers three things, never two** (`django/workspaces.py`,
0.47.0). Granted, denied, and *could not be asked* — the third is
`WorkspaceLookupUnavailable`, which callers turn into 503. `require_capability`
used to log a `FunctionCallError` and return `None`, and the degrade path's
membership lookup swallowed the same exception, so a workspaces outage reached
the caller as a **denial**: a user locked out by an outage and a user correctly
refused got the same 403, and the consumer's `unavailable → 503` branch could
never fire. `require_capability` and `require_role` now default to
`strict=True`; `get_membership` keeps `strict=False` because it is a reader with
legitimate non-authorization callers, and any caller rendering its `None` as 403
must ask for strict.

### Silenced checks — `django/check_guard.py`

`SILENCED_SYSTEM_CHECKS` was a blanket line nothing in the fleet read: any
project could mute any library's security check with no signal to anybody.

| Name | What it is |
|---|---|
| `declare_security_critical(id, why)` | Returns the id, so the module constant IS the declaration — the marking lives with the check and cannot drift into a separate list |
| `SecurityCriticalError` / `SecurityCriticalWarning` | Override `is_silenced()`: the blanket setting does not apply to them |
| `STAPEL_SECURITY_CHECK_WAIVERS` | `{id: reason}`. The only route to quiet — per check, greppable, with a written reason, reported at every boot (W002). A blank reason waives nothing (E002); a waiver for a non-critical id is reported (W003) |
| `stapel_check_guard` | E001 a critical id silenced by the blanket route; W001 lists everything else that is muted |

Core marks its own: `cors.E001`, `auth_backends.E003`, `blacklist.W001`,
`mandate.E001`, `prodguard.E001`/`E002`.

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
`NetIntelProvider`), `W003` (the seam is depended on but never wired — fires
when `STAPEL_NETINTEL` is configured while `PROVIDER` is left at the default
`NullProvider`, or when captcha rules are keyed by a class no request can
ever carry; silent under DEBUG, and it names settings, never their values).
A broken or unwired provider degrades, it never blocks a deploy.

MaxMind kind derivation: Anonymous-IP flags (tor > vpn > hosting) → ASN
hosting list → org-name keyword heuristic → **`residential` only when the
Anonymous-IP database was consulted and did not list the address** →
otherwise `unknown` with `confidence=None`. Residential requires evidence: a
known ASN is not evidence of a residence, and treating it as one made every
unenumerated hosting provider come out as the most permissive class in every
consumer of the seam. `asn`/`asn_org`/`country` still travel with an
`unknown` profile, so "we know who routes it, not what it is" stays
distinguishable from "nothing is known".

The builtin `HOSTING_ASNS` list (AWS/Azure/GCP/Cloudflare/Fastly/…) is a
**heuristic fallback only and intentionally incomplete**; it can promote to
`datacenter` and can no longer demote to `residential`. The accurate source
of truth is the offline MaxMind **Anonymous-IP** database
(`MAXMIND_ANONYMOUS_DB`, `is_hosting_provider`), consulted first — and now
also the only thing that can assert `residential`. Without that mmdb the ASN
heuristic under-detects datacenter/VPN egress and everything it does not
promote stays `unknown`. AS15169 (Google's
main ASN) is deliberately excluded — it also carries consumer traffic;
AS396982 (Google Cloud) is the datacenter-only ASN. `manage.py
download_geolite` is a TODO (netintel package docstring). Consumers: captcha
challenge policy, OAuth region resolution (stapel-auth), rate limits,
analytics.

### Observability — `STAPEL_OBSERVABILITY` (`observability/`)

Structured logging, a metrics facade, an error-reporting seam and **trace
correlation through the comm envelope**
(docs/pending/data-storage-and-observability-v2.md §2). The framework/platform
border is the usual one: this package *emits* clean signals through seams;
Prometheus/Grafana/Loki/Sentry/Alertmanager *collect and display* them and are
a deployment's business, not a library's.

Public surface (all re-exported from `stapel_core.observability`):

- `metrics.counter/gauge/histogram/observe/timer(name, value, labels=…)` — a
  module instruments itself and never imports `prometheus_client`.
- `logging_config(service=…)` / `configure_logging(…)`, `JsonFormatter`,
  `TraceContextFilter` — one JSON object per record.
- `report_error(exc, context=…, tags=…)` / `report_message(…)`.
- `start_trace(...)` / `continue_trace(envelope)` / `current_trace()` /
  `trace_ids()`, plus `parse_traceparent` / `format_traceparent` for W3C
  interop and `sanitize_id` for ids read off the network.
- `TraceContextMiddleware` (`observability/middleware.py`).
- Health/readiness are **not re-implemented**: `health_check`,
  `readiness_probe`, `liveness_probe`, `prometheus_metrics`,
  `get_health_urls`, `register_dependency_check` and
  `register_metrics_exporter` are lazily re-exported from
  `stapel_core.django.monitoring.health`, so the observability surface is one
  import.

| Key | Default | Semantics | What it customizes |
|---|---|---|---|
| `METRICS_BACKEND` | `stapel_core.observability.backends.PrometheusMetricsBackend` | replace (dotted path/class/instance) | Where measurements land (`MetricsBackend`: `counter`/`gauge`/`histogram`/`expose`). Ships `Prometheus`, `Statsd`, `Logging`, `Noop` |
| `ERROR_REPORTER` | `stapel_core.observability.errors.NoopErrorReporter` | replace (dotted path/class/instance) | Where `report_error()` sends an exception (`ErrorReporter`: `capture_exception`/`capture_message`). Ships `Sentry`, `Logging`, `Noop` |
| `SERVICE_NAME` | `None` → `comm.service_name()` | replace | Name stamped on every record, metric and report |
| `LOG_FORMAT` / `LOG_LEVEL` | `"json"` / `"INFO"` | replace | `logging_config()` output shape and level |
| `LOG_STATIC_FIELDS` | `{}` | **merge** into every record | Deployment/region/image-tag fields |
| `LOG_INCLUDE_SOURCE` | `False` | replace | Adds `module`/`func`/`line` |
| `LOG_EXEMPT_LOGGERS` | `[]` | **merge** | Loggers `logging_config()` leaves entirely to the host |
| `REDACT_FIELDS` | password/token/secret/authorization/cookie/… | replace | Record fields blanked to `"***"` at the formatter |
| `METRIC_NAMESPACE` | `"stapel_"` | replace | Prefix on every metric name (matches `STAPEL_METRICS_PREFIX` on `/api/metrics/`) |
| `HISTOGRAM_BUCKETS` | HTTP-latency ladder (s) | replace | Default histogram buckets; per-call `buckets=` wins |
| `STATSD_HOST` / `STATSD_PORT` | `"127.0.0.1"` / `8125` | replace | `StatsdMetricsBackend` target |
| `REQUEST_ID_HEADER` / `TRACE_ID_HEADER` / `CORRELATION_ID_HEADER` | `X-Request-ID` / `X-Trace-Id` / `X-Correlation-Id` | replace | Headers `TraceContextMiddleware` reads and echoes |
| `TRUST_INCOMING_TRACE` | `True` | replace | Accept a caller-presented trace (what makes one trace span services). Incoming ids are always sanitized; turn off at an internet-facing edge that wants ids it minted |
| `ECHO_TRACE_HEADERS` | `True` | replace | Stamp the ids on the response |
| `REQUEST_METRICS` | `True` | replace | `http_requests_total` + `http_request_duration_seconds` from the middleware |

`METRICS_BACKEND` and `ERROR_REPORTER` are `import_strings` (implicitly
env-closed — they name the class the process runs, not data). The three header
names and `TRUST_INCOMING_TRACE` carry trust weight and are `no_env` for the
same reason `netintel.TRUSTED_PROXY_HEADER` is.

**Nothing in the facade raises into a caller.** A measurement is an observation
of the work, not part of it: a missing client library, an unreachable statsd,
a label set that contradicts an earlier registration and a reporter that
itself fails are each absorbed, logged once, and dropped. Instrumentation that
can take a request down fails exactly when the system is already unhappy.

Optional dependencies are guarded and *degrade with a name*: without
`prometheus-client` (`stapel-core[prometheus]`) the default backend still
constructs, reports `available = False`, records nothing, and is named by
check `W002`; without `sentry-sdk` (`stapel-core[sentry]`)
`SentryErrorReporter` does the same via `W003`. Never an `ImportError` at
request time.

System checks (W-level, registered by the `stapel_core.django` app, **all
gated on adoption** — a service with no `STAPEL_OBSERVABILITY` block is told
nothing, the `netintel.W003` rule): `W001` backend could not be built,
`W002` backend is unavailable, `W003` reporter could not be built / is the
no-op default while `SENTRY_DSN` is set, `W004` `TraceContextMiddleware` is in
no `MIDDLEWARE` so no request starts a trace.

#### Correlation — what no off-the-shelf APM can do here

A generic APM does not know about `stapel_core.comm`, so a request that fans
out into Actions and Functions across modules and services becomes a scatter
of unrelated log lines. Four ids fix that, and they ride in the **envelope**:

`Event` carries `trace_id` (the whole operation), `span_id` (this hop),
`correlation_id` (the *business* operation, which may outlive one trace) and
`causation_id` (the message that caused this one — what makes a fan-out a tree
rather than a bag). They are filled from the ambient trace context at
construction, so `emit()` inside a request inherits it with no call site
passing anything, and they are `compare=False` — two events are the same event
because of what they say, not because of which trace observed them.

The loop closes on the far side: `comm.deliver()` and
`BaseBusConsumerCommand` bind `continue_trace(event)` around the handler, so a
subscriber's own logs, metrics and emits join the operation that caused them —
including when the outbox runs the handler minutes later in another process,
where nothing about the originating request is otherwise in scope.
`Event.from_json` restores the ids explicitly rather than re-stamping with the
reader's trace, which is exactly the outbox relay's situation.

Adoption is three lines in a settings module:

```python
from stapel_core.observability import logging_config
LOGGING = logging_config(service="chat")
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "stapel_core.observability.middleware.TraceContextMiddleware",
    ...,
]
STAPEL_OBSERVABILITY = {
    "ERROR_REPORTER": "stapel_core.observability.errors.SentryErrorReporter",
}
```

Facade metrics need no wiring at all: `stapel_core.observability.exporter`
registers the backend's exposition into the `/api/metrics/` endpoint core
already serves, so a module's counter shows up on the scrape URL the
deployment already scrapes — no second endpoint, no second port.

### Event store — `STAPEL_EVENTSTORE` (`eventstore/`)

Append-only sink for high-volume streams that are written often, read as
aggregates, grow without bound, and stay out of band with business
transactions (LLM-call ledger, gateway audit, analytics, delivery logs — one
core primitive, not N bespoke tables). Facade API (root export
`stapel_core.eventstore`, lazy):

- `append(stream, payload, *, ts=None, project=None, task=None, container=None)`
  — buffered write. `append_batch(events)`, `flush()`.
- `query(stream, *, after=None, limit=100, time_range=None, filters=None,
  reverse=False) -> EventPage` — cursor read in `(ts, id)` order (id
  tie-break, so bursts never skip/repeat); `EventPage.cursor` (opaque
  `Cursor` token) feeds the next `after=`. `filters` match identity columns
  or payload keys. `reverse=True` walks newest-first (`(-ts, -id)`; `after`
  then advances into the past) — the journal read order.
- `anchor.anchor_page(stream, *, filters=None, anchor=None,
  direction="next", limit=100) -> AnchorPage` — the `AnchorPagination` wire
  contract (`{items, next_anchor, prev_anchor, has_next, has_prev, count}`,
  newest first, ISO-timestamp anchors, `next`/`prev`/`center`) served from a
  stream, so a journal that moves from a bespoke table into the store keeps
  its released HTTP shape byte-for-byte (first consumer:
  stapel-workspaces `GET <id>/audit`).
- `rollup(stream, *, group_by, sum_fields, time_range=None, filters=None,
  into=None) -> list[RollupRow]` — group-by (identity columns or payload
  keys) + sum-fields; `into=` upserts buckets into a rollup table (replace /
  recompute semantics). Concrete rollups are the consumer's business.
- `purge(stream, *, older_than=None, filters=None) -> int` — retention
  mechanism (`older_than`) **and** subject-scoped erasure (`filters`, same
  contract as `query`: identity columns or payload keys). At least one bound
  is required — `purge(stream)` would delete the whole stream, so it raises.
  An erasure needs no cut-off date: `purge(stream, filters={"subject_id":
  …})` forgets that subject's whole history and leaves everyone else's. A
  backend whose `purge` predates `filters` raises `PurgeFiltersUnsupported`
  rather than silently widening the erasure into a retention sweep
  (`purge_accepts_filters(backend)` is the signature check behind it);
  unfiltered retention still reaches such a backend.

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
| `AUDIT_STREAMS` | `[]` | replace | Streams `manage.py audit_trail <person>` reads for one person's cross-module history; empty = discover by the `audit`/`*.audit` naming convention (a deployment that ROUTES an audit stream to another backend lists it here — discovery cannot see across backends) |

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

### JWT credential seam — who may receive tokens (`django/jwt/`)

Three named choke points, and every path in the package goes through one of
them. This is the seam a security fix edits; a caller-side check is not a fix
here, because the next caller will not have it.

| Choke point | Owns | Refuses |
|---|---|---|
| `jwt_provider` (`django/jwt/provider.py`) | ALL minting: `create_tokens(user)`, `create_tokens_from_data(data)`, `refresh_access_token(token)` | a revoked jti, a user-level ban (`is_user_blacklisted`) — before the mint, never after |
| `revocation_cache()` (`core/revocation_store.py`) | the ONE key namespace all three revocation facts are written to, shared by every peer service | nothing itself — it is what makes the others reach across services (0.39.0; mechanism generalized into `core/fleet_cache.py` in 0.45.0) |
| `fleet_cache()` (`core/fleet_cache.py`) | the mechanism under every namespace that must be shared rather than per-service — revocation and verification grants today | nothing itself — it is what stops a per-service `KEY_PREFIX` turning fleet state into a per-service illusion (0.45.0) |
| `is_user_tombstoned()` (`django/jwt/tombstone.py`) | "this uid was deleted at the issuer" — consulted on BOTH halves of the gate: authentication (consumer mode) and re-mint (unconditionally) | a deleted account, for at least the refresh-token lifetime (0.40.0/0.41.0) |
| `load_user_by_uid` (`django/jwt/utils.py`) | the re-mint identity on EVERY refresh path | a **tombstoned** uid (0.41.0), a deleted user, an **inactive** user (0.38.0) |
| `get_or_create_user_from_jwt` (`django/jwt/utils.py`) | the principal every authentication path resolves — middleware, DRF class, `JWTAuthBackend`, channels | an unresolvable user, an **inactive** user (0.38.0) |
| `jwt_cookie_names()` (`django/jwt/utils.py`) | the ONE resolution of `JWT_COOKIE_NAME`/`JWT_REFRESH_COOKIE_NAME` — HTTP extractor, `set_jwt_cookies`, config loader, both logout views, the CSRF-exemption middleware, the published OpenAPI scheme, and the Channels handshake (0.44.2) | nothing itself — it is what stops one half of the stack setting a cookie the other half never reads |

**Only a token we signed can be revoked** (0.40.0). `blacklist_token()` used
to take its `jti` from an *unverified* decode, so anyone who could observe a
victim's token — any component that logs or forwards one — could mint an
unsigned JWT carrying that `jti` and POST it to the unauthenticated logout
endpoint, killing the victim's live session. The decode now verifies.

**A browser cannot set a header on `new WebSocket()`** (0.44.1). The Channels
handshake extractor (`django/jwt/channels.py`) read only the `Authorization`
header, the `Sec-WebSocket-Protocol` subprotocol and `?token=`. HTTP
authenticates with an httpOnly JWT **cookie**, and there was no cookie branch —
so every real browser handshake closed 4401, the client read that as a
permanent refusal, and the product polled instead. The socket was built,
mounted, proxied and smoke-tested; the smoke test sent a header a browser can
never send, so it proved nothing about the only path that matters. The cookie
is now the fourth channel, tried **last** (an explicit credential always
wins), with names resolved through `utils.jwt_cookie_names()` — the ONE
resolution the HTTP side uses, so the socket cannot read a cookie HTTP never
sets. The refresh cookie is honoured too, gated on `JWT_REFRESH_ALLOWED` and
re-minted through `load_user_by_uid`; the fresh token lands in
`scope["stapel_refreshed_access_token"]` because a handshake has no response
to set a cookie on.

**And a cookie on a socket is ambient authority, so it needs an origin
allowlist** (`django/jwt/ws_origin.py`, 0.44.1). The browser attaches the
cookie to a handshake started by *any* page, and WebSockets are protected by
neither the same-origin policy nor CORS. The cookie branch alone would have
been Cross-Site WebSocket Hijacking, so the guard ships in the same release
and **fails closed**: no allowlist means every cookie handshake is refused
(close 4403), because an empty allowlist is a misconfiguration, not a
wildcard. Declared as `STAPEL_WS_ALLOWED_ORIGINS`, with
`STAPEL_REALTIME["ALLOWED_ORIGINS"]` read as a fallback so a realtime host
declares its origins once. Only the cookie is gated — a header, subprotocol or
`?token=` cannot be produced by a page that has never seen the token, and
gating them would refuse every service-to-service and native client. Boot
gates: `stapel_core.jwt.E001` (security-critical, unsilenceable — cookie WS
auth reachable with no allowlist) and `stapel_core.jwt.E002` (an entry that
can never match an `Origin`). `stapel_chat.E014` states the same fact at its
layer and reads the same list, so the two verdicts agree by construction;
consumers should delegate to `ws_origin.websocket_origin_allowlist()` and
`ws_origin.cookie_websocket_auth_reachable()` rather than re-read settings.

**The sweep behind that fix** (`tests/test_ws_credential_sweep.py`, 0.44.2)
proves what the per-case tests cannot — the *absence* of a second door, which
is the half that let the original defect live for months. It asserts that
every source the extractor can report is classified ambient or not (so a fifth
credential channel cannot be added without deciding whether the origin gate
applies to it), that the handshake and the HTTP extractor agree on the cookie
name under a **renamed** setting (the default proves nothing — both halves used
to hardcode the same literal), and that no other module in the package either
resolves that name from settings or reads a credential off an ASGI scope.

**A cookie is a browser credential, session or JWT** (0.40.0).
`CsrfExemptAPIMiddleware` counted only the JWT cookie, so an `/api/` request
authenticated by Django's **session** cookie alone — which every browser that
ever logged in holds, because the JWT middleware calls `login()`, and which
DRF's `SessionAuthentication` accepts on its own — was blanket-exempted from
CSRF on every mutating endpoint. It now counts both; a request with no cookie
at all stays exempt, because there is nothing to forge.

**Minting from credentials happens in exactly one view.**
`JWTCookieLoginView` (`django/jwt/login_views.py`) is the admin login form —
its template is `admin/login.html` — and it is **staff-only, not
configurable**: `get_form_class()` resolves to Django's
`AdminAuthenticationForm` (lazily, so importing the module before
`django.setup()` still works) and `form_valid()` independently refuses a
non-staff user before `login()` and before `create_tokens()`. Both, because
either alone is a bypass: a subclass naming its own `authentication_form`
would defeat the first, and the first alone would let a permissive form
report success and strand the user. A deployment that needs a **non-admin**
cookie login writes a different view — a setting that can switch a staff gate
off is a staff gate that is off in whichever environment nobody audited.

**Refreshing re-reads the database, everywhere — by default.**
`jwt_provider.refresh_access_token(token)` uses `load_user_by_uid` unless the
caller passes something else; `None` still means "re-mint from the token's own
claims" but now has to be typed out. A refresh token lives
`JWT_REFRESH_TOKEN_LIFETIME` (7 days by default), so re-minting from its own
claims resurrects a staff role revoked in between (AS-2 REPLACE) and
re-credentials a closed account. Making the loader opt-in meant every consumer
call site was one forgotten argument away from that; from 0.39.0 the safe
behaviour is what you get for free, in core AND in every consumer.

**Revocation is fleet-wide, not per-service** (`core/revocation_store.py`,
0.39.0). Both blacklists used to write through `django.core.cache.cache`, and
Django builds the real key from the *deployment's* `KEY_PREFIX` — which every
service sets differently, on purpose. Sharing one Redis is not sharing a
namespace: `auth` wrote `auth:1:jwt_blacklist:<jti>` while `profiles` looked
for `stapel_profiles:1:jwt_blacklist:<jti>`, so a revoked token kept working
everywhere except where it was revoked. `revocation_cache()` borrows the
deployment's cache connection (same backend, same `LOCATION`, same `OPTIONS`)
with `KEY_PREFIX`/`VERSION` forced to fleet values, so every peer computes the
same key on every backend. In 0.45.0 that borrow-and-force step moved to
`core/fleet_cache.py` — verification grants turned out to have the identical
defect, and the answer to a second instance is the same mechanism, not a
second one. `revocation_store.py` keeps the revocation-specific half (which
namespace, which alias) and its public API is unchanged.

```python
# both optional; if you change either, change it in EVERY peer service
STAPEL_JWT_REVOCATION_CACHE = "default"                  # alias to borrow
STAPEL_JWT_REVOCATION_NAMESPACE = "stapel_revocation"    # the shared prefix
STAPEL_JWT_TOMBSTONE_TTL = 604800   # optional; clamped to JWT_REFRESH_TOKEN_LIFETIME
```

System checks (tag `stapel_blacklist`): `stapel_core.revocation.E001` — the
named alias is absent and there is no `default`, so bans are written nowhere;
`W004` — the alias is absent but `default` exists; `W003` (security-critical)
— a non-default namespace, which is legitimate for two fleets on one store and
is the original defect if it is one service's local opinion; `E002`
(security-critical) — a tombstone TTL shorter than the refresh lifetime. Plus the existing
`stapel_core.blacklist.W001` (fail-open hatch) and `W002` (LocMem, so
revocation is per-worker).

**Deletion is a fact, not a moderation action** (`django/jwt/tombstone.py`,
0.40.0). A consumer-mode verifier (`JWT_CREATE_USERS_FROM_TOKEN=True`) exists
to materialise a local row for a user it has never seen, and it cannot tell
that case from "a user I deleted" — both surface as `DoesNotExist`. So the
issuer publishes a **tombstone** keyed `user_deleted:<uid>` into the shared
revocation namespace, written by a `post_delete` receiver on
`AUTH_USER_MODEL` (connected in `CommonDjangoConfig.ready`) rather than by a
caller who must remember — cascades, `manage.py shell`, the admin and GDPR
erasure jobs are all covered. Consumer-mode verifiers consult it before
trusting a claim; issuer mode skips it there and pays nothing, because on the
authentication path the local database *is* the account.

**Both halves of the gate, not just authentication** (0.41.0). `load_user_by_uid`
— the re-mint loader every refresh path passes — consults the tombstone too,
and **unconditionally**, unlike the authentication check. A consumer-mode
service holds a shadow row for a user the issuer has since deleted, so the row
is still there and the query still finds it: without this, that service
re-minted a fresh access token for a deleted account. The token was refused at
authentication, but "harmless because of a check somewhere else" is the shape
that stops being true after one refactor, and a second path to a guarded action
that skips the choke point is this seam's entire failure history. It is not
gated on `JWT_CREATE_USERS_FROM_TOKEN` because a guard that reads a mode flag
has a config-shaped bypass, and the misconfiguration it closes —
`JWT_REFRESH_ALLOWED=True` on a consumer-mode service — happens precisely
because settings get copied between services. The cost is one cache read per
refresh (bounded by the access-token lifetime, not per request), and no new
failure mode: the refresh path already fails closed on an unreachable store via
the user-ban check.

Four key spaces, deliberately distinct, so the four questions never answer
each other's: `jwt_blacklist:<jti>` (is this token revoked), `user_blacklisted:<uid>`
(is this person banned), `user_deleted:<uid>` (is this account gone),
`user_deactivated:<uid>` (is this account suspended).

TTL derives from the deployment's own `JWT_REFRESH_TOKEN_LIFETIME`, so
lengthening refresh tokens lengthens tombstones automatically;
`STAPEL_JWT_TOMBSTONE_TTL` may raise it and is **clamped**, never obeyed,
below the refresh lifetime. `stapel_core.revocation.E002` refuses a boot that
configures it shorter — a tombstone that ends before the credential does is
not a trade-off, it is an unclosed hole with a number on it.

All four revocation reads fail **CLOSED** on an unreachable store, flipped
together by the single `STAPEL_BLACKLIST_FAIL_OPEN` hatch. For the tombstone
that means a consumer-mode service loses availability while its cache is
down, rather than a deleted person losing their deletion.

**Deactivation is a fact too, and it reaches the peers** (`django/jwt/deactivation.py`, 0.43.0).
Measured on a deployed fleet at 0.41.0: `is_active=False` at the issuer, same
unexpired token — the issuer answered 401 and every consumer-mode peer kept
answering 200. Two causes, either sufficient. The `is_active` claim was
replayed onto the local shadow row **before** the `is_active` gate read that
row, so a token minted while the account was live asserted `is_active: true`,
reactivated the row, and passed the check it had just satisfied — a gate a
credential can satisfy by asserting its own conclusion is not a gate. And
nothing published the change, so a peer would have learned of it only at the
next mint, or never.

Now: a `post_save` receiver on `AUTH_USER_MODEL` (connected in
`CommonDjangoConfig.ready`, like the tombstone) publishes `user_deactivated:<uid>`,
consumer-mode verifiers consult it before trusting any claim, and **the
`is_active` claim no longer moves the local column in either direction**. In
consumer mode that column now records only what *that* service decided
locally, which a bearer token may not overrule; fleet-wide lifecycle lives in
the key space. Issuer mode is unchanged and pays no cache read — its database
is the account.

**Reactivation lifts the record, and that is the deliberate difference from
the deletion tombstone.** `lift_tombstone` exists only for an operator who
deleted the wrong row, because every automatic reason to lift a tombstone is a
way for a token to undo a deletion. That operator now gets a `DropReport`
instead of `True`-if-it-did-not-raise (0.47.0): the person they are restoring
stays locked out fleet-wide if the lift found nothing, and until 0.47.0 nothing
said so. Suspending and restoring an account is an
ordinary, repeatable operation, so the same receiver deletes the key on
`is_active=True`. Do not copy the tombstone's no-automatic-lift rule here by
analogy.

Only the authoritative store publishes: the receiver returns early in consumer
mode, because a peer broadcasting its own shadow row's state could *lift* the
issuer's deactivation — the same failure, inverted. The one hole the receiver
cannot close is `User.objects.filter(...).update(is_active=False)`, which emits
no signal; call `deactivate_user(uid)` alongside it.

**Not yet closed** (0.40.0, listed so it is not rediscovered as news):
refresh tokens are neither **rotated** nor **bound to a tracked session row**,
so "log out everywhere" depends entirely on the blacklist and a stolen
refresh token is usable for its full lifetime by whoever holds it. That is a
dedicated wave, not a patch — it needs a session table, an issuing seam that
writes to it, and a migration path for tokens already in the wild. Also:
`JWT_CREATE_USERS_FROM_TOKEN=True` remains a staff-minting primitive by
design (default off) — the tombstone closes resurrection, not elevation; the
staff claim is still authoritative in that mode by construction.

### OAuth providers & audience verification (`oauth.py`)

`OAuthProvider` is the registry seam every stapel OAuth integration builds on: subclass it, implement `get_user_data`, `register_provider(MyProvider())` from your `AppConfig.ready()`.

Since 0.42.0 the seam also answers the question that decides whether a caller-supplied token may be trusted at all — **which OAuth client was this token minted for?**

```python
from stapel_core.oauth import OAuthClientConfig, check_audience, fetch_user_data

reason = check_audience(provider, access_token, config)   # None == accepted
if reason:                                                 # refuse, every time
    return None
return fetch_user_data(provider, access_token, config)
```

- **`OAuthClientConfig(client_id, client_secret, accepted_audiences)`** — this deployment's own client, plus every client ID a caller-supplied token may legitimately carry. `accepted_audiences` is a tuple because one project routinely owns several clients: Google issues separate Web / iOS / Android client IDs for one project, so a native app's token carries a different `aud` than the web app's and both are the same deployment. A single-value check would refuse every mobile sign-in.
- **`OAuthProvider.verifies_audience`** (`False`) and **`OAuthProvider.verify_audience(access_token, config)`** (returns `False`) — the hook. **Both defaults refuse**, which is the only safe answer for a provider nobody has taught to introspect: an access token is a bearer credential scoped to the client it was issued to, so a token minted for *somebody else's* app against a victim's provider account would otherwise resolve to that victim's identity. Override the method with a real mechanism (the provider's tokeninfo/introspection endpoint) **and** set the flag; setting the flag without the mechanism is the one way to make this seam lie.
- **`check_audience`** returns `None` (accepted) or one of `AUDIENCE_UNVERIFIABLE` / `AUDIENCE_UNPINNED` / `AUDIENCE_MISMATCH` — all refusals. An exception inside a provider's verifier is logged and becomes `AUDIENCE_UNVERIFIABLE`: a verification step that fails open is not a verification step, and a provider outage must not open a door.
- **`fetch_user_data(provider, access_token, config=None)`** — call `get_user_data` through this. `get_user_data` gained an optional `config` parameter in 0.42.0; providers written against the old one-argument signature are detected and called with one argument, so third-party providers keep working untouched.

Only tokens this deployment did **not** mint need the check. A token obtained through your own authorization-code exchange (`exchange_code`, i.e. the `/authorize/` → `/callback/` flow) is yours by construction; a token handed to you in a request body is not.

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
| `REQUIRE_NETWORK_BINDING` | `True` | replace | Refuses HTTP calls with tokens that carry no network binding. `False` accepts an unbound token from anywhere that can reach the door |
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
| `STEP_UP` | `{}` → `{"ENFORCE": True, "LEVELS": ["high"], "SCOPE": "sensitive", "MAX_AGE": 900}` | merge over defaults | Step-up on HIGH admin operations (AS-6, Q8a — on by default). A gated op needs a fresh `stapel_core.verification` grant for `SCOPE` on top of the mandate; enforced in `StapelModelAdmin`. `ENFORCE=False` disables |
| `AUDIT_SINK` | `access.audit.eventstore_sink` | replace (dotted) | Where access events (`access.dac_escalation` / `access.step_up_denied`) land: `callable(stream, payload, *, project, container)`, gateway-shaped. Default appends to the eventstore |
| `AUDIT_STREAM` | `"audit"` | replace | Eventstore stream for access audit lines (env-readable) |
| `NOTIFY` | `None` | replace (dotted) | Optional alerting shim `callable(event, payload)` after the sink (e.g. push to notifications/SIEM); best-effort |

All keys `no_env` except `AUDIT_STREAM`. Feature is opt-in by the first role:
with no roles resolvable the backends behave like today's Django (checks
`stapel_core.access.E00x/W00x` flag misconfiguration).

**Step-up on HIGH (AS-6, §3.8).** `delete` in the standard preset (and any
operation declared HIGH) requires a fresh verification grant — the mandate
says a role *may* act, step-up says it was re-proven recently. The grant store
is `stapel_core.verification`'s, i.e. the *same* one stapel-auth's step-up flow
(and the legacy `/totp/step-up/` bridge, scope `sensitive`/max_age `900`) write
to (no auth hook). Only `StapelModelAdmin` enforces it (permission-layer deny
closes every mutation path; the add/change/delete views return an educational
403 — core has no web verification flow). **Degradation**: self-disables when
no verification factor is registered (a grant would be unobtainable) — `W005`
flags it; enforcement resumes once stapel-auth (or `register_factor`) is
present.

**The three channels the gate reads (0.45.0).** Until 0.44.2 there was
effectively one, and no admin browser could fill it: `has_fresh_step_up`
called `has_grant(user, scope)` with no `token=`, so the `X-Verification-Token`
fallback was unreachable from this gate (and a browser form POST cannot set a
header anyway), leaving a grant in *this process's* `django.core.cache.cache` —
namespaced per service. So the property this section used to claim, "completing
step-up anywhere satisfies the admin gate", held only inside one `KEY_PREFIX`,
and the 403 pointed operators at a cross-service flow that could not satisfy
the check. With `ENFORCE=True` by default, any process that registered a factor
but did not mint the grant refused every HIGH operation with instructions that
could not be followed. What the gate reads now, in order:

1. **The fleet-wide grant store** — `GRANT_NAMESPACE` above, keyed by the
   fleet-stable `user.pk`. This is what finally makes "completing step-up
   anywhere in the fleet counts here" true rather than documented.
2. **The session** — `request.session["stapel_step_up"] = {scope: granted_at}`,
   written by `record_step_up_in_session(request)`. The session cookie is the
   one credential the admin browser carries on every subsequent form POST, and
   this channel keeps working where the cache is not shared. Freshness is
   judged against the *current* `MAX_AGE`, so shortening the policy tightens
   live sessions immediately.
3. **A presented verification token** — `X-Verification-Token` (API clients) or
   the `verification_token` query parameter / form field (a browser returning
   from an auth-service step-up redirect). A token accepted at an admin *view*
   is pinned onto the session by `adopt_step_up_token`, because the confirm
   form that follows carries neither the query string nor a header. Pinning
   happens only in views: `step_up_blocks` stays side-effect free, since
   `has_*_permission` calls it during rendering.

Session-recorded proofs are bounded by `MAX_AGE` (900s by default) but are
**not** reached by `revoke_grants()`, which deletes from the shared store —
flushing the session (or the user's whole session store) is what ends one
early. Nor is a presented token: `revoke_grants()` returns a `DropReport` per
scope (0.46.0) and those reports describe grants only; killing a token takes
`drop_verification_token(token)`.

The `dac_escalation` / `step_up_denied` signals are forwarded to the eventstore
(`AUDIT_SINK`) + `NOTIFY` shim, **best-effort** (a sink failure never breaks
`has_perm`).

Audit surface: `manage.py access_report [--json]` — role × model × operation
matrix, DAC grants above mandate, undeclared models, and a `step_up` section
(gated models, grant-uptake aggregate). Role *assignment* transport (JWT claim
`staff_roles`, `StaffRole` in stapel-auth) is AS-2; admin visibility built on
the same declarations is AS-3.

#### Migrating an existing project to the mandate

**Day 1 is a no-op.** Upgrading core/auth changes nothing until you opt in:
`MandateBackend` grants nothing while no roles resolve, `@access` decorators
are passive class attributes (no migrations), the only schema change is the
additive `staff_roles` JSONField on `AbstractStapelUser`, and step-up
self-disables where no verification factor exists (`W005`). The legacy flat
`Staff` group (`ensure_staff_group_permissions`, `staff_group` command) keeps
working — it is deprecated by documentation, not by code.

Enable in five independent, reversible steps (live reference:
stapel-example-monolith — settings, `seed_demo_staff`, smoke matrix in
`svc-app/app/tests/test_admin_suite.py`):

1. **Backends** — swap the single `ModelBackend` for the
   MandateBackend + AuditedModelBackend pair (drop-in: existing manual grants
   keep working, escalations above the mandate become audited).
2. **Roles** — define custom roles in `STAPEL_ACCESS["ROLES"]` (same deploy
   config in every service; scope keys are **app labels** — `"billing"`, not
   package names); assign in the auth service only. A service hosting
   stapel-auth locally should put `stapel_auth.staff_roles.assignment_roles`
   first in `ROLE_SOURCES` so a revocation lands on the very next request.
3. **Declarations** — library models ship decorated (AS-5); your own
   business models usually need nothing (undecorated = `@access.standard`).
   Decorate the `sensitive`/`ops`/`secret` exceptions; host-override via the
   `MODELS` registries.
4. **`StapelModelAdmin`** — change the base class of your ModelAdmins to get
   ops read-only (even for a superuser), secret masking, and the step-up
   gate. A bare `ModelAdmin` keeps mandate *visibility* enforcement but does
   NOT gate step-up — a documented trade-off.
5. **Step-up** — already on by default (`LEVELS=["high"]`); it activates by
   itself once stapel-auth (or a host `register_factor`) is present. Tune or
   disable via `STAPEL_ACCESS["STEP_UP"]`.

AS-2 sync note for local staff accounts: `is_staff`/`is_superuser` keep the
legacy *upgrade-only* shadow-sync (locally promoted admins never lose the
flag from a token), while `staff_roles` is **replaced** from the claim on
every authentication (revocation must land — A3). A migration script must
therefore set `is_staff` first, then assign roles (`assign_staff_role`
refuses non-staff targets). Exit path from the legacy `Staff` group: assign
equivalent roles → `access_report` shows what the group still grants above
the mandate → empty the group → optionally `STRICT=True` to make the mandate
a ceiling. The `stapel_access`/`stapel_admin`/`stapel_nav` system checks are
the misconfiguration diagnostics (W001 backend missing, W002 unaudited DAC,
E003 STRICT unenforceable, W003/admin.W001 wrong app_label in a `MODELS`
key, E004/W005 step-up, nav.E001/E002 malformed registries).

### GDPR providers (`gdpr/`)

Subclass `GDPRProvider` (define `section`, implement `export` / `delete` /
`anonymize`) and either register with `gdpr_registry` (monolith) or ship a
management command subclassing `GDPRServiceConsumerCommand` with
`gdpr_service_name` matching an entry in `GDPR_COLLECTING_SERVICES`
(microservices).

### Data owners — `register_gdpr_owner` (`gdpr/owners.py`)

The stapel-gdpr 0.5.0 erasure protocol, implemented once. A library that
holds rows about a subject subscribes from its `AppConfig.ready()`:

```python
from stapel_core.gdpr import register_gdpr_owner

register_gdpr_owner(
    "recordings",                                  # its GDPRProvider.section
    ["account", "workspace", "meeting", "recording"],
    erase_subject,                                 # the library's own code
)
```

`erase(subject_type, subject_key, workspace_id) -> counts | None` is the
only part that is the library's: idempotent, counting what it removed,
`None` when the key names nothing of its own. Everything else is protocol
and comes from here:

- `gdpr.erasure.requested` → erase, then `gdpr.section.erased` with
  `counts` and a deterministic `receipt_id`
  (`<owner>:<subject_type>:<subject_key>:<correlation_id>`, so a redelivery
  mints the same receipt), **emitted inside the erase's transaction** — the
  receipt leaves iff the erasure committed.
- a subject type the owner does not claim → ignored silently (the
  orchestrator created no part for it, so a receipt would teach it nothing);
  a malformed payload → logged and dropped, never retried forever; a key
  `erase` cannot parse (`TypeError`/`ValueError`/`ValidationError`) → logged
  and **never receipted**. Any other exception propagates for redelivery.
- `gdpr.owner.probe` → `gdpr.owner.alive {owner, subject_types}` from the
  same module, which is the whole point of the probe: the answer is evidence
  that the erasure subscriber is *consumed*, not that a container is running.
  It reports the types registered here, not the host's declaration.
- `user.deleted` (the pre-0.5.0 account signal, `legacy_user_deleted=True`
  by default, and only when the owner claims `account`) → the same
  `erase("account", …)`, so the two paths cannot drift.

`pseudonymize(value, prefix="erased:")` is the fleet's one keyed-HMAC funnel
(HMAC-SHA256 under `SECRET_KEY`, 32 hex, `erased:`-prefixed and therefore
idempotent). A ledger-carrying owner erases the ids that NAME the person and
keeps the economics — the bill is the product's record, without the person.
A `SECRET_KEY` rotation splits pseudonyms; accepted fleet-wide.

**New owner libraries MUST use this helper** — not a copy of it. The nine
libraries that predate it (workspaces, profile, notifications, recordings,
agent, docs, media/cdn, billing, translations) carry the code verbatim and
migrate to `register_gdpr_owner` on their next minor: the migration is
deleting their `actions.py` GDPR handlers and their `_receipt_id` /
`_emit_receipt` / `pseudonymize` copies, and calling this from `ready()`.
Their core floor bumps to 0.35.0 when they do, which is why it is their
next minor rather than this release.

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

### Copy-seam field partition — `FieldSpec` (`django/fieldspec.py`)

For a seam that materializes one row from another (series master →
occurrence, template → instance, draft → published). Declare on the model
which side of the seam every field falls on:

```python
FIELD_SPEC = FieldSpec(
    copy=("access_level", "admit_required", "pin_code"),   # carried verbatim
    recompute=("id", "code", "title", "created_by"),       # derived by the seam
    never=("created_at", "started_at"),                    # left at the default
)
```

`FIELD_SPEC.values(source)` builds the `copy` half as a dict (drop it into
`objects.create(...)` / `get_or_create(defaults=...)`) and validates the
partition first: every concrete field must appear in **exactly one** list, or
`FieldSpecError` names the offenders — unassigned fields, names that are not
fields, and names in two lists. `FIELD_SPEC.validate(Model)` is the same check
for a test.

Why: a hand-written "fields to carry" list is right exactly once, and the next
field added to the model is silently not carried. In a real product this lost
two of four settings fields on a meeting room, and both losses inverted the
host's intent (an "open" series slammed the door on the first join; a
PIN-protected series materialized rooms with no PIN). Adding a field now reds
until the author says which of the three it is.

**Boundary:** this enforces that a decision was made, not that it was right —
a field wrongly classified `never` passes green. What it removes is the field
nobody ever classified.

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
| `rebuild_projection <name> [--check] [--batch-size N]` | Re-derive a comm Projection read-model from its owner's `source_of_truth` Function (batched, all-or-nothing, progress); `--check` compares row counts without writing |
| `generate_flow_docs --out DIR [--lang X] [--llm] [--llm-cache FILE]` | Render flow markdown + `flows.json`; `--lang` resolves i18n keys, `--llm` machine-translates missing keys (content-hash cached) |
| `generate_error_keys --out FILE` | Emit `errors.json` (the error-key registry: `{code, status, params, remediation, en}`) — the backend codegen artifact the frontend error bundle is generated from |
| `generate_project_docs --out DIR [--languages …] [--llm]` | Bilingual flow doc trees, one per project language (`STAPEL_I18N["LOCALES"]`) |
| `translate_catalogs --domain D --lang X [--seed F] [--llm] [--app L \| --out DIR] [--approve … \| --approve-all]` | Generate/refresh `translations/D.X.json` + `.state.json` provenance (seed → translator seam, byte-stable, content-hash cached); the target dir must be one the catalog loader reads, else refused |
| `check_translation_catalogs --domain D [--languages …] [--app L \| --out DIR] [--strict]` | CI gate: catalogs cover the canon, are fresh, preserve `{params}` (E); counts unreviewed — anything no human approved (W) |
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

**Boot gates (`django/boot.py`).** Django runs system checks for management
commands and `runserver` and **none at all** for `gunicorn
config.wsgi:application` — which is how every generated project boots, so the
E-gates guarded dev and CI and possibly production not at all.
`BootGateMiddleware` sits at index 0 of `COMMON_MIDDLEWARE`: its `__init__`
runs an allowlist of settings-only, DB-free check tags and raises
`ImproperlyConfigured` with **every** finding's id, message and hint verbatim,
then raises `MiddlewareNotUsed` so Django unhooks it and the per-request cost
is zero. `get_wsgi_application()` builds a `WSGIHandler`, whose `__init__`
calls `load_middleware()`, so the refusal lands at worker boot, before the
first request — the same moment `manage.py` refuses today. (`AppConfig.ready()`
is deliberately not used: it would crash `manage.py check` itself, so the tool
whose job is printing the diagnosis could never print it.)

| Setting | Default | Semantics |
|---|---|---|
| `STAPEL_BOOT_GATES` | `"enforce"` | `enforce` refuses the worker \| `warn` logs the same causes and serves \| `off` does not run the checks. Anything unrecognised means `enforce` — a typo must not open a gate. W-checked (`stapel_core.boot.W001`) whenever it is not `enforce`. |

`BOOT_GATE_TAGS` is an explicit allowlist, not "everything registered":
`stapel_auth_backends`, `stapel_cors`, `stapel_conf`, `stapel_comm`,
`stapel_bus`, `stapel_captcha`. DB-touching and
URLconf-resolving checks stay in `stapel_preflight` — a boot gate that needs
the database up is a liveness probe, not a config gate. A project with a
hand-rolled `MIDDLEWARE` that never picked up the middleware gets
`stapel_core.boot.W002`.

`stapel_config` is deliberately absent: it resolves manifest-required keys out
of `os.environ` alone (so a deployment whose secret arrives as
`DJANGO_SECRET_KEY`, with a valid `settings.SECRET_KEY`, is refused) and finds
its `CONFIG.MD` by walking up from `Path.cwd()` (so the verdict depends on the
launch directory). It stays registered for `manage.py check` and
`stapel_preflight`; it can rejoin once required keys are resolved against the
settings the process actually uses and manifest discovery is explicit.

### Is a comm seam wired? (`comm.function_unreachable_reason`)

`function_unreachable_reason(name) -> str | None` answers "can `call(name)`
reach this function in *this* deployment", asked of the configured transport
branch for branch: `inprocess` needs a provider in this process, `http` needs a
matching `FUNCTION_ROUTES` prefix **and ignores the registry** (because
`call()` does), `nats` and a dotted-path custom transport are wired by
construction, and anything else is a transport `call()` cannot dispatch at all.
Any check asking "is module X wired" must go through this. Reading
`FUNCTION_ROUTES` directly is the recurring bug it exists to end: that table is
http-only, so under NATS — where the subject *is* the function name — it
reported correctly wired fleets as unwired (`stapel_core.cdn.E002`, and twice
in stapel-workspaces before that). It is never a liveness probe: settings and
registry only, so a wired-but-down provider is still the runtime's problem.

### Walking a repo's own sources (`stapel_core.testing`)

`iter_own_sources(root, suffix=".py")` and the separately exposed predicate
`is_foreign_source(path, root)`. Gates that walk a source tree must not read
an installed sibling library and report it as this repo's violation — a gate
accusing the wrong file is worse than no gate. Foreignness is decided by
**marker, never by name-list**: a virtualenv is a directory containing
`pyvenv.cfg` whatever it is called, a `site-packages` component is installed
or vendored code, and `build`/`dist` are excluded only when they carry
packaging markers (`build/lib`, `*.egg-info`, `*.dist-info`) so a source
directory legitimately named `build` is not silently skipped. `__pycache__`,
`.git`, `node_modules`, `.tox` and `.mypy_cache` are never sources. The
predicate is exposed separately on purpose: in CI there is no in-repo venv,
so the exclusion must be assertable on synthetic paths or it is untested
exactly where it matters.

### URL mounting — `STAPEL_MOUNTS` (`django/mounts.py`)

Where modules live in *this* deployment, as a merge-over-builtins registry —
cross-module URL targets are **derived** from it, never hardcoded
root-relative. Two mount kinds: **local** (in this URLconf; resolved with
`reverse()` via the mount's URL namespace, so include-prefix mounting and
`SCRIPT_NAME`/`FORCE_SCRIPT_NAME` both work) and **external** (a sibling
service behind the same proxy; script-prefix + declared path prefix).

```python
# builtins: admin (local, namespace "admin"),
#           auth (external at f"{STAPEL_AUTH_SERVICE_PREFIX}/" when non-empty;
#                 default "auth" = historical microservices layout)
STAPEL_MOUNTS = {
    "auth": {"prefix": "sso/", "external": True},  # move the auth service
    "auth": None,                                   # monolith: no auth service
    "admin": {"prefix": "backoffice/admin/", "namespace": "admin"},
}
# derived (lazy, mount/script-prefix aware):
#   LOGIN_URL = LOGOUT_REDIRECT_URL = lazy_admin_login_url()   (the default)
#   admin_login_url() / admin_index_url() / mount_path(key, suffix)
#   mount_reverse(key, name)  — reverse inside a local mount's namespace
```

With default settings the derived `LOGIN_URL` evaluates to the historical
`"/auth/admin/login/"` — existing deploys are unchanged. A monolith sets
`STAPEL_AUTH_SERVICE_PREFIX = ""` and login derives to `reverse("admin:login")`,
which follows any prefix the project is mounted under. `AdminLoginRedirectMiddleware`,
`JWTCookieLoginView`, the admin/swagger service navigation and
`setup_centralized_admin_login()` all build their targets through this
mechanism. Mount labels (`name=`) are the feed for future `NAV_LINKS`.

System checks (tag `stapel_mounts`, `django/checks.py`):
`stapel_core.mounts.E001/E002` — `LOGIN_URL` / `LOGOUT_REDIRECT_URL` /
`LOGIN_REDIRECT_URL` pointing at a path this URLconf cannot `resolve()` (and
that matches no declared external mount) fails `manage.py check` at deploy
time instead of 404-ing users after redirect; `E003` — malformed
`STAPEL_MOUNTS`; `W001` — Django's untouched stock defaults
(`/accounts/login|profile/`) that this URLconf does not serve.

**Module convention:** a stapel module never emits an absolute URL path —
only `reverse()` / URL names / this registry. Settings that hold URL targets
should be URL names (`LOGIN_REDIRECT_URL = "admin:index"`) or lazy
derivations, so the module works at the domain root, under a service prefix,
and in a monolith mounted under any sub-path.

### Cross-service navigation — `STAPEL_SERVICES` + `NAV_LINKS` (`django/nav.py`)

The admin/Swagger "Services" menu is driven by two deploy-config registries
(admin-suite AS-4) — no service list or tool/monitoring link is hardcoded in
the framework.

- **`STAPEL_SERVICES`** — the sibling services of this deployment, an env-JSON
  (12-factor, read by both Python and the non-Django agent service), or a
  Django-setting list of the same shape. Written by the project generators
  (`stapel-create-project` seeds it, `stapel-new-service` appends a row — the
  same discipline as `STAPEL_BUS_ROUTES`). A monolith leaves it unset: one
  implicit service is derived from `URL_PREFIX` / `SERVICE_NAME` and the "All
  Services" section collapses.

  ```bash
  STAPEL_SERVICES='[{"name":"Auth","prefix":"auth"},{"name":"Billing","prefix":"billing"}]'
  ```

- **`STAPEL_ADMIN["NAV_LINKS"]`** — extra tool/monitoring/dashboard links, a
  two-channel merge-registry. Channel 1: a module registers its own dashboard
  in `AppConfig.ready()` (re-exported from `stapel_core.django.admin`).
  Channel 2: the project adds/patches/removes via the setting (partial dict
  patches a code link, full dict adds one, `None` removes). Sections
  (`tools`, `monitoring`, `dashboards`) are fixed by the mechanism; contents
  are policy. The framework ships **no** monitoring links.

  ```python
  # channel 1 — module code (ready()):
  from stapel_core.django.admin import register_nav_link
  register_nav_link("translate.dashboard", section="dashboards",
                    title="Translator Dashboard", url="/translate/dashboard/",
                    requires="staff",           # staff | superuser | low/mid/high
                    service_dashboard=True)     # see below

  # channel 2 — project settings (merge over code):
  STAPEL_ADMIN = {"NAV_LINKS": {
      "monitoring.grafana": {"section": "monitoring", "title": "Grafana",
                              "url": "/monitoring/grafana/", "external": True},
      "translate.dashboard": None,              # disable the built-in link
  }}
  ```

Every link is filtered by the viewer's admissibility (`requires`; the target
keeps its own perimeter — nginx `auth_request`, `IsStaffUserForSwagger`), and
the **Swagger links respect the introspection env-gate** — they render only
when this deployment mounts the schema (`get_dev_urls` mounts `/swagger/` for
`DJANGO_ENV in {local, dev}`). Both the admin `base_site.html` and the Swagger
UI inject render from these registries via the `stapel_services` context
processor. System checks (tag `stapel_nav`, `django/nav_checks.py`):
`stapel_core.nav.E001` malformed `STAPEL_SERVICES`, `E002` malformed
`STAPEL_ADMIN["NAV_LINKS"]` — the render layer fails soft (never 500s), the
check surfaces the misconfiguration at deploy time.

**`current_dashboard_url` selection (§2 arbitration).** A `dashboards`/`tools`
link declares itself *the* current service's dashboard by setting
`service_dashboard=True` (`register_nav_link(..., service_dashboard=True)` or
the `NAV_LINKS` overlay). `current_dashboard_url(user)` picks the first
admissible flagged link, in registry order — explicit and deterministic, no
guessing from the URL shape. Only when **no** link carries the flag does it
fall back to the legacy heuristic (kept for backward compatibility with
pre-flag registrations): the first admissible `dashboards`/`tools` link whose
URL falls inside the current service's `URL_PREFIX` (a monolith with no
prefix accepts any local link). At most one link should carry the flag per
deployment; `stapel_core.nav.W003` warns (does not block) when more than one
does — the first one in registry order still wins, the rest are ignored.

### Adoption checks — silence is the error (`django/adoption_checks.py`)

A third genre of system check, next to the config checks (`stapel_nav`) and
the topology checks (`stapel_mounts`). Config and topology checks ask "is this
setting well-formed / is this mount where it belongs". An **adoption** check
asks the question `stapel-tools`' `adoption_lint` (ADO001) asks from outside
the process, from inside it:

> the project switched an axis on — did the code the axis affects actually
> take a position on it?

Same three parts every time, and they are the genre:
**derivable premise → derivable obligation → an explicit waiver instead of
silence.** The waiver is what keeps the check alive: a check that demands one
particular answer gets silenced wholesale the first time a product legitimately
needs the other one.

**The first one (tag `stapel_adoption`) — the anonymous axis.** Premise:
`stapel-auth`'s `AUTH_ANONYMOUS` is on, so guest sessions exist and a guest is
`request.user.is_authenticated`. Obligation: a view whose *whole* gate is a
bare `IsAuthenticated` therefore admits guests, and nothing in its source says
whether that was meant. Waiver: any of three explicit answers.

```python
from stapel_core.django.api.permissions import (
    ANONYMOUS_ALLOWED, ANONYMOUS_DENIED, IsNotAnonymousUser,
)

class CreateRoomView(APIView):                 # 1. keep guests out, enforced
    permission_classes = [IsAuthenticated, IsNotAnonymousUser]

class RoomInfoView(APIView):                   # 2. a more specific gate
    permission_classes = [IsAuthenticated, HasRoomAccess]

class JoinRoomView(APIView):                   # 3. guests are the product
    permission_classes = [IsAuthenticated]
    stapel_anonymous_access = ANONYMOUS_ALLOWED
```

Only the view that says nothing is red. `stapel_anonymous_access` takes exactly
`ANONYMOUS_ALLOWED` / `ANONYMOUS_DENIED` — a `stapel_`-prefixed name nothing in
Django or DRF carries (it cannot appear by accident) with a closed vocabulary
(a misspelled value is reported, never read as a declaration).

| id | level | meaning |
|---|---|---|
| `stapel_core.adoption.E001` | Error | a view **in this project's own source** gates on bare `IsAuthenticated` with no stance declared anywhere in its MRO |
| `stapel_core.adoption.E002` | Error | `stapel_anonymous_access` is set to something that is not a stance |
| `stapel_core.adoption.W001` | Warning | `DEFAULT_PERMISSION_CLASSES` is *itself* bare `IsAuthenticated` — reported once, at the setting, not once per view |
| `stapel_core.adoption.W002` | Warning | the same silence, but in a view that arrived in an installed `stapel_*` wheel |

Two deliberate asymmetries, both about keeping the check un-mutable:

- **views that never wrote a `permission_classes` line are not reported.** They
  inherit the project default; charging each of them for a decision made in
  `settings.py` is a flood, so W001 reports the decision once where it lives.
- **level follows who can act.** E-level findings become deploy blockers via
  `stapel_preflight`; blocking a deploy on a file that lives in someone else's
  wheel is how a whole tag ends up in `SILENCED_SYSTEM_CHECKS`. Library-side
  silence is the same finding at W-level (W002 names the package), to be fixed
  at the modules' release cadence.

Does **not** catch: authorization done inside the view body (report it with
`ANONYMOUS_DENIED`, which also makes it discoverable from the class header);
whether an `ANONYMOUS_ALLOWED` view is *correct* (it is a declaration of
intent, not a proof); non-DRF views gated by `login_required`, which has the
same guest ambiguity and is invisible here.

**Shared surface survey (`django/urlsurvey.py`).** Every check that reasons
about the *actual HTTP surface* — §37 mount containment and this one — walks
the resolver through the same primitives (`iter_surface()`, `iter_url_patterns`,
`path_segments`, `callback_owner_app_label`). Written once they are a
mechanism; the `stapel_mounts` private names re-export from there unchanged.

## Anti-patterns

- **Do not sync an account-lifecycle flag from a token claim.** A token asserts the state of the moment it was minted for the rest of its life, so a claim that can write `is_active` can reactivate a row and then satisfy the gate that reads it. Lifecycle travels in the revocation namespace; the local column is a local decision.
- **Do not resolve an identity from a token you did not mint without checking its audience.** `OAuthProvider.get_user_data` answers "who is the holder of this token", never "may this token speak for that person here". Run `check_audience` first for any caller-supplied token, and leave `verifies_audience = False` on a provider you cannot actually introspect — a provider that looks verified but is not is worse than one that plainly refuses.
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
- **Do not leave a declared `emit_*` helper uncalled.** A schema'd
  `events.emit_foo` nobody in the package ever calls is a contract the
  consumer waits on forever (EMIT005); wire it or delete it, or suppress
  with a named reason if it is genuinely triggered only from outside the
  repo.
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
- **A best-effort `try/except` around a network call is only correct paired
  with two other things**, never on its own
  (docs/pending/env-address-class-v2.md §3.6, motivated by meettoday's
  LiveKit twirp calls — host-kick and room-PIN wrapped every failure in
  `try/except` + `logger.warning` and then silently did nothing in
  production for as long as LiveKit was unreachable, with no signal anywhere
  an operator would look): (a) `logger.error`, not `.warning` — a warning
  that fires for days in production is invisible by construction, and (b) a
  `register_dependency_check(name, probe, critical=...)`
  (`django/monitoring/health.py`, next to `register_metrics_exporter`) on the
  same probe, so the first failed call lights up `checks{}` on
  `/api/health/` and `stapel_dependency_up{dependency=...}` on
  `/api/metrics/` instead of nowhere. `critical=False` (the default) keeps
  the process at HTTP 200/`degraded` — a downed non-essential dependency must
  not take the rest of the product's surface down with it (the same blast-
  radius argument as the nginx upstream gate, §2 of that document);
  `critical=True` is for a dependency this process cannot serve its purpose
  without, and flips readiness to 503 — but only on a **determined** failure.
- **Do not answer a question you could not ask.** A dependency probe has
  three answers: `True`, `False`, and `None` for "I could not ask". Returning
  a truthy sentinel for the third one is the defect that let a stand run
  twelve hours on an unmigrated schema while reporting healthy — `ok =
  bool(probe())` coerced "unknown" to "ok". An undetermined dependency is
  rendered as `"unknown"` in `checks{}` (distinct from `"error"`), **omits**
  its `stapel_dependency_up` sample rather than dropping it to `0` (a series
  falling to zero because nobody could ask is a false verdict an alert would
  fire on) while `stapel_dependency_probe_ok` goes to `0` so the silence is
  itself alertable, and does **not** take the process out of rotation: an
  inability to ask is not proof the dependency is down, and every replica
  loses the same probe at the same instant.
- **Do not supply a container-shaped setting from a bare environment
  variable.** `AppSettings` refuses it (`ImproperlyConfigured`, plus
  `stapel_core.conf.E002` at `manage.py check` time) for every key whose
  declared default is a `list`/`tuple`/`set`/`dict`. `DATA_OWNERS=auth,profiles`
  read as a string iterates character by character into a dozen owners named
  `a`, `u`, `t`, `h`, `,` — each of them a `str`, so every downstream type
  check passes and GDPR erasure gets certified against nonsense. The value's
  home is the `STAPEL_<MOD>` dict in the settings module.
- **Do not read `getattr(settings, ...)` ad hoc in a stapel package** — expose
  an `AppSettings` namespace so keys, defaults and dotted-path seams stay
  discoverable.
- **Do not hardcode root-relative URL paths** (`"/auth/admin/login/"`,
  `redirect("/admin/")`, `build_absolute_uri("/auth/api/...")`). Every such
  string silently assumes the module sits at the domain root and 404s the
  moment the deployment mounts the project under a prefix. Use `reverse()`,
  URL names in settings, or the mount registry (`django/mounts.py`).
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
