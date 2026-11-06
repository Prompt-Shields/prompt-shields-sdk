# Spend budgets (budget.v1)

One budget document and one counter format, shared by every enforcement
point: the SDK (`ShieldsRouter`, the plain clients) and the gateway's shared
store (#28). An enforcement point that follows this page reads and writes
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
| `scope` | `business_unit` (`X-PS-Business-Unit`), `use_case` (`X-PS-Use-Case`), `owner` (`X-PS-Owner`; team budgets), or `global` (every call, no `subject`). |
| `subject` | The attribute value, compared exactly. `*` gives every value its own counter. A call without the attribute never matches a non-global budget. |
| `window` | `day` (00:00 UTC), `week` (ISO week, Monday 00:00 UTC), `month` (calendar month, UTC). |
| `mode` | `warn`, `downgrade` or `block` (below). |
| `warn_at` | Fraction of `limit_usd` from which the state is `warn` (default 0.8). |
| `downgrade_model` | Only for plain SDK clients, which do not route: the model used once a downgrade budget is exhausted, when the client opts in with `allow_budget_downgrade=True` (the switch is not checked against any data policy). Routers ignore it. |
| `on_store_error` | `open` (default) or `closed` (below). |

## Counter keys

```
ps:budget:v1:{namespace}:{scope}:{subject}:{window}:{period}
```

- `ps:budget:v1` is fixed; `v1` is the key format's version.
- `namespace`: the document's `namespace` (default `default`). Use one per
  tenant in a shared Redis.
- `scope`: `global`, `business_unit`, `use_case` or `owner`.
- `subject`: the call's attribute value, or `_` for `global`.
- `window`: `day`, `week` or `month`.
- `period`: the UTC window containing the call: `YYYY-MM-DD`, `YYYY-Www`
  (ISO week-numbering year and week, zero-padded: `2026-W05`), or `YYYY-MM`.
- `namespace` and `subject` are percent-encoded: every UTF-8 byte outside
  `[A-Za-z0-9._~-]` becomes `%XX` with uppercase hex. This is stricter than
  JavaScript's `encodeURIComponent`, which leaves `!'()*` alone; see the
  `subject-percent-encoded` vector.

The key holds the subject's spend in USD for that window, as a Redis string
written with `INCRBYFLOAT`. It does not name a budget, so budgets with the
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

After the call, add its cost to each distinct key. A failed write never
fails the call. Calls in flight at once do not see each other's cost, so a
limit can be overshot by the calls running when it is reached.

## Telemetry

Every event of a call with at least one matching budget carries:

| Field | Values |
|---|---|
| `budget_state` | `ok`, `warn`, `unknown`, `exceeded` |
| `budget_id` | The budget that set the state |
| `budget_action` | `none` or `downgraded` |

A blocked call sends nothing, so it has no event.

## SDK stores

`InProcessBudgetStore` (the default) keeps counters in the process's memory:
**per process**, so N workers or replicas can together spend up to N times a
limit, and a restart resets them. `RedisBudgetStore` (`pip install
'prompt-shields[optimizer-redis]'`) implements the format above and shares
counters across processes and with the gateway.
