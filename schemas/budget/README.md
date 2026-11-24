# Spend budgets (budget.v1)

One budget document and one counter format, shared by every enforcement
point: the SDK (`ShieldsRouter`, the plain clients, #29), the gateway's shared
store (#28) and the central policy's `budgets` section (#340), whose
`budgets` list holds the same budget objects. An enforcement point that follows this page reads and writes
the same counters as the others, so a Redis shared by the SDK and the
gateway gives one spend total per subject.

- `budget.v1.schema.json`: the document.
- `vectors/keys.json`: counter keys and TTLs every implementation must produce.
- `vectors/decisions.json`: the decision (state, action, ceiling) every
  implementation must reach for given spend.

## Document

```json
{
  "schema_version": "budget.v1",
  "namespace": "acme",
  "on_store_error": "open",
  "budgets": [
    { "id": "support-daily", "scope": "use_case", "subject": "support-bot",
      "window": "day", "limit_usd": 10, "mode": "downgrade", "warn_at": 0.8 }
  ]
}
```

| Field | Meaning |
|---|---|
| `scope` | `business_unit` (`X-PS-Business-Unit`), `use_case` (`X-PS-Use-Case`), `owner` (`X-PS-Owner`), `team` (`X-PS-Team`; SDK `ps_metadata.team`), `project` (`X-PS-Project`; SDK `ps_metadata.project`), `user` (a hashed user id: the gateway's verified OIDC subject hash, the SDK's `hash_user_id(ps_metadata.user_id)`; never the raw id), `service` (`X-PS-Service`; SDK `calling_service`), `model` (the model the caller requested), or `global` (every call, no `subject`; with one `namespace` per tenant, the tenant's budget). |
| `subject` | The attribute value, compared exactly. `*` gives every value its own counter. A call without the attribute never matches a non-global budget. |
| `window` | `day` (00:00 UTC), `week` (ISO week, Monday 00:00 UTC), `month` (calendar month, UTC). |
| `limit_usd` / `limit_tokens` | Exactly one. `limit_tokens` counts input plus output tokens as the provider reports them; a token budget's mode is `warn` or `block`. |
| `mode` | `warn`, `downgrade` (USD budgets) or `block` (below). |
| `warn_at` | Fraction of `limit_usd` from which the state is `warn` (default 0.8). |
| `downgrade_model` | Only for plain SDK clients, which do not route: the model used once a downgrade budget is exhausted, when the client opts in with `allow_budget_downgrade=True` (the switch is not checked against any data policy). Routers ignore it. |
| `on_store_error` | `open` (default) or `closed` (below). |

## Counter keys

```
ps:budget:v1:{namespace}:{scope}:{subject}:{window}:{period}
ps:budget:v1:{namespace}:{scope}:{subject}:{window}:{period}:tokens
```

- `ps:budget:v1` is fixed; `v1` is the key format's version.
- `namespace`: the document's `namespace` (default `default`). Use one per
  tenant in a shared Redis.
- `scope`: one of the scopes above.
- `:tokens` is appended to a token budget's counter; a USD counter has no
  suffix (see the `token-counter-suffix` vector).
- `subject`: the call's attribute value, or `_` for `global`.
- `window`: `day`, `week` or `month`.
- `period`: the UTC window containing the call: `YYYY-MM-DD`, `YYYY-Www`
  (ISO week-numbering year and week, zero-padded: `2026-W05`), or `YYYY-MM`.
- `namespace` and `subject` are percent-encoded: every UTF-8 byte outside
  `[A-Za-z0-9._~-]` becomes `%XX` with uppercase hex. This is stricter than
  JavaScript's `encodeURIComponent`, which leaves `!'()*` alone; see the
  `subject-percent-encoded` vector.

The key holds the subject's spend in USD (or tokens) for that window, as a
Redis string written with `INCRBYFLOAT`. It does not name a budget, so budgets with the
same scope, subject and window (in any document, or with different limits)
share one counter: the counter is the spend, a budget is a limit on it. A
call's cost is added once to each distinct key among its matching budgets.

**TTL.** Set on a counter's first write in its window (when `TTL` is
negative), never extended: seconds to the window's end, rounded up, plus
86,400 seconds of grace.

**Atomic increment.** One script per write, so concurrent writers never lose
an update and a counter never exists without a TTL:

```lua
-- KEYS[1] counter, ARGV[1] increment (USD), ARGV[2] TTL seconds
local v = redis.call('INCRBYFLOAT', KEYS[1], ARGV[1])
if redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return v
```

Reads are `MGET` of the matching keys; a missing key is 0.

**Reservations.** Enforcement points that reserve (the gateway and the SDK's
built-in stores) hold each call's pre-call estimate in a sorted set at
`{<counter key>}:res` (the braces make the counter key the Redis Cluster hash
tag), one member `<call id>|<amount>` scored by its expiry in epoch ms. A
read is the counter plus the live reservations (expired members dropped). A
block budget's reservation is refused, atomically, when counter plus
reservations already reach its limit. After the call one script drops the
member and adds the actual amount with the increment above, and returns the
counter's new value; a failed call drops it and adds nothing. The scripts are
in `gateway/src/middlewares/psBudget/store.ts` and
`packages/sdk/prompt_shields/budgets.py`.

## Decision

Before a call, read the counter of every matching budget.

- **State** of a budget: `exceeded` when spend >= `limit_usd`, `warn` when
  spend >= `warn_at` x `limit_usd`, else `ok`; `unknown` when the store
  could not be read. The call's `budget_state` is the worst over its
  budgets (`ok` < `warn` < `unknown` < `exceeded`) and `budget_id` the first
  budget, in document order, in that state.
- **block**: an exhausted block budget refuses the call before anything is
  sent (SDK: `BudgetExceeded`).
- **downgrade**: each matching downgrade budget's remaining amount
  (`limit_usd - spend`, at least 0) is a cost ceiling; the call's ceiling is
  the least of them. A router applies it as the route policy's `max_cost`
  clamp over the allowed candidates (so the data policy still applies), and
  serves the cheapest allowed candidate when nothing fits. The route reason
  gets `+budget-downgrade` when the budget changed the model. A plain
  client switches to `downgrade_model` once the budget is exhausted, but
  only when it was built with `allow_budget_downgrade=True`: a plain client
  runs no data policy, so the switch is not policy-checked (and on a
  multi-vendor OpenAI-compatible endpoint the model id can pick another
  provider). Without the opt-in it sends the call unchanged and logs a
  warning, like a warn budget.
- **warn**: never changes the call.
- **Store errors.** `on_store_error: open` (default): budgets that could
  not be read are `unknown` and do not change the call. `closed`: they act
  as exhausted (block refuses, downgrade gets ceiling 0). The default is
  `open` so that a store outage does not take every LLM call down with it;
  data-policy enforcement is unaffected and stays fail-closed. Choose
  `closed` when overspend is worse than an outage.

Before sending, reserve the call's estimate on each distinct key (USD on USD
keys, tokens on token keys); a refused block reservation refuses the call
like an exhausted budget. After the call, settle each key with the call's
actual cost or tokens (the estimate when unknown). A failed write never
fails the call. A limit can be overshot by the difference between the
admitted calls' estimates and their actual usage (or, on a store without
reservations, by the calls running when it is reached).

**Threshold notifications.** When a write moves a counter from `before` to
`after` across `warn_at` x limit or the limit (`before < threshold <=
after`), the writer reports it once: the gateway as
`ps_gateway_budget_threshold_crossings_total`, a `ps.budget` log line and an
optional signed webhook; the SDK through `BudgetEnforcer(on_threshold=...)`
and a log warning. The write is atomic, so only one writer sees each
crossing.

## Telemetry

Every event of a call with at least one matching budget carries:

| Field | Values |
|---|---|
| `budget_state` | `ok`, `warn`, `unknown`, `exceeded` |
| `budget_id` | The budget that set the state |
| `budget_action` | `none` or `downgraded`; `blocked` on a refused call's event |

The gateway sends one event for a call it refuses (HTTP 429
`budget_exceeded` with `budget_state: exceeded`, or 503
`budget_store_unavailable` with `budget_state: unknown`), with
`budget_action: blocked`, the budget's id, the call's attribution, the
requested model and its call id. No provider was called, so the event has no
usage, cost or latency. The SDK raises `BudgetExceeded` before the call and
sends no event for it.

## SDK stores

`InProcessBudgetStore` (the default) keeps counters in the process's memory:
**per process**, so N workers or replicas can together spend up to N times a
limit, and a restart resets them. `RedisBudgetStore` (`pip install
'prompt-shields[optimizer-redis]'`) implements the format above and shares
counters across processes and with the gateway.
