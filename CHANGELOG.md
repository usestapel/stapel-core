# Changelog

## [0.50.0] — 2026-08-28

### 0.49.0 counted the DLQ into a process nothing could scrape

The counter was right and unreachable. A consumer, an outbox worker and the
function server are exactly the processes that park events — and none of them
serves HTTP, so `/api/metrics/` does not exist for them and no scrape config
can point at them. `bus_dlq_total` incremented where nothing could read it,
which is monitoring that cannot report: indistinguishable from healthy, and
the same shape as the outage it was added for.

Found by a deployment agent that refused to write the alert rule, correctly,
on the grounds that its subject could not exist.

### `serve_metrics()` — a listener for processes that serve no HTTP

`STAPEL_OBSERVABILITY["EXPORTER_PORT"]` (and `EXPORTER_ADDR`, default
`0.0.0.0`). **Off unless set** — a worker that opens a port nobody asked for
is a surprise, and in some deployments a security finding. Started by
`BaseBusConsumerCommand` and by `serve_functions` before either enters its
loop.

It serves `facade_exposition()` — the same text `/api/metrics/` serves — so a
worker's metrics are not a second dialect a scrape config has to learn.

It never raises. A worker must not fail to start because its metrics port is
taken; it logs the bind failure and carries on doing the job it exists for.
Port `0` is honoured as "any free port" rather than read as "off", which is a
distinction a falsiness check gets wrong and a test caught.

### The series now exists before anything fails

`declare_topics()` creates `bus_dlq_total` at zero for each subscribed topic
at consumer startup. Without it the counter does not exist until the first
event is parked, and `rate(bus_dlq_total[15m]) > 0` over a series with no
samples never fires — an alert with no subject, which is the very failure
this metric was introduced to end.

### `event_type` is no longer a label — BREAKING for anyone already querying it

It is unbounded: every event type a deployment ever publishes would become its
own series. It stays in the log line beside the traceback, where a human is
already looking. The labels are now `{topic, reason}` — the two things an
alert routes on. 0.49.0 shipped hours ago and nothing consumes the metric yet,
so this is corrected now rather than carried.

## [0.49.0] — 2026-08-28

### A dead-letter queue nobody counts is a place work goes to be forgotten

The second half of 0.48.0's outage. Eight login codes were parked in the DLQ
between 2026-08-25 08:52 and 2026-08-27 20:32 UTC — eight people who asked for
a code and never got one. Every one of them was logged at ERROR. The
containers reported "Up", the HTTP layer kept answering "Verification code
sent successfully" because publishing to the bus really had succeeded, and it
ended when a human happened to look.

The signal existed the whole time. **Nothing counted it, so nothing could
alarm on it.** DLQ depth is the most load-bearing number a bus deployment has,
and it was the one number nobody had, because parking an event was a handful
of lines inside each backend's retry loop and every backend spelled it
differently.

### `stapel_core.bus.dlq.record_parked()`

One function, called by every backend at the moment it gives up on an event.
It increments `bus_dlq_total` — **alert on a non-zero rate; that is work being
dropped on the floor** — labelled by `topic`, `event_type` and `reason`, and
logs the giving-up in one consistent shape.

`reason` separates the two ways an event lands here because they need
different answers: `handler` is code that failed on a message the bus
understood, `undecodable` is a message it could not read at all — a
producer/consumer format split, not a handler bug.

The traceback is not traded away for a tidy line: it is how this outage was
actually diagnosed, so `record_parked` attaches it whenever it is called from
inside an exception handler, and the per-backend `logger.exception` calls it
replaces are gone rather than left to double-log.

Wired into `kafka`, `nats` and `redis_streams`. A backend that parks an event
without calling this is invisible in exactly the way the outage was, and
`MODULE.md` now says so for anyone writing one.

### Notes

Recording never raises. The caller is already on a failure path, and a
metrics backend that is unavailable must not turn a parked event into a crash
that loses it entirely — there is a test that asserts precisely that.

## [0.48.0] — 2026-08-28

### A retry that reuses the dead connection is not a retry

ironmemo, 2026-08-26 21:58 UTC: the database dropped the notifications
consumer's idle connection. Every event after that failed with
`InterfaceError: connection already closed` — for 46 hours, into the DLQ, while
the container reported "Up 3 days" and the HTTP layer kept answering
`{"message": "Verification code sent successfully"}` to every OTP request,
because publishing to the bus really did succeed. Nobody logging in could get
a code, and nothing in the system contradicted the claim that codes were sent.

The Kafka consume loop already wrapped each handler call in three retries with
backoff. They could not help: **all four attempts reused the same dead
socket**, so retrying was structurally incapable of changing the condition it
was retrying. Nothing in the loop ever reset the connection, which is why one
idle drop became a permanent outage rather than one lost event.

### `stapel_core.django.db` — hygiene that measures

`close_stale_connections()` and the `worker_db_lifecycle()` context manager.
Long-lived non-request workers now start every unit of work on a connection
known to answer.

`close_old_connections()` alone would not have fixed this, and that is the
whole reason a new primitive exists. It closes a connection that already
errored or aged out; a connection the *server* killed while the worker sat idle
has done neither, so it survives that call and the failure lands on the next
event instead. `close_stale_connections()` keeps that behaviour and adds a
probe — `is_usable()`, one round trip — so the answer is measured rather than
assumed. A probe that raises counts as unusable; a connection inside an atomic
block is never touched (closing it would discard the transaction, a worse
outcome than the stale connection this exists to fix).

### Applied to every long-lived loop, not only the one that broke

The NATS backend and the function server already called
`close_old_connections()` per unit of work. The Kafka path did not — the guard
existed in some places and not others, and the outage happened where it was
missing. Fixing only Kafka would have left the same door open elsewhere, so
the sweep found a third loop with no guard at all:

- `bus/backends/kafka.py` — inside the retry loop, so each attempt can differ
  from the last one.
- `bus/backends/redis_streams.py` — **had no connection hygiene whatsoever**;
  same retry loop, same defect, simply not yet hit in production.
- `bus/backends/nats.py` — upgraded from the non-probing version.
- `django/management/commands/serve_functions.py` — same, for a server that is
  idle between calls for hours.

`MemoryBus` is deliberately excluded: it drains a queue in the publisher's own
process and returns, so it inherits that process's connection lifecycle and
never sits idle holding one.

### Notes

The test suite proves the ordering (`hygiene, handler, hygiene, handler`), and
that assertion goes red against the pre-fix loop at index 0 — the inversion
control for the whole change. It uses a controllable probe rather than a real
connection on purpose: this suite runs on sqlite, whose backend defines
`is_usable()` as an unconditional `return True`, so a "real connection" test
here would be green on a mechanism that never ran.

## [0.47.0] — 2026-08-26

### A truthful answer to the wrong question is worse than silence

0.46.0 fixed the verification drops and its audit named six more calls with the
same shape, deliberately left for their own release. This is that release.

**The shape is not "the function returned nothing."** Django's `cache.delete`
returns `False`, not `None` — so it was never the absence of a return value
that hid the defect. The return was a *truthful answer about a key that module
never writes*, which is no evidence at all about the record the caller meant. A
truthful answer to the wrong question looks like information, which is exactly
what makes it worse than silence.

Four of the six said even less than that. `lift_tombstone`,
`unblacklist_user`, `remove_from_blacklist` and `clear_all` returned `True` for
**"the call did not raise"** — one value for "removed it", "there was nothing
there", "it is still readable" and "the store never answered".

### One vocabulary, in `stapel_core.core.drop`

`DropOutcome` and `DropReport` moved out of `verification.grants` (which
re-exports them, unchanged, so `stapel_core.verification` imports keep working)
into `core/drop.py`, with `measured_drop` / `drop_cache_key` / `measured_clear`.
A second enum per module would fold the facts back together at the seam.

`DropOutcome` gained a fourth member:

* `UNAVAILABLE` — the store could not be reached, so **nothing is known** about
  the record. Not a removal, and not evidence of absence either. The same fact
  `CodeOutcome.UNAVAILABLE` and `StoreUnavailable` already name for reads.

Everything but `DROPPED` is logged, naming the namespace the key was computed
under, so a caller who ignores the return value still cannot obtain a quiet
no-op. `DropReport` stays falsy unless `DROPPED`.

### Fixed — what each verb now returns, and how it is measured

| Verb | Was | Now |
|---|---|---|
| `django.jwt.tombstone.lift_tombstone(uid)` | `True` if it did not raise | `DropReport` — read, delete, read back, against the fleet revocation namespace |
| `django.jwt.authentication.unblacklist_user(uid)` | `True` if it did not raise | `DropReport`, same namespace |
| `core.token_blacklist.TokenBlacklist.remove_from_blacklist(jti)` | `True` if it did not raise | `DropReport`, same namespace |
| `core.token_blacklist.TokenBlacklist.clear_all()` | `True` if it did not raise | `DropReport`, measured with a probe |
| `verification.codes.OneTimeCodeStore.discard(id)` | `None`, swallowing `StoreUnavailable` | `DropReport`; the outage is now `UNAVAILABLE` |
| `verification.codes.OneTimeCodeStore.unblock(id)` | `None` | `DropReport` |
| `django.mandate.invalidate_mandate_cache(user_id)` | `None` | `DropReport` (+ `absence_is_normal=` for the broadcast path) |
| `verification.policy.invalidate_policy_cache(user_id)` | `None` | `DropReport` — **namespace move deferred, see below** |
| `django.workspaces.invalidate_membership_cache(ws, user)` | `None` | `DropReport` (found in this sweep, same shape) |

**`lift_tombstone` is the costly one, which is why it is first.** The operator
calling it is restoring a **wrongly deleted user**. A `True` that lifted
nothing told them the person was back while that person stayed locked out of
every consumer-mode service in the fleet, with nothing anywhere saying
otherwise. `NOT_FOUND` there does not mean "admitted": it means nothing was
tombstoned under *this* deployment's revocation namespace, and the peer holding
the tombstone still refuses.

**`unblacklist_user` was the same shape in the same key space.** Its sibling
`blacklist_user` has documented since 0.39.0 that "a caller that ignores the
result cannot tell a ban from a no-op"; the concern was simply never carried to
the delete path.

**`clear_all` genuinely cannot read back what it removed** — there is no key to
re-read and nothing enumerates what was there — which is precisely where `True`
for "did not raise" is most comforting and least true. It now writes a probe,
clears, and reads the probe back, so it measures the one thing that *is*
measurable: whether the clear reached the namespace this library writes.
`STILL_PRESENT` on a backend whose `clear()` is a no-op, `UNAVAILABLE` on one
that does not retain what it is given (a dummy backend, where a clear can never
be verified at all). It does **not** claim that only revocation keys were
removed, and says so.

**`OneTimeCodeStore.discard` swallowed `StoreUnavailable`**, so "the store was
down", "there was nothing to discard" and "dropped" were one value — in the
module whose own header insists absence and wrongness are different facts. It
still does not raise (a discard is usually the best-effort tail of some other
operation), but the outage is now in the return value and in the log, which is
the difference between best-effort and unknowable.

### Documented — `OneTimeCodeStore` is service-local on purpose

Recorded rather than left as an accident of history: this store stayed on
`django.core.cache.cache` when 0.45.0 moved challenges, grants and tokens onto
the fleet namespace, and that is correct. **Both halves of a one-time code
happen in one service** — the same service issues, delivers and checks it; what
must travel is the *outcome*, and the grant does. Fleet-sharing it would
publish a hashed bearer credential to every peer, make the per-identifier
attempt budget and block shared state a peer could exhaust or clear, and stop
the send-rate window being a property of the service that sends. The reports
these verbs return carry `namespace="service-local:<KEY_PREFIX>"`, so the scope
is visible rather than assumed. The same reasoning, written out, applies to the
mandate cache: each peer caches its own answer and drops it from the same
fleet-wide revocation broadcast.

### Deferred — the policy cache's namespace is a wire format

`verification.policy` is the last part of `stapel_core.verification` still on
`django.core.cache.cache`. Invalidating a policy in the auth service therefore
does **not** invalidate it in the peer that enforces it, which keeps the stale
answer for `POLICY_CACHE_TTL` (60s) — self-healing, bounded, but a user who
turns a scope ON can be unprotected in a peer for up to a minute.

Moving it onto `fleet_cache` is a **wire-format change between peers**, exactly
as `GRANT_NAMESPACE` was: two peers on different sides of the change both read
a cache neither writes. So it is not bundled into an otherwise additive
release. `invalidate_policy_cache` now reports `namespace="service-local:…"`,
which makes the gap visible in the return value instead of invisible in a
`None`; `tests/test_drop_reports.py::TestPolicyInvalidationMeasures` pins the
stale peer copy so the deferral cannot be forgotten.

### Fixed — a workspaces outage was rendering as 403, not 503

Reported during this sweep by the stapel-forms owner, verified against 0.45.0,
and the same family: "did not raise" read as "answered no".

`django.workspaces.require_capability` logged a `FunctionCallError` and
returned `None` — a **denial** — when the provider had rendered no verdict at
all; `_require_capability_fallback`'s membership lookup swallowed
`WorkspaceLookupUnavailable` besides. Downstream, a consumer's authorization
layer had an `unavailable → 503` branch that **could never fire**: a workspaces
outage produced 403, indistinguishable from a genuine refusal, and an operator
watching had nothing to tell the two apart.

Now there are three answers, never two: a membership, `None` (a verdict), or
`WorkspaceLookupUnavailable` (the question could not be asked). `strict=False`
restores the old shape for a soft, non-authorization caller — but it is not the
default, because the default is what the caller who never thought about it
gets. `require_role`'s default flipped to `strict=True` with it, for the same
reason; `get_membership` keeps `strict=False` because it is a *reader* with
legitimate non-authorization callers, and its docstring has said since 0.31.0
that anyone rendering its `None` as 403 must ask for strict.

**For the stapel-forms owner:** `test_a_workspaces_outage_still_renders_403_not_503`
is expected to go **red** against core 0.47.0. That red is the signal the fix
landed, and the caveat published in every capability entry's `gates.behavior`
("a 403 means either not-granted or no-verdict") can come out of the contract.
Publishing it there rather than leaving it undocumented is why this got fixed
at all.

### Tests

`tests/test_drop_reports.py` (24). Each verb is exercised in the shape that
made the old return useless: a record written under ONE namespace and dropped
under another — two fleets, or two services sharing one store — or a store that
cannot answer. The proofs that the old return said nothing were run against the
0.46.0 bodies verbatim before deleting them: `old_lift_tombstone` returns
`True` across a namespace mismatch while `is_user_tombstoned` still answers
`True`; `old_clear_all` returns `True` on a backend that ignores `clear()`;
`old_discard` returns `None` whether the store worked or was down;
`plain_cache.delete("a-key-nobody-wrote")` returns `False`, the truthful answer
to the wrong question. On 0.46.0 the new file does not import at all.

`tests/test_workspaces_capability.py` carries the no-verdict pair
(`test_a_provider_that_raised_is_not_a_denial`,
`test_a_membership_lookup_with_no_verdict_is_not_a_denial`) in place of the old
`test_remote_failure_is_fail_closed_and_not_cached`, which asserted the defect.

Log assertions attach a handler to the module's own logger rather than using
`caplog`: whether caplog observes anything depends on the host's LOGGING
config, and a log assertion that silently observes nothing is the same genre of
defect as the delete under test.

Suite 3220 passed.

## [0.46.0] — 2026-08-26

### A delete that removed nothing looked exactly like a delete that worked

`stapel_core.verification` exported `create_challenge`, `get_challenge` and
`complete_challenge` — and nothing that **drops** a challenge. So a consumer
testing the expired-challenge path had to reach around the module: into the
private `grants._cache()`, or straight at `django.core.cache.cache` with a
hand-computed key.

stapel-auth 0.28.0 took the second road and **published nothing**. Its test
simulated an expired challenge by deleting the key through the plain cache.
When core 0.45.0 moved challenges, grants and tokens onto the fleet-wide
namespace, that delete began computing
`<service>:1:stapel:verification:challenge:<id>` while the module reads
`stapel_verification:1:…` — it removed nothing, and there was no way to tell.
The test's setup silently did nothing and its assertion "verified" an
emptiness that had never been created. The release died on it.

The consumer patched its own helper. The seam stayed private, so the next
consumer was going to make the same mistake.

**The lesson is not "there was no delete function".** It is that a delete which
removed nothing was indistinguishable from one that worked.

### Added — the terminal verbs, and they report what they did

| Verb | Removes | Returns |
|---|---|---|
| `drop_challenge(challenge_id)` | the challenge | `DropReport` |
| `drop_verification_token(token)` | one stateless `X-Verification-Token` | `DropReport` |
| `revoke_grants(user_id, scopes)` | that user's grants for those scopes | `list[DropReport]`, one per scope, in order |

All three are exported from `stapel_core.verification`, alongside the new
`DropOutcome` and `DropReport`. Each reads the key **this module** computes,
deletes it, and reads it **back** — so `DROPPED` is a measurement, not a claim.

`DropOutcome` names three facts that must never be folded into one another
(the rule `CodeOutcome` already states for reads):

* `DROPPED` — a record was there and a read-back confirms it is gone.
* `NOT_FOUND` — nothing was stored under that key. Already spent, aged out, or
  **the writer computed a different key** — a different `GRANT_NAMESPACE`, or a
  caller reaching for `django.core.cache.cache`. This is the defect above,
  named.
* `STILL_PRESENT` — the delete ran and the record is still readable. The store
  did not obey; never report it as success.

`DropReport` is **falsy unless the outcome is `DROPPED`**, so the obvious
`assert drop_challenge(cid)` is a real assertion. A bare `str`-Enum would not
do: every member of one is truthy, so that one-liner would pass on `NOT_FOUND`
— the exact outcome the primitive exists to expose. `NOT_FOUND` logs a warning
and `STILL_PRESENT` an error, both naming the namespace and telling the reader
what to compare, so a caller who ignores the return value still cannot obtain a
quiet no-op.

**Placed in the ordinary public API on purpose, not in a `testing` sidecar.**
These are operational primitives that tests also use: a step-up nobody
requested, an erasure, a session known to be compromised are the same removal
`record_failed_attempt` already performs when the attempt budget runs out. A
"for tests only" function that operations will reach for anyway should be
named honestly rather than quarantined somewhere the contract does not
describe — the private version of this seam is what cost a consumer a release.

### Changed — `revoke_grants` returns reports instead of `None`

Additive: nothing could depend on the `None`. "Revoke everywhere" is a
security operation, and one that removed nothing must not be
indistinguishable from one that worked.

### Documented — what `revoke_grants` does not reach

A verification token minted by `complete_challenge` is keyed by the token
itself (`TOKEN_KEY`), so it cannot be enumerated from a user id: it survives
`revoke_grants` for its full `max_age` and its holder still satisfies
`has_grant(user, scope, token=…)`. Until 0.46.0 nothing public could remove
one at all. `drop_verification_token` closes that when the value is in hand;
when it is not, the token's lifetime remains the only bound.

### Audited, not fixed here — the same shape elsewhere

Every other place this library creates state a consumer can only clear through
a private accessor, or clears it with a call that cannot say whether anything
went. Named so the next reader does not have to find them again:

* **`verification.policy.invalidate_policy_cache(user_id)`** — public and
  exported, but returns `None`, and it goes through `django.core.cache.cache`:
  the one part of `stapel_core.verification` that did **not** move to
  `fleet_cache` in 0.45.0. Invalidating a policy in the auth service therefore
  does not invalidate it in the peer that enforces it, which keeps the stale
  answer for `POLICY_CACHE_TTL` (60s). Self-healing, but a user who turns a
  scope on can be unprotected in a peer for up to a minute. Moving it is a
  wire-format change between peers, like `GRANT_NAMESPACE` was, so it gets its
  own release rather than riding this one.
* **`verification.codes.OneTimeCodeStore.discard()` / `.unblock()`** — both
  return `None`, and `discard()` swallows `StoreUnavailable` besides, so "the
  store was down", "there was nothing to discard" and "dropped" are one value.
  In the module whose own docstring insists absence and wrongness are different
  facts. Its `check()` already returns a verdict; the drop verbs want the same
  `DropReport` treatment.
* **`core.token_blacklist.TokenBlacklist.remove_from_blacklist()` /
  `.clear_all()`** — return `True` when the call did not raise, not when
  something was removed.
* **`django.jwt.authentication.unblacklist_user()`** — the same. Its sibling
  `blacklist_user` already documents that "a caller that ignores the result
  cannot tell a ban from a no-op"; the concern was never carried to the delete
  path.
* **`django.jwt.tombstone.lift_tombstone()`** — the same, and the most costly
  of them: the operator calling it is restoring a wrongly-deleted user, and a
  `True` that lifted nothing leaves that user locked out with no signal.
* **`django.mandate.invalidate_mandate_cache()`** — returns `None`, on the
  service-local cache, while what invalidates it (the revocation broadcast) is
  fleet-wide.

### Tests

`tests/test_challenge_drop.py` (18 tests). The regression the original defect
needed is `TestTheDropIsNeverSilent`: a challenge written under one fleet
namespace, dropped under another, must come back `NOT_FOUND` — visibly a
no-op, not silently one — and the record must still be there afterwards. The
plain-cache delete that shipped in the consumer is kept in the suite beside it
(`test_a_plain_cache_delete_has_nothing_to_say`) so the reason the primitive
exists stays visible: it returns a truthful `False` about a key this module
never writes, which is no evidence at all about the record the caller means.
On 0.45.0 the file does not import.

## [0.45.0] — 2026-08-24

### The gate asked for a credential the browser it guarded could not produce

`StapelModelAdmin` refuses a HIGH-clearance admin operation — `delete` in the
standard preset — until the operator holds a fresh verification grant. `ENFORCE`
defaults to `True`. Here is what "holds a grant" actually meant:

`has_fresh_step_up(user)` called `has_grant(user, scope)` with **no `token=`**,
so the `X-Verification-Token` fallback was unreachable from this gate — and a
browser form POST could not have set that header anyway. That left exactly one
channel: a grant in **this process's** `django.core.cache.cache`, which Django
keys under the service's own `KEY_PREFIX`.

So the module's own docstring — *"completing step-up anywhere in the session
satisfies the admin gate"* — was true only inside one `KEY_PREFIX`, and
`step_up_denied_message` sent operators to an auth-service step-up flow that
could not satisfy the check it was quoted in. This repository already documents
that exact mechanism silently breaking revocation across services
(`core/revocation_store.py`, 0.39.0) and moved revocation onto a forced
fleet-wide namespace. Verification grants were never moved.

**Third instance of this class tonight**, after the WebSocket handshake that
read a header a browser cannot send (0.44.0) and the OpenAPI scheme that named
a cookie the service never issues (0.44.2): a guard reading a credential
channel the real client has no way to fill, kept green by tests that fill it
some other way.

**Mirrored in the tests, which is why nobody caught it.** Every green "step-up
satisfied" assertion in `tests/test_access_stepup.py` opened the gate by calling
`grant_verification()` directly, in-process, into the same cache the check
reads. No test drove it the way an admin browser can, and none covered the
cross-prefix case — even though `tests/test_revocation_namespace.py` had been
standing up two caches differing only in `KEY_PREFIX` since 0.39.0. That
technique is now applied where it belonged.

### SECURITY — verification grants are fleet-wide state and now live in a fleet-wide namespace

**Grants, challenges and verification tokens have moved out of the service's own
cache prefix.** `stapel_core.verification.grants` now writes through
`stapel_core.core.fleet_cache` — the deployment's own cache connection (same
backend, same `LOCATION`, same `OPTIONS`) with `KEY_PREFIX` and `VERSION` forced
to fleet values and `KEY_FUNCTION` dropped, so a per-service key function cannot
re-isolate what the fleet must share. The key is fleet-stable because the
identity is: `user.pk` is the UUID the JWT `user_id` claim carries.

**Deployments must know: live grants in the old per-service keys simply stop
counting.** They are not migrated and not deleted — they sit at
`<service>:1:stapel:verification:grant:<uid>:<scope>` until their TTL expires,
invisible to the new reader. In practice a user mid-session is asked for one
extra factor: a `sensitive` grant lives 900s, the `@requires_verification`
default 300s. Rolling a fleet is therefore safe in any order, but a peer still
on ≤0.44.2 will not see grants minted by an upgraded peer and vice versa, so
plan for a window where step-up does not converge across the fleet. Nothing
fails open at any point: a missing grant is a refusal, never an admission.

**Added — `STAPEL_VERIFICATION["GRANT_NAMESPACE"]` / `["GRANT_CACHE"]**
(defaults `"stapel_verification"` / `"default"`). These are a wire format
between peers, not a local preference. Both are `no_env` — a namespace picked
up from a container's environment is a per-service opinion by construction,
which is the one thing they exist to prevent.

**Added — `stapel_core.verification.E001` / `W001` / `W002`**, the mirror of
the revocation namespace findings. `W001` (security-critical, so a blanket
`SILENCED_SYSTEM_CHECKS` line cannot mute it) fires on a non-default namespace:
legitimate for two fleets on one store, and the original defect if it is one
service's local opinion. `E001` fires when `GRANT_CACHE` names an alias that
does not exist and there is no `default` — grants written nowhere means every
step-up-gated operation is refused forever.

### Fixed — the admin gate now reads channels an admin browser can fill

`step_up_blocks` takes the whole **request**, not just the user, and reads
three channels:

1. **The fleet-wide grant store**, above — which finally makes "completing
   step-up anywhere in the fleet counts here" a property of the code and not
   just of the docstring.
2. **The session.** `record_step_up_in_session(request)` records
   `{scope: granted_at}` under `request.session["stapel_step_up"]`. The session
   cookie is the one credential the admin browser carries on every subsequent
   form POST, and this channel keeps working where the cache is not shared
   (LocMem, split Redis DBs — `stapel_core.blacklist.W002` already warns about
   that shape). Freshness is judged against the *current* `MAX_AGE`, so
   shortening the policy tightens live sessions immediately; a timestamp in the
   future is refused rather than trusted.
3. **A presented verification token** — `X-Verification-Token` for API clients,
   or the `verification_token` query parameter / form field, which is the
   spelling a browser can produce when an auth-service step-up redirects back.
   A token accepted at an admin *view* is pinned onto the session
   (`adopt_step_up_token`), because the confirm form that follows carries
   neither the query string nor a header. Pinning happens only in views:
   `step_up_blocks` stays side-effect free, since `has_*_permission` calls it
   during page rendering.

A token is still validated against the user it was minted for and the scope it
was minted in — a token belonging to someone else, or to another scope, opens
nothing. Session-recorded proofs are bounded by `MAX_AGE` but are **not**
reached by `revoke_grants()`, which deletes from the shared store; flushing the
session is what ends one early.

`step_up_denied_message` now names only paths a reader can take, and
`stepup.py`'s module docstring describes the three channels instead of claiming
a property one cache prefix could not deliver.

### Changed — `core/fleet_cache.py`, the mechanism, named for what it does

The borrow-the-connection-and-force-the-prefix step moved out of
`core/revocation_store.py` into `core/fleet_cache.py`. Two instances of one
defect get one mechanism, not two: `revocation_store.py` keeps the
revocation-specific half (which namespace, which alias) and its public API —
`revocation_cache()`, `revocation_namespace()`, `revocation_cache_alias()`,
`reset_revocation_cache()`, `DEFAULT_NAMESPACE`, `NAMESPACE_VERSION` — is
unchanged, so nothing that imports it needs editing.

### Tests

`tests/test_step_up_channels.py` (21 tests). **20 of them fail on 0.44.2** —
`20 failed, 1 passed`, the one pass being the negative control (an unverified
user is still refused). These are the defect itself rather than a missing
symbol:

- a grant minted in the `auth` prefix is invisible in the `stapel_profiles`
  prefix — *"step-up completed in one service is invisible in the next"*;
- the admin gate stays shut on a grant minted by a peer service;
- `revoke_grants` does not cross services either;
- the grant is still readable in this service's own `django.core.cache.cache`
  namespace — *"the grant is still in this service's own cache namespace"*;
- **`X-Verification-Token` does not open the admin gate at all** — the
  unreachable-fallback half of the report, reproduced.

Browser-shaped requests are asserted to *be* browser-shaped: `_assert_browser_shaped`
checks `X-Verification-Token` is absent from the request, the same discipline
0.44.1's `_browser_scope` introduced for the WebSocket handshake. The
cross-prefix cases use `tests/test_revocation_namespace.py`'s two-cache
technique unchanged. The existing in-process tests in
`tests/test_access_stepup.py` are kept and re-pointed: they pin the *policy*
(which levels are gated, degradation, that a superuser is not exempt), and the
new file pins *reachability* — with one test asserting the grant left the
per-service prefix, so the shared helper cannot silently go back to proving the
old behaviour. Suite: 3176 passed.

## [0.44.2] — 2026-08-24

### The ONE cookie name, actually the only one

0.44.1 introduced `jwt_cookie_names()` and called it "the ONE resolution" —
the point being that the socket cannot read a cookie the HTTP side never sets.
It converted three call sites and left four behind. `getattr(settings,
"JWT_COOKIE_NAME", "stapel_jwt")` was still re-derived in the CSRF-exemption
middleware, the JSON logout view, the admin logout redirect, and — as a bare
literal — in the published OpenAPI security scheme. On a default deployment
every copy agrees, which is exactly why the drift is invisible until a service
sets `JWT_COOKIE_NAME` and one half of the stack starts naming a cookie the
other half never issues. Every one of them now calls `jwt_cookie_names()`.

**Fixed:** a deployment that renames `JWT_COOKIE_NAME` had its OpenAPI schema
advertise the `stapel_jwt` default, so generated clients looked for a cookie
the service does not set.

### The sweep the WebSocket fix was missing

`tests/test_ws_credential_sweep.py` proves the *absence* the per-case tests
cannot — and absence is the half that let the original defect live for months:

- every source the handshake extractor can report is classified ambient or
  not, so a fifth credential channel cannot be added without deciding whether
  the origin gate applies to it (`AMBIENT_SOURCES == {cookie}`);
- the handshake and the HTTP extractor resolve the same cookie name under a
  **renamed** setting — the default proves nothing, since both halves used to
  hardcode the same literal;
- no other module in the package resolves that name from settings, and no
  other module reads a credential off an ASGI scope — a second reader of the
  handshake headers would be a second authentication path, and the one that
  would not inherit the origin gate.

The sweep fails on 0.44.1. No behavioural change to the socket itself: the
cookie channel, the mandatory fail-closed origin allowlist,
`STAPEL_WS_ALLOWED_ORIGINS`, `stapel_core.jwt.E001`/`E002` and close codes
4401/4403 are all unchanged from 0.44.1.

*Tests: 3144 → 3155.*

## [0.44.1] — 2026-08-24

*(0.44.0 was tagged and never reached PyPI: a test in this release named a
sibling library in an `override_settings(INSTALLED_APPS=...)`, which makes
Django import it for real — green locally, `ModuleNotFoundError` on a clean
CI runner. The release-gate did its job. 0.44.1 is the same change with the
test reading the setting instead of loading the app; **0.44.0 does not exist
on PyPI — floor on `>=0.44.1`**.)*

### The socket a browser could never open

A browser **cannot** set an `Authorization` header on `new WebSocket()`. The
product authenticates HTTP with an httpOnly JWT **cookie**. And
`stapel_core.django.jwt.channels._extract_token` read exactly three things:
the `Authorization` header, the `Sec-WebSocket-Protocol` subprotocol, and
`?token=`. There was no cookie branch.

So every real browser handshake closed **4401**. The react pair read 4401 as a
permanent refusal and stopped retrying, and the product fell through to a
polling half — for months. The socket was built, mounted, proxied and
smoke-tested. The smoke test passed an `Authorization` header **a browser can
never send**, so it proved nothing about the only path that matters.

That is the lesson worth keeping, and it is a test-quality lesson before it is
a code one: *the suite was green over a client that does not exist.* Every
test added here drives the handshake a browser actually makes — the JWT cookie
in the handshake headers, and no `Authorization` header at all
(`_browser_scope` asserts its absence).

**Added — the cookie is the fourth credential channel.** Cookie names resolve
through the new `stapel_core.django.jwt.utils.jwt_cookie_names()`, the same
call the HTTP extractor, `set_jwt_cookies`, the config loader and the admin
login view now make, so the socket can never read a cookie the HTTP side does
not set. The cookie is tried **last**: an explicit credential (header,
subprotocol, `?token=`) always wins, and only a browser with nothing else
falls through to the ambient one.

**Added — the refresh cookie is honoured on the handshake.** An access cookie
lasts an hour by default; the refresh cookie behind it lasts days. A tab left
open past expiry reconnects holding both, and a 4401 there is the same close
that taught the client to give up. Gated on `JWT_REFRESH_ALLOWED` (the flag
the HTTP middleware reads) and re-minted through `load_user_by_uid`, so a
deactivated, deleted or tombstoned uid is refused here as it is on HTTP. A
handshake has no response to set a cookie on, so the new token is stamped into
`scope["stapel_refreshed_access_token"]` for the host to hand back.

`scope["stapel_auth_source"]` now names the channel the credential arrived on.

### SECURITY — cookie-authenticated WS handshakes now REQUIRE an origin allowlist

**Every deployment that serves WebSockets to browsers must declare one.**
Without it, cookie handshakes are refused (close **4403**) and
`manage.py check` fails with `stapel_core.jwt.E001`.

A cookie is **ambient authority**. The browser attaches it to a WebSocket
handshake started by *any* page on the internet, and WebSockets are protected
by neither the same-origin policy nor CORS — there is no preflight, and the
cross-site handshake succeeds without the attacker's page ever reading the
cookie. Shipping the cookie branch alone would have turned a broken socket
into **Cross-Site WebSocket Hijacking**, so the branch and the guard ship
together in one release and are not separable.

The guard **fails closed**. An empty allowlist is a misconfiguration, not a
wildcard, and a malformed entry is dropped rather than honoured — a typo must
not be the thing that decides a deployment does not get a guard.

Declare the origins, **with their ports**:

```python
STAPEL_WS_ALLOWED_ORIGINS = [
    "https://app.example.com",
    "http://localhost:5173",
]
```

`STAPEL_REALTIME["ALLOWED_ORIGINS"]` is read as a fallback (as a plain
settings dict — core still does not import `stapel-realtime`), so a host
already running the realtime substrate declares its origins **once**. Two
lists that can disagree is how two layers end up giving contradictory verdicts
about one socket.

**Only the cookie is gated.** `Authorization`, the subprotocol and `?token=`
are not ambient: an attacker's page cannot produce a credential it has never
seen, so an `Origin` check adds nothing there — while requiring one would
refuse every service-to-service and native client, which legitimately send
none. The refusals stay distinguishable: a rejected origin closes **4403**, a
rejected credential still closes **4401**.

**New checks** (`stapel_core.django.jwt.ws_origin`, registered from
`CommonDjangoConfig.ready`; it imports no `channels`):

| id | level | fires when |
|---|---|---|
| `stapel_core.jwt.E001` | error, **security-critical** — no blanket `SILENCED_SYSTEM_CHECKS` line can mute it | this deployment serves WebSockets *and* authenticates browsers by cookie, and no allowlist is declared |
| `stapel_core.jwt.E002` | error | an allowlist entry that is not a `scheme://host[:port]` origin, so it can never match — the `studio.localhost` vs `http://studio.localhost:8600` incident |

E-level even though the runtime fails closed, because that refusal **is** the
shipped defect in its other form: a socket every browser is turned away from,
a client that reads the close as permanent, and a product that quietly polls.
The operator learns it at deploy time, not from a support ticket months later.

**Coordination with `stapel_chat.E014`.** stapel-chat 0.4.0 reports the same
fact at its own layer. Both read the same allowlist, so the two verdicts agree
by construction — chat cannot say "guarded" while core says "unguarded". The
mechanism belongs here so every consumer of the core socket inherits it: a
chat module's check cannot protect a video socket. Consumers should delegate
to `ws_origin.websocket_origin_allowlist()` and
`ws_origin.cookie_websocket_auth_reachable()` rather than re-reading settings,
and chat's `E012` probe (`_extract_token` with a cookie scope) keeps working —
it clears on this release.

### Upgrade

1. Declare `STAPEL_WS_ALLOWED_ORIGINS` (or `STAPEL_REALTIME["ALLOWED_ORIGINS"]`)
   on every service that serves browser WebSockets. Include the port whenever
   it is not the scheme's default.
2. Run `manage.py check`. `stapel_core.jwt.E001` names the services that still
   need it; `E002` names an entry that would never have matched.
3. Nothing else changes for clients that already send a subprotocol or
   `?token=` — they were never ambient and are not gated.

## [0.43.0] — 2026-08-24

### Security — deactivation stopped at the issuer

Measured on a deployed fleet at 0.41.0. `is_active=False` set at the issuer,
same unexpired access token, immediately after:

```
iron-auth       /auth/api/v1/sessions/   -> 401   (issuer mode, correct)
iron-profiles   /profiles/api/v1/me      -> 200   <-- still served
iron-workspaces                          -> 200   <-- still served
iron-billing                             -> 200   <-- still served
```

Two independent causes, either one sufficient.

**The claim satisfied the gate meant to judge it.** In consumer mode
(`JWT_CREATE_USERS_FROM_TOKEN=True`) the `is_active` claim was replayed onto
the local shadow row *before* the `is_active` gate read that row. A token
minted while the account was live carries `is_active: true` for the rest of its
lifetime, so it reactivated the row and then passed the check it had just
satisfied. The ordering was deliberate and documented — the reasoning being
that the issuer is authoritative — but a claim is not the issuer, it is a
snapshot of the issuer taken when the token was minted.

**And nothing told the peers.** Even with the ordering fixed, a consumer-mode
service learns lifecycle only from claims, so deactivation would have waited
for the next mint — up to an access-token lifetime, or forever if the user
never signs in again.

The interim remedy (`blacklist_user`, working fleet-wide since 0.39.0) made the
only correct operator procedure "deactivate **and** ban". That is the
remember-to-do-it shape this package already rejected for deletion when the
tombstone became a `post_delete` receiver rather than a caller's duty.

**Added — `stapel_core.django.jwt.deactivation`**

- `user_deactivated:<uid>` — a fourth key space in the shared revocation
  namespace, alongside `jwt_blacklist:` (is this token revoked),
  `user_blacklisted:` (is this person banned) and `user_deleted:` (is this
  account gone). Four questions, four keys.
- A `post_save` receiver on `AUTH_USER_MODEL`, connected in
  `CommonDjangoConfig.ready`, publishes on `is_active=False`. The state change
  carries its own announcement; no caller has to remember.
- `deactivate_user(uid)`, `lift_deactivation(uid)`, `is_user_deactivated(uid)`.
  Reads fail **CLOSED**, through the same single `STAPEL_BLACKLIST_FAIL_OPEN`
  hatch as the blacklists and the tombstone. TTL is `tombstone_ttl()` — shared
  deliberately, because both answer "how long can a credential naming this uid
  still be presented", and a second knob is how the halves of revocation drift
  apart.
- Consumer-mode verifiers consult it in `get_or_create_user_from_jwt`, before
  any claim is trusted. Issuer mode does not — its database is the account, and
  it pays no cache read.

### Reactivation LIFTS the record — this is NOT the deletion rule

`lift_tombstone` exists only for an operator who deleted the wrong row, because
every automatic reason to lift a tombstone is a way for a token to undo a
deletion. **Deactivation is different on purpose**: suspending and restoring an
account is an ordinary, expected, repeatable operation, so the same receiver
deletes the key when a user is saved with `is_active=True`. An account that
cannot be un-suspended is a bug, not a hardening. Do not copy the tombstone's
no-automatic-lift rule here by analogy.

### Changed — the `is_active` claim no longer writes the local column

**In either direction.** It is not "write only the restrictive direction": a
claim that can write this column at all is a claim that participates in the
decision it is supposed to be judged by. In consumer mode the column now
records only what *that* service decided locally, which a bearer token cannot
overrule; fleet-wide lifecycle lives in the key space. `is_staff`,
`is_superuser`, `staff_roles`, `email`, `phone`, `auth_type` and `is_anonymous`
sync exactly as before (AS-2 unchanged).

Only the authoritative store publishes — the receiver returns early in consumer
mode. A peer broadcasting its own shadow row's state could *lift* the issuer's
deactivation, which is this same failure inverted.

### Upgrading

- **Deployments can drop the "deactivate and ban" procedure.** Existing
  `user_blacklisted:` entries keep working and are untouched.
- **`User.objects.filter(...).update(is_active=False)` still does not
  publish** — Django emits no `post_save` for queryset updates. Call
  `deactivate_user(uid)` alongside it, or deactivate through instances. This is
  the one hole the receiver cannot close, and it is named rather than left to be
  discovered.
- **Consumer-mode shadow rows that a claim previously set `is_active=False`
  will no longer be reactivated by a claim.** In practice there should be none
  (an issuer refuses to mint for an inactive user, so `is_active: false` rarely
  reaches a token at all), but if your peers hold such rows, set them active
  once — the fleet-wide state is the key space now.

## [0.42.0] — 2026-08-24

### Security — the OAuth seam could not ask who a token was minted for

`OAuthProvider.get_user_data(self, access_token)` carried no provider
configuration, so no implementation could verify a token's audience even if it
wanted to. That is not an abstract gap. An OAuth access token is a bearer
credential scoped to the **client** it was issued to; a service that accepts
one straight from a request body and resolves the profile behind it will
accept a token minted for *somebody else's* OAuth app against the victim's
provider account, and log the caller in as the victim. Downstream,
stapel-auth's `POST /oauth/login/` is exactly that endpoint (see stapel-auth
0.27.0).

The seam can now answer the question, and every answer that is not a positive
proof is a refusal:

```python
from stapel_core.oauth import OAuthClientConfig, check_audience, fetch_user_data

reason = check_audience(provider, access_token, config)   # None == accepted
if reason:
    return None                                           # refuse
return fetch_user_data(provider, access_token, config)
```

**Added**

- **`OAuthClientConfig(client_id, client_secret, accepted_audiences)`** — the
  deployment's own client plus every client ID a caller-supplied token may
  legitimately carry. `accepted_audiences` is a **tuple, not a single value**:
  one project routinely owns several clients (Google issues separate Web /
  iOS / Android client IDs), so a native app's token carries a different `aud`
  than the web app's and both are the same deployment. A single-value check
  would have refused every mobile sign-in.
- **`OAuthProvider.verifies_audience`** (default `False`) and
  **`OAuthProvider.verify_audience(access_token, config)`** (default returns
  `False`). **Refuse-if-unverifiable is the default**, so a provider nobody
  taught to introspect fails closed instead of silently passing. Setting the
  flag without implementing the mechanism is the one way to make this seam lie.
- **`check_audience(provider, access_token, config)`** → `None` when accepted,
  else `AUDIENCE_UNVERIFIABLE` (no mechanism), `AUDIENCE_UNPINNED` (nothing
  configured to compare against) or `AUDIENCE_MISMATCH` (a different client).
  An exception inside a provider's verifier is logged and becomes
  `AUDIENCE_UNVERIFIABLE` — a verification step that fails open is not a
  verification step, and a provider outage must not open a door.
- **`fetch_user_data(provider, access_token, config=None)`** — the compat
  caller. `get_user_data` gained an optional `config` parameter; providers
  written against the pre-0.42 one-argument signature are detected by
  introspection and still called with one argument.

### Compatibility

Additive. `get_user_data(self, access_token)` implementations — including
every third-party provider registered through `register_provider` — keep
working unchanged; `tests/test_oauth.py`'s own fake provider still uses the
old signature and is part of the gate. Nothing verifies an audience until a
provider opts in, and nothing calls `check_audience` until a consumer does.

Only tokens a deployment did **not** mint need the check: a token from your
own `exchange_code` (the `/authorize/` → `/callback/` flow) is yours by
construction.

## [0.41.0] — 2026-08-24

### Security — the deletion gate had an authentication half and no mint half

**Upgrade note — affects consumer-mode services
(`JWT_CREATE_USERS_FROM_TOKEN=True`) that also set `JWT_REFRESH_ALLOWED=True`.**

0.40.0 put the deletion tombstone on the authentication path and stopped
there. The re-mint path was left unguarded, and it is reachable: a
consumer-mode service holds a **shadow row** for a user the issuer has since
deleted, so the row is still present locally and `load_user_by_uid` still
found somebody to re-mint from. That service issued a fresh access token for
a deleted account.

The token was then refused at authentication by the 0.40.0 gate, so no
request was ever served — which is why this shipped as a residual rather than
a finding. That reasoning is wrong, and worth writing down rather than
re-deriving: **the token is harmless because of a check somewhere else.** That
is precisely the shape that stops being true after one refactor, and "a second
path to a guarded action that skips the choke point" is the entire failure
history of this seam — it is the class that produced the login bypass, the
stale-claims refresh, the per-service blacklist, and the resurrection bug in
the first place. Closing one door by exact path and leaving the others is how
containment misses.

`load_user_by_uid` now consults the tombstone, and refuses.

**Deliberately not gated on `JWT_CREATE_USERS_FROM_TOKEN`,** unlike the
authentication-side check. Two reasons:

- A guard that reads a mode flag has a config-shaped bypass. The
  misconfiguration this closes — `JWT_REFRESH_ALLOWED=True` on a
  consumer-mode service, which the docs describe as auth-service-only — exists
  *because* settings get copied between services, so the flags cannot be
  trusted to be coherent with each other.
- The cost argument that justified gating the authentication check does not
  apply. That one runs per request; this one runs per refresh, which is
  bounded by the access-token lifetime.

**No new behaviour when the store is unreachable.** The tombstone read fails
closed like every other revocation read, but the refresh path already failed
closed there — `JWTProvider.refresh_access_token` runs the user-ban check
first, and that has failed closed since 0.25.0. A deployment sees no
availability change it was not already seeing.

*What can break:* a consumer-mode service that was re-minting tokens for
users deleted upstream stops doing so. Those tokens did not authenticate
anywhere; they were minted and discarded.

7 tests in `tests/test_deletion_tombstone.py::TestTheMintHalfOfTheDeletionGate`;
5 fail on 0.40.0, covering the loader, the provider, `JWTRefreshView` and the
middleware's proactive refresh. The two that pass on both are the controls —
a live user must still load and still refresh, because a gate that refuses
everyone is not a fix.

## [0.40.0] — 2026-08-24

### Security — a deleted account could re-create itself from its own token

**Upgrade note — affects every service running
`JWT_CREATE_USERS_FROM_TOKEN=True` (consumer/shadow-copy mode).**

Consumer mode exists so a service can materialise a local row for a user it
has never seen: identity lives in the auth service, everyone else
shadow-copies on first contact. The mode cannot tell that case from "a user I
deleted", because both surface as `User.DoesNotExist`. So a deleted user's
still-valid token walked into a consumer service and was re-created from its
own claims — reported live as the profiles service answering for an account
that existed nowhere else. Deletion is the one lifecycle event a bearer token
must never undo, and it was the one the shadow-copy design had no way to
represent.

`stapel_core.django.jwt.tombstone` adds the missing fact. The issuer writes
`user_deleted:<uid>` into the fleet-wide revocation namespace built in 0.39.0,
so every peer reads the same key regardless of its own cache `KEY_PREFIX`.
Three properties worth stating, because each one is a way this could have been
built wrong:

- **Written by the deletion, not by a caller.** A `post_delete` receiver on
  `AUTH_USER_MODEL`, connected in `CommonDjangoConfig.ready()`. A deletion
  therefore cannot happen *without* its tombstone — including cascades,
  `manage.py shell`, the admin, and GDPR erasure jobs. The interim remedy
  ("ban the user as part of deleting them") was a side effect of a different
  mechanism that someone had to remember, and it died when its entry expired.
- **Its own key space.** `user_deleted:` is distinct from `jwt_blacklist:`
  (per token) and `user_blacklisted:` (per user), so "is this token revoked",
  "is this person banned" and "is this account gone" never answer each
  other's question.
- **Consulted before any claim is trusted**, and only in consumer mode.
  Issuer mode already answers correctly by reading its own database and does
  not pay a cache read per request to be told so.

**TTL is derived, and clamped, never merely defaulted.** It comes from the
deployment's own `JWT_REFRESH_TOKEN_LIFETIME`, so a deployment that lengthens
its refresh tokens lengthens its tombstones automatically — the silent drift
this is really guarding against is the one where those two numbers are changed
months apart. `STAPEL_JWT_TOMBSTONE_TTL` may raise it; a lower value is
clamped up *and* refused at boot by `stapel_core.revocation.E002` (Error, not
Warning: unlike the fail-open hatch there is no stance a deployment can hold
here — a tombstone that ends before the credential does is an unclosed hole
with a number on it).

**Store unreachable: fails CLOSED.** An unreachable store answers
"tombstoned", so a deleted principal is not admitted because the thing that
knows they are deleted is offline. This is the more expensive default and it
is chosen deliberately: it costs a consumer-mode service its availability
while its cache is down, rather than costing a deleted person their deletion —
and the degraded state is exactly when a revocation matters most. It is also
consistent with both blacklists, which have failed closed since 0.25.0. The
single documented hatch `STAPEL_BLACKLIST_FAIL_OPEN` flips all three together;
there is deliberately no separate knob, because two knobs are how the halves
of revocation drift apart.

*What can break:* a consumer-mode service whose cache is unreachable now
refuses authentication instead of admitting it. Set
`STAPEL_BLACKLIST_FAIL_OPEN` only with the trade-off understood. Restoring a
user from backup after deleting them requires `lift_tombstone(uid)` — nothing
in the library lifts a tombstone automatically, because every automatic reason
to do so would be a way for a token to undo a deletion again.

### Security — anyone could revoke anyone's session

`JWTProvider.blacklist_token()` decoded with `verify=False`, so the `jti` it
revoked came from an unauthenticated string. Anyone who could observe a
victim's token — any component that logs, proxies or forwards one — could mint
an unsigned JWT carrying that `jti` and `exp` and POST it to the logout
endpoint, which requires no authentication. The victim's live session died. A
denial of service on another user's account, delivered through the revocation
machinery itself.

The decode now verifies the signature: only a token this deployment signed can
revoke anything. An already-expired token still returns `False` exactly as
before (the `expires_in > 0` guard means there is nothing left to revoke), and
logging out late is unaffected — the refresh token beside it is a separate
live credential and is revoked on its own.

*What can break:* nothing legitimate. `JWTLogoutView` ignores the return value
and still clears cookies and the Django session, so a user logging out sees no
difference. A token signed with a retired key is no longer blacklistable —
it also no longer authenticates.

### Security — a session cookie is a browser credential too

`CsrfExemptAPIMiddleware`'s docstring has described the right rule since
0.28.0: a request whose only credential is a cookie is a browser session and
must keep CSRF. The code counted only the **JWT** cookie. Django's session
cookie was not counted at all — so an `/api/` request authenticated by
`sessionid` alone was blanket-exempted from CSRF on every mutating endpoint.

That is not a hypothetical pairing. The JWT middleware calls `login()`, so any
browser that ever authenticated holds a session cookie, and DRF's
`SessionAuthentication` accepts it on its own. Both cookies are counted now.

*What can break:* a same-origin front-end that posts to `/api/` with only a
session cookie and neither a CSRF token nor `X-Requested-With: XMLHttpRequest`
will start getting 403s. That is the CSRF protection working. An anonymous
`/api/` request with no cookie at all stays exempt — there is nothing to forge.

### Left for a dedicated wave

Refresh tokens are neither **rotated** nor **bound to a tracked session row**.
"Log out everywhere" therefore rests entirely on the blacklist, and a stolen
refresh token is usable for its full lifetime by whoever holds it. Closing
that needs a session table, an issuing seam that writes to it, and a migration
path for tokens already in the wild — design, not a patch. Recorded in
`MODULE.md` so it is not rediscovered as news.

36 tests in `tests/test_deletion_tombstone.py`; 14 fail on 0.39.0.

## [0.39.0] — 2026-08-24

### Security — revocation did not leave the service that performed it

**Upgrade note — every split deployment must read this. Any token you
believed revoked before upgrading may still be live on your other services
until it expires on its own.**

Both blacklists wrote through `django.core.cache.cache`. Django builds the
real cache key as `f"{KEY_PREFIX}:{VERSION}:{key}"` from the **deployment's**
`CACHES`, and every service in a split deployment sets its own `KEY_PREFIX`
(`auth`, `stapel_profiles`, ...) precisely so its ordinary caches do not
collide with its peers'. Revocation is the one thing that must collide.

Sharing a Redis is not sharing a namespace. Reproduced on a consumer's stand:
a token blacklisted in the auth service still returned **200** from the
profiles service. Both pointed at `redis://redis:6379/0`; auth wrote
`auth:1:jwt_blacklist:<jti>` and profiles looked for
`stapel_profiles:1:jwt_blacklist:<jti>`, found nothing, and served the
request. So "log out everywhere", "revoke suspicious session" and
password-change revocation were all per-service illusions: a revoked token
kept working on every service **except** the one that revoked it. That is
what made the 0.38.0 login bypass unrecoverable while it was live — there was
no way to kill a token once minted.

`stapel_core.core.revocation_store` is the mechanism. It borrows the
deployment's own cache connection — same backend, same `LOCATION`, same
`OPTIONS`, so the same Redis and the same pool — and forces `KEY_PREFIX` and
`VERSION` to values that are a property of the FLEET rather than of the
service. Any peer running this library against the same store computes the
same key. `KEY_FUNCTION` is dropped for the same reason: a per-service key
function would re-isolate the namespace this exists to share.

Both halves of revocation now use it, so they cannot drift apart again:

- `TokenBlacklist` (per jti) — previously had no bypass at all, which is the
  half that was reproduced live.
- `blacklist_user` / `is_user_blacklisted` (per user) — previously reached
  for `cache.client.get_client()`, a raw django_redis handle, to bypass
  `KEY_PREFIX`. That workaround was right about the problem and wrong about
  the scope: it worked on exactly one backend and fell back silently to the
  broken prefix-scoped path on every other. The namespace replaces it, and no
  backend is special any more.

Two settings, both optional, and **both must match across every peer if
changed**:

```python
STAPEL_JWT_REVOCATION_CACHE = "default"                # alias to borrow
STAPEL_JWT_REVOCATION_NAMESPACE = "stapel_revocation"  # the shared prefix
```

New system checks (tag `stapel_blacklist`), because a namespace that is not
shared fails silently on both sides — the revoking service reports success,
the verifying service reports 200:

- `stapel_core.revocation.E001` — the configured alias is not in `CACHES` and
  there is no `default`: bans are written nowhere.
- `stapel_core.revocation.W004` — the alias is missing but `default` exists.
- `stapel_core.revocation.W003` (security-critical) — a non-default
  namespace. Legitimate for two fleets on one store; the original defect if
  it is one service's local opinion.

*What can break:* the key moves, so entries written before the upgrade are
invisible after it. Those entries are bounded by the token lifetime, but an
operator who revoked something during an incident should **re-issue the ban
after upgrading**. Deployments that relied on the raw-django_redis path are
unaffected in behaviour; the key they write simply moves with everyone else's.

### Security — the safe refresh was opt-in, so consumers opted out by default

0.38.0 fixed core's own `JWTRefreshView` by passing `load_user_by_uid`. That
left the argument optional on `jwt_provider.refresh_access_token()`, which is
the method every consumer's own refresh endpoint calls — and its default
meant "re-mint from the refresh token's own claims", which are as old as the
token (up to `JWT_REFRESH_TOKEN_LIFETIME`, 7 days). Every call site that
omitted the argument resurrected whatever the database had since revoked: a
demoted admin's staff flag, a deactivated account, a deleted user. Core's own
view had been exactly such a call site for as long as it existed.

A safe behaviour that each caller must remember is not a safe behaviour. The
django-layer provider now defaults to the database loader. `None` keeps its
documented meaning — re-mint from the token's claims, the framework-free
`TokenManager` behaviour — but has to be typed out, which makes it greppable
and makes it a decision. There is no legitimate use for it inside a Django
process.

*What can break:* a caller that passed nothing and relied on stale claims now
gets `None` when the database has no active user for the token. That is the
fix. A caller that genuinely wants claim-only re-minting passes `None`
explicitly.

### The deleted user, and why it is not closed here

A consumer also reported the profiles service serving the profile of a
**deleted** user. The verifying path in core does not trust claims — the
middleware, the DRF class, `JWTAuthBackend` and the channels middleware all
resolve their principal from the database through
`get_or_create_user_from_jwt()` — so with the default
`JWT_CREATE_USERS_FROM_TOKEN=False` a deleted user authenticates nobody. With
consumer mode ON, that same function **creates** the user from the token's
claims, and it cannot distinguish "a user this service has never seen" (the
whole point of consumer mode) from "a user this service deleted".

Closing that properly needs a deletion tombstone the issuer publishes and
verifiers consult — design work, routed rather than invented here. What 0.39.0
changes is that the existing remedy now works: banning the user as part of
deleting them (`blacklist_user`) reaches every peer service, because the ban
finally shares a namespace with them.

21 tests in `tests/test_revocation_namespace.py`; the cross-service ones
reproduce the stand exactly — two cache connections differing only in
`KEY_PREFIX` — and fail on 0.38.0 with "a token revoked in one service is
still valid in the next".

## [0.38.0] — 2026-08-24

### Security — the admin login view checked that you had a password, not that you were staff

**Upgrade note — every deployment that mounts `JWTCookieLoginView` (this is
the fleet's admin login) must read this. A consumer had it live in
production.**

`JWTCookieLoginView` (`django/jwt/login_views.py`) is the admin login view:
its template is `admin/login.html`, its redirect target is the admin index,
and its `dispatch()` refuses to *keep* a non-staff session. But it never
named an `authentication_form`, so Django fell back to the plain
`AuthenticationForm` — which checks `is_active` and nothing else — instead of
`django.contrib.admin.forms.AdminAuthenticationForm`, which enforces
`is_staff`. `form_valid()` then called `login()`, `jwt_provider.create_tokens(user)`
and `set_jwt_cookies()` with no staff check of any kind. The three `is_staff`
reads in that file all sat in the already-authenticated branch of
`dispatch()`; the credential-processing branch had none.

At the consumer that meant: **any active account's own username and password
minted a full fleet-wide JWT access/refresh pair.** The tokens are the
deployment's credential everywhere, so this walked past the deployment's
password-login gate, its lockout service (credential stuffing therefore ran
unthrottled), its TOTP step-up, and its tracked-session creation — the
resulting session had no tracked row, was invisible to session listings, and
survived both "log out everywhere" and password-change revocation.

The view is now staff-only, through two independent gates:

- `get_form_class()` resolves to `AdminAuthenticationForm` — imported lazily
  inside the method, because importing `django.contrib.admin.forms` at module
  scope pulls in the User model and raises `AppRegistryNotReady` for any
  project that imports this module before `django.setup()`.
- `form_valid()` refuses a non-staff user **again**, before `login()` and
  before any token is minted, and clears any stale auth cookies on the way
  out.

Both, deliberately. A subclass naming its own `authentication_form` — an
ordinary thing to do, to add a captcha — would otherwise silently reopen a
full authentication bypass; and the form alone would let a permissive
subclass report "logged in" and strand the user. The refusal is worded
exactly like Django's own ("...for a staff account"), so the response does
not confirm to an attacker that the password was correct.

**This is not configurable, on purpose.** A setting that can turn a staff
gate off is a staff gate that is off in whichever environment nobody audited.
A deployment that legitimately needs a non-admin cookie login writes a
different view.

*What can break:* a deployment that was using this view as its general user
login — which is the vulnerability, not a feature — will find non-staff
logins refused. Those users were receiving admin-grade credentials.

### Security — the refresh endpoint re-minted from week-old claims

`JWTRefreshView` (`django/jwt/views.py`) called
`jwt_provider.refresh_access_token(refresh_token)` **without** the
`load_user_by_uid` callback that `django/jwt/middleware.py` passes on both of
its refresh paths — where the comment already said why: otherwise "a revoked
staff role/flag would resurrect on refresh under REPLACE (AS-2)". So the one
*explicit* refresh endpoint was the one path that re-minted from the refresh
token's own claims, for up to `JWT_REFRESH_TOKEN_LIFETIME` (7 days by
default). Revoke an admin's staff flag, and their next refresh handed it
back.

It now passes the loader, exactly as the middleware does. A revocation or a
demotion takes effect on the next refresh, everywhere, with no path exempt.

*Still open, stated plainly:* this endpoint has no tracked-session
requirement and does not rotate the refresh token. Both are design work, not
a patch; "log out everywhere" therefore still rests entirely on the
blacklist.

### Security — an inactive account authenticated on every JWT path, and kept its session

The class behind the two defects above is the same one, one layer down:
`is_active` appeared nowhere in `django/jwt/` as a *rejection* condition —
only as a claim being serialized or written back. `django.contrib.auth.login()`
does not check `user_can_authenticate()`; that lives in `authenticate()`,
which every JWT path bypasses by design, because the credential it verifies
is a signature and not a password. Three consequences, all fixed here:

- **`get_or_create_user_from_jwt()`** (`django/jwt/utils.py`) — the one seam
  the middleware, the DRF authentication class, `JWTAuthBackend` and the
  channels middleware all resolve their principal through — now returns
  `None` for an inactive user. All four already treated `None` as
  "authenticate nobody", so all four inherit the gate and no fifth caller can
  forget it. The check runs *after* the claim sync, so consumer mode still
  reactivates a stale-disabled row from the authoritative claim first.
- **`EmailAuthBackend.get_user()`** (`django/jwt/session.py`) and
  **`JWTAuthBackend.get_user()`** (`django/jwt/backends.py`) — both overrode
  Django's `get_user` and dropped its `user_can_authenticate()` call. That
  method resolves `request.user` from the session on every request *after*
  the one that authenticated, so a deactivated account kept a live session
  for the whole life of the session cookie: deactivation only took effect at
  the next login, which is precisely the login that will not happen.
- **`load_user_by_uid()`** (`django/jwt/utils.py`) — the re-mint loader every
  refresh path now passes — refuses a deactivated user, so a refresh token can
  no longer outlive the account it speaks for.

*What can break:* a deployment that relied on `is_active=False` users
continuing to authenticate. Django's own contract has never allowed that.
`get_or_create_user_from_jwt()` returning `None` is the visible API change:
callers that used it as "sync this row and hand it back" now get `None` for
an inactive user. The write behaviour (GDPR-01: a bearer token never edits
account lifecycle in authoritative mode) is unchanged — read the row, not the
return value.

### The seam, so the next caller cannot reopen it

`MODULE.md` gains **JWT credential seam — who may receive tokens**: the three
choke points (`jwt_provider` for all minting, `load_user_by_uid` for every
re-mint identity, `get_or_create_user_from_jwt` for every resolved
principal), what each refuses, and the four holes of this class that are
*known and not yet closed* — logout blacklisting on an unverified decode,
`CsrfExemptAPIMiddleware` exempting session-cookie-only `/api/` requests, the
absence of refresh rotation and tracked sessions, and
`JWT_CREATE_USERS_FROM_TOKEN=True` being a staff-minting primitive by design
(default off). Listed so they are not rediscovered as news.

26 tests in `tests/test_admin_login_gate.py`; 13 of them fail on 0.37.0.

## [0.37.0] — 2026-08-24

### The serializer seam, written once instead of twenty-four times

`SerializerSeamMixin` is the seam that makes a stapel view swappable without a
fork: a host names a different serializer on a subclass and the library's HTTP
method bodies keep working. It is declared in system-design §8.1, it is quoted
in every library's MODULE.md — and the core never shipped it. So every module
wrote it: twenty-four copies across twenty-three libraries, byte-identical
below the docstring, one of them
inside `stapel-tools`' library template so that every *future* library is born
with the duplicate too.

Twenty-four copies of eight lines is not a maintenance cost worth a release on its
own. What makes it one is what the copies mean. A seam is a promise to a host
project, and a promise re-typed per library is a promise that can drift per
library — quietly, because nothing tests a mixin that only forwards attributes.
Two copies had already drifted (below), and neither drift was visible from any
other module. The framework's job is to make that class of divergence
impossible to write, not to notice it later.

`stapel_core.django.api.views` now carries:

- **`SerializerSeamMixin`** — the canonical superset: the two class attributes,
  the two getters, `None` meaning "this direction carries no serializer" rather
  than an error. Behaviourally identical to the twenty-one copies that agreed;
  a consumer deletes its local class, imports this one, and no call site moves.
- **`StapelAPIView`** — `APIView` + the seam + the two moves the hand-written
  bodies were already making at three hundred call sites:
  `validated_request_data(request)` and `serialized_response(payload)`. A view
  that asks for a request serializer it never declared now raises
  `ImproperlyConfigured`, not a 400: a view bug must not be reported to the
  client as their mistake.

Both are pinned by 23 tests over the semantics consumers actually rely on —
override resolution through the getter *and* through the attribute, per-request
getter overrides, MRO placement left of `APIView`, and the `None` direction.

**What was deliberately not unified.** Three of the twenty-four are real
divergences, not drift, and this release leaves them alone: stapel-auth
synthesizes its getters through `__getattr__` so a view with a dozen
serializers writes none; stapel-listings is a ViewSet module whose mixin hooks
DRF's own `get_serializer_class()` for per-action selection; stapel-mailtrap
declares the response direction only. Averaging those into the canon would have
been the same mistake as the duplication, in the other direction.

Consequently this mixin **never defines `get_serializer_class()`**. DRF's
`GenericAPIView` and every `ViewSet` already define it; a mixin sitting first in
the MRO that shadowed it would silently disable per-action serializer selection
in exactly the modules that need it most. That absence is a contract, and a test
asserts it.

Consumers are not migrated here — each removal is that library's own release.
MODULE.md carries the per-lib recipe (import, delete the local copy, floor-bump
the pin to `>=0.37.0`), including the two libraries that spell it
`SerializerSeamsMixin`, the one that folded it into a base view, and the
`stapel-tools` template that must be fixed or new libraries keep inheriting the
copy.

Additive only: nothing in 0.36.0 changes shape.

## [0.36.0] — 2026-08-24

### Observability, and a paginator that could not walk backwards

Three things, one theme: a signal that exists but cannot be followed is not
a signal.

#### `AnchorPagination` `direction=prev` returned the far end of the window

The bug is one missing `order_by`. A backward page filtered correctly — with
ordering `-id` and anchor 3, `id__gt=3` selects exactly the right rows — and
then ordered that filtered set by the **display** ordering and sliced its
head. On a newest-first list the head of `id__gt=3` ordered `-id` is the
newest row in the table, so paging back from anchor 3 in a ten-row set
returned 8, 9, 10. Not the adjacent page. The rows next to the anchor were
not merely mis-sorted, they were **unreachable**: every hop back landed on
the same extreme, so a client walking `prev` from the bottom of a list could
never arrive anywhere else. Ascending orderings had it symmetrically — the
oldest rows instead of the newest.

The `items[::-1]` a few lines down was the tell. It only makes sense over a
nearest-first fetch, which is what the query was supposed to be and never
was. So the fetch now uses the reverse of the declared ordering and the flip
back into display order becomes what it always claimed to be:

```python
# ordering="-seq", anchor=3, limit=3
# before: [10, 9, 8]   — the far end, and the same answer on every hop
# after:  [6, 5, 4]    — the page adjacent to the anchor, newest first
```

Two facts about the blast radius. First, the correct answer was already in
this repository: `eventstore.anchor.anchor_page` implements the same wire
contract for streams, and its `prev` branch reads ascending, trims, and
reverses — exactly right. One contract had two implementations and they
disagreed; the queryset one was the wrong one. Second, `stapel-chat` is the
only fleet consumer that exercises `prev`, and it had already noticed: its
history test asserts `len(seqs) == 2 and all(s > 2)` where every other test
in that class asserts an exact sequence. A loosened assertion is a defect
report written in the only language a test can write one in. With this
release that test can say `== [4, 3]`, which is what it meant.

Also fixed, in the same place: `prev` **without** an anchor served the head
page in reverse of the declared ordering — the one thing a paginator must
never do. There is no cursor to walk back from, so it now returns the head
page the way `next` would.

#### `stapel_core.observability` — the facade, built once

Every service reinvents four things, and the fleet had four different
answers to each: how a log line is shaped, where a number goes, where an
exception goes, and how any of them are tied together. The border is the one
we draw everywhere else — the framework **emits** through seams; Prometheus,
Grafana, Loki, Sentry and Alertmanager **collect and display**, and belong
to a deployment.

- **Structured logging.** `logging_config(service="chat")` is a `LOGGING`
  dict: one stdout handler, one JSON object per record, a mandatory field set
  (`ts, level, service, logger, msg, trace_id, span_id, correlation_id,
  causation_id, request_id`), exceptions decomposed into `exc_type` /
  `exc_message` / `stack`, and `REDACT_FIELDS` blanked **at the formatter** —
  a structured logger takes whatever `extra=` hands it, so the one place
  redaction cannot be routed around is the last one. `configure_logging()`
  for processes that never see Django settings.
- **Metrics facade.** `metrics.counter/gauge/histogram/timer(name, value,
  labels=…)`. A module instruments itself and never imports a client library;
  `METRICS_BACKEND` decides where the number lands — `PrometheusMetricsBackend`
  (default), `StatsdMetricsBackend`, `LoggingMetricsBackend`,
  `NoopMetricsBackend`, or yours. Two real backends, not one plus an
  interface, because a seam with a single implementation is a wrapper.
- **Error-reporting seam.** `report_error(exc, context=…, tags=…)` over
  `ERROR_REPORTER`, Sentry-shaped (`capture_exception` / `capture_message`
  with `level`, `tags`, `context`) and **no-op by default**. The default is
  the design: a framework that ships exceptions and their context to a third
  party unless you opt out has made a decision it has no standing to make.
  The in-flight trace ids ride along as tags, so the issue in the tracker and
  the lines in the aggregator are joined by `trace_id`.
- **Health/ready.** Not re-implemented. `/api/health/`, `/api/health/ready/`,
  `/api/health/live/`, `/api/metrics/` and `register_dependency_check` have
  shipped in `django.monitoring.health` for releases; they are re-exported
  (lazily) so the observability surface is one import.

Nothing in the facade raises into a caller. A missing client library, an
unreachable statsd, a label set that contradicts an earlier registration, a
reporter that itself fails — each is absorbed, logged once, and dropped.
Instrumentation that can take a request down fails precisely when the system
is already unhappy.

Optional dependencies degrade **with a name**. Without `prometheus-client`
the default backend still constructs, records nothing, reports `available =
False`, and check `W002` says so at `manage.py check` time; without
`sentry-sdk`, `SentryErrorReporter` does the same through `W003`. Never an
`ImportError` on a request path. All four checks (`W001` backend unbuildable,
`W002` unavailable, `W003` reporter, `W004` no `TraceContextMiddleware`) are
gated on evidence of adoption, so a service that never configured
`STAPEL_OBSERVABILITY` is not told about a backend it never asked for.

#### Correlation through the comm envelope — the part an APM cannot do

Generic Grafana and generic Sentry do not know about `stapel_core.comm`. A
request fans out into Actions and Functions across modules and services, and
without an id carried through, that is a scatter of log lines findable only
by reading everything in a time window. So the ids ride in the envelope:
`Event` gains `trace_id` (the whole operation), `span_id` (this hop),
`correlation_id` (the *business* operation, which may outlive one trace) and
`causation_id` (the message that caused this one — what makes a fan-out
reconstructible as a tree instead of a bag).

They fill from the ambient trace context at construction, so `emit()` inside
a request inherits it with no call site passing anything, and they are
`compare=False`: two events are the same event because of what they say, not
because of which trace observed them — every equality assertion written
before correlation existed still holds. `Event.from_json` restores them
explicitly instead of re-stamping with the reader's trace, which is exactly
the outbox relay's situation: it reads a row minutes later, in another
process, and the event still belongs to the operation that wrote it.

The loop closes on the far side. `comm.deliver()` and
`BaseBusConsumerCommand` bind `continue_trace(event)` around the handler, so
a subscriber's logs, metrics and its own emits join the operation that caused
them — no consumer subclass changes a line. `TraceContextMiddleware` starts
the trace at the edge, reading `traceparent` (W3C) or `X-Trace-Id` /
`X-Request-ID` / `X-Correlation-Id`, sanitizing every id it takes off the
wire (closed alphabet, length cap — an id from the network lands in every log
field and metric label the operation touches), echoing them on the response,
and recording `http_requests_total` / `http_request_duration_seconds` labelled
by the URL **pattern**, never the resolved path: a label with one value per
object id is how instrumentation takes down the system it measures.

Facade metrics need no wiring: `observability.exporter` registers the
backend's exposition into the `/api/metrics/` endpoint core already serves,
so a module's counter appears on the URL the deployment already scrapes — no
second endpoint, no second port, no scrape-config change.

Adoption is three lines:

```python
from stapel_core.observability import logging_config
LOGGING = logging_config(service="chat")
MIDDLEWARE = [..., "stapel_core.observability.middleware.TraceContextMiddleware", ...]
STAPEL_OBSERVABILITY = {
    "ERROR_REPORTER": "stapel_core.observability.errors.SentryErrorReporter",
}
```

#### Notes

- New extra `stapel-core[prometheus]` (`prometheus-client>=0.19`), folded into
  `[all]` and into `[dev]` — without it the suite would only ever exercise the
  degraded path of the backend every deployment gets by default.
- `docs/llms.txt` budget 4600 → 6400: the facade adds 25 called symbols across
  four surfaces plus two extension points. Raised, not fitted into by
  shortening intents — a trimmed-to-fit context file reads exactly like a
  complete one.
- Additive throughout. No existing signature, setting default or wire shape
  changed; the only behavior difference outside the new package is that
  `direction=prev` now returns the page it always claimed to.

## [0.35.0] — 2026-08-23

### The sixty lines every data owner writes by hand — `register_gdpr_owner`

stapel-gdpr 0.5.0 turned erasure into a protocol: the orchestrator creates
one `ErasurePart` per owner that claims the subject type, emits
`gdpr.erasure.requested`, and waits for a receipt. Nine libraries now answer
it — workspaces, profile, notifications, recordings, agent, docs, media,
billing, translations — and all nine answer it with **the same code**,
copied. Not similar code: the same receipt id derivation, the same
transaction, the same four guards, the same probe handler beside the same
erasure handler. The ninth copy (billing 0.9.0) was written the day this
release was queued.

A protocol implemented nine times is a protocol that disagrees with itself
nine ways, and the failure mode is not a crash — it is a receipt that says
an erasure happened. So the protocol now lives here, once:

```python
# stapel_recordings/apps.py
def ready(self):
    from stapel_core.gdpr import register_gdpr_owner
    from .erasure import erase_subject

    register_gdpr_owner(
        "recordings",
        ["account", "workspace", "meeting", "recording"],
        erase_subject,
    )
```

The library keeps exactly what is its own: `erase(subject_type, subject_key,
workspace_id) -> counts | None` — idempotent, counting what it removed,
`None` for "this key names nothing of mine". Everything around it is
protocol, and every arm of it is what the copies converged on:

- **`gdpr.erasure.requested`** → erase, then `gdpr.section.erased` carrying
  `counts` and a deterministic `receipt_id`
  (`<owner>:<subject_type>:<subject_key>:<correlation_id>`) **emitted inside
  the erase's transaction**. Derived rather than random, so an
  at-least-once redelivery mints the SAME receipt instead of a second one
  the audit trail cannot follow back; inside the transaction, so the receipt
  leaves iff the erasure committed. An owner that receipts a rollback is
  worse than an owner that stays silent — the orchestrator counts the
  receipt and finalizes.
- **A subject type this owner does not claim** → ignored, no receipt. The
  orchestrator created no part for it; answering would be answering for
  somebody else.
- **A malformed payload** → logged and dropped, not raised: a payload this
  shape will never parse, and raising would redeliver it until the broker
  gives up. **A key `erase` cannot parse** (`TypeError` / `ValueError` /
  `ValidationError`) → logged and **never receipted**; it names no row here,
  so receipting would claim an erasure that never happened. Anything else
  propagates, which is what redelivery is for.
- **`gdpr.owner.probe`** → `gdpr.owner.alive {owner, subject_types}` from
  the same module as the eraser. That co-location is the entire point of the
  probe: the answer is evidence that the erasure subscriber is *consumed*,
  not that a container is running somewhere. It reports the types registered
  in this process — a probe that echoed the host's `DATA_OWNERS`
  declaration back would confirm nothing.
- **`user.deleted`**, the pre-0.5.0 account signal stapel-gdpr keeps
  emitting for one minor (`legacy_user_deleted=True`, and only for an owner
  that claims `account`) → the same `erase("account", …)`. There is no
  second erasure implementation to drift, and when the handler goes no
  erasure logic goes with it.

Registering one owner name twice with the same terms is a no-op — a
`ready()` that runs twice must not double-subscribe; with different terms it
raises, because the second registration would silently win for some payloads
and lose for others.

`stapel_core.gdpr.pseudonymize(value, prefix="erased:")` is the same
collapse for the other duplicated function: the fleet's one keyed-HMAC
funnel (HMAC-SHA256 under `SECRET_KEY`, truncated to 32 hex, `erased:`-
prefixed and therefore idempotent), which stapel-video, stapel-agent and
stapel-billing each carried. A ledger-carrying owner erases the ids that
NAME the person and keeps the economics — the bill is the product's record,
without the person. A `SECRET_KEY` rotation splits pseudonyms; documented
and accepted fleet-wide, the alternative being a second key nobody rotates.

`gdpr.py` is a package (`gdpr/`) now — same import path, same exports, plus
`gdpr/owners.py`. The pre-0.5.0 `GDPRProvider` / `gdpr_registry` /
`GDPRServiceConsumerCommand` surface is untouched.

**Migration.** New owner libraries MUST use the helper. The nine that
predate it migrate on their **next minor** — not in this release: adopting
it bumps their `stapel-core` floor to 0.35.0, and nine floor bumps for a
refactor nobody asked for is not a migration, it is a fleet-wide rebuild.
The migration itself is a deletion: drop the library's `actions.py` GDPR
handlers and its `_receipt_id` / `_emit_receipt` / `pseudonymize` copies,
call `register_gdpr_owner` from `ready()`. MODULE.md ("Data owners") states
this, and the surface entry names it as what to reach for instead of a
hand-written `actions.py`.

### `eventstore.purge` — an erasure has no cut-off date

`purge(stream, older_than=…, filters=…)` has had `filters` since 0.24.0, and
a subject-scoped erasure still had to name a time it did not have: the bound
was mandatory, so "forget everything about this workspace" was written as
"delete everything about this workspace older than now" — a gate whose
correctness rests on a clock.

- `older_than` is optional. `purge("ws.audit", filters={"workspace_id": w})`
  removes that subject's whole history and leaves everybody else's alone.
- At least one bound is required. `purge(stream)` reads like "purge this
  stream" and would delete every row it has ever held, so it raises
  `ValueError` naming both ways to bound it.
- A backend whose `purge` predates `filters` — backends are a public seam
  (`STAPEL_EVENTSTORE["BACKEND"]` / `ROUTES`), and a store written against
  the old signature is still a valid `EventStore` — now raises
  `PurgeFiltersUnsupported` when handed one, instead of the `TypeError` that
  used to surface at the call site. Silently purging by time instead would
  erase rows nobody asked about and keep the ones somebody did. Unfiltered
  retention still reaches such a backend unchanged;
  `purge_accepts_filters(backend)` is the signature check behind it, exposed
  because a caller that must choose deserves to ask.

The Postgres/SQLite backend applies payload filters through the same JSON
lookup `query` uses, so what a filter selects for deletion is exactly what
it selects for reading.

## [0.34.0] — 2026-08-22

### A durable NATS consumer silently ignored every config change after its first boot

**The defect class: a seam that is silent on both sides.** A service adds a
subscription topic. Its worker starts, logs
`consuming durable=chat-service_actions subjects=[…, 'stapel.evt.user.created', …]`
— exactly the widened list the code declares. Publishers publish. The
events reach the stream. The handler never runs, not once, for as long as
the durable lives. Nothing raises, nothing warns, no metric moves; the log
line the operator would check to diagnose it is the one telling the lie.

The cause is in nats-py, and it is one line of control flow:

```python
# nats/js/client.py — pull_subscribe()
if durable:
    await self._jsm.consumer_info(stream, durable)
    should_create = False      # ...and `config` is never looked at again
```

`pull_subscribe(durable=…)` BINDS to an existing consumer and throws away
the `ConsumerConfig` it was handed. A durable outlives the process that
created it, so the consumer keeps whatever `filter_subjects` it was born
with — forever. Every topic added after the durable's first boot is
delivered to a filter that does not match it. This was proven live, hop by
hop, on a client fleet.

Nothing about this is specific to subjects. `ack_wait` drifts the same
silent way (a durable born with the 30s default redelivers messages a
handler is still retrying), and so does `ack_policy` (`none` makes every
`msg.ack()` a no-op).

**The fix — reconcile on bind, or refuse to run.** `NatsJetStreamBus`
now fetches the live consumer config before binding and compares it,
field by field, against the declared one:

- **Reconcilable** (`RECONCILABLE_FIELDS`) — `filter_subject(s)`,
  `ack_wait`, `max_deliver`, `max_ack_pending`, `backoff`, `description`,
  `metadata`, `inactive_threshold`, `num_replicas`, `rate_limit_bps`,
  `sample_freq`, `headers_only`. Drift here is **updated in place**: the
  edit goes through `CONSUMER.DURABLE.CREATE` against the existing durable
  (`js.update_consumer` where the client has one, `js.add_consumer`
  otherwise), which the server applies without touching `delivered` or
  `ack_floor`. Verified on nats-server 2.14.5: publish 3, ack 2, widen the
  subjects — `ack_floor.consumer_seq` stays `2` and the next delivery is
  message #3. The consumer is **never** deleted and recreated; that would
  reset the floor and replay the entire stream through the handler.
- **Immutable** (`IMMUTABLE_FIELDS`) — `ack_policy`, `replay_policy`,
  `deliver_subject`, `deliver_group`, `max_waiting`. The server will not
  change these on a live consumer, so the boot **fails loudly** with
  `ConsumerConfigConflict`, naming the durable, the drifted fields and
  both subject sets. A crash-loop with a named cause beats silent
  deafness; whether to rename the group or accept a replay is an
  operator's call, not a boot sequence's.
- **Start position** (`START_POSITION_FIELDS`) — `deliver_policy`,
  `opt_start_seq`, `opt_start_time` — is compared by neither list. These
  decide where a consumer *starts*; an existing durable is long past that
  point, and a difference is history rather than drift.

Fields the backend does not declare are left alone: an operator who tuned
`max_ack_pending` by hand keeps their tuning.

Whatever the server answers, the reconciliation is verified against it
afterwards (`assert_consumer_matches`), for freshly created durables too —
so a server that accepts an edit without applying it also cannot leave a
deaf worker running. Startup now logs one of

```
NatsJetStreamBus reconciling durable=… in place — drift: {…}. Subjects before=[…] after=[…]
NatsJetStreamBus durable=… verified against the server: subjects=[…]
NatsJetStreamBus consuming durable=… (matched|reconciled|absent) subjects=[…]
```

**Behaviour change** (hence the minor, not a patch): a consumer whose live
config cannot be reconciled now refuses to start where it previously
started and received nothing. Deployments carrying such a durable will see
the crash on the next deploy — which is the point.

New public names in `stapel_core.bus.backends.nats`:
`ConsumerConfigConflict`, `reconcile_durable`, `assert_consumer_matches`,
`consumer_drift`, `subject_set`, `RECONCILABLE_FIELDS`,
`IMMUTABLE_FIELDS`, `START_POSITION_FIELDS`.

## [0.33.2] — 2026-08-22

### Fix — `replay_done` was missing from `RESERVED_FRAME_TYPES`

`stapel_core.comm.signals.RESERVED_FRAME_TYPES` was meant to mirror every
frame type the `stapel-realtime` wire protocol owns, so that `signal()`
refuses a module that tries to name a courtesy frame after one of them. The
substrate's `REPLAY_DONE` (`"replay_done"`, sent to mark the end of a
resumable client's catch-up window) was never added to the core's set —
`stapel-realtime`'s own cross-package test
(`tests/test_envelope.py::TestCoreAgreement::test_the_protocol_owns_exactly_the_names_the_core_reserves`)
pinned this exact gap as `extra == {"replay_done"}`. Left unfixed, a module
could legally `signal(stream, "replay_done", …)`, and a resuming client
would misread that courtesy frame as the end of its own replay.

`replay_done` is now reserved alongside the other ten protocol frame types
(`hello`, `welcome`, `replay`, `live`, `ephemeral`, `ping`, `pong`,
`resync`, `kick`, `error`); `signal()` raises `InvalidSignalType` for it,
matching the other reserved names.

## [0.33.1] — 2026-08-22

### Fix — `Event.to_json()` dropped the partition key

`to_json()` popped `key` before serialising, so a keyed event lost its
routing key on any round trip through a storage/wire boundary — the
transactional outbox is exactly that: `event_json = event.to_json()` on
write, `Event.from_json(row.event_json)` on delivery (`django/outbox/models.py`,
`django/outbox/relay.py`). Once `event.key` came back `None`,
`KafkaBus.publish()` fell back to `event.event_id` — a fresh random UUID
per message — for the producer partition key. Same-key events (e.g. every
`listing.published` for one listing) landed on different Kafka partitions
instead of one, so per-key ordering silently degraded to round-robin.
Direct publishes (no outbox — `OUTBOX_ENABLED=False`, or the in-memory/NATS
transports) never round-tripped through JSON with the key attached, which
is why nothing caught it: NATS partitions by subject, not by this field.

Fix is additive: `key` is now included in the envelope JSON, and
`Event.from_bytes`/`from_json` read it back with `d.get("key")`, so
payloads written before this release (no `key` field at all) still
deserialize with `key=None`, unchanged from today's behavior.

Also noted while reading this path, filed as a separate concern rather
than fixed here: the outbox table (`OutboxEvent`) has no retention/purge —
dispatched rows accumulate forever. Tracked in
`stapel-realtime-design.md` §11.5's adjacent-defects note; no GitHub issue
exists in `usestapel/stapel-core` yet.

## [0.33.0] — 2026-08-22

### Signal — the fourth comm primitive

Action, Function and Task all address **code**. The meaning none of them
covers is the one every module ends up wanting: *"show this to a live
observer, if one is watching."* `stapel-realtime-design.md` §3 names it and
this release lands the emitter half:

```python
from stapel_core.comm import signal

signal(f"recordings:ws:{workspace_id}", "recording.status",
       {"recording_id": str(rec.pk), "status": rec.status})
```

At-most-once and ephemeral: delivery only to whoever is connected at the
moment of the emit, ordering only within one stream key, never ahead of the
transaction it describes (`transaction.on_commit`, no outbox row). Nothing
else is promised — a lost frame is correct behaviour, because the truth stays
in the DB behind REST and a signal is a reason to refetch, not the state
itself. That is exactly what separates it from an Action: an Action is a debt
to the system (outbox, at-least-once, subscribers obliged to handle it), a
Signal is a courtesy to a screen. The canonical bridge runs the useful way
round — an `@on_action` handler turning a committed fact into a `signal()`.

Addressing is canonical and validated on every emit:
`<mod>:<scope_type>:<scope_id>[:<topic>]`, built with `stream_key()`. The
scope is *part of the name*, so a group physically cannot cross a workspace;
a malformed key raises `InvalidStreamKey` even with delivery switched off, so
the no-op default still gates the canon. The wire v1 envelope is
`{"v", "type", "stream", "payload"}` — `stream` optional in the schema but
populated from day one (that field is what makes a multiplexed socket
possible later without breaking the envelope), and deliberately no `seq`:
frame kind is structural, so an ephemeral frame can never be persisted as
journal state.

Delivery is an axis, `STAPEL_COMM["SIGNAL_TRANSPORT"]`, closed by default.
Core ships the emitter only — stdlib, no channels, no redis, no ASGI —
because pulling Channels into core would tax 26 libraries for a surface three
modules have and no host mounts, while putting the emitter in the delivery
library would make `recordings` depend on Channels for one call. Emitting has
to be free or modules quietly stop signalling. The separate `stapel-realtime`
library registers itself into the seam with
`register_signal_transport("channels", …)`; the contract is
`transport(stream_key, frame)`, called after commit, allowed to fail (the
frame is dropped and logged — a courtesy to an observer must never break the
caller). A misconfigured axis would otherwise be the perfect silent failure,
so it is boot-gated: system check **`stapel_core.comm.E003`** fails a host
whose `SIGNAL_TRANSPORT` names nothing importable and callable.

### realtime-check — the border that stops the fifth WebSocket stack

The fleet already has four independent realtime implementations (video lobby,
chat, studio-dialog, runner-protocol); three of them re-invent the same 80% —
JWT on the socket, close codes, resume protocol, group fan-out — and no host
mounts any of the three. CI never noticed, because every library is green in
isolation. Per the canon, the fix is a mechanism, not a paragraph:
`python -m stapel_core.lint.realtime_check .` (wired into CI next to
emit-check) draws the border by asking who is on the other side of the
socket. A human in a browser → `stapel-realtime`, emitting through
`comm.signal()`. Our own process → a named application protocol
(`stapel-runner-protocol`) that owes an answer to "why not a Function/Task".

Errors: **RT001** a Channels consumer, **RT002** hand-rolled socket auth
middleware (the fleet has one home, `stapel_core.django.jwt.channels`),
**RT003** a raw `websockets.serve()` server. Warnings: **RT004** a
hand-rolled SSE endpoint, **RT005** direct channel-layer fan-out instead of
`comm.signal()`. Escape hatch for a genuine one-off:
`# realtime-check: ok — <reason>`. The four existing implementations are
grandfathered by an allowlist qualified by distribution name (the fleet's
flat layout makes a bare `consumers.py` suffix meaningless), where every
entry names the migration phase that deletes it — a debt register that
shrinks and never grows.

## [0.32.0] — 2026-08-21

### emit-check gains EMIT005 — a declared `emit_*` helper nobody calls

`stapel_listings/events.py`'s `emit_listing_updated` was fully written and
schema-backed, and had zero call sites anywhere in the package: an event the
module promised but never actually fired, invisible to CI because the
existing emit-check rules (EMIT001-004) only look at how an emit call is
*wrapped*, never at whether the wrapper itself is *used*. The stapel-search
design doc's contract gate (`stapel-search-design.md` §11) names this the
same "declared ⇒ unwired" disease its own index-lint layer guards against,
and asks for the fix to land in the emit gate directly, ahead of any
stapel-search code.

`lint/emit_check.py` now also flags **EMIT005**: a module-level `emit_*`
function (never bare `emit` — that name is the library primitive, meant to
be called by every *consumer* of a package, not from within it) with no
call site anywhere in the files given to one invocation of the tool. The
check pools every declaration and every call name across the whole scanned
tree first, since the realistic shape is cross-file — the helper lives in
`events.py`, the call site is in a service or view module elsewhere in the
same package. Suppress a helper that is genuinely wired only from outside
the repo with the same `# emit-check: ok — <reason>` pragma the other four
rules already use, appended to the `def` line.

Known limitation, by the same pragmatic-AST-pass design as EMIT001-004: only
module-level `def`/`async def` nodes are seen (a nested function or a class
method named `emit_*` is invisible in both directions), and only actual
`ast.Call` sites count — a helper only ever passed by reference (e.g.
`signal.connect(events.emit_foo)`) reads as unwired and needs the pragma.

## [0.31.0] — 2026-08-21

### `request_notification` gains `telegram_chat_id`, the third direct address

stapel-notifications 0.13.0 made telegram a fourth delivery channel, and its
consumer already reads `telegram_chat_id` off the `notification.requested`
payload alongside `email` and `phone`. Nothing could put it there: the core
producer helper had no such argument, and the emit schema is
`additionalProperties: false`, so a producer publishing the event by hand was
the only way through. The direct-address family was two thirds of the way
across the seam.

`telegram_chat_id` now behaves exactly like the two addresses beside it — a
`str | None` keyword, always present on the payload, `["string", "null"]` in
`notifications/schemas/emits/notification.requested.json`, counted by the
recipient guard (a telegram-only request publishes instead of returning
`False`) and usable as the partition key when it is the only address given.

Additive on both sides: the new schema property is optional, an older
producer's payload still validates, and a consumer that ignores the key sees
what it saw before. The immediate caller is stapel-forms' resend-to-telegram
path, where the destination chat comes from the form's own configuration
rather than from a `UserContact` row.

## [0.30.1] — 2026-08-16

### Fixed — the contract artifacts, which 0.30.0 shipped stale

0.30.0 was tagged with `docs/capabilities.json` still saying 0.29.0: the
version was bumped without re-running `make contract`. Four contract tests
caught it in CI and the release never published. No code differs from 0.30.0;
this is the artifacts catching up. Tracker #169, verbatim.

## [0.30.0] — 2026-08-16

### A configured seam that nothing routes to is not a hole

`check_shipped_scope_provider` takes `surface_mounted` (keyword-only, default
`True`, so callers that have not started measuring keep today's behaviour). When
it is `False` the finding degrades from Error to Warning.

The case is real and the fleet uses it on purpose: a module installed for its
provider seam and its subscribers, with its own URL surface left unmounted,
because the host owns the rooms/boards and the library owns a provider or two.
There the shipped scope provider decides nothing — nothing routes to the code
that would consult it. Refusing that boot demands a provider that provably never
runs. meettoday hit exactly this on 2026-08-16: `stapel_video.E009` kept the
sandbox backend down over a tenancy hole the deployment does not have, while the
same file's `E008` was already gating on a URLconf walk and passing.

Warning rather than silence, because the measurement is honest about its own
limit: a URLconf walk cannot see a host calling the module's `services` from its
own Python. The reading is "configured open, consulted by nothing today, and
that changes the day you mount it" — and the hint says so.

### `module_urls_mounted` — the walk itself moves into core

`stapel_core.django.mounts.module_urls_mounted("stapel_x")` answers whether any
view from a module is reachable in this deployment's URLconf. Walked, not
reversed: a host may mount an include under any prefix and namespace, and a
`reverse()` by name would read that as "not mounted". An unloadable URLconf
answers `True` — Django's own url checks report that, and a caller must not turn
one defect into two.

It was a private helper copied inside stapel-video. Every module that ships a
scope seam needs the same question, so the mechanism belongs one layer down
rather than re-copied per library.

## [0.28.0] — 2026-08-16

### A one-time code is not a row

`stapel_core.verification.codes.OneTimeCodeStore` — the TTL-scoped, hashed
store an OTP flow keeps its codes in instead of a table. It joins the challenge
and grant stores already in this package: the mechanism lives here, the policy
(lifetime, attempt budget, code length, delivery) stays with the caller and is
passed in on every call.

Two defects of the table are closed by construction. **The code no longer rests
in the clear**: what is stored is an HMAC-SHA256 digest keyed by `SECRET_KEY`
and salted per entry, so a reader of the store holds nothing replayable, and a
six-digit code's million preimages cannot be swept offline without the app
secret — which is what would have made a bare digest decorative. The
identifier is hashed into the key too: cache keys are readable to anything that
can `SCAN` the instance, and a plain one would publish who is signing in right
now. **Expired entries no longer need a sweeper**: the entry's TTL *is* the
code's lifetime, so nothing accumulates and no host has to remember a beat
schedule for it.

**Absence and wrongness are different facts**, and `check()` refuses to fold
one into the other. `NOT_FOUND` means the wait expired — aged out, already
spent, or the cache restarted — and the honest answer is an invitation to start
over, not "invalid code". `MISMATCH` means the digits were wrong. Telling a
user they made a mistake when the system merely stopped waiting is the same
defect as rendering "we could not ask" as "you may not".

**The attempt budget lives inside the entry**, not beside it: one record, one
TTL, one death. A counter outliving its code re-blocks a fresh request; a code
outliving its counter hands back an unlimited guessing budget. The block is
deliberately a separate key with its own lifetime, because a block must survive
the code it killed. A wrong guess bumps the counter without touching the
deadline — guessing must not extend the wait.

**Everything fails closed.** An unreachable cache yields `UNAVAILABLE` from
`check()` and raises `StoreUnavailable` from the write paths; it never yields
`OK`, and `send_wait()` refuses to read an outage as "no limit applies".

Redis is not durable and this is a deliberate acceptance, not an oversight: a
restart drops every pending code, the user requests another, and because a
dropped entry is indistinguishable from an aged-out one, the "the wait expired,
ask again" message is already true for that case.

## [0.27.0] — 2026-08-15

### The layer that lied

A live stand ran twelve hours against an unmigrated schema while reporting
healthy, because every layer was allowed to stay silent. Three of the classes
behind that are closed here.

**The schema probe belongs in the framework.** `stapel_core.django.monitoring
.schema_health` answers "is the running code's schema at head" — lifted from a
product-local copy that was duplicated per service because there was nothing
to import from. `CommonDjangoConfig.ready()` registers it, so a service gets
the answer without wiring anything, and a product carrying its own copy can
delete it. Its earned properties are the point and are pinned by tests: a
determined verdict is cached for 30s while a **non-answer is never cached** (a
pinned "I could not tell" makes a two-second blip outlive itself); a database
error is a `warning` with **no stack** and anything else gets one; and
`schema_at_head` is **omitted** when undetermined rather than dropped to zero,
so a drift alert has nothing to fire on when nobody could ask. Registered
non-critical: drift must not pull every backend out of rotation during a
normal rolling migration.

**The dependency-check contract has a third state.** `register_dependency_check`
did `ok = bool(probe())`, so a sentinel meaning "unknown" coerced to `True`
and rendered as healthy — a third state was not merely unsupported, it was
silently wrong. A probe may now return `None`. `/api/health/` reports it as
`"unknown"`, distinct from `"error"`; `stapel_dependency_up` is **omitted**
for that dependency while the always-emitted `stapel_dependency_probe_ok`
drops to `0`; and — the part that matters most — `readiness_probe` does
**not** 503 on an undetermined critical dependency. An inability to ask is not
proof the dependency is down, and taking a service out of rotation on it
converts a blip into an outage, since every replica loses the same probe at
the same instant. A probe that *raises* is still an error, deliberately: "I
could not ask" is said on purpose, by returning `None`, so it can never be
confused with a bug in the probe.

**A settings list from the environment is refused, not silently mangled.**
`AppSettings._raw` returned `os.environ.get(key)` as a raw string with no
parsing, so `DATA_OWNERS=auth,profiles` was iterated character by character
into thirteen owners named `a`, `u`, `t`, `h`, `,` — every one of them a
`str`, so the type checks passed and erasure was certified against nonsense.
Any key whose declared default is a `list`/`tuple`/`set`/`frozenset`/`dict` now
raises `ImproperlyConfigured` naming the key, the shape and the reason, and
the new `stapel_core.conf.E002` system check finds it at `manage.py check`
time instead of at whatever first read happens in production. Refusing beats
parsing: `DATA_OWNERS` entries are legally a bare name *or* a dict, so any
format chosen here is right for some values and lossy for others. Scalars are
untouched — the environment is what they are for.


### i18n — a monolith's own app can satisfy the registry gate honestly

The pairing gate resolved a package's registry export at `<top-level package
dir>/docs/errors.json` and nowhere else. A monolith's local apps have no such
place: `accounts`, `rooms`, `calendar_app` are apps inside one project, not
wheels with a `docs/` of their own, so a product's four apps produced four
E `no_registry_export` findings for doing nothing wrong. A gate an entire
supported topology cannot satisfy is not a gate, it is a tax.

Where the export lives now follows what the package **is**. A distributable
still carries it inside its wheel — the only place a consumer who installed it
can look — and that is what the resolver reads first. A package with no
installed distribution, living inside the project root, is declared instead by
the project's own export: `<BASE_DIR>/docs/errors.json`, or
`STAPEL_I18N["REGISTRY_EXPORT"]` where a project keeps it elsewhere. That is
the artifact `generate_error_keys` already writes and projects already commit,
so a monolith satisfies the gate with the file it has, not with an exemption.

Both reasons the gate exists survive intact. A library shipping catalogs with
no registry export is still red **inside a project whose export declares every
one of its codes** — something that ships as a wheel carries its own export or
it has none, and no project export stands in for it (pinned against a real
installed distribution, dist-info and all). And the project export answers for
a code only where it attributes that code to that app: it cannot become a
place to launder a neighbour's keys, and a translated key the export does not
claim for its owner is `unexported`. An entry that attributes nothing still
counts — a pre-`owner` artifact declares its codes, exactly as an un-attributed
key counts as owned in `owned_keys`.

`STAPEL_I18N["REGISTRY_EXPORT"]` is env-closed: it decides which file the gate
accepts as a project's declaration of its codes, and a stray variable of that
generic name would turn a red into a green from outside the repo.

Three mechanisms that existed and reached nobody. A predicate with no
consumers, a setting nothing read, and a guard nothing called.

### The third principal state

The fleet's authorization vocabulary has two words — anonymous and
authenticated — and views treat the second as sufficient. It is not. A
registered account with no accepted, unsuspended membership in any workspace
is neither: it is a **guest**, and stapel-workspaces has said so since the
mandate-model vardict (`permissions.is_guest` / `has_active_mandate`). That
predicate had **zero consumers outside its own package**, and in a split
deployment no sibling could reach it: the workspaces comm surface publishes
`check_membership` and `check_capability`, both workspace-scoped, and neither
answers "does this user hold a mandate anywhere".

`stapel_core.django.mandate` is that missing word. `MandateState` has three
values — `ANONYMOUS`, `GUEST`, `MANDATED` — and a fourth outcome that is not
one of them: `MandateLookupUnavailable`, for a question that could not be
asked. A failed lookup is never reported as `GUEST`. `HasWorkspaceMandate`
(`django.api.permissions`) is the DRF gate: three answers, and a 503
(`error.503.mandate_unavailable`) where a caller might have expected a 403,
because an unanswerable authorization question degrades to refusal, not to a
verdict about the user.

**`IsNotAnonymousUser` is untouched and still means what it says.** Widening
it was the option not taken: a class that reads as "is a real user" must not
quietly start meaning "holds a mandate", and that exact confusion is why an
earlier fix missed. The two classes stay two, and the surface index now says
so (`instead_of` names the older one).

The question travels over the comm Function `workspaces.check_mandate` —
reachability decided by `function_unreachable_reason`, so it is right for
every transport — and falls back to the in-process predicate when
`stapel_workspaces` happens to be installed, which is what lets a monolith use
this today. **The provider half belongs in stapel-workspaces** and is not in
this release; `MANDATE_SCHEMA` / `MANDATE_RESULT_KEY` are the contract it
implements.

A deployment wired for neither refuses loudly: every mandated view answers
503, `stapel_core.mandate.E001` names the views and the missing wiring at
`manage.py check` / `stapel_preflight`, and the finding is security-critical
so no blanket line mutes it. Not on the boot-gate roster — it resolves the
URLconf, the re-entrancy trap that list already excludes `stapel_mounts` for.

**Cache:** answers are cached per user for `STAPEL_MANDATE_CACHE_SECONDS`
(default 30, `0` disables). Invalidation is not a TTL alone:
`workspace.member_removed` and `workspace.member_suspended` drop the entry as
they arrive, wired from `CommonDjangoConfig.ready()`. The TTL bounds the bus
failing, not the normal path. Grants may lag by up to the TTL (that direction
fails toward refusal); a non-answer is never cached at all.

### `SILENCED_SYSTEM_CHECKS` becomes visible

Nothing in the fleet read it. stapel-core, stapel-auth, stapel-workspaces and
stapel-tools name it only in check hints ("silence with SILENCED_SYSTEM_CHECKS
if ..."), so any project could mute any library's security check with one line
and leave no signal for an operator, a reviewer or a gate.

`stapel_core.django.check_guard` reports what is being silenced (W001) and
refuses the blanket route for checks a library declares security-critical
(E001). Two halves, and neither can drift from the other:

* **the marking lives with the check** — `declare_security_critical(id, why)`
  returns the id, so the module constant *is* the declaration;
* **the finding refuses to go quiet** — `SecurityCriticalError` /
  `SecurityCriticalWarning` override `is_silenced()`, so
  `SILENCED_SYSTEM_CHECKS` does not apply to them.

The route out is per-check, explicit and greppable, and carries a reason:

```python
STAPEL_SECURITY_CHECK_WAIVERS = {"stapel_auth.E004": "why this deployment is different"}
```

A waiver reports itself at every boot with its reason (W002); a blank reason
waives nothing (E002); a waiver for a non-critical id says so (W003).

**Breaking, on purpose:** four of core's own checks are now marked, so a
deployment silencing any of them by the blanket route goes red —
`stapel_core.cors.E001` (allow-all origins with credentials),
`stapel_core.auth_backends.E003` (a backend that resolves a principal without
checking a secret), `stapel_core.blacklist.W001` (the revocation fail-open
hatch), plus the new `stapel_core.mandate.E001`.

### `guard_secret` stops being a call somebody has to remember

`django/prodguard.py` has shipped `guard_secret` (placeholder prefixes,
`MIN_SECRET_LENGTH = 50`) and `guard_db_password` since 0.8.1, imported by
exactly one thing: the prod settings tier stapel-tools *generates*. A project
not scaffolded by `stapel-create-project`, or scaffolded before the template
grew the call, gets nothing — and nothing detects the absence, because
`manage.py check` cannot report an `ImproperlyConfigured` a settings module
never raised. That is how a six-character `SECRET_KEY` boots production.

The same two functions now run from the check registry every project already
inherits (`stapel_core.django` in `INSTALLED_APPS`), on the boot-gate roster
so they reach gunicorn, where the settings-module call would have run and
didn't. The check *calls* the guards rather than restating their rules. No
finding ever carries a value.

`STAPEL_PRODGUARD` is `"auto"` (default) | `"enforce"` | `"off"`. Auto
enforces in any process that is neither `DEBUG` nor a test run — a library's
own suite configures a short fake `SECRET_KEY` and no `DEBUG`, and a check
that turns every repo's CI red is a check that gets silenced wholesale on day
one. `"off"` reports itself (W001); an unreadable value means auto, never off.
`STAPEL_PRODGUARD_SECRETS` names further settings to hold to `guard_secret`.
The database password is only asked for where the engine has one.

### Added

- `stapel_core.django.mandate`: `MandateState`, `mandate_state`,
  `has_mandate`, `MandateLookupUnavailable`, `mandate_seam_unreachable_reason`,
  `invalidate_mandate_cache`, `subscribe_mandate_invalidation`,
  `MANDATE_FUNCTION` / `MANDATE_SCHEMA` / `MANDATE_RESULT_KEY`.
- `django.api.permissions.HasWorkspaceMandate`, `MandateUnavailable`.
- `stapel_core.django.check_guard`: `declare_security_critical`,
  `SecurityCriticalError`, `SecurityCriticalWarning`, `security_critical_ids`,
  `waivers`, `STAPEL_SECURITY_CHECK_WAIVERS`.
- `django.prodguard.check_production_secrets`, `prodguard_mode`.
- `error.503.mandate_unavailable` (+ ru/es catalogs).
- Boot-gate roster: `stapel_check_guard`, `stapel_prodguard`.

### Upgrade

Two of these can refuse a worker, which is what a minor bump is for. Before
deploying, run `manage.py check` (or `manage.py stapel_preflight`) on each
service and read the findings:

1. `stapel_core.prodguard.E001`/`E002` — a placeholder, empty or under-50
   character `SECRET_KEY`, or the shipped database password. This is a real
   finding about a real deployment; fix the value. `STAPEL_PRODGUARD="off"`
   exists for a staged rollout and reports itself every boot.
2. `stapel_core.check_guard.E001` — a `SILENCED_SYSTEM_CHECKS` entry that is
   now security-critical. Either drop the silencing and fix the finding, or
   move the id to `STAPEL_SECURITY_CHECK_WAIVERS` with a reason.

Nothing else changes behaviour: `HasWorkspaceMandate` is opt-in per view, and
`stapel_core.mandate.E001` is silent until a view uses it.

## [0.26.0] — 2026-08-15

The error registry and the error catalogs are two halves of one contract, and
nothing connected them. An ownership move stripped ten GDPR keys from a
service's catalogs while its registry kept declaring them; the codes existed,
no catalog carried them, and a Russian user read English sentences. No gate
went red anywhere. This release makes that shape impossible.

### The two scopes both stay — they answer different questions

`generate_error_keys` is instance-scoped and stays that way: its companion is
`schema.json`, and a consumer of a service needs every code that service can
emit, including codes owned by co-mounted modules. `translate_catalog` is
ownership-scoped and stays that way: that scoping is what removed 410
duplicated entries. The defect was never either scope. It was that no artifact
and no gate joined them.

### `owner` per registry entry

`build_error_registry()` now emits `owner` alongside `code`, `status`,
`params`, `remediation` and `en`. A consumer can pair a code with the owning
package's catalogs without knowing the mount graph — that implicit knowledge
is what broke.

**Breaking:** the registry shape changed, so every repo's drift gate goes red
until `docs/errors.json` is regenerated. That is deliberate: the gates carry
the migration.

### Two gates, one per direction

`generate_error_keys` refuses to write when a declared code's owner ships a
language that lacks the key. An owner with declared codes and no catalogs at
all is a printed counter, not an error — an owner that never claimed a
language has coverage debt, not a broken translation contract, and blocking it
would make i18n adoption a precondition of declaring error codes.

`check_translation_catalogs` gains the reverse: catalogs for owned keys with
no registry export, and a translated owned key the export omits.

### stapel-core ships its own registry export

41 keys, all owned by core. The common errors moved here by the ownership fix
reached no consumer in any locale, in any repo — any consumer written the
obvious way skipped core entirely, because core published only one half.

### Fixed: registration by import side effect

`stapel_attributes` registered its 12 keys only when a serializer happened to
be imported, so registry emission depended on whether the schema was built
first. The embedding apps force the registration; command-only emission is now
byte-identical to the full pipeline.

## [0.25.0] — 2026-08-15

The hardening wave: every item here is a mechanism that already existed and
sat on the wrong path or guarded the wrong population. Three of the four
change runtime behaviour, and one of them can refuse to start a worker —
hence the minor bump. Read the upgrade notes.

### Security — a revoked access token no longer authenticates

`TokenManager.validate_access_token` held the blacklist it was constructed
with and deliberately did not consult it; its docstring said "caller should do
that separately". Callers split into two populations. The ones that
remembered: `JWTAuthMiddleware`, the DRF `JWTCookieAuthentication`, channels,
and assorted stapel-auth views. The ones that did not: **`JWTAuthBackend`** —
the Django auth backend ironmemo wires — `stapel-auth/sessions/services.py`,
`openid/views.py`, and every future caller of `jwt_provider.validate_token`, a
method that reads as "validate this token" and silently meant "validate
everything except revocation". On those paths a user who logged out kept
authenticating until the token expired on its own: up to a full access-token
lifetime, and on the refresh path up to a week.

**Revocation moved inside the validation seam. `validate` now means valid.**

- `TokenManager.validate_access_token` / `validate_refresh_token` refuse a
  blacklisted jti when the manager holds a blacklist. The jti is read from the
  signature-verified payload, never from an unverified decode. A manager
  constructed with no blacklist behaves exactly as before and touches no cache.
- `JWTProvider.validate_token` additionally refuses a token belonging to a
  blacklisted user — a django-layer concept that stays in the django layer.
  `JWTProvider.refresh_access_token` runs both checks *before* minting: the
  mint is the one operation that outlives the credential presented for it. The
  old "IMPORTANT: Caller MUST check blacklist before calling this" contract is
  deleted, because a contract whose violation is silent admission of a revoked
  token is not a contract.
- `JWTAuthBackend` is deliberately **not** patched. It inherits the fix through
  the provider, and the regression test asserts it fixed without touching it —
  patching the backend would have been the N-patches shape, leaving the next
  `validate_token` caller to reopen the hole.

0.24.0 already made both blacklists fail closed on every backend
(`STAPEL_BLACKLIST_FAIL_OPEN` is the single, W-checked escape hatch), so the
store is trustworthy and the original excuse for keeping the check out of the
validation path is gone. A cache outage now yields 401s on the bypass paths
too — the failure is honest and uniform instead of path-dependent.

Cost: marginal, and mostly already paid. `JWTAuthMiddleware` is in
`COMMON_MIDDLEWARE`, so most fleet requests already do the jti and user-level
cache GETs; for them this adds one duplicate jti GET on the access path
(Redis sub-ms, LocMem µs). The paths that gain a *new* GET are exactly the
paths that until now skipped revocation. That GET is the fix.

**Upgrade note.** Strictly stricter; no schema, no data migration. The only
observable change for a correct deployment is that logout and revocation start
working on every path, immediately. Any caller that relied on `validate_token`
ignoring revocation (none found in-fleet) must stop.

### Added — `manage.py check` names a per-process revocation store

`stapel_core.blacklist.W002` (W-level, tag `stapel_blacklist`, silent under
`DEBUG`). Both blacklists write to the default Django cache. With
`LocMemCache` that store lives inside one worker: a logout served by worker 3
revokes the token in worker 3 and nowhere else, and the next request —
balanced to worker 1 — authenticates the token the user just killed. Nothing
surfaced this; the revocation call returns success either way. Now that
revocation is enforced inside the validation seam, this is the difference
between "revocation works" and "revocation works one time in N".

### Security — the E-gates now run under gunicorn, and can refuse a worker

Django runs system checks for management commands and `runserver`. It runs
**none** for `gunicorn config.wsgi:application` — and that is exactly how every
generated Stapel project boots (stapel-tools emits that CMD and a bare
`get_wsgi_application()`, with no migrate/check step in front of it). The
entire 0.24.0 E-gate wave therefore guarded developer laptops and CI, and may
have guarded production not at all. A gate that only fires where the damage is
cheap is decoration.

`stapel_core.django.boot.BootGateMiddleware`, inserted at **index 0 of
`COMMON_MIDDLEWARE`**. `get_wsgi_application()` builds a `WSGIHandler`, whose
`__init__` calls `load_middleware()`, which instantiates every middleware — so
under gunicorn this runs at worker boot, before any request is served. The
middleware runs an allowlist of check tags in its `__init__` and:

- raises `ImproperlyConfigured` listing **every** ERROR-level finding's id,
  message and hint verbatim (all of them, not the first: an operator who has to
  redeploy to discover the second misconfiguration learns to distrust the
  gate), then
- raises `MiddlewareNotUsed` on success, so Django unhooks it and the
  per-request cost is zero. The gate runs once per worker.

Every project that builds `MIDDLEWARE` from `COMMON_MIDDLEWARE` is covered on
its next core bump, with no entrypoint edit anywhere in the fleet. ASGI and
channels take the same path. `AppConfig.ready()` was rejected as the seam:
`populate()` ordering is not settled there, it would break `shell`/`migrate` on
the box being used to debug the finding, and it would crash `manage.py check`
itself at setup — so the tool whose whole job is printing the diagnosis could
never print it, which is a gate lying about its cause, structurally.

| Setting | Default | Semantics |
|---|---|---|
| `STAPEL_BOOT_GATES` | `"enforce"` | `enforce` refuses the worker \| `warn` logs the same causes and serves \| `off` skips the checks. An unrecognised value means `enforce` — a typo must not open a gate. |

`BOOT_GATE_TAGS` is an explicit allowlist of settings-only, DB-free tags:
`stapel_auth_backends`, `stapel_cors`, `stapel_conf`, `stapel_comm`,
`stapel_bus`, `stapel_captcha`. DB-touching and
URLconf-resolving checks stay in `stapel_preflight` — a boot gate that needs
the database up is a liveness probe wearing a config gate's clothes, and would
turn "Postgres is three seconds behind" into a fleet-wide boot failure.

Two new W-checks (tag `stapel_boot`): `stapel_core.boot.W001` whenever the gate
is not enforcing — an opt-out must stay a stated choice, not become forgotten
configuration — and `stapel_core.boot.W002` when a hand-rolled `MIDDLEWARE`
never picked up the middleware, which is the only way a non-conforming project
learns its E-gates never run under gunicorn.

**Upgrade note, read this one.** A service that today runs happily under
gunicorn *with a configuration its own E-gates reject* will **refuse to boot**
after bumping core. That is the intended meaning of an E-gate and it is not
softened. The compat story is sequencing, not dilution:

1. `manage.py stapel_preflight` — already in the deploy idiom — runs
   `run_checks()` and shows the exact findings **before** the new core is live.
2. The refusal names every finding with its own hint, so the fix is in the
   error message.
3. `STAPEL_BOOT_GATES="warn"` exists for a deployment that must boot tonight,
   as a stated, W-checked choice. It is not the default and will not become one.

**`stapel_config` is deliberately NOT on the roster**, though it was while the
roster was being drafted. Measured across twelve real deployments it is a
no-op — `discover_manifest_path()` reads `STAPEL_CONFIG_MANIFEST` or walks up
from `Path.cwd()` for a `CONFIG.MD`, and no fleet service ships one where that
walk can reach it, so `config_manifest_required_keys` came back empty
everywhere. And on the day a project does the right thing and adopts a
scaffolded `CONFIG.MD`, the check would refuse a *correct* deployment: it
resolves the manifest's key name out of `os.environ` alone, so a service that
supplies its secret as `DJANGO_SECRET_KEY` — with a perfectly valid
`settings.SECRET_KEY` — is rejected (reproduced against
`stapel-example-minimal/CONFIG.MD`). Its verdict is also cwd-dependent: same
image, same environment, same settings, different answer depending on the
directory the process started in. None of that belongs in something that can
refuse a worker. The check stays registered and is still reported by
`manage.py check` and `stapel_preflight`; it can rejoin the roster once
required keys are resolved against the settings the process actually uses and
the manifest is discovered explicitly rather than by walking the cwd.

### Fixed — a wiring check that asked the route table a NATS question

`stapel_core.cdn.E002` ("CDN fields are declared but the cdn module is not
wired up") decided wiring by calling `_route_for("cdn.media_exists")` — a
read of `STAPEL_COMM["FUNCTION_ROUTES"]`, which is **http-only** by
construction. Under the NATS transport the subject *is* the function name and
there is no route table at all, so every correctly wired NATS deployment that
declared a single `CdnImageField` was told its CDN was missing. This is the
third sighting of one defect: the same reasoning was found and fixed twice in
stapel-workspaces (E011, W001) before anyone looked back at the check they had
both been modelled on.

Survivable while `manage.py check` was the only place it fired; not survivable
next to a boot gate. It blocks `manage.py check`, `migrate` and
`stapel_preflight` on a healthy fleet today, and had `stapel_cdn` ever joined
`BOOT_GATE_TAGS` it would have refused every worker in a NATS deployment.

The fix is one shared answer rather than one patched caller:

**`stapel_core.comm.function_unreachable_reason(name) -> str | None`** — "can
`call(name)` actually reach this function here?", asked of the transport
branch for branch, returning the operator-facing reason or `None`:

- `inprocess` — `call()` reads the process-local registry, so a provider must
  be registered in *this* process;
- `http` — `call()` resolves a longest-prefix `FUNCTION_ROUTES` entry and
  never consults the registry, so only a matching route counts. Note this is
  **stricter** than what E002 did before, not weaker: an installed provider no
  longer excuses a missing route, because `call()` would not use it;
- `nats` — wired by construction; nothing at check time can or should prove the
  provider is up, which is what the runtime timeout is for;
- a dotted path — a custom transport does its own addressing;
- anything else — `call()` raises `FunctionRouteNotConfigured` on every call, so
  the seam is as unreachable as an unwired one.

Never a liveness probe: it reads settings and the registry and nothing else.
Any check in any module that asks "is module X wired" should call this instead
of reading `FUNCTION_ROUTES`; a sweep of core found E002 to be the only other
reader.

### Added — one tree-walk helper, so a gate stops accusing the wrong file

`stapel_core.testing.iter_own_sources(root, suffix=".py")` and the separately
exposed predicate `is_foreign_source(path, root)`.

Three incidents in one day, all one shape: a test that wanted "our package's
sources" implemented it as "every `*.py` under the root, minus a hand-written
list of directory names". That list is an open-world enumeration of a
closed-world question. The day someone's virtualenv is called `env312` or
`.direnv/python-3.12`, the walk reads an **installed sibling library's** file
and reports it as this repo's violation — a red on a file the repo does not
own, sending the reader to hunt a defect that is not there.

Foreignness is decided by **marker, never by name-list**:

- a virtualenv is a directory containing `pyvenv.cfg`, whatever it is called;
- a path with a `site-packages` component is installed or vendored code, with
  or without a cfg;
- `build`/`dist` are excluded **only** when they carry packaging markers
  (`build/lib` layout, `*.egg-info`, `*.dist-info`) — a source directory
  legitimately named `build` must not be silently skipped, which would be the
  same stale-list disease inverted;
- `__pycache__`, `.git`, `node_modules`, `.tox`, `.mypy_cache` are never
  sources.

Walking the package's `__path__` instead is not sufficient on its own: stapel
repos are flat (the repo root IS the package directory), so an in-repo `.venv`
and `build/lib/<pkg>/` sit *inside* the package path. The predicate is exposed
separately because in CI there is no in-repo venv — a test that only walked the
real tree would pass vacuously and the exclusion would rot exactly where it
matters, so it is asserted on synthetic paths instead.

Core hosts the helper rather than stapel-tools because every module already
depends on stapel-core and already imports `stapel_core.testing` in its
conftest: it is importable by construction in exactly the population that needs
it. Core's own `tests/test_import_lock_discipline.py` is migrated onto it and
its local skip-list deleted; the fleet sweep of the remaining offenders and the
stapel-tools lint rule ride separately.

### Changed — W001 now reports every environment variable a namespace ignores

`AppSettings.ignored_env_vars()` iterated `import_strings` alone, while
`_env_allowed()` already knew the full truth. So a variable set against a
`no_env` key — stapel-auth's registration and behaviour toggles, core's own
`STAPEL_MEDIA["BACKEND"]` — was dropped with **no warning at all**, reproducing
the very silence W001 was built to end. `no_env` was invented for the same
threat model (its own comment: generic names that could "silently change a
trust/security decision"), and a set-but-ignored variable on such a key is
precisely "the operator believes X is configured and it is not".

The walk now covers **every key where `_env_allowed()` is False** — the
implicit closure over `import_strings`/`resolvers` plus `no_env` — with names
still spelled exclusively by `env_var_names()`. The noise worry (generic names
like `BACKEND` colliding with unrelated variables) is answered by W-level
severity, the per-namespace dedup that was already there, and the fact that the
collision *is* the information: that variable is being ignored. The message
branches on which family closed the key, so a policy toggle is never described
as naming a class. Media's `STAPEL_MEDIA_BACKEND` alias stays correctly
unreported — that variable is honored, and the check must never claim a
variable was dropped when it was read.

**Upgrade note.** Expect new W001 lines at `manage.py check` on deployments
that set generically-named environment variables. Each one is a true statement
about a variable that is not being read; none of them blocks a deploy.

### Added — `resolvers=` on `AppSettings`

`AppSettings(..., resolvers={"PROVIDER": callable_or_dotted_path})`. A value
that is legally "registry short name OR dotted path" cannot go through the base
class's eager `import_string`, so packages either subclassed `__getattr__`
(stapel-notifications) or kept the key out of `import_strings` with a comment
(stapel-auth's `OAUTH_PROVIDER_CLASSES`) — and the second silently drops the
key out of W001's scope. Declaring `no_env` instead would close the door with
no way back out, since `no_env ∩ env_overridable` is a construction error by
design.

A resolver key is **policy-wise a member of the import_strings family**:
implicitly env-closed, reopened only by `env_overridable`, reported by W001
with the class wording. Only the string→object step is delegated, at the same
lazy, cached point where `import_string` runs today. A dotted-path resolver is
itself imported at first use, so a `conf.py` never drags a channel module into
every import of the package. Resolver exceptions pass through with their own
type and message, so a registry's `ImproperlyConfigured` naming the short names
it knows survives instead of degrading to a bare `ImportError`. Declaring a key
in both `import_strings` and `resolvers` raises at construction rather than
picking a winner.

A free-floating `resolve=` callable was **rejected**: divorced from the
import_strings policy family it would be a third kind of key with unspecified
environment semantics — new surface, no gate.

### Security — an unrecognised ASN is no longer asserted to be residential

`MaxMindProvider` ended its kind derivation with "the ASN database has a row
for this address → residential". The only thing standing between a VPS and
that verdict was `HOSTING_ASNS`, a hand-written frozenset of eighteen large
cloud ASNs, and datacenter space changes weekly. A hosting provider nobody had
enumerated therefore came out as the *most permissive* class in every consumer
of the seam — the captcha challenge matrix, rate limits, the login-anomaly
signal all treated it as somebody's home connection. A stale hand-list with a
permissive fallback answers "no evidence" with "safe".

`residential` is now a claim that needs evidence, and the provider has exactly
one source of it: `MAXMIND_ANONYMOUS_DB` — a maintained enumeration of
hosting/anonymiser space — consulted and not listing the address. Absent that
database the kind is `unknown`, with `confidence=None`: no claim at all. The
profile still carries `asn`, `asn_org` and `country`, so a consumer can tell
"nothing is known about this address" from "we know who routes it and not what
it is". `HOSTING_ASNS` can still promote an ASN to `datacenter`; it can no
longer demote one to `residential`.

Traffic impact under the shipped defaults: **none**. `DEFAULT_CHALLENGE_MATRIX`
maps `residential` and `unknown` to the same level (`invisible`), so no user
sees a challenge they did not see before. Deployments that configure
`MAXMIND_ANONYMOUS_DB` are byte-identical.

**Upgrade note for a host that overrides `unknown` in `CHALLENGE_MATRIX`.**
That entry used to describe a sliver of traffic (addresses missing from the ASN
database). On an ASN-only deployment it now describes the bulk of it — most
consumer ISP traffic lands in `unknown`. If you set `{"unknown":
"interactive"}` and run without `MAXMIND_ANONYMOUS_DB`, you are now asking to
challenge nearly everyone: either configure the Anonymous-IP database, or move
that strictness onto the kinds you meant.

### Added — `manage.py check` names an unconfigured IP-intelligence seam

`stapel_core.netintel.W003`. The default `PROVIDER` is `NullProvider`, so out
of the box `classify_ip` answers `unknown` for every address and anything keyed
off `IpProfile.kind` runs blind with nothing saying so.

The warning fires only where the deployment can be shown to expect
classification, because the core cannot see host code that calls `classify_ip`:
either `STAPEL_NETINTEL` configures the seam (`MAXMIND_*`, `HTTP_URL_TEMPLATE`,
`HTTP_API_KEY`, `EXTRA_DATACENTER_ASNS`, `TRUSTED_PROXY_HEADER`) while leaving
`PROVIDER` at the default, or `STAPEL_CAPTCHA` carries challenge rules keyed by
a network class other than `unknown` — rules no request can ever match while
every request is unclassified. Silent in `DEBUG`, silent for a custom
`NullProvider` subclass, and silenceable through `SILENCED_SYSTEM_CHECKS`. The
message names settings, never their values (`HTTP_API_KEY` is a credential and
check output lands in deploy logs).

## [0.24.1] — 2026-08-15

### Fixed — 0.24.0 made passkey and TOTP endpoints staff-only at runtime

`_unpoison_serve_permissions` walked `inspect.getmembers(drf_spectacular.views)`
and rebound `permission_classes` on every APIView subclass it found. That
module does `from rest_framework.views import APIView`, so the walk yielded
DRF's BASE class and the loop rewrote the default for every view in the
process that does not declare its own permissions.

Effect in any service declaring `SPECTACULAR_SETTINGS` — which is every
service using `get_spectacular_settings`: `APIView.permission_classes`
became `IsStaffUserForSwagger`, so passkey authenticate/begin and the TOTP
endpoints answered staff-only. Ordinary users could not complete those
login paths. Making the schema staff-only must cost exactly the schema.

The walk is now restricted to classes drf-spectacular actually defines.

Found by the stapel-example-monolith aggregate: 15 path objects disagreed
with stapel-auth's own contract, and the disagreement was live behaviour,
not a documentation artifact.

## [0.24.0] — 2026-08-14

### Security — an environment variable can no longer choose which class the process loads

**Upgrade note — behaviour change in every module built on `AppSettings`,
including every sibling repo, the moment it picks up this core.**

`AppSettings` resolves each key `settings.<NAMESPACE>` dict → flat Django
setting → environment variable → default, and keys listed in `import_strings`
are dotted paths it imports and instantiates. The two together meant a
same-named environment variable selected the *implementation* of a provider,
backend, policy or audit sink. In a shared pod, anything able to export a
variable — a leaked value, a sibling container's config, a stray line in an
entrypoint — picked the code that runs on the privileged path. Closing it
namespace by namespace, with a `no_env` list per module, made safety depend on
every author remembering a flag; roughly ten fleet repos (booking, calendar,
chat, listings, shop, social, tasks, vault, video and others) declared
`import_strings` with no `no_env` at all.

**A key in `import_strings` is now implicitly `no_env`.** It still resolves
from the project's `STAPEL_<MODULE>` dict, from a flat Django setting of the
same name, or from the default — the project's own settings module is trusted.
The environment is not, and is simply not consulted for such a key. No module
can reopen this by forgetting a flag.

The deliberate opt-out is the new `env_overridable=` argument, for a
deployment that genuinely selects an implementation per environment:

    AppSettings(
        "STAPEL_BILLING",
        defaults={...},
        import_strings=("PAYMENT_PROVIDER",),
        env_overridable=("PAYMENT_PROVIDER",),   # env may pick this one
    )

It is an opt-OUT of the safe default and greppable fleet-wide by that one
name. Declaring the same key in both `no_env` and `env_overridable` now raises
`ValueError` at construction instead of silently picking a winner.

*What can break, and how you are told:* a deployment that was selecting an
implementation with a bare environment variable now **falls back to the
default (or the settings value)** — different code running. That silence is
the actual hazard, so it is not left to a manifest grep: **`manage.py check`
names the variable.** The new system check `stapel_core.conf.W001` (warning,
tag `stapel_conf`, registered by `CommonDjangoConfig`) walks every live
`AppSettings` namespace in the process and reports each environment variable
that is set while the namespace refuses to read it, naming the variable, the
namespace and the key, with both remedies: move the value into the project's
`STAPEL_<MODULE>` settings dict (recommended), or add the key to
`env_overridable=` in that module's `AppSettings` declaration. A warning, not
an error — the process is running the safe implementation; what is wrong is
the operator's picture of it. A namespace is only visible once its `conf`
module has been imported, so run the check on the service, not on an empty
harness.

Inside core, exactly one key changes behaviour: `STAPEL_MEDIA["WATERMARK"]`
(`media/conf.py`) was in `import_strings` but not in `no_env`, so a bare
`WATERMARK` env var used to select the watermark callable and no longer does.
Core's other `import_strings` keys — `access` (`AUDIT_SINK`, `NOTIFY`),
`gateway` (`POLICY_ENGINE`, `RATE_LIMITER`, `AUDIT_SINK`, `NETWORK_VERIFIER`,
`TIER_RESOLVER`), `flows` (`DOC_TRANSLATOR`, `FLOW_DOC_RENDERER`) and `i18n`
(`TRANSLATOR`) — were already `no_env` by hand and are unaffected.

### Security — a ban is enforced on every cache backend, and stops failing open

**Upgrade note — behaviour change on every service that authenticates.**

`stapel_core.django.jwt.authentication.is_user_blacklisted()` decided that an
unreachable, misconfigured or simply different cache backend meant "not
banned". It asked `cache.client.get_client()` — an attribute only django_redis
has — swallowed every exception, and answered `False` when it got nothing
back. Two consequences, both silent:

- On Django's default LocMemCache (what any project that forgets `CACHES`
  gets), `blacklist_user()` logged an error and stored nothing, and
  `is_user_blacklisted()` answered `False` forever. Ban and force-logout were
  unenforceable, and nothing anywhere said so.
- With Redis down or throwing, every banned user was silently unbanned —
  precisely during the incident where a ban is the response an operator
  cannot wait on.

Both halves are closed:

- The user blacklist now falls back to the Django cache framework when the
  backend is not django_redis, so `blacklist_user()` / `unblacklist_user()` /
  `is_user_blacklisted()` work on every backend. (Raw Redis is still
  preferred, because only there can the key bypass `KEY_PREFIX` and be visible
  fleet-wide; the fallback is scoped to the cache and prefix a service
  shares.) `blacklist_user()` and `unblacklist_user()` now **return `bool`**
  instead of `None` so a caller can tell a stored ban from a dropped one.
- `is_user_blacklisted()` **fails CLOSED** on any store error, matching
  `stapel_core.core.token_blacklist.TokenBlacklist`. It honours the same
  existing escape hatch, `STAPEL_BLACKLIST_FAIL_OPEN = True`, rather than
  introducing a second knob — set it only where an unreachable cache must not
  lock users out.

*What can break:* a deployment whose cache is unhealthy will now reject
authentication (401) instead of admitting everyone, at
`django/jwt/middleware.py`, `django/jwt/authentication.py` and the Channels
WebSocket handshake. That is the intended trade; `STAPEL_BLACKLIST_FAIL_OPEN`
restores the old behaviour explicitly.

New W-level boot check `stapel_blacklist`
(`stapel_core.django.blacklist_checks`) reports when `STAPEL_BLACKLIST_FAIL_OPEN`
is on, so the hatch cannot become forgotten configuration.

### Security — the OpenAPI document is staff-only, and the setting that says so now works

**Upgrade note — `/schema/`, `/swagger/` and `/redoc/` stop answering anonymous
callers.**

`SPECTACULAR_SETTINGS["SERVE_PERMISSIONS"]` was
`['rest_framework.permissions.AllowAny']`, so anyone who could reach the
service could read the full API surface: every route, every request and
response shape, and — because `DEFAULT_SCHEMA_CLASS` is
`PermissionAwareAutoSchema` — each endpoint's permission classes, which is a
map of where to push. `JWTAuthMiddleware.SKIP_CONTAINS` skips those paths, so
nothing else was gating them either.

The default is now `stapel_core.django.openapi.swagger.IsStaffUserForSwagger`
(the class this repo already shipped for the purpose). A genuinely public API
sets `STAPEL_PUBLIC_API_SCHEMA=True`, which restores `AllowAny` and is also
exported as a plain boolean setting of the same name.

Found while pinning this: **the setting was decorative anyway.**
`drf_spectacular/settings.py` snapshots `settings.SPECTACULAR_SETTINGS` in its
module body, and its views bind `permission_classes =
spectacular_settings.SERVE_PERMISSIONS` at class-definition time. For any
project written as `from stapel_core.django.settings import *` the snapshot
happens *during* that import, before `SPECTACULAR_SETTINGS` exists — the same
import-order bug `_unpoison_spectacular_settings` already fixed for
`TITLE`/`VERSION`/`DESCRIPTION`, but with a security consequence instead of a
cosmetic one: whatever a project declared, drf-spectacular kept its own
`AllowAny`. `AppConfig.ready()` now forces `SERVE_PERMISSIONS` onto both the
singleton and every drf-spectacular view class. Projects that already set
`SERVE_PERMISSIONS` themselves get the value they asked for, possibly for the
first time.

### Security — a failed clearance check hides the nav link instead of showing it

`stapel_core.django.nav._viewer_allowed` ended its clearance branch with a bare
`except Exception: return True`, commented "mandate not engaged — degrade to
staff". That is right for a build without `stapel_core.access`; it also caught
a broken role source, a malformed clearance level and every other error raised
*inside* the mandate, and answered "allowed" — any failure in the
authorization machinery became a grant.

Only `ImportError` degrades now (the mandate is genuinely absent). A failure
while evaluating the mandate hides the link and logs a warning. An
unrecognized `requires` value no longer falls through to "any staff member"
either.

Blast radius is nav-link visibility only — the targets carry their own
perimeter — but the shape is the one being swept out of the codebase.

### Security — the privilege gateway's third authorization factor is on

**Upgrade note — scope tokens issued without a `network` binding stop working
over HTTP.**

`STAPEL_GATEWAY["REQUIRE_NETWORK_BINDING"]` defaulted to `False`, and
`gateway/network.py` reads it as `if not bound: return not
REQUIRE_NETWORK_BINDING`. So a scope token carrying no network binding was
accepted from anywhere that could reach the container-facing door
(`gateway/http.py`) — while the module's own docstring called network identity
"the third authorization factor". A factor that is off by default is not a
factor; only the token was doing any work.

The default is now `True`. Issue tokens with `issue_token(project,
network=...)` (exact IP or CIDR), which is what a container-manager already
does. `STAPEL_GATEWAY = {"REQUIRE_NETWORK_BINDING": False}` restores the old
behaviour for deployments that deliberately issue unpinned tokens.

### Security — a token no longer creates local users, or grants them staff, by default

**Upgrade note — every downstream service that relies on JIT user creation
must now declare it.**

`JWT_CREATE_USERS_FROM_TOKEN` defaulted to `True`, so a service that never
considered the question got the trusting mode: an unknown `user_id` was
materialised as a local row, and `is_staff` / `is_superuser` / `is_active`
were REPLACED from the token's claims on every request
(`django/jwt/utils.py`). One compromised signing key, or one over-broad claim
from an upstream issuing tokens for a different audience, became a local
superuser — in a service that never decided to consume an external identity
source at all.

The default is now `False`: the local database decides who exists and what
they may do, and a token naming an unknown user is treated as stale
(the existing "authoritative user store" mode, unchanged in behaviour). Both
read sites go through one helper, `_create_users_from_token()`, so the two
halves of the decision cannot drift apart.

*What can break:* a downstream microservice whose users genuinely live in the
auth service will start rejecting first-time logins. Set
`JWT_CREATE_USERS_FROM_TOKEN = True` in that service's settings — consuming an
external identity source is a design decision, and this is where a service
says it made one.

### Security — cookies are TLS-only by default, and a forwarded header is no longer believed on sight

**Upgrade note — affects every service that star-imports
`stapel_core.django.settings`.**

The shipped settings served bearer credentials over cleartext by default and
trusted a client-settable header:

- `JWT_COOKIE_SECURE` defaulted to `False` (`# True in production with HTTPS`),
  and that value propagated into every cookie write in `django/jwt/utils.py`.
- `SESSION_COOKIE_SECURE = False  # set True in prod` — a comment is not a
  mechanism.
- `CSRF_COOKIE_SECURE` was never set at all, so Django's `False` applied.
- `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` was set
  **unconditionally**. `X-Forwarded-Proto` is a request header: it is
  trustworthy only where a proxy strips the incoming value and writes its own.
  A process reachable directly — a debug port, a misrouted ingress, a pod
  addressable inside the cluster — let any caller declare its own connection
  secure, which makes every `is_secure()` decision the caller's to make.

Now:

| setting | was | is | opt-out |
| --- | --- | --- | --- |
| `JWT_COOKIE_SECURE` | `False` | `True` | `JWT_COOKIE_SECURE=False` |
| `SESSION_COOKIE_SECURE` | `False` | `True` | `SESSION_COOKIE_SECURE=False` |
| `CSRF_COOKIE_SECURE` | unset (`False`) | `True` | `CSRF_COOKIE_SECURE=False` |
| `SECURE_PROXY_SSL_HEADER` | always trusted | `None` | `STAPEL_TRUST_PROXY_SSL_HEADER=True` |

The library's own fallbacks in `django/jwt/utils.py` (`getattr(settings,
"JWT_COOKIE_SECURE", False)`, twice) now default to `True` as well, so a
service that configures Django by hand does not get a cleartext cookie from an
absent setting.

New `stapel_core.django.prodguard.guard_cookie_security(globals())`, in the
genre of the existing `guard_secret` / `guard_db_password`: call it from the
prod settings tier and it refuses to boot on a cleartext session/CSRF/JWT
cookie, a missing HTTPS redirect or HSTS, or a `SECURE_PROXY_SSL_HEADER` the
deployment never vouched for. It reports every problem at once. An edge that
already redirects and sends HSTS states that with
`STAPEL_TLS_TERMINATED_UPSTREAM = True`.

*What can break:* a deployment genuinely served over plain HTTP loses its
cookies until it sets the flags to `False` explicitly. (Local development is
mostly unaffected: browsers treat `http://localhost` as a trustworthy origin
and accept `Secure` cookies there.) A deployment behind a TLS-terminating
proxy will see `request.is_secure()` return `False`, and `build_absolute_uri()`
emit `http://` URLs, until it sets `STAPEL_TRUST_PROXY_SSL_HEADER=True` — one
environment variable, stating a fact only the deployment knows.

`SECURE_SSL_REDIRECT` and `SECURE_HSTS_SECONDS` are deliberately **not**
flipped in the shared settings: enabling a redirect for a service whose proxy
does not forward the protocol produces an infinite redirect loop, and HSTS is
close to irreversible for a domain. Both are required by
`guard_cookie_security()` instead, where a human is making a per-deployment
decision. `SECURE_CONTENT_TYPE_NOSNIFF` and `SECURE_REFERRER_POLICY` were
already safe by Django's own defaults (`True` / `same-origin`) and needed no
change.

### Security — captcha no longer disables itself when its secret goes missing

**Upgrade note — a deployment naming a captcha backend without a secret will
now refuse to boot.**

`build_verifier(backend, secret)` returned `NoopVerifier` — which returns
`True` for every token — whenever the secret was empty, whatever backend was
named. Every consumer (`CaptchaMixin.validate_captcha_token`,
`_require_captcha_if_configured`, `@captcha_protected`) reads
`isinstance(verifier, NoopVerifier)` as "captcha is off" and waves the request
through. So `BACKEND='turnstile'` with a rotated-away, typo'd or unmounted
`SECRET` was indistinguishable from a healthy captcha, and the brute-force
floor under OTP request, password reset and magic link was gone with nothing
to notice.

- `NoopVerifier` is now returned only when captcha is *deliberately* off: no
  `BACKEND`, or `BACKEND='noop'`. It is never a fallback for a backend that
  was asked for and could not be built.
- A named backend with an empty secret raises the new
  `stapel_core.captcha.CaptchaConfigurationError`.
- New E-level boot check `stapel_captcha`
  (`stapel_core.django.captcha_checks`): E001 for a named backend without a
  secret, E002 for a backend that cannot be built at all (bad dotted path, not
  a `CaptchaVerifier`). An operator meets the misconfiguration at
  `manage.py check`, not as a 500 on somebody's password reset.

*What can break:* a deployment that set `BACKEND` but never mounted `SECRET`
and has, unknowingly, been running with captcha off. Either mount the secret
or set `BACKEND='noop'` — the opt-out is now an explicit statement.

### Security — step-up verification stops reading its policy from the environment

`AppSettings` falls back to `os.environ[KEY]` for any key a namespace does not
list in `no_env`. Every sibling namespace declares one — `access`, `netintel`,
`gateway`, `secrets`, `security`, `media` — and `STAPEL_VERIFICATION` did not,
while owning some of the most generic key names in the fleet:

- `DEFAULT_LEVEL=opt_in` in a container's environment switched every
  `@requires_verification(level=None)` view from mandatory to off.
- `DEFAULT_FACTORS=<anything>` arrived as a `str`, so `list("otp_email")`
  became single characters, `factor_registry.available_for()` returned
  nothing, and `default_on` views passed straight through to the handler.
- `MAX_ATTEMPTS`, `DEFAULT_MAX_AGE`, `CHALLENGE_TTL`, `EXTRA_FACTORS` and
  `POLICY_CACHE_TTL` were equally settable by accident.

All of them are now `no_env`. They still resolve from the
`STAPEL_VERIFICATION` dict, a flat Django setting, or the default — an
environment variable of the same name is ignored.

*What can break:* a deployment that was deliberately configuring step-up
through bare env vars (`DEFAULT_LEVEL=…`) rather than settings. Move those
values into `STAPEL_VERIFICATION` in the settings module.

### Security — cross-service payloads are validated in production, not only in DEBUG

**Upgrade note — behaviour change for every `@function` / `@on_action` with a
registered schema.**

`STAPEL_COMM["VALIDATE_SCHEMAS"]` defaulted to `None`, which meant "follow
`settings.DEBUG`". So payload validation ran where payloads are hand-written by
a developer, and was off in production, where they arrive from another service
over HTTP (`comm/http.py` `FunctionCallView`) or NATS. Dev and prod ran
different code paths and the unchecked one was the one that mattered.

- The default is now `True`, independent of `DEBUG`. Opt out explicitly with
  `STAPEL_COMM = {"VALIDATE_SCHEMAS": False}`.
- A missing `jsonschema` no longer skips validation silently. `_validate()`
  raises the new `stapel_core.comm.exceptions.SchemaValidatorUnavailable`
  instead — the same stance the privilege gateway already takes
  (`gateway/service.py`): a validator that cannot run must not report success.
- `jsonschema>=4.18` moved from the `gateway` extra into the base
  dependencies, because a default-on control has to be installable by default.
  It remains listed under `gateway` for anyone pinning that extra.

New boot check `stapel_comm` (`stapel_core.comm.checks`): E-level when
validation is on but `jsonschema` cannot be imported (caught at
`manage.py check`, not at the first cross-service call), W-level when
validation is off, so the opt-out stays a stated choice.

*What can break:* a service whose emitted payloads have quietly drifted from
their registered schema will now raise `SchemaValidationError` in production
where it previously delivered. That drift was the thing the schema was
registered to catch. Fix the payload or the schema; `VALIDATE_SCHEMAS: False`
is the escape hatch if the fix has to wait.

### Added — the event store reads like a journal, purges like an eraser, and answers for a person

Three mechanisms, one motive: every audit journal in the fleet belongs in the
event store, and three small gaps were what kept consumers building bespoke
tables instead (stapel-workspaces 0.24 built one — see its Unreleased entry
for the deletion).

- `eventstore.query(..., reverse=True)` — newest-first cursor reads
  (`(-ts, -id)`, `after` advances into the past, same id tie-break). Journals
  are written oldest-first and read newest-first; without this every
  journal-shaped consumer had to refetch a stream from the top to show its
  last page — reason enough to keep a bespoke ORDER BY table.
- `eventstore.anchor.anchor_page(stream, *, filters, anchor, direction,
  limit)` — the fleet's `AnchorPagination` wire contract (`{items,
  next_anchor, prev_anchor, has_next, has_prev, count}`, ISO-timestamp
  anchors, `next`/`prev`/`center`) served from a stream. A journal that moves
  its storage into the store keeps its released HTTP shape byte-for-byte.
- `eventstore.purge(..., filters=...)` — subject-scoped erasure. Retention
  never needs a filter; a GDPR delete of one person's audit lines does, and
  without it every module keeps a deletable table just to be able to forget.
  This is the unblock for moving stapel-auth's `AuthAuditLog` (whose GDPR
  provider deletes per user) onto a stream.
- `manage.py audit_trail <person>` — the operator's cross-module window:
  every audit line naming a person (canonical payload keys `subject`,
  `subject_id`, `actor_id`) across every audit stream
  (`STAPEL_EVENTSTORE["AUDIT_STREAMS"]`, or discovered by the
  `audit`/`*.audit` naming convention), merged newest-first. The per-module
  HTTP endpoints each gate their own slice under their own mandate; the
  cross-module question is an operator's, so it lives on the operator
  surface, not on any product API.

### i18n — the two halves of the error contract can no longer drift apart

One contract, two generators, two scopes: `generate_error_keys` exports the
registry *instance-scoped* (every code the deployment can emit — correct, it
is the API companion of schema.json), while `translate_catalogs` writes
catalogs *ownership-scoped* (a package ships only the keys it owns — also
correct). Nothing connected them. Measured result: stapel-auth's registry
declared ten `error.*.gdpr.*` codes whose catalogs live in stapel-gdpr, which
shipped no registry of its own — so a consumer pairing registries with
catalogs rendered ten English sentences in a Russian UI, silently. Core was
the same shape at larger scale: 41 translated keys in
`django/translations/`, no `docs/errors.json`, reachable by no consumer.

Both scopes stay. The seam is now explicit and gated from both sides:

* every `errors.json` entry carries **`owner`** — the package whose
  `translations/errors.<lang>.json` must carry the key
  (`build_error_registry`). The registry is self-describing: a consumer finds
  each code's catalogs without knowing the mount graph;
* `generate_error_keys` refuses to emit a registry whose declared codes an
  owner's shipped language does not translate
  (`check_registry_catalog_pairing`, new): an ownership move that strips a
  translation goes red at the next emission of every instance that mounts the
  key. An owner that ships no catalogs at all in an otherwise translated
  instance is a printed `unshipped` counter, not a blocker — no claimed
  language, no contract to break;
* `check_translation_catalogs` gains the reverse direction: a package
  shipping catalogs for keys it owns must publish a registry export that
  declares them — `no_registry_export` / `unexported` are E-level. The
  export's canonical home is `<top-level package dir>/docs/errors.json`
  (`DOMAIN_EXPORTS`), which package-data already carries in fleet wheels;
* core now ships its own **`docs/errors.json`** (41 keys, all
  `owner: stapel_core`), drift-gated by `tests/test_error_registry_artifact.py`
  — downstream error-catalog compilers can drop their `registry: null`
  special case for core.

**Upgrade note:** the artifact shape grew a field, so every module's committed
`docs/errors.json` drifts until regenerated
(`STAPEL_REGEN_ERROR_KEYS=1 pytest tests/test_error_keys.py`, or
`make contract` in triad repos); shape tests pinning the exact key set need
`"owner"` added. That red is the migration, not collateral.

### Security — an authentication backend must check the secret it is handed

`stapel_core.django.jwt.session.EmailAuthBackend` resolved a user by email and
returned it **without comparing a password**. Its docstring said passwords were
"handled by the JWT tokens", which is true of the JWT middleware and false of
`AUTHENTICATION_BACKENDS`: any project that listed the dotted path handed the
backend every `django.contrib.auth.authenticate()` call in the process, and a
known email address plus any nonempty string became a valid login. The
2026-08-11 IronMemo audit found exactly that wiring in production code
(AUTH-01, P0) reachable through stapel-auth's legacy `/token/` endpoint.

The backend now does what its name promises and nothing more — email is the
*lookup* key, credential verification is Django's: `check_password`,
`user_can_authenticate`, an empty/absent password denies, an unknown address
still runs one hash so timing does not enumerate accounts, and an email
matching two rows denies because no single principal owns the secret.

The wider defect is the seam, not the class: the wiring and the backend live in
different repositories, so neither review sees the whole. New E-level boot gate
`stapel_auth_backends` (`stapel_core.django.auth_backend_checks`) fails startup
when an entry in `AUTHENTICATION_BACKENDS` overrides `authenticate()` without
declaring `verifies_credentials = True` — and fails harder when it declares
`False`. Third-party backends, which cannot carry the declaration, are listed
once per project in `STAPEL_SECURITY["REVIEWED_AUTH_BACKENDS"]` (new settings
namespace `stapel_core.security`).

**Upgrade note:** a project whose `AUTHENTICATION_BACKENDS` contains a custom
backend will not boot until that backend declares the attribute or is reviewed
into the allowlist. That is the point of the gate — the decision is being asked
for, once, out loud.

### Security — one SSRF-hardened fetcher for the whole fleet (`stapel_core.net`)

`stapel-cdn` had a genuinely good guarded fetcher: https-only, every resolved
address validated against private/loopback/link-local/CGNAT/metadata ranges
(including the IPv4-mapped, 6to4 and NAT64 re-encodings of them), the socket
pinned to the validated IP so DNS rebinding gains nothing, every redirect
re-validated from scratch, and a byte cap enforced mid-stream. It sat in a
module nothing else depends on, so when `stapel-agent` needed to download
audio it wrote `requests.get(url, timeout=600)` and read `.content` — no
scheme check, no address check, no cap (audit AGENT-01). That is the fleet's
recurring shape: the mechanism was built and the second consumer never picked
it up, because picking it up was not possible.

`stapel_core.net.fetch_bytes` is that fetcher, moved to where every module can
reach it, plus the two controls the audit found missing: a **total deadline**
covering the whole operation and all its redirect hops (a per-socket timeout
alone bounds nothing against a server that trickles one byte per window), and
an oversize `Content-Length` refused before a body byte is read. It also takes
an exact-match `allowed_hosts` allowlist for the common case where the remote
is a known API rather than a user-supplied address, and never sends an
`Authorization` header, so there is nothing to leak across an origin change.
`max_bytes` is mandatory and has no default: a caller that has not decided how
much of its memory a stranger may fill has not finished thinking about the
fetch.

### Security — a bearer token can no longer write account lifecycle

`get_or_create_user_from_jwt` wrote the `is_active` claim into the local user
row in **both** deployment modes, including the authoritative one where that
row *is* the account. Every token minted before an account was closed keeps
carrying `is_active=true` until it expires, so replaying one undid the closure
(audit GDPR-01, P0). Lifecycle now follows the same rule staff attributes
already followed: in authoritative mode it is never written from a claim, and
in consumer mode only when the claim is actually present — an absent claim is
silence, not `True`, and must not reactivate anybody. An absent `email` claim
likewise no longer nulls the stored address.

## [0.23.1] — 2026-08-10

### Fixed — the error reference resolves a key by its owner, not by its directory

0.22.0 gave error keys an owner and stopped demanding that every module
re-translate core's 41 cross-cutting keys. The write side was scoped; the read
side was not. `generate_error_docs --translations <dir>` loaded that one
directory and nothing else, while the reference it renders covers the **whole**
registry — so a module that deleted its duplicates lost 41 Russian rows to
`_(en)_` English fallbacks, with no gate saying so. Measured on stapel-profiles,
the one library that had already pruned: regenerating its committed
`docs/errors.ru.md` produced 41 of 53 rows in English.

That is the same duplication defect wearing a documentation costume — the
mechanism landed and one of its consumers, the doc renderer, never picked it
up — and it made the fleet-wide sweep a choice between a red gate and a
degraded reference.

`module_catalog(domain, language, dir)` (`stapel_core.i18n`) is the read seam:

- the module's own text wins — a declared override is exactly the module
  shipping its own text, and the runtime merge orders it the same way;
- a key the module does not own is read from the catalog of the package that
  does (`owner_catalog()`, over INSTALLED_APPS);
- a key the module **does** own is never back-filled from a same-named package
  installed elsewhere: an owner's own gap is the `missing` coverage error, and
  filling it would hide it;
- a key nobody owns, or whose owner ships nothing in that language, stays
  absent and the caller renders its honest `_(en)_` fallback, as before.

`build_error_docs` goes through it, so pruning is byte-neutral for
`docs/errors.<lang>.md`: the reference a module renders while it still
duplicates core's keys and the one it renders after deleting them are the same
bytes (pinned by `test_pruning_leaves_the_error_reference_byte_identical`, and
verified end-to-end in every library the sweep touched).

The `flows` domain tracks no ownership, so it resolves exactly as before.

## [0.22.0] — 2026-08-10

### Added — core ships its own error catalogs, and a module translates only what it owns

Core registers **41** cross-cutting error keys — `COMMON_ERRORS` (the
HTTP-status generics plus the eleven `error.400.field.*` DRF validation keys),
the six verification step-up keys, the three captcha/network keys — and shipped
**no** `translations/` directory at all, while `CommonDjangoConfig` sat in
every project's INSTALLED_APPS. The loader had a slot for a core catalog and
nothing to put in it.

That absence was not passive. `check_translation_catalogs` took its canon from
`source_texts("errors")` — the *whole* in-process registry — and raised
`missing` for every canonical key absent from the module's own `translations/`
directory. Going green therefore **required** each module to re-translate all
41. Measured across the five libraries that ship catalogs today: 687 entries,
of which **410 are core's keys copied verbatim** (auth 41+41 of 128/127,
workspaces 41+41 of 67/67, profiles and billing 41+41 of 53/53,
notifications 41+41 of 43/43). Divergence across all five, both languages:
**zero**. Not one library ever meant to reword anything — the gate made them
all copy. Seventeen more libraries are queued.

`django/translations/errors.{ru,es}.json` now carries those 41 keys, generated
the same way a module's are (seeded from the curated stapel-translate corpus,
two keys machine-translated) and **byte-identical to what all five libraries
already ship** — so deleting the duplicates is a no-op at runtime, verified:
`load_app_catalogs` returns the same 53 keys for stapel-profiles with its
copies removed. The catalogs live in the `stapel_core.django` app package
because that is what `CommonDjangoConfig.path` is, and they are declared in
`package-data` — verified by installing the built wheel into a clean venv and
loading them back through `load_app_catalogs`, not by assumption.

### Added — key ownership, and a gate that refuses a silent re-translation

`register_service_errors(errors, remediation=None, owner=None)` records which
package answers for a key. `owner` defaults to the caller's top-level package
and is recorded only for a key nobody owns yet — **first registrant wins** — so
re-registering somebody else's key still overrides its en text (the fork-free
override seam of i18n-shipping.md §3 is untouched, and a test pins it) without
taking over the duty of translating it. Core claims its own keys explicitly
where import order would otherwise decide. `error_owners()` / `error_owner()`
read it back; `docs/errors.json` is unchanged, so no downstream artifact moves.

**The loader does not change.** `load_app_catalogs` stays a flat later-wins
merge over INSTALLED_APPS. Pinning core-owned keys inside it would have meant
either breaking the host-app-overrides seam or teaching a file merge about a
registry. Precedence stays positional — core's catalog, then a module's
declared override, then the host app last — and the invariant "nobody shadows
core by accident" is enforced where the accident happens: at write and gate
time.

- `check_translation_catalogs` scopes coverage to the keys the gated app
  **owns** (resolved from its directory against INSTALLED_APPS), and raises
  **E `foreign`** for an entry belonging to another package *that already ships
  that language for that key*. The carve-out is deliberate: covering a key its
  owner does not translate — a host generating one language for the whole
  fleet — is gap-filling, not shadowing, and stays silent until the owner ships
  it, at which point the gate goes red and the copy is deleted or declared.
- `translate_catalogs` will not emit a key the target package does not own. The
  command that manufactured the 410 duplicates can no longer manufacture one.
- A deliberate reword declares itself: `translate_catalogs --declare-override
  KEY…` writes `override: <owner>` into the `.state.json` row — a state
  transition by command, exactly like `--approve`, never a hand-edit. The
  catalog file stays a flat `{key: text}` map, which the runtime merge,
  `gen-errors.mjs` and human readers all depend on. `StateSidecar.set` now
  round-trips fields it does not manage, so a declaration survives the next
  retranslation instead of being silently dropped. A declaration that repeats
  the owner's text verbatim is **W `vacuous_override`** — it protects nothing.
- `STAPEL_I18N["UNDECLARED_OVERRIDES"]` (`"error"` default, `"warn"`) is the
  single policy switch, an escape hatch for a host onboarding a legacy catalog
  it did not write. Fleet libraries run the default.

Ownership scoping engages only where ownership resolves; a domain with no owner
resolver (`flows`) and a directory outside any installed app behave exactly as
before.

**For the five libraries already shipping catalogs**: delete the 41 core keys
from `errors.{ru,es}.json`, prune the matching `.state.json` rows, and raise the
`stapel-core` floor to `>=0.22` — the floor bump is what makes the deletion
safe. The numeric gate for that sweep is 410 → 0. Verified against
stapel-profiles: 82 `foreign` errors before, zero errors after deletion, and
the merged runtime catalog identical either way.

## [0.21.0] — 2026-08-10

### Added — `stapel_core.templates`: a missing template variable stops being invisible

Django's default is `string_if_invalid = ''`: a template that reads a variable
nobody passed renders an empty string and carries on. Fine for a page, silent
data loss for anything generated once and sent away. The measured case is
email — a library renames the context variable behind `{{ code }}`, its own
tests stay green because they render with its own context, and the OTP mail
ships with a blank where the code was. 200 OK, no exception, no log line,
nobody can log in.

The shape is a **sentinel plus an assertion**, not a crash:
`strict_template_variables(TEMPLATES)` substitutes a recognisable marker for an
unresolved variable, and `assert_no_missing_variables(rendered)` fails a test
naming every variable that went missing. `stapel_core.templates.W001` warns
under `DEBUG` when an engine is still silent.

Making the engine *raise* was built first and rejected. Not for the reason
usually given — `{% if var %}` is safe, because `IfNode` catches
`VariableDoesNotExist` itself and never consults `string_if_invalid`, and a
test pins that. What raising actually breaks is `{{ x|default:"y" }}`: with a
non-empty `string_if_invalid` Django returns it *before* the filter chain runs,
so `default` never fires — an exception where nothing is wrong, on stock Django
templates (the admin above all), from an engine-wide setting. A string marker
leaves that call to an assertion instead.

Test settings are the home. Production is deliberately untouched: this package
must not change how a host's mail renders as a side effect of an upgrade. And
the marker is the net, not the closure — it catches the variable a test
happened to exercise. The closure is a template contract asserted in CI
(`docs/templates.json`, `stapel_tools.template_contract` 0.35.0).

## [0.20.2] — 2026-08-09

### Fixed — the error reference renders Spanish as a language, not as a tag

`generate_error_docs --lang es` produced a page titled `# Errors — es` with
English column headers: `LANGUAGE_NAMES` and the header table in
`i18n/errordocs.py` knew `en` and `ru` and nothing else, and an unknown tag
fell back to itself. The catalog machinery was already language-agnostic — the
*document* was not, so the first library to ship a third language would have
committed a reference page that reads as a machine artifact.

`es` is now a first-class entry in both tables (`Español`; `Código / Estado /
Parámetros / Acción / Texto`), which is what the five libraries shipping
`translations/errors.es.json` in this wave generate against. The fallback for
an unlisted tag is unchanged and still renders — a language simply reads
better once it is named.

## [0.20.0] — 2026-08-09

### Fixed — i18n provenance stopped laundering machine output, and `translate_catalogs` stopped writing where nothing reads

Two defects in `stapel_core.i18n`, found while preparing a Spanish wave across
the libraries. Both had the same shape: a command reporting success for work
that had not happened.

**1. `is_reviewed()` counted a curated corpus as human review.** Any origin
other than `llm` was "reviewed", so a translation routed through
`translate_catalogs --seed` — the cheap, obvious path, and the one the fleet
uses for the stapel-translate builtin fixtures — drove the gate's `unreviewed`
counter to zero for text no human had ever read. It surfaced the hard way: an
agent adding Spanish hand-populated the machinery's own `.llm-cache.json` to
keep the provenance honest, i.e. the honest path was the non-obvious one.

Provenance now records **where a value came from**, and a separate predicate
answers **whether a human signed it off**:

* `llm` — machine translation from the `TRANSLATOR` seam;
* `seed:<label>` — lifted from a curated corpus: cheap, paid for, still
  machine-made;
* `imported` — already in the catalog with no sidecar row, authorship unknown
  (this branch used to record `human`, which was the same laundering in another
  place);
* `human` — a person read it and ran `--approve`.

`is_reviewed(origin)` is true for `human` alone and is what the gate's **W**
counter reports. The other axis is `is_curated(origin)` — human, seed or
imported — which answers a different question: may this value be silently
re-derived? No. A stale seed still stays put and is still reported `stale`,
because the corpus was curated against the OLD English and re-seeding would
paper over exactly the drift the gate exists to show. `TranslateResult.unreviewed`
now counts seeded and imported values alongside translated ones, and both
commands print the count.

*Existing catalogues:* `.state.json` files are read as written — no rewrite, no
re-approval, no regeneration, and byte-identical on disk. What changes is the
interpretation. Across `stapel-auth`, `stapel-billing`, `stapel-notifications`,
`stapel-profiles` and `stapel-workspaces` (343 ru keys), the unreviewed count
goes 33 → 339: 306 `seed:stapel-builtin` keys move from "reviewed" to
"unreviewed", which is what they always were. This was chosen over bulk-blessing
them to `human` — that is the laundering being removed — and it costs nothing:
`unreviewed` is a non-blocking **W**, no library gates on warnings or runs
`--strict`, and all five suites stay green. The number is now the honest size of
the review backlog for the Spanish wave to work against.

**2. `translate_catalogs --out` defaulted to a directory the loader never
opens.** The default `translations` resolved against the working directory (a
service root), while `load_app_catalogs` walks the *package* directories of
INSTALLED_APPS. The command wrote the file, printed success, and the catalog was
invisible forever after.

The write target is now derived and checked against the read side:

* default → the app package the command runs from (or the nearest one above the
  working directory);
* `--app LABEL` → that installed app's `translations/`;
* explicit `--out` → accepted only when it is `<root>/translations` for a root
  the loader walks; otherwise `CatalogDirError`, naming the directories the
  loader does read.

Outside any app package there is no defensible default, so it refuses instead of
inventing one. `resolve_catalog_dir()` and `load_app_catalogs()` both go through
the new `catalog_search_dirs()`, so "writable" and "readable" cannot drift
apart. `check_translation_catalogs` resolves its directory the same way — gating
a directory the loader cannot read is as useless as writing into one.

New in `stapel_core.i18n`: `CatalogDirError`, `ORIGIN_IMPORTED`,
`ORIGIN_SEED_PREFIX`, `catalog_search_dirs`, `is_curated`, `is_seeded`,
`resolve_catalog_dir`, `seed_origin`.

`tests/test_i18n_provenance_and_outdir.py` (20 tests) covers both fixes in both
directions: each defect is reproduced by an assertion that fails on the old
behaviour, and each legitimate look-alike — approval clearing the counter, a
library repo regenerating its own catalog with a relative `--out`, a stale seed
staying put — is asserted to stay silent.

## [0.19.0] — 2026-08-06

### Fixed — a Function call no longer dies silently on the transport's size cap

Measured on ironmemo (upload path, 2026-08-06): a ``llm.complete`` reply over a
meeting transcript exceeded NATS's 1 MiB ``max_payload``. ``msg.respond()``
raised ``MaxPayloadError`` INSIDE the subscription callback — after the
function had already run. Nothing was sent back, so the caller sat until its
timeout and reported a generic failure; the work was done, the answer thrown
away, and the only line naming the real cause lived in another process's log on
another host.

Both ends of the seam now refuse to fail quietly:

* **Server** (``serve_functions``): the reply size is checked against the
  broker's announced ``max_payload`` and, when it does not fit, a small
  structured marker goes out INSTEAD of the result. ``respond()`` is also
  wrapped — an exception there is, again, a caller that hears nothing at all.
  The size check lives in the module-level ``fit_reply()`` so it is testable.
* **Client** (``nats_function_transport``): an oversized REQUEST is refused
  before publishing (nats-py otherwise raises a bare ``MaxPayloadError`` that
  arrives as an opaque "failed over NATS"), and the server's marker is turned
  back into the same precise exception.

New ``FunctionPayloadTooLarge(FunctionCallError)`` carries the function name,
the actual size, the limit and the direction, and says what to do about it: a
Function is a request/response seam, not a file transfer — return a REFERENCE
the caller resolves. Raising the broker's ``max_payload`` buys headroom, not a
different answer. Subclassing ``FunctionCallError`` keeps existing handlers
working.

## [Unreleased]

## [0.18.0] — 2026-08-03

### Added
- `register_dependency_check(name, probe, *, critical=False)` — the outbound
  dependency lamp, next to the existing `register_metrics_exporter`. A module
  or a product registers a cheap probe for an outbound dependency; the first
  failed call shows up as `checks.<name>` on the health endpoint and
  `stapel_dependency_up{dependency="<name>"}` on the metrics endpoint.

  Motivated by a production incident (`docs/pending/env-address-class-v2.md`
  §3.6): meettoday's host-kick and room-PIN writes both go through twirp calls
  wrapped in best-effort `try/except`, so an unreachable LiveKit meant those
  two features silently did nothing — for a day, with nothing anywhere saying
  so. Best-effort is a legitimate pattern; best-effort *without a lamp* is how
  a broken dependency becomes invisible. The accompanying canon: a
  swallowed exception is allowed only paired with `logger.error` and a
  registered check.

- `register_dependency_check` and the pre-existing `register_metrics_exporter`,
  plus the four health/metrics views and `get_health_urls`, are now declared in
  the `surface` catalogue — so an agent asking "does the fleet already have a
  way to signal a broken outbound dependency?" gets an answer. Shipping the
  registry without cataloguing it would have repeated the exact defect it was
  built to fix.

### Changed
- `tests/test_contract.py` no longer uses `pytest.importorskip` for
  `stapel_tools`. A drift gate whose emitter is missing was reporting
  `1 skipped` and exit 0 — indistinguishable from "no drift", and invisible
  among the rest of a green run. It now fails hard and says why. Measured
  across the fleet on 2026-08-03: ten gates shared this shape, kept alive only
  by an earlier CI step incidentally installing the tool.

## [0.17.1] — 2026-08-02

### Added
- `docs/capabilities.json` — the core's first contract document: a `surface`
  section cataloguing the permission classes, factories, predicates and
  templates a product is meant to call instead of rolling its own (#183).
- `docs/llms.txt` — the fifth contract artifact, an agent-sized slice of
  `docs/capabilities.json`, wired into `make contract` / `make contract-check`
  and drift-gated in `tests/test_contract.py` (badge-canon §3).
- Badge canon in README, classifier 3.14, `migration-lint` enabled in CI.

### Fixed
- `docs/capabilities.json`, `docs/flows.json`, `docs/errors.json` and
  `CONFIG.MD` now ship in the wheel via `package-data` (#184); `docs/llms.txt`
  likewise, so `--from-installed` tooling actually sees all five contract
  artifacts.

## [0.17.0] — 2026-07-30

### Added
- **`FieldSpec` (`stapel_core.django.fieldspec`) — a copy seam must classify
  every field of its model (#133).** A seam that materializes one row from
  another (recurring series master → occurrence, template → instance, draft →
  published) always begins as a hand-written list of fields to carry over.
  That list is correct exactly once: the next field added to the model is
  silently not carried, and nothing says so. In a real product this dropped
  two of four settings fields from a meeting room, and both losses inverted
  the host's declared intent — an "open" series slammed the door on the first
  join, and a PIN-protected series materialized rooms with no PIN. Found by
  users, not by tests.
- The declaration lives on the model, next to the fields:
  `FieldSpec(copy=(...), recompute=(...), never=(...))`. `spec.values(source)`
  builds the `copy` half as a dict and validates first; `spec.validate(Model)`
  is the same check for a test. Every concrete field must land in **exactly
  one** list — `FieldSpecError` names unassigned fields, declared names that
  are not fields (a rename left behind), and names in two lists. "Forgot"
  becomes "must decide".
- Deliberately small, and honest about its limit: it enforces that a decision
  was made, **not that it was right**. A field wrongly classified `never`
  passes green. What it removes is the field nobody ever classified — the case
  that actually leaked.

## [0.16.1] — 2026-07-30

### Fixed
- **`STAPEL_VERIFICATION["EXTRA_FACTORS"]` was a documented escape hatch with
  no caller (#145).** `load_configured_factors()` is what MODULE.md names as
  the way a host substitutes or adds a verification factor — and nothing in
  stapel-core or stapel-auth ever called it. A host that followed the
  documentation to the letter got a decorative setting, silently: no factor
  registered, no check, no warning. A product hit this on a real security fix
  (a phone factor that sends nothing had to be demoted to `strength="weak"`,
  otherwise `strong_factors()` was non-empty and 2FA enrolment was waived) and
  had to call the loader from its own `AppConfig.ready()` to make the fix real.
  `CommonDjangoConfig.ready()` now calls it, so the declaration is the whole
  wiring — the documentation became true instead of being corrected downwards.
- **The override is now order-independent.** `EXTRA_FACTORS` entries are
  registered *pinned*: the registry keeps the host's factor for that id and
  ignores a later library registration of the same id. Before, "last
  registration wins" meant the overriding app had to be listed **below**
  `stapel_auth` in `INSTALLED_APPS`, and moving it up made the override
  decorative again with no signal. An app that already calls the loader itself
  (the pre-0.16.1 workaround) keeps working and simply re-pins the same class.
- An `EXTRA_FACTORS` dotted path that cannot be imported, or does not yield a
  valid factor, now raises `ImproperlyConfigured` at boot instead of being
  skipped — a broken escape hatch is louder than a silent one.
- `FactorRegistry.register(factor, *, pin=False)` and
  `register_factor(factor, *, pin=False)` gained the keyword; `pinned_names()`
  exposes the host-claimed ids for introspection. Existing call sites are
  unaffected.

### Notes
- Audit of the same defect class across all 28 stapel repos (public,
  documented host-substitution setting whose applying loader is never called
  inside its own shipping unit): this was the **only** truly dead one. The one
  neighbour worth naming is `GDPR_PROVIDERS` — the registry lives in
  stapel-core but the only code that applies the setting is
  `stapel_gdpr/apps.py`, so declaring it without `stapel_gdpr` in
  `INSTALLED_APPS` is inert in the same shape, one repo removed (and it is a
  flat setting outside any `AppSettings` namespace, hence invisible to the
  CONFIG.MD tooling). Not fixed here.

## [0.16.0] — 2026-07-30

### Added
- **Adoption checks — a third genre of system check (tag `stapel_adoption`,
  `stapel_core.django.adoption_checks`).** Config checks (`stapel_nav`) ask
  whether a setting is well-formed; topology checks (`stapel_mounts` E004) ask
  whether a mount is where it belongs. An *adoption* check asks the question
  `stapel-tools`' `adoption_lint` (ADO001) asks from outside the process, from
  inside it: **the project switched an axis on — did the code the axis affects
  actually take a position on it?** Its idiom is three parts, and the third is
  the one that keeps such a check alive: derivable premise → derivable
  obligation → **an explicit waiver instead of silence**.
- The first one is the anonymous axis. Premise: `stapel-auth`'s
  `AUTH_ANONYMOUS` is on, so guest sessions exist and a guest *is*
  `request.user.is_authenticated`. Obligation: a view whose entire gate is a
  bare `IsAuthenticated` therefore admits guests, and its source says nothing
  about whether that was meant. `stapel_core.adoption.E001` reports that
  silence — never the choice. Three ways to be green, all explicit:
  `IsNotAnonymousUser` in `permission_classes`; any other/stronger permission
  class (capability, object, role); or
  `stapel_anonymous_access = ANONYMOUS_ALLOWED` / `ANONYMOUS_DENIED` on the
  view (new constants in `stapel_core.django.api.permissions`).
- The formulation is the substance. A check that demanded `IsNotAnonymousUser`
  everywhere would be wrong on its first real consumer — in meettoday an
  anonymous guest joining a call is the product, and several views must stay
  open to one — and would be added to `SILENCED_SYSTEM_CHECKS` whole on day
  one. Turning an unwritten assumption into a declared one is also worth as
  much as the protection: "guests may join this call" stops being an unwritten
  property of a permission class that is not there.
- `stapel_anonymous_access` is deliberately hard to set by accident: a
  `stapel_`-prefixed attribute nothing in Django or DRF carries, with a closed
  two-value vocabulary — a misspelled value is reported
  (`stapel_core.adoption.E002`), never read as a declaration.
- Two asymmetries, both about keeping the check un-mutable.
  `stapel_core.adoption.W001` reports a bare-`IsAuthenticated`
  `DEFAULT_PERMISSION_CLASSES` **once, at the setting**, instead of charging
  every view that never wrote a `permission_classes` line for a decision made
  in `settings.py`. `stapel_core.adoption.W002` carries the same finding at
  W-level when the view arrived in an installed `stapel_*` wheel: E-level
  findings become deploy blockers through `stapel_preflight`, and blocking a
  deploy on a file the reader cannot edit is exactly how a whole tag gets
  silenced. Level follows who can act.
- **`stapel_core.django.urlsurvey`** — the one URLconf walk every
  surface-reasoning check shares (`iter_surface()`, `iter_url_patterns`,
  `path_segments`, `callback_owner_app_label`, `view_of`). These lived as
  private helpers inside `django/mounts.py` and served exactly one check; a
  second check needing the same walk is what makes them a mechanism rather
  than a helper. `mounts._iter_url_patterns` / `_path_segments` /
  `_callback_owner_app_label` re-export from there unchanged.

### Notes
- Minor, not patch: a new Error-level check can turn a currently green
  consumer red at `manage.py check` / `stapel_preflight` time. Nothing is
  removed or renamed, so it is not a major. First live run (meettoday,
  63 findings): 25 E001 in the project's own `rooms`/`recordings`/`accounts`/
  `calendar_app` views, 37 W002 across five installed modules, 1 W001.

## [0.15.12] — 2026-07-29

### Fixed
- The `to_strict_subset` tests no longer import pydantic. Core does not depend
  on it and must not: dataclasses inside, DRF at the HTTP edge, pydantic only
  where untrusted structured text arrives. The test passed locally because the
  shared dev venv had pydantic from a sibling library, and failed in CI where
  the dependency list is the truth — 0.15.11 never reached PyPI as a result.
  The schema shapes are now literals, which is also more honest: a test in core
  should not need pydantic to describe pydantic's behaviour. The companion test
  pinning that premise stays in `stapel-agent`, which has pydantic legitimately.

## [0.15.11] — 2026-07-29

### Added
- **`stapel_core.schema_strict.to_strict_subset()`** — moved here from
  `stapel-agent` 0.6.6 (one day old; `stapel_agent.schema_strict` still
  re-exports it, so nothing breaks). It is a pure JSON Schema transform with no
  provider knowledge in it, and two different sides need it: the transport that
  sends the schema, and any caller that wants to inspect what will actually go
  out *before* paying for the call. With it living inside the transport, the
  second was impossible without importing the LLM library into a service that
  has no business making LLM calls in-process.

## [0.15.10] — 2026-07-29

### Added
- **`stapel_core.hashing`** — `canonical_hash()` / `canonical_json()`: a stable
  version key for a JSON-able artifact. Derived work (a summary, an LLM
  extraction, a user's edit log) has to say which version of its source it was
  built from; a timestamp cannot answer that, because it moves when nothing
  meaningful changed, and a revision counter cannot either, because two writers
  hand the same number to different content.
- The canonicalization is the substance, not the sha256 around it. `json.dumps`
  leaves key order, whitespace and non-ASCII escaping free, and each of those
  changes the bytes without changing the meaning — so two processes that
  disagree on any of them mint different keys for identical content, which
  every reader downstream interprets as "the source changed". All three are
  pinned, and pinned as a compatibility contract: this recipe reproduces a
  digest recorded by an independent implementation over a 107-segment
  transcript (`sha256:798782c1…`, checked 2026-07-29), so relaxing any of them
  invalidates every key already stored.
- Digests carry their algorithm (`sha256:<hex>`). A bare hex string is
  un-migratable — the day another algorithm is needed, nothing distinguishes an
  old value from a new one and every stored key has to be discarded.
- Non-JSON input (UUID, datetime) raises `TypeError` rather than falling back
  to `default=str`. That fallback would hash `repr` output, so a value whose
  representation changed but whose meaning did not would read as changed
  content — a false staleness signal, the failure mode that is hardest to
  notice because nothing errors.

## [0.15.9] — 2026-07-29

### Added
- **`default_language()`** — the fallback language is the project's, not the
  framework's. A framework that hardcodes `"en"` as the final answer imposes a
  product assumption: a service built for a Russian-speaking market wants `ru`
  there, and every English string it falls back to is a defect. Resolution:
  `STAPEL_LANGUAGE["DEFAULT"]` → `settings.LANGUAGE_CODE` → `"en"` only when
  there is no project at all. Preferring Django's own setting means a host that
  already configured its language gets the right fallback without learning a
  second knob — the same reasoning as taking `LANGUAGE_COOKIE_NAME` for the
  cookie name.
- `resolve_language_from_request()` now defaults to it. The pure
  `resolve_language()` keeps its Django-free `DEFAULT_LANGUAGE` so the async
  consumer path can still import it with no settings configured.


## [0.15.8] — 2026-07-29

### Added
- **`stapel_core.language`** — one place every service can ask "what language
  is this for?". Resolution is not a single lookup but a small state machine
  over four independent inputs: the user's explicit choice, whether the device
  may override it, what the device asked for this request, and the last
  language we saw for this user. `app_language` and `use_device_language` stay
  separate on purpose — someone who picked Russian may still want the device to
  win while travelling.

  `supported_languages` has two modes and the difference is real: a set means
  static UI strings exist only in those languages and anything else falls back;
  `None` means accept whatever was asked for, which is what LLM translation
  needs and what no fixed catalogue can express.

  `resolve_language()` takes plain values rather than a request, because the
  caller that needs it most — a notification consumer rendering an email in a
  different process hours later — has no request to read.
  `resolve_language_from_request()` is the thin Django wrapper.

  Restored from the marketplace codebase, where it ran for years and then
  failed to make the trip into the framework. Two things were fixed rather than
  carried over: Accept-Language is now quality-weighted (the original read the
  first entry and ignored `q=`, so `"de;q=0.2,en;q=0.9"` resolved to German —
  the opposite of what the header says), and the region subtag survives when
  the supported set distinguishes it, so `pt-BR` and `pt-PT` stop collapsing.

  Transports, in order of authority: stored preferences → `X-App-Language`
  header (a mobile client or a service-to-service hop has no cookie jar) →
  cookie. The cookie name defaults to Django's own `LANGUAGE_COOKIE_NAME`
  rather than an invented one, so the stock `set_language` view interoperates
  for free; header and cookie names are overridable via `STAPEL_LANGUAGE`.


## [0.15.7] — 2026-07-26

### Fixed
- **`preflight.W002` no longer warns about a peer a monolith never calls.**
  `check_peer_internal_routes()` probed `WORKSPACES_SERVICE_URL` unconditionally,
  but a monolith with `stapel_workspaces` installed answers membership
  in-process — hosts import `stapel_workspaces.permissions.require_role`
  directly and this HTTP client is never reached. The peer URL defaults to a
  service hostname that only exists in a microservice compose, so every
  monolith got a warning about a broken peer that nothing talks to (found on
  meettoday, 2026-07-26), plus a 3s timeout on each preflight run. The check
  now skips when the app is installed locally. A check that warns about a
  topology it never established is the same defect this one exists to catch.

## [0.15.6] — 2026-07-26

### Fixed
- **`stapel_core.mounts.E004` no longer flags a canonical mount as a
  violation.** `_path_segments` stripped regex anchors once across the whole
  path instead of per segment, so a module that registers a `re_path` router
  (stapel-currencies uses `r"api/v1"`) and is then mounted under a host
  prefix produced `currencies/^api/v1/...` — the `^` landed mid-path, the
  segment read `"^api"`, and the §37 containment check reported an error
  against a perfectly correct mount. Every generated project that included
  `currencies` failed its own `manage.py check`. Surfaced by the scaffold's
  own gate, once an unrelated E003 stopped masking it.

## [0.15.5] — 2026-07-26

### Added
- **`stapel_core.django.peers`** — the rule that 0.15.3 and 0.15.4 each
  re-derived locally, extracted so every cross-service client shares one
  copy: `service_answered()` (a view's 404 renders JSON, the URL resolver's
  renders HTML), `get_with_path_discovery()` (candidate mount points
  newest-first, remembering the one that answered) and
  `PeerRouteUnavailable` ("this service and its peer disagree about the
  path" — an explicit failure, never a silent empty result). The fleet sweep
  that followed the 2026-07-26 incident found seven callers of that shape,
  which is three too many for a rule kept in one module's private function.
  `stapel_core.django.workspaces._service_answered` is now an alias.
- Python **3.14** in the CI matrix. The stand runs 3.14; CI tested
  3.11–3.13, so an import-lock inversion that deadlocks only on 3.14 shipped
  and was found by a service answering 502 after every deploy.

### Added — `stapel_core.django.peers`: cross-service calls that cannot mistake a routing 404 for an answer

The rule 0.15.4 introduced inside `django/workspaces.py` (a 404 rendered as
HTML came from Django's URL resolver, a 404 rendered as JSON came from the
view) turned out to be needed by every cross-service client: stapel-translate's
key collectors had the identical pair of bugs — a hardcoded peer path plus a
404 read as "no keys" — and had silently collected nothing since the §60
v1-canon sweep.

- `service_answered(resp)` — the Content-Type discriminator, extracted;
  `workspaces._service_answered` is now an alias of it (no behaviour change).
- `get_with_path_discovery(base_url, candidates, ...)` — GETs the first
  candidate mount point that reaches a *view*, returns `(response, url)`, and
  raises `PeerRouteUnavailable` naming every path it tried when none does.
  Transport errors propagate unchanged (another path cannot help a refused
  connection); a view's own 404 stops discovery, because it is an answer.
- `PathResolver` — remembers the mount point that answered, so the probe
  costs one extra request per process rather than per call.

## [0.15.4] — 2026-07-26

Two more callers of the same shape 0.15.3 fixed, found by sweeping every
cross-service call in the fleet after that incident.

### Fixed
- **`check_cdn_media_exists` was calling a path stapel-cdn stopped serving.**
  The §60 v1-canon sweep moved the CDN API under `v1/` and this caller was
  not swept with it, so every existence check had been hitting Django's URL
  resolver instead of the view. Same treatment as the workspaces client: the
  mount point is discovered rather than assumed (the prefix depends on what
  the HOST chose, so no literal here can be right for every deployment), and
  a 404 that came from the resolver rather than the view is an outage, not
  "the file does not exist".
- **A routing 404 no longer degrades authorization.** The HTTP function
  transport mapped every 404 to `FunctionNotRegistered` — which is a
  *degrade* signal: `require_capability` answers it by falling back to the
  builtin role→capability table, where every client-defined custom role
  denies. So a mis-set `FUNCTION_ROUTES`, or a service that never mounted
  `get_function_urls()`, would silently downgrade authorization instead of
  failing loudly. The function view renders its 404 as JSON; the resolver
  renders HTML — only the former is now read as "no such function".

## [0.15.3] — 2026-07-26

### Fixed
- **A routing 404 from a peer service is no longer read as a verdict.**
  Owner-reported live incident: opening "My meetings" on the ironmemo stand
  showed `Forbidden: not a member of this workspace` — to the account that
  OWNS the workspace, with the membership row (`role=owner`, accepted, not
  suspended) sitting right there in the workspaces database.

  stapel-workspaces 0.4.2 moved its whole API under `v1/` (the §60 v1-canon
  sweep). This library's membership client kept requesting the pre-v1 path,
  Django's URL resolver answered 404, and `get_membership` read that as "no
  such membership" — then cached the non-answer for 30 seconds, so every
  caller that renders `None` as HTTP 403 confidently denied the user. Nothing
  logged a cause: the 404 branch was the "normal" path. The same bug silently
  broke `get_or_create_personal_workspace`, so registration produced accounts
  with no personal workspace.

  Three guards, because each one alone would have let this through:
  - The internal API's mount point is **discovered**, newest-first, and
    remembered per process — a mixed-version fleet keeps working in either
    direction instead of reading routing 404s as answers.
  - A 404 is only a verdict when the **view** rendered it. DRF answers
    `application/json`; Django's resolver and proxies answer HTML. A
    non-answer is never cached and never equated with "not a member".
  - `get_membership(..., strict=True)` / `require_role(..., strict=True)`
    raise `WorkspaceLookupUnavailable` instead of returning `None`, so a
    caller that turns `None` into 403 can turn an outage into 503. The
    default stays `None`, so existing callers are unaffected.

### Added
- **`preflight.E004`** — `stapel_preflight` now asks the workspaces service
  whether it actually serves the path this build calls, before a deploy.
  Every system check in this codebase validates a service's OWN urlconf;
  nothing had ever looked outward at a peer, which is precisely where this
  contract broke. Read-only (it asks about a nil UUID, so any answer proves
  the route exists). An unreachable peer is `W002`, a warning — only a
  service that answers on NO known path blocks the deploy.

## [0.15.2] — 2026-07-26

### Fixed
- **A Kafka consumer now provisions the topics it subscribes to.** It
  already declares them — it passes them to `subscribe()` on the next line.
  Requiring somebody to *also* list them by hand somewhere else (a deploy
  script, a runbook, an infra repo) is a second source of truth, and it
  drifted: the ironmemo stand ran for weeks with six recordings topics
  missing from its deploy script's list, delivering nothing, on containers
  that reported healthy. The NATS backend never had this failure mode — its
  stream captures `<prefix>.>`, so a new topic needs no broker-side change
  at all; Kafka now matches. Each topic's `.dlq` is created alongside it (a
  poison message with no DLQ to park in is a dropped message).

  Best-effort by construction: an existing topic is the normal case, and a
  broker that refuses creation never blocks a consumer that may already
  have its topics. Deployments where topics are infra-owned and applications
  hold no create ACL set `KAFKA_PROVISION_TOPICS=false`;
  `KAFKA_TOPIC_PARTITIONS` / `KAFKA_TOPIC_REPLICATION_FACTOR` (both `1`)
  size what is created.
- `UNKNOWN_TOPIC_OR_PART` is logged as a single WARNING per topic instead
  of an ERROR on every poll. librdkafka re-reports it on each metadata
  refresh — several lines per second, per topic — which at ERROR buried
  every real failure in the same log.

## [0.15.1] — 2026-07-26

### Fixed
- 0.15.0's own test suite: the "healthy deployment" case asserted that
  `stapel_preflight` finds no errors while running inside a suite that
  deliberately registers check-tripping models — green alone, red in the
  full run, and the tag went out on it. The case now pins the report shape
  and the `ok` flag directly, and a separate test asserts that Django check
  ERRORS are surfaced (using that same pollution as the fixture it is).

## [0.15.0] — 2026-07-26

Minor: the first half of the upgrade-contract work — a deployment can now
be *asked* whether it can take the code, instead of finding out by
crashing.

### Added
- **`manage.py stapel_preflight`** (`--json` for a release harness): a
  read-only pre-deploy check that runs against the real settings, database
  and installed packages. Every check is a failure that took the ironmemo
  stand down on 2026-07-25/26, each one predictable from information that
  was already there:
  - `preflight.E001` — an unapplied INITIAL migration whose table already
    exists (the app-rename/extraction hazard that killed `migrate` fleet-wide);
  - `preflight.E002` — `ACTION_TRANSPORT="bus"` on an in-process bus
    backend (consumer processes that no-op and restart forever);
  - `preflight.E003` — a configured transport whose client library is not
    installed (`nats`/`confluent_kafka`);
  - `check.*` — Django's own system-check ERRORS, surfaced BEFORE the
    deploy rather than inside the container's `migrate`;
  - `preflight.I001` — the migrations that would be applied;
  - `preflight.I002` — the OAuth `redirect_uri` this deployment will send,
    since that is a third-party registration no code change can update.
  Findings are structured (level/code/message/fix/context) so a release
  UI can show decisions instead of a traceback; a broken check degrades to
  a warning instead of masking the rest. Exit code 1 = do not deploy.

## [0.14.2] — 2026-07-26

### Fixed
- **A standalone bus consumer refuses to run on an in-process bus** instead
  of exiting silently. `MemoryBus.consume()` drains a queue that lives in
  the PUBLISHER's memory, so a consumer process gets nothing, returns, exits
  0 — and a container restart policy turns that into an infinite quiet
  loop with zero events delivered. 0.11.0 flipped the default backend from
  Kafka to MemoryBus, so every deployment that did not then set
  `STAPEL_BUS_BACKEND` landed there and could not tell (ironmemo stand: all
  actions/consumer workers restart-looping for weeks, cross-service events
  dead). `BaseBusConsumerCommand` now raises `CommandError` naming the
  setting and a broker backend; `--allow-in-process` keeps the
  single-process test path. `BusBackend.in_process` marks such backends.

## [0.14.1] — 2026-07-25

### Fixed
- **The taskstore's initial migration now ADOPTS a pre-rename table**
  (`django/taskstore/migrations/0001_initial.py`). 0.8.0 renamed this
  app's label `stapel_tasks` → `stapel_taskstore` and pinned `db_table`
  to the historical name, so the table never moved — but the migration
  STATE stayed under the old label, so on every database that migrated
  before 0.8.0 the app arrived looking unapplied and its `CreateModel`
  hit `relation "stapel_tasks_taskrecord" already exists`. Result:
  `manage.py migrate` died at container boot for the whole fleet, while
  fresh installs were fine — which is why it stayed invisible until a
  real upgrade (ironmemo stand, three weeks behind). The table and its
  index are now created only when genuinely absent
  (`CreateModelIfAbsent` / `AddIndexIfAbsent`, both driven by the
  HISTORICAL `to_state` model so a fresh database still gets the
  0001-era shape). `replaces=` was not an option: the old label now
  belongs to the real `stapel-tasks` module and claiming its migrations
  would swallow that module's history.

## [0.14.0] - 2026-07-24

### Added — Workspaces Org Program Wave 0 (all additive; unblocks W1-W5)

- **`verification.factors`: factor strength** — `VerificationFactor.strength:
  "strong" | "weak"`, default `"weak"`. Canon: an email code alone is NOT 2FA,
  so a factor is weak unless its registrar explicitly marks it strong
  (stapel-auth will mark `totp`/`passkey`/`otp_phone`). New surfaces:
  `factor_registry.describe()` (`[{"id", "strength"}]`),
  `factor_registry.strong_names()`, and `strong_factors(user)` — the strict
  "does this user have real 2FA" predicate (strong AND available). `register()`
  validates strength against `FACTOR_STRENGTHS`. All exported from
  `stapel_core.verification`.
- **`django.users`: org-provisioned user primitives** on `AbstractStapelUser`:
  - `password_change_required` / `mfa_enrollment_required` booleans (default
    False) — first-login policy flags set by auth's provision flow.
  - `auth_type` choice `"login"` — org-provisioned login/password identity.
  - `username_validator = StapelUsernameValidator()` (new
    `django/users/validators.py`), with the `username` field re-declared to
    carry it: the stock alphabet plus AT MOST ONE `/` as the org-namespace
    separator (`org_slug/local`). Bare usernames validate exactly as before;
    leading/trailing/double slashes and invalid chars on either side reject.
  - **Migration truth**: `AbstractStapelUser` is abstract — the model changes
    materialize per concrete user model. Core's own `users.User` ships
    migration `users.0009_org_program_wave0` (2 AddField with defaults +
    choices/validator AlterFields — expand-only, no data change, no DB
    constraint change). Hosts pointing `AUTH_USER_MODEL` at their own subclass
    must run `makemigrations` in their user app after upgrading and will get
    the same expand-only operations.
- **`django.workspaces`: `require_capability(workspace_id, user_id,
  capability)`** consumer helper — asks the `workspaces.check_capability` comm
  Function (workspaces 0.6+) with the same 30 s cache pattern as
  `get_membership` (verdict cached per ws/user/capability; remote failure is
  fail-closed and uncached). Against an old workspaces without the Function
  (`FunctionNotRegistered`/`FunctionRouteNotConfigured`) it degrades to
  `get_membership` + the builtin role→capability fallback table
  (`BUILTIN_ROLES`, spec §A1 mirror — stapel-workspaces owns the authoritative
  registry; unknown custom roles deny). Wildcards `"*"` and `"prefix.*"`
  supported by the matcher.

## [0.13.0] - 2026-07-22

### Added — source-agnostic image descriptor (`stapel_core.media`)

- `StapelImage` (+ `StapelImageArray`, `ImageSource`): the single contract a
  renderer (`@stapel/image` `<Image>`) consumes for ANY image — a superset of
  `RenderMetadata` with a `source` tag (`cdn`/`file`/`link`) and an
  always-present top-level `url`, so a client renders whether or not a CDN is
  wired. `variants` is the ladder or `[]` (renderer degrades to the url).
- `media.image(source, value)` builder — routes BY the per-value `source` to
  the right provider (`cdn`→CDN comm, `file`→PIL), NOT the deployment's global
  `STAPEL_MEDIA_BACKEND`. Fixes the empty-ladder gap where a pil-default
  deployment described cdn-uploaded avatars with the wrong provider (different
  variant naming → zero variants found).
- `media.drf` — `StapelImageSerializer` / `StapelImageArraySerializer` /
  `RenderMetadataSerializer` + `media.dto` dataclass mirrors, so any
  ref-carrying serializer denormalizes an image next to the ref and
  drf-spectacular emits a stable component.

### Fixed

- `StapelDataclassSerializer`: a `str` field defaulting to `""` now allows the
  blank value (was `required=False` but `allow_blank=False`, rejecting the very
  value it defaulted to). Desynced from `blank=True, default=""` models and
  made "clear field" indistinguishable from "leave unchanged" on PATCH.
  `allow_blank` also joins the `field(metadata=…)` override keys.

## [0.12.5] - 2026-07-20

### Added — `"sso"` identity anchor on `AbstractStapelUser.AUTH_TYPE_CHOICES`

`django/users/models.py`'s `AUTH_TYPE_CHOICES` gains `("sso", "SSO")`,
alongside `email`/`phone`/`oauth`/`anonymous`. Consumers (stapel-auth 0.8.0)
promoting an anonymous guest session via SSO JIT provisioning need an
accurate `auth_type` to record — reusing `"oauth"` or `"email"` for an SSO
anchor would misreport how the account was actually established. Migration
`0008_alter_user_auth_type` — choices-only, no data change.

## [0.12.4] - 2026-07-17

### Changed — CDN fields unfrozen: `image_type` is an open string (cdn-modularity.md §2.1)

`django/cdn/fields.py` `CdnImageField`/`CdnImageListField` no longer raise
`ValueError` at class-definition time against a hardcoded 6-value enum
(`CDN_ASSET_TYPES`/`CDN_IMAGE_TYPES`/`CDN_ALL_TYPES` — a verbatim copy of
the legacy marketplace `ImageType`/`AssetType` TextChoices). That froze
the client half of the stack to a fixed marketplace-specific list while
the server (`stapel-cdn` 0.7.0+) already accepted any configured type —
"half the stack is modular, half isn't, and nothing catches the mismatch"
(cdn-modularity.md §0.1).

- **`image_type`** is now any lowercase slug (`^[a-z0-9_-]+$`) — checked
  eagerly (cheap, config-independent shape check), but no longer checked
  against a frozen enum at import time. Whether a type is actually
  *configured* for this deployment is a lazy, boot-smoke concern (see
  system checks below), never a model-import-time one.
- **New `ref_kind` kwarg** (`"hash"` default | `"slug"`) replaces the old
  implicit `image_type in CDN_ASSET_TYPES` membership test that decided
  whether a ref's `<id>` half must be a 64-hex hash or a free-form name.
  `CdnImageField(image_type="catalog", ref_kind="slug")` replaces what
  used to be inferred from `"catalog"` being a hardcoded "asset" type.
- **New `django/cdn/conf.py`** — `STAPEL_CDN["ASSET_TYPES"]` (default
  `("avatar",)`, the zero-infrastructure default: the one CDN type every
  project plausibly has, nothing marketplace-specific baked in). Single
  source of truth shared with `stapel-cdn`'s own config for the same
  namespace.
- **New `django/cdn/checks.py`** (tag `stapel_cdn`) —
  `stapel_core.cdn.E001`: a declared field's `image_type` isn't in
  `STAPEL_CDN["ASSET_TYPES"]` (would fail `validate()`/`full_clean()` on
  every save attempt). `stapel_core.cdn.E002`: any CDN field is declared
  but no `cdn.*` comm route is configured at all — the class of "design
  shouldn't allow this" bug from the meettoday incident (a `CdnImageField`
  frozen to CDN format with no CDN service behind it, caught only when a
  user clicks "Change avatar" in production).
- Removed `CDN_ASSET_TYPES`/`CDN_IMAGE_TYPES`/`CDN_ALL_TYPES` module
  constants (nothing else in the repo referenced them outside this
  module's own tests).
- Fleet note: this is a behavior change for any code that caught the old
  `ValueError` on unconfigured types (unlikely, unverified across the
  fleet) — run the full suite after bumping.

## [0.12.3] - 2026-07-17

### Fixed — drf-spectacular import-order bug: blank schema title/version

Root cause: `stapel_core/django/__init__.py` eagerly imports
`.openapi.mcp` -> `.openapi.schemas`, and the latter does a *non-lazy*
`from drf_spectacular.openapi import AutoSchema` (needed as
`PermissionAwareAutoSchema`'s base class). Any project whose settings
module opens with `from stapel_core.django.settings import *` (the
documented pattern) triggers that whole import chain *before* the settings
module reaches its own `SPECTACULAR_SETTINGS = get_spectacular_settings(...)`
assignment further down — because importing `stapel_core.django.settings`
requires first fully executing the parent package's `__init__.py`.
`drf-spectacular`'s module-level `spectacular_settings` singleton
snapshots `django.conf.settings.SPECTACULAR_SETTINGS` at that exact
(too-early) instant and never re-reads it, so it stays permanently pinned
to the empty defaults (`TITLE=''`, `VERSION='0.0.0'`) — every schema the
process emits (`/schema/`, Swagger UI, the offline `spectacular` mgmt
command) reports a blank title and `0.0.0` version regardless of what the
project configured, for the rest of that process's lifetime.

- **`stapel_core.django.apps._unpoison_spectacular_settings()`**: called
  from `CommonDjangoConfig.ready()` (which Django only runs after settings
  are fully resolved). Diffs the already-built `spectacular_settings`
  singleton against the project's real `SPECTACULAR_SETTINGS` and, if they
  disagree, corrects it in place via drf-spectacular's own
  `apply_patches` seam — the same in-place object every module that did
  `from drf_spectacular.settings import spectacular_settings` already
  holds a reference to. Idempotent and zero-effect when the import order
  was already correct (or `SPECTACULAR_SETTINGS` isn't set, or
  drf-spectacular isn't installed).
- Regression test (`tests/test_spectacular_import_order.py`) reproduces the
  broken order by force-reimporting `drf_spectacular.settings` while
  `SPECTACULAR_SETTINGS` is absent, then asserts the patch corrects it;
  revert-checked (temporarily neutering the patch reproduces 2 red tests).
- Projects that locally worked around this in their own `AppConfig.ready()`
  (e.g. stapel-example-monolith's `_unpoison_spectacular_settings_cache`)
  can drop the local patch now that the framework fixes it at the source.

## [0.12.2] - 2026-07-17

### Added — §37 surface topology: reserved_paths() + mount-containment check

The live incident: nginx reserved the bare ``/calendar`` prefix for the
backend (because *something* Django-side lives under it) and silently
killed the frontend's SPA page at that same path. Canon (BACKLOG §37) was
already clear — a backend module owns only ``/<mod>/api/`` (versioned
inside), ``/<mod>/swagger/``, ``/<mod>/schema.json``, ``/<mod>/admin/``; a
bare module root or any other suffix is frontend territory — but nothing
machine-checked it, and nothing published it for generators/KB to read.

- **`stapel_core.django.mounts.reserved_paths()`**: for every Stapel module
  actually installed in this process (the same `INSTALLED_APPS`
  introspection `nav.discover_modules()` uses), the fixed set of §37
  sub-surfaces (`MODULE_RESERVED_SUFFIXES` — `api/`, `swagger/`,
  `schema.json`, `admin/`) the backend claims under that module's own mount
  root. Exported on `GET /nav` as the new `reserved_paths` field (keyed by
  module, alongside `modules`/`services`/`sections`) so a frontend router, a
  deploy-config generator (nginx/traefik location blocks — reserve only
  these, never the bare module prefix) and the KB read the one list instead
  of re-deriving the canon by hand.
- **`stapel_core.django.nav.is_stapel_app()`**: public wrapper of the
  existing `_is_stapel_app` marker/pip-package test, for consumers outside
  `nav` that need the same "is this app_config a Stapel module?" answer.
- **System check E004** (`stapel_core.mounts.E004`, tag `stapel_mounts`):
  walks the root resolver depth-first; for every URL pattern owned by an
  installed Stapel module (same ownership test as module discovery, applied
  to the view's `__module__`), flags an Error when no `api`/`swagger`/
  `admin`/`schema(.json)` segment appears anywhere in its full mounted path.
  Host (non-Stapel) URLs are never matched — a project stays free in its
  own paths; this is only about the modules it installed. Skips entirely
  when `ROOT_URLCONF` is unset (standalone package harness).
- **Fleet audit** (manual, `urls.py`/`urls_v1.py` read across every sibling
  module): one live violator — `stapel-translate` mounts its dashboard HTML
  pages at `translate/dashboard/...`, with no canonical segment anywhere —
  exactly the class of bug E004 exists to catch. Every other module (auth,
  billing, calendar, chat, cdn, categories, gdpr, listings, notifications,
  profiles, workspaces, mailtrap, agent, tasks, recordings, geo, video,
  reviews, currencies) mounts entirely under `api/` (nested `admin`
  sub-segments, e.g. auth's `admin_api` gate, stay compliant since `api` is
  present in the same path). Fixing `stapel-translate` is out of scope here
  (its own repo, its own release) — reported to the coordinator.
- Tests: `tests/test_mounts.py` (`TestReservedPaths`,
  `TestModuleSurfaceContainment` — compliant modules, nested-admin-inside-api,
  the translate-dashboard and bare-module-root violations, host URLs never
  flagged, nested `include()` walked, mixed installed-modules report only
  the violators), new fixture `tests/mounts_surface_urls.py`.

## [0.12.1] - 2026-07-17

### Added

- **Redis Streams bus backend** (`stapel_core.bus.backends.redis_streams.RedisStreamsBus`)
  — third standard comm-bus transport alongside Kafka and NATS JetStream.
  Select via `STAPEL_BUS_BACKEND=redis_streams` (or the alias `redis`).
  One Redis stream per topic (`XADD`, optionally capped with `MAXLEN ~` via
  `STAPEL_REDIS_BUS_STREAM_MAXLEN`); one consumer group per subscriber
  (`XREADGROUP`+`XACK`), named after the `group` passed to `consume()` —
  same convention as Kafka's `group.id` / NATS's durable name. Entries left
  pending by a consumer that died mid-handler are reclaimed via
  `XAUTOCLAIM` once idle past `STAPEL_REDIS_BUS_CLAIM_IDLE_MS` (default
  60s) and re-run through the same retry/DLQ path. Delivery semantics
  mirror the existing backends: at-least-once, 3x retry with backoff then
  DLQ (`<topic>.dlq` stream), poison messages DLQ'd raw instead of wedging
  the consumer, fresh consumer groups start at the beginning of the stream
  (matches Kafka's `earliest` / JetStream's `DeliverAll`). New extra
  `redis-bus = ["redis>=5"]` (declared explicitly — redis-py is already a
  transitive dependency via django-redis, but transitivity is not a
  contract); `bus.checks` E001 covers it (`pip install
  'stapel-core[redis-bus]'` hint when `redis` isn't importable). New
  connection settings: `STAPEL_REDIS_BUS_URL` (falls back to `REDIS_URL`),
  `STAPEL_REDIS_BUS_CLAIM_IDLE_MS`, `STAPEL_REDIS_BUS_STREAM_MAXLEN`.

## [0.12.0] - 2026-07-17

Legacy sweep (owner directive: only current code, no backward-compat
shims). **Breaking** — house law: minor bump, no deprecation cycle (alpha
policy: no migrations, clean slate every time).

### Removed

- **`stapel_core.kafka` legacy transport** — `kafka/consumer.py`
  (`BaseKafkaConsumerCommand`), `kafka/producer.py`, `kafka/config.py`,
  `kafka/health.py` deleted; `stapel_core.bus` is the only transport.
  `kafka/__init__.py` no longer re-exports the bus API
  (`Event`/`publish_event`/`get_bus`/`KafkaConfig`) nor the
  `BaseKafkaConsumerCommand` alias — the package now carries only the event
  contract constants (`EventType`, `TOPIC_*`).
- **`EventType.TRANSLATIONS_CHANGED` / `TOPIC_TRANSLATIONS_CHANGED`**
  (deprecated 0.3.x) — the translate→notifications sync is the comm Action
  `translations.changed`; nothing publishes or consumes the Kafka topic.
  `DLQ_PREFIX`/`dlq_topic` also gone from `kafka/topics.py` (the bus backend
  has its own DLQ naming); the `kafka.Event` dataclass (duplicate of
  `stapel_core.bus.Event`) deleted with the transport.
- **Django JWT import shims** — `stapel_core.django.utils`,
  `stapel_core.django.jwt_provider`, `stapel_core.django.authentication`
  deleted; import from `stapel_core.django.jwt.utils` / `.jwt.provider` /
  `.jwt.authentication`.
- **`IRON_HOST`** — env fallback and settings alias removed; `STAPEL_HOST`
  only (settings `__all__` now exports `STAPEL_HOST`).
- **Flat captcha settings** — `CAPTCHA_BACKEND` / `CAPTCHA_SECRET` no longer
  configure anything; `STAPEL_CAPTCHA = {"BACKEND": ..., "SECRET": ...}` is
  the single spelling.
- **Captcha dual `verify()` signature** — the level-kwarg signature sniffing
  (`_call_verifier`) is gone; `CaptchaVerifier.verify(token, ip=None, *,
  level=None)` is the one contract (built-ins updated; custom backends must
  accept the keyword).
- **Language cookie name overrides** — `STAPEL_COOKIE_APP_LANGUAGE` /
  `STAPEL_COOKIE_USE_DEVICE_LANGUAGE` settings removed;
  `stapel_app_language` / `stapel_use_device_language` are hardcoded.
- **`flows.i18n` backward-compat re-exports** — `CATALOG_DIRNAME`,
  `CommDocTranslator`, `DocTranslationCache` import from
  `stapel_core.i18n`; the `STAPEL_FLOWS["DOC_TRANSLATOR"]` default now
  resolves `stapel_core.i18n.CommDocTranslator`.
- **`setup_centralized_admin_logout()`** (deprecated no-op) — use
  `get_admin_logout_urlpattern()`.
- **`JWTStatusView` flat `user` wire block** — the response carries
  `authenticated` + presented `profile` + `tokens`; the legacy auth-identity
  shape (`user.user_id`/`username`/…) is gone from the wire.
- **`RevisionSyncMixin.revision_parameters`** compat alias — use
  `REVISION_PARAMETERS`.

Kept ("legacy"-flagged in the code, but this is live runtime behavior, not
back-compat): DAC behavior when no mandate is engaged (`access/backend.py`),
the `ConfigKeyUnknown` path of `get_config` without `required=`, nav prefix-fallback.

## [0.11.2] - 2026-07-17

Three more owner findings from the same live run, all additive/non-breaking.

### Fixed — validation error params now carry the field's limit (max_length, etc)

DRF's `ErrorDetail` carries only `.code` (e.g. `'max_length'`) — the actual
limit lives on the field that raised it
(`serializer.fields[field_name].max_length`), so a catalog consumer
(frontend i18n) could never render "no more than N characters" for a
`max_length`/`min_length`/`max_value`/`min_value`/`max_digits`/
`decimal_places` field error — `params` was always just `{field}`.

- `StapelDataclassSerializer.is_valid()` now attaches `self` to the raised
  `ValidationError` (`exc.stapel_serializer`); `stapel_exception_handler`
  reads the error code's known limit attribute off that field
  (`_field_limit_params`) and merges it into `params`. A plain
  `rest_framework.serializers.Serializer` that never attaches itself
  degrades to the field-name-only `params` exactly as before.
- Tests: `tests/test_errors.py` (max_length/min_value/max_value errors carry
  their limit when a serializer is attached; a bare `DRFValidationError`
  with no attached serializer stays field-name-only; `is_valid()` without
  `raise_exception` and on valid data behave exactly like stock DRF).

### Added — `error_language` on the error envelope

Live finding: the backend sent a Russian `error` message and the frontend
had no way to know whether that text was safe to show verbatim or needed
translating — `COMMON_ERRORS` templates are always English, but the DRF/
Django-`ValidationError` fallback (`str(detail)`) renders in whatever locale
`LocaleMiddleware`/`Accept-Language` activated, since DRF's own field
messages are `gettext_lazy`.

- `StapelError` gets an additive `error_language` field (the active Django
  locale via `get_language()`, through a `default_factory` so every existing
  construction site — `StapelErrorResponse()`, both `stapel_exception_handler`
  branches — picks it up with no code change there). Canon is unchanged: the
  client still translates by `localizable_error`+`params`; `error` stays a
  fallback/debug string, now with a known language attached.
- Tests: `tests/test_errors.py` (`error_language` present and matching
  `get_language()` by default, follows `translation.override(...)` across
  `StapelErrorResponse` and all three `stapel_exception_handler` tiers).

### Fixed — bus singleton no longer sticks to a stale backend after a settings change

`get_bus()` resolves the backend lazily on first call, but then holds it as
a module-level singleton for the rest of the process. If something called
`get_bus()` once before a later `override_settings(STAPEL_BUS_BACKEND=...)`
(or the pytest-django `settings` fixture, used throughout this suite), the
first resolved backend stuck around regardless of how the setting changed
afterward — the owner caught this live.

- `bus.router` now connects to Django's `setting_changed` signal (the same
  mechanism DRF uses to invalidate its own cached `api_settings`) and calls
  `reset_bus()` whenever `STAPEL_BUS_BACKEND` changes — any runtime
  reconfiguration path (tests first and foremost) invalidates the singleton
  automatically instead of silently keeping the stale backend. Production's
  env-based config needs no such hook (`os.environ` is read once at boot and
  doesn't change without a restart) — the very first `get_bus()` call
  already sees the final value there.
- Tests: `tests/test_bus_nats.py` (`override_settings` and the `settings`
  fixture both invalidate a previously-cached backend; an unrelated setting
  change leaves a live backend instance untouched).

## [0.11.1] - 2026-07-17

### Added — config-manifest required/optional semantics enforced at boot, code-sourced metadata

Follow-up to the 0.11.0 postmortem: `get_config`'s `Required: yes` was only
ever enforced (`ConfigUnavailable`) the first time some code path actually
called `get_config(key)` — possibly deep in a request handler, possibly
never for a key nothing reads through the config seam yet. Separately,
CONFIG.MD's `Purpose`/`Required`/`Default` cells are hand-maintained and can
silently drift from the code's actual default — exactly what had just
happened to `STAPEL_BUS_BACKEND`'s row (still said `...kafka.KafkaBus` after
0.11.0 moved the real default to `...memory.MemoryBus`; fixed in this
release too).

- **New system check** `stapel_core.config.checks` (tag `stapel_config`,
  E001): walks every key marked `required` — CONFIG.MD rows **and**
  call-site declarations (below) — and fails `manage.py check` /
  boot-smoke with `config 'X' is required — needed for: <purpose> — see
  CONFIG.MD` when it has no value and no default. A required key with a
  default never fails (the default is deliberately how "required, but the
  default is safe" is expressed) — only a required key with *nothing* to
  fall back on blocks boot. Registered from `CommonDjangoConfig.ready()`.
- **`declare_config(key, *, source=, purpose=, required=, default=)`** /
  **`declared_config_entries()`** / **`clear_declared_config()`**
  (`stapel_core.config`): call-site metadata registration (first-declaration-
  wins, mirrors `stapel_core.django.swappable.declare_swap`) — a code-sourced
  registry a future CONFIG.MD regenerator can read instead of trusting the
  hand-written table to already be in sync. Never overrides a real CONFIG.MD
  row for the same key (the table stays authoritative); only fills in keys
  the table does not know about yet.
- **`get_config(key, ..., purpose="", required=None)`**: passing these
  kwargs records the same metadata via `declare_config` as a backstop (the
  same "declare explicitly, or record lazily on first use" shape as
  `declare_swap`) — free for any existing call site, no behavior change when
  omitted. One behavior addition: `required=True` explicitly at a call site
  whose key has no CONFIG.MD row yet still fails closed (`ConfigNotDeclared`)
  on a missing value instead of the generic `ConfigKeyUnknown` — required is
  required, declared in the table or not.
- Fixed CONFIG.MD's stale `STAPEL_BUS_BACKEND` default cell (kafka →
  memory), and documented both mechanisms in CONFIG.MD's own preamble.
- Tests: `tests/test_config.py` (`declare_config` registration/first-wins/bad
  source, `get_config`'s new kwargs incl. the unknown-key-but-required
  fail-closed path and backward compatibility when omitted).
  `tests/test_config_checks.py` (required-missing fails, required-present
  passes, optional-missing never flagged, partial-missing reports only the
  missing key, a call-site-only declaration is still gated, CONFIG.MD wins
  over a conflicting call-site declaration for the same key, sanity check
  against core's own real shipped CONFIG.MD).

## [0.11.0] - 2026-07-17

### Changed — BREAKING: bus default backend kafka → memory (in-process)

Live run finding (meettoday): `request_notification` (OTP emails) silently
never left the process — `STAPEL_BUS_BACKEND` defaulted to
`stapel_core.bus.backends.kafka.KafkaBus`, `confluent-kafka` was not
installed, and every `publish()` raised `ModuleNotFoundError` deep inside
`KafkaBus._get_producer()`, swallowed by the (by-contract) fail-soft
`except Exception: logger.exception(...); return False` in
`notifications.request_notification`. Nobody was watching that log.

- **Default backend is now `stapel_core.bus.backends.memory.MemoryBus`**
  (`bus/router.py`, `django/settings.py`) — synchronous, in-process delivery
  to subscribers of *this* process, the correct semantics for a dev box or a
  monolith with no broker. Kafka/NATS are explicit opt-in via
  `STAPEL_BUS_BACKEND` (env var or Django setting) — a deployment that
  already sets it (to anything, including kafka) is unaffected. A
  deployment that relied on the *implicit* kafka default with no explicit
  `STAPEL_BUS_BACKEND` must now set it explicitly to keep cross-process
  delivery — hence the minor bump, not a patch.
- **`stapel_core.bus.publish()` logs loudly on a missing transport
  library**: an `ImportError`/`ModuleNotFoundError` out of the backend
  (kafka/nats without their extra installed) is now logged at `ERROR` with
  topic/event_type and a `pip install 'stapel-core[kafka|nats]'` hint,
  *before* re-raising — callers that fail-soft on publish errors (like
  `request_notification`) still leave a loud trace instead of a silent one.
  Other failures (broker unreachable, etc.) propagate unchanged — no new
  fail-open behavior.
- **New system check** `stapel_core.bus.checks` (tag `stapel_bus`, E001):
  `manage.py check` now fails when the *effective* `STAPEL_BUS_BACKEND`
  names `kafka`/`nats` but the corresponding client library
  (`confluent_kafka`/`nats`) is not importable — caught at boot-smoke time
  instead of the first (silently swallowed) `publish()` in production.
  Registered from `CommonDjangoConfig.ready()`.
- Tests: `tests/test_bus_checks.py` (check clean/error on
  memory/routing/custom/kafka/nats, env-over-setting resolution, loud
  publish-time logging + re-raise, non-`ImportError` failures still
  propagate unchanged). `tests/test_cov_infra_bus.py`'s
  `_resolve_backend_path` default-resolution tests updated for the new
  default.

### Added — admin/API navigation between installed Stapel modules (BACKLOG §37)

A monolith was blind to its own installed apps in the admin: no in-app way
to jump from one Stapel module's admin section to another's, or to a
module's Swagger/schema — only the pre-existing cross-*service* "Services"
menu (STAPEL_SERVICES, admin-suite AS-4), which a monolith doesn't seed
(§37 clarification: "a monolith doesn't seed env — it sees its own apps directly").

- **`stapel_core.django.nav.discover_modules()` / `build_modules()`**: pure
  `INSTALLED_APPS` introspection, no deploy-config seed required. An
  AppConfig counts as a Stapel module when it carries `stapel_module = True`
  (the explicit marker a project's own local app sets to opt in) or its
  `name` follows the published `stapel_*` pip-package convention
  (auto-detected; `stapel_core` itself and its `stapel_core.django.*`
  internal apps are always excluded — framework, not content). Each entry
  resolves `admin_url` (the stock Django per-app admin index,
  `/admin/<app_label>/`, mounts-registry-aware fallback when
  `ROOT_URLCONF` can't resolve it), `swagger_url`/`schema_url` (prefers a
  per-module Swagger mount when the module has one — the §37
  `/<mod>/swagger/` canon — else the deployment-wide Swagger when mounted;
  `None`, never a guessed 404, when introspection is off).
- **Admin index "Apps" block**: `admin/base_site.html` (the existing
  template-override seam) gained an Apps dropdown next to the Services one,
  linking every discovered module to its admin section, Swagger, and
  schema. Wired via the existing `stapel_services` context processor
  (`stapel_modules` key).
- **`GET /nav`** (`stapel_core.django.nav_views.get_nav_urls()`): staff-gated
  JSON aggregate of modules + services + extra nav sections, for a future
  frontend to consume the same navigation the admin renders.
- Tests: `tests/test_nav_modules.py` (marker/pip-convention/core-exclusion
  filtering, 2-3 mocked modules with correct admin/swagger/schema links incl.
  the per-module-vs-deployment-wide-fallback branch, the single-module case,
  the context processor, the `/nav` view incl. staff gating, and a
  template-level render of the Apps block incl. hidden-when-empty).

## [0.10.4] - 2026-07-16

### Added — §55 slice 2: swap declarations, presenter auto-catalog, reference consumer

- **`declare_swap(key, default)` / `declared_swaps()`**
  (`stapel_core.django.swappable`): import-time registration of a swap
  point, independent of the first `get_model()`/`get_presenter()` call
  (which now also records its default lazily, as a backstop). Declarations
  survive `clear_swap_cache()` — they describe the library's swap surface,
  not the host's override state.
- **Presenter auto-catalog** (`stapel_core.django.api.catalog`, spec §4):
  pure introspection of the two registries — declared STAPEL_SWAP points +
  every concrete `Presenter` subclass (now tracked by
  `Presenter.__init_subclass__`; `all_presenters()` exposes it) — rendered
  into `PRESENTERS.MD`: swap key → default class → DTO → field table
  (name/type/source/description, descriptions straight from DAO
  `help_text` / `PresenterField.help_text`). Public API for the scaffold
  wave: `autodiscover_presenters()` (imports `<app>.presenters`
  django-admin-style), `presenter_catalog()`, `render_presenters_md()`,
  `write_presenters_md()`.
- **`manage.py presenter_catalog [--out PRESENTERS.MD] [--check]`**:
  regenerate the catalog, or `--check` it against the file (REL-freshness
  gate — stale/missing catalog exits 1). Core's own `PRESENTERS.MD` is
  committed at the repo root and ships in the wheel (package-data), like
  `CONFIG.MD`.
- **Reference get_presenter() consumer** (`JWTStatusView`): the status
  payload now carries `profile` — the presented user DTO built through
  `get_user_profile_presenter()` (never a direct `UserProfilePresenter`
  import), so `STAPEL_SWAP["USERS_PROFILE_PRESENTER"]` reaches the endpoint
  with no core fork. `null` when anonymous. The flat legacy `user` block is
  unchanged (wire compatibility). `users/presenters.py` now declares its
  swap point at import time (`DEFAULT_PRESENTER` single-sources the dotted
  path) — previously the swap key was invisible until first resolution.
- Docstring updates: the SWAP001 lint is no longer "next wave" — it shipped
  in stapel-tools (`stapel_tools.swap_lint`, part of `stapel-verify`).
- Tests: `tests/test_presenter_catalog.py` (declarations incl.
  first-wins/copy/cache-clear semantics, catalog entries for the users
  pilot, markdown rendering, write, management command incl. `--check`
  fresh/stale/missing, and the status-view `profile` block: presented DTO,
  anonymous `null`, honors STAPEL_SWAP).

## [0.10.3] - 2026-07-16

### Added — `stapel_core.media`: one media interface, two storage paths (images-and-cdn.md §1, §61)

Presenters/serializers call `stapel_core.media.describe(ref)` and get the
immutable render-metadata snapshot (§5: `{mime, bytes, width, height,
aspect, duration_ms, preview_b64, square, variants[]}`) regardless of where
the pixels live. Switching backends is configuration, never a code branch
in the caller.

- **`RenderMetadataProvider` Protocol** + two providers: `pil` (default —
  plain Django `ImageField`/storage, zero infrastructure) and `cdn`
  (delegates to the stapel-cdn service's `cdn.describe` comm Function; the
  recommended production opt-in). Dotted-path escape hatch for custom
  providers.
- **`STAPEL_MEDIA` namespace / `STAPEL_MEDIA_BACKEND`** (flat setting and
  env var honored): `BACKEND` (`"pil"` default), `THUMBNAIL_SIZES`
  (`16/32/64/120`), `PREVIEW_SIZES` (`160/240/480/560/720/1080`),
  `WEBP_QUALITY`, `WATERMARK` (dotted-path PIL engine, off by default),
  `SQUARE_EPSILON`.
- **Reusable ladder core** (`stapel_core.media.variants`) — the tier
  semantics extracted from `stapel_cdn.services` as a library, not a
  copy-paste: pure plan math (`plan_variants`, `scaled_size`, `is_square`,
  `variant_name`) — min-side thumbnails (§3.4), w/h preview branches
  (§3.2), square dedup (§3.3), no upscaling — plus `generate_variants`,
  the PIL engine writing `<stem>__<tier><branch>.webp` siblings through the
  field's own storage. Persisting the returned `VariantMeta` list and
  scheduling (post_save / Celery) are the host's hooks, not this library's
  pipeline. stapel-cdn keeps its pyvips engine over the same semantics.
- **`RenderMetadata` / `VariantMeta` TypedDicts** (`media.types`) — the
  §5 form, hand-mirrored in TypeScript by `@stapel/image`.
- New optional extra: `stapel-core[media]` → `Pillow>=9.0` (only the PIL
  backend needs it; the facade and cdn provider import nothing heavy).

### Changed
- `CdnImageField.get_<name>_url(variant='720', branch=None)` — the URL
  template is unified with stapel-cdn 0.6.0 (images-and-cdn.md §0.1
  finding: this was a second, drifted template). Preview tiers (> 120)
  resolve to their branch file (`{tier}w.webp` default, pass
  `branch="h"` for the height branch); thumbnail tiers stay `{tier}.webp`;
  the base prefix is overridable via `STAPEL_CDN_MEDIA_URL` (default
  `/cdn/media/`). Old call sites keep working and now point at files that
  actually exist under the new cdn semantics.


## [0.10.2] - 2026-07-16

### Added — Projection local mode: colocation semantics (projections-and-composition §1)

One `Projection` declaration now works in two modes; the mode is a property
of the TOPOLOGY at process start, never of business code:

- **`Projection.live_query`** (new field) — the owner's keyed batch Function
  for the local-mode read path. Contract: `{"keys": [<str>, ...]}` →
  `{key: {..fields..}}`.
- **`Projection.force_mode`** (new field) — optional `"local"`/`"remote"`
  override of the auto-detect (e.g. a test exercising the remote path in a
  monolithic dev environment).
- **`resolve_mode(proj)`** — `"local"` when the owning app is installed in
  this process, `"remote"` otherwise. The owner is derived from the first
  `consumes` topic prefix and looked up by *app label* via
  `apps.get_app_config(label)` — NOT `apps.is_installed()`, which compares
  dotted module paths. Relies on the convention "app_label == bus namespace
  prefix" (topology §37).
- **`comm.projections.read(name, keys)`** — the ONE mode-blind accessor
  business code uses instead of querying the `ProjectionModel` directly
  (a direct ORM query silently hard-wires remote mode into the caller).
  remote: one `projection_key__in` query, bookkeeping columns stripped;
  local: one synchronous `call(live_query, {"keys": [...]})`. Identical
  result shape either way: `{key: fields}`, stringified keys.
- **`validate_registry()`** now branches on the resolved mode: a local
  projection is valid without `model` but must declare `live_query`; a
  remote projection must declare `model` (unchanged checks otherwise).
- **`wire_projections()`** skips local-mode projections — no bus
  subscription (the owner applies its own events in-process already; there
  is no table to feed).
- `rebuild`/`drift_check`/`projection_status` remain remote-only (local data
  is live by construction — there is nothing to backfill).

### Added — real `info.version` in OpenAPI (api-versioning plan, step 0)

- `get_spectacular_settings()` gains `package="stapel-<mod>"`: resolves
  `info.version` from the installed distribution's metadata when `version`
  is not passed explicitly. An explicit `version` wins; with neither, the
  historical `"1.0.0"` default is kept (an unknown package logs a warning
  and falls back rather than crashing settings.py). Closes the
  placeholder-version finding: modules passed nothing, so Swagger UI and
  emitted `docs/schema.json` lied about the version.

## [0.10.1] - 2026-07-14

### Fixed — `users_user.avatar` URLField truncation on OAuth signup (500)

- `stapel_core.django.users.User.avatar` widened `URLField` 200→500
  (migration `0007_widen_user_avatar`, expand-only). Django's
  `URLField` defaults `max_length` to 200; real OAuth provider avatar URLs
  (Google/GitHub) routinely exceed that, so the implicit default degraded
  from "wrong length" to `StringDataRightTruncation` on `INSERT` — a 500 on
  signup, not a validation error, since Postgres enforces the column width.
  Belt-and-suspenders companion fix in `stapel-auth` (0.5.5) drops an
  over-long provider avatar rather than crashing.

## [0.10.0] - 2026-07-11

### Added — Projection: event-carried read-models over Action (module-communication §10, Q2)

A fourth comm primitive alongside Action/Function/Task. A cross-domain read
(a catalog listing showing a like count owned by an engagement module) is
served by a **local read-model table** filled from the owner's Action events
— no synchronous call on the read path. The pattern was previously
re-invented per table (idempotency hand-rolled as a unique constraint,
backfill as a one-off script, counters drifting when a bulk `update()`
skipped a `post_save` signal); `Projection` formalises it.

- **`stapel_core.comm.Projection`** — declare `name`, `consumes` (Action
  topic[s]), `model` (a `ProjectionModel` table), `source_key` (payload field
  = row identity), `source_of_truth` (owner Function for rebuild) and an
  optional `sequence_field` ordering token; override `apply(event)` to map an
  event to the upserted fields. Declaring a subclass registers it.
- **`stapel_core.django.projections`** app — the abstract
  **`ProjectionModel`** base (carries `projection_key` unique + `projection_seq`
  + `projection_event_id` + `projection_updated_at`; a concrete read-model
  adds only its projected columns) and the `rebuild_projection` command. No
  migrations of its own (the base is abstract).
- **Generated idempotency + ordering.** The consumer runner upserts a row
  only when the event's position (`sequence_field`, else event timestamp) is
  strictly newer than the row's, so a redelivered duplicate is a no-op and a
  reordered/stale event never overwrites fresher state — idempotency by event
  id + unique source key, no per-table bespoke constraint.
- **Wired through the ordinary action registry** — same in-process
  `on_commit` delivery in a monolith, same bus consumer across services; the
  projection code does not change when modules split.
- **First-class rebuild.** `manage.py rebuild_projection <name>` (or
  `comm.rebuild(name)`) re-derives the whole table from the owner's
  `source_of_truth` Function — batched, all-or-nothing, with progress —
  replacing hand-written backfill scripts; `--check` (`comm.drift_check`)
  compares local vs source row counts without writing; `comm.projection_status`
  reports row count / last sequence / lag.
- **Loud config validation** at app ready (`ProjectionConfigError`): one table
  = one source (no two projections target the same model), the model must
  derive from `ProjectionModel`, required attributes present — a misdeclared
  read-model fails at startup, not on the first stale read.

### Added — Channels JWT auth middleware: WebSocket auth over the same `jwt_provider` (gap G14)

Realtime services no longer hand-roll a WebSocket auth middleware. The HTTP JWT
stack (`middleware.JWTAuthMiddleware` / `authentication.JWTCookieAuthentication`)
now has an ASGI/Channels counterpart that reuses the **same**
`jwt_provider` — same signing config, same token- and user-level blacklists,
same `get_or_create_user_from_jwt` user sync — so a token that authenticates an
HTTP request authenticates a WebSocket identically.

- **`stapel_core.django.jwt.channels`** — `JWTAuthMiddleware` (plain ASGI
  middleware) plus a `JWTAuthMiddlewareStack(inner)` factory for call-site
  symmetry with Channels' `AuthMiddlewareStack`. On success it populates
  `scope["user"]` (the Django user, carrying the transient staff-roles claim)
  and `scope["stapel_claims"]` (the validated token payload) — the WebSocket
  mirror of `request.user`. DB/cache work runs through
  `channels.db.database_sync_to_async`.
- **Two token conventions, documented precedence** (first match wins):
  `Authorization: Bearer <token>` header → `Sec-WebSocket-Protocol` subprotocol
  (both `"bearer.<token>"` and `["bearer", "<token>"]` shapes; schemes
  `authorization`/`bearer`/`access_token`/`jwt`/`token`) → `?token=<jwt>` query
  param. Header first (not logged), query last (lands in access logs).
- **Reject before accept, silently.** A missing/malformed/expired/blacklisted
  token — or any error during validation — closes the handshake with
  application close code **4401** (`CLOSE_CODE_UNAUTHORIZED`, the WebSocket
  analogue of HTTP 401) before `websocket.accept`; the consumer is never
  invoked. Failures log at DEBUG only, so a flood of bad tokens can't spam the
  error log.
- **Optional dependency (`stapel-core[channels]`).** The submodule is never
  imported on a normal HTTP-only Django start (nothing in `stapel_core` /
  `stapel_core.django` imports it); importing it without `channels` installed
  raises a clear `ImportError` pointing at the extra.
- Tests: `tests/test_jwt_channels.py` (27) — token extraction from all three
  channels + precedence, the valid/expired/missing/blacklisted-token and
  banned-user paths, 4401-before-accept, silent (no-error-log) rejection on
  exception, non-websocket pass-through, and the optional-dependency contract
  (not-imported-on-start subprocess check + helpful ImportError when absent).

### Added — Gherkin projection of flows: `.feature` + playwright-bdd step-defs (flow-system.md §3)

The flow is the source, the `.feature` is a projection (wish #3): the
`generate_flow_docs` family gains a Gherkin generator, so one flow now fans
out to SA-doc, flows.json **and** an executable BDD suite — no second source
of truth to drift.

- **`stapel_core.flows.gherkin`** — `render_feature` turns a flow into a
  localized Gherkin `Feature` (one happy-path `Scenario`; positional
  Given/When/Then over the resolved i18n step notes, consecutive keywords
  folded to `And`; non-English languages emit the `# language:` header +
  localized keywords — ru built in, unknown → English). `render_step_defs` /
  `render_fixtures` emit the matching **playwright-bdd** step library
  (first-instance runner decision for all pairs): HTTP steps drive the
  codegen typed client (`@stapel/core` `createStapelClient`, the `stapel`
  world fixture); human/UI steps are honest `TODO(testid)` stubs (the flow
  model carries no testid plan yet — system-design §7.20); action/function/
  task steps are pending side-effect assertions; parametrized/unrouted
  endpoints get explicit pending stubs. Nothing is invented.
- **`manage.py generate_flow_features --out features [--languages] [--llm]`**
  — one self-consistent bundle per project language (`<flow_id>.feature` +
  `steps/flows.steps.ts` + `steps/fixtures.ts`); step-def regexes are the
  resolved notes of that language, so each bundle runs in the project
  language. Byte-stable ⇒ the release-gate drift check (regenerate +
  `git diff --exit-code`) works exactly like the SA-doc trees. i18n
  resolution is the standard chain (catalogs → `translate.resolve` →
  `--llm` DOC_TRANSLATOR with the content-hash cache).
- **`load_flows_json`** — rebuild `(flows, endpoint index)` from an exported
  `flows.json` (inverse of `export_json`): the projection can run from the
  committed machine artifact without booting the producing Django instance;
  never touches the global registry. `write_language_bundle` is the shared
  bundle writer; a parity test proves json-loaded and live-registry flows
  render byte-identical output.
- **Reference artifacts** — `docs/examples/auth-flow-features/`: the three
  stapel-auth flows as committed en+ru bundles generated from a committed
  snapshot (`source/flows.json` + catalogs), gated by
  `tests/test_flow_feature_reference.py` (regenerate-and-diff;
  `STAPEL_REGEN_FLOW_FEATURES=1` to refresh).
- Tests: `tests/test_flow_gherkin.py` (18) + the reference drift gate (2) —
  keyword mapping/dialects, JS-regex escaping, Django→OpenAPI path
  conversion, typed-client step bodies, honest-TODO paths, i18n-resolved
  regexes, bilingual byte-stable bundles, loader round-trip.

### Added — step-up on HIGH admin operations + access audit forwarding (admin-suite AS-6)

Step-up is now part of the standard preset, not opt-in (Q8a): a HIGH-required
admin mutation — `delete` in `@access.standard`, or any operation a model
declares HIGH — needs a *fresh* verification grant on top of the mandate. The
mandate (AS-1) decides whether a role *may* act; step-up decides whether it was
re-proven recently.

- **`stapel_core.access.stepup`** — the policy: `STAPEL_ACCESS["STEP_UP"] =
  {"ENFORCE": True, "LEVELS": ["high"], "SCOPE": "sensitive", "MAX_AGE": 900}`
  (all `no_env`). `ENFORCE` is `True` by default. **Convergence, no auth
  hook**: the grant checked is a `stapel_core.verification` grant — the same
  store stapel-auth's step-up flow and the legacy `/totp/step-up/` bridge write
  to (scope `sensitive`/max_age `900` match on purpose); completing step-up
  anywhere satisfies the admin gate.
- **Enforcement in `StapelModelAdmin`** — `has_{add,change,delete}_permission`
  deny a gated operation without a fresh grant (closing the direct URL, the
  bulk delete action, and inline saves uniformly); the add/change/delete
  *views* return an **educational 403** (core has no web verification flow, so
  this honest 403 telling the user how to obtain the grant is the contract,
  §3.8). MID/LOW operations are never gated; a bare `admin.ModelAdmin` still
  enforces category visibility through the backend but not step-up.
- **Degradation (§3.7)** — step-up self-disables when no verification factor is
  registered (no stapel-auth, no host factor): a grant would be unobtainable,
  so enforcing would brick every HIGH operation. Behavior falls back to the
  AS-1/AS-3 mandate alone; `W005` (`stapel_access`) flags the degraded state.
  Q8a's `ENFORCE=True` only takes effect once the mechanism is present.
- **Audit forwarding (`stapel_core.access.audit`)** — a receiver wired from
  `CommonDjangoConfig.ready()` forwards the two access signals as events:
  `dac_escalation` → `access.dac_escalation`, the new
  `step_up_denied` → `access.step_up_denied`, both to
  `STAPEL_ACCESS["AUDIT_SINK"]` (default the core eventstore, stream
  `AUDIT_STREAM`, gateway-shaped `callable(stream, payload, *, project,
  container)`) and then the optional `NOTIFY` alerting shim. **Best-effort**
  unlike the gateway (whose audit *is* the authorization record): the durable
  record already exists (backend/admin logs) and `dac_escalation` fires inside
  `has_perm`, so a sink failure is logged and swallowed, never raised — a
  telemetry outage must not lock admins out.
- **`access_report`** gains a `step_up` section: enforce/capable/active state,
  scope/max_age/levels, the gated-model × action list, and an *aggregate* of
  how many active staff hold a fresh grant (counts only — no grant material).
- **System checks** — `E004` (malformed `STEP_UP`), `W005` (enforced but
  degraded). No migrations (policy + signals + admin behavior only).
- Tests: `tests/test_access_stepup.py` (31) — policy parse/normalize/reject,
  degradation on/off, HIGH-delete denied without grant / allowed with a fresh
  one, MID/LOW untouched, `ENFORCE=False`, superuser under step-up (A5),
  educational-403 view, `step_up_denied` signal, audit forwarding to sink +
  NOTIFY, sink-failure swallowed, idempotent wiring, report section, checks.

### Fixed — explicit `service_dashboard` flag for `current_dashboard_url` (admin-suite AS-4 follow-up)

The AS-4 review flagged the original `current_dashboard_url` heuristic
("first admissible `dashboards`/`tools` link under the current `URL_PREFIX`")
as too implicit for a mechanism contract — it guesses ownership from URL
shape instead of being told.

- `register_nav_link(..., service_dashboard: bool = False)` — a module now
  declares explicitly that a link *is* its service's dashboard; the
  `STAPEL_ADMIN["NAV_LINKS"]` overlay accepts the same key (full add or
  partial patch).
- `current_dashboard_url()` selects the first admissible flagged link, in
  registry order, when one exists. **Backward-compatible fallback**: with no
  flagged link, it falls back to the original prefix-matching heuristic
  unchanged — existing registrations (pre-flag) keep working.
- **`stapel_core.nav.W003`** (tag `stapel_nav`) warns — does not block — when
  more than one link carries `service_dashboard=True`; the first one in
  registry order still wins, matching the resolution `current_dashboard_url`
  already applies.
- Tests: `tests/test_nav.py` (flag wins over the heuristic and over a
  matching prefix, flag ignores the current prefix, admissibility gating,
  settings-overlay flag, fallback preserved with no flag, first-of-two
  ordering, duplicate-flag warning). MODULE.md navigation section documents
  the selection order.

### Added / Changed — cross-service navigation registries (admin-suite AS-4)

Service navigation is no longer hardcoded in the framework — the legacy
`STAPEL_SERVICES` list in `core/config.py` and the Tools/Monitoring/dashboard
links baked into `admin/base_site.html` + the Swagger inject were policy in a
mechanism (§2.1). Both move to deploy-config:

- **`STAPEL_SERVICES` — deploy-config env-JSON** (§2.2), read by
  `stapel_core.django.nav.get_services`:
  `STAPEL_SERVICES='[{"name":"Auth","prefix":"auth"},{"name":"Billing","prefix":"billing"}]'`
  (a Django-setting list of the same shape also works). Written by the project
  generators (`stapel-create-project` seeds it, `stapel-new-service` appends a
  row — the same discipline as `STAPEL_BUS_ROUTES`), never by the framework. A
  monolith leaves it unset: a single implicit service is derived from
  `URL_PREFIX` and the "All Services" section collapses. **The hardcoded
  `stapel_core.core.config.STAPEL_SERVICES` list is removed** — consumers read
  the registry.
- **`STAPEL_ADMIN["NAV_LINKS"]` — two-channel merge-registry** (§2.3) for the
  Tools/Monitoring/Dashboards sections: a module registers its own dashboard in
  `AppConfig.ready()` via `register_nav_link(key, section=..., title=...,
  url=..., requires="staff", external=False)` (channel 1, re-exported from
  `stapel_core.django.admin`); the project adds/patches/removes via the setting
  (channel 2 — a partial dict patches a code link, a full dict adds one, `None`
  removes). Sections (`tools`, `monitoring`, `dashboards`) are fixed by the
  mechanism; contents are policy. The framework ships **no** monitoring links —
  those are three lines of the project's deploy config.
- **Two render gates**: every link is filtered by the viewer's admissibility
  (`requires` — staff / superuser / an AS-1 clearance level; the target's own
  perimeter — nginx `auth_request`, `IsStaffUserForSwagger` — still guards the
  destination), and the **Swagger links respect the introspection env-gate** —
  they render only when this deployment actually mounts the schema
  (`get_dev_urls` mounts `/swagger/` only for `DJANGO_ENV in {local, dev}`,
  detected by reversing `swagger-ui`).
- **Surfaces**: the admin `base_site.html` and the Swagger UI inject
  (`CustomSpectacularSwaggerView`) both render from the registries via the
  `stapel_services` context processor / `nav_sections` — the hardcoded sections
  are gone.
- **System checks** (tag `stapel_nav`): `E001` malformed `STAPEL_SERVICES`
  env-JSON, `E002` malformed `STAPEL_ADMIN["NAV_LINKS"]` overlay — the render
  layer fails soft (never 500s the admin), so the check is what surfaces the
  misconfiguration at deploy time.
- Tests: `tests/test_nav.py` (registry/merge/env-JSON, render gating for
  staff/superuser/non-staff/anonymous, empty registry, monolith vs
  microservices, malformed-config checks).

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
**upgrade-only** to **REPLACE from the claim** (c.3).

- **`staff_roles` field on `AbstractStapelUser`** (`JSONField(default=list)`,
  migration `users/0006`): the shadow copy of the `staff_roles` JWT claim.
  Auth is the single writer (A2); consumers only mirror it.
- **`serialize_user_to_jwt_data` emits the `staff_roles` claim** on
  staff/superuser tokens only, sorted for a stable ordering. Present-but-empty
  is authoritative "zero roles"; absence means the model has no field (pre-AS-2)
  and consumers must not touch local state.
- **`get_or_create_user_from_jwt` sync-down REPLACE (c.3, breaking on the
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

### Added — outbox atomicity as a seam (docs/module-extension-gaps.md §"Systemic pattern")

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
