import { Counter, defaultRegistry, Gauge } from '../psMetrics/registry';

/**
 * Budget metrics (#28). The `budget` label is the operator's budget id, so
 * cardinality is bounded by the config. Spend is only exported for budgets
 * with a concrete `subject` (or global): a per-value budget (`subject: "*"`)
 * would need the owner, business unit or use case as a label.
 */
export const budgetSpendUsd = defaultRegistry.register(
  new Gauge(
    'ps_gateway_budget_spend_usd',
    'Spend counted against a budget in its current window (settled plus in-flight reservations), as last seen by this replica, in USD.',
    ['budget', 'window']
  )
);
export const budgetLimitUsd = defaultRegistry.register(
  new Gauge('ps_gateway_budget_limit_usd', 'Configured budget limit in USD.', [
    'budget',
    'window',
  ])
);
export const budgetDecisionsTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_budget_decisions_total',
    'Budget checks by budget, state (ok, warn, unknown, exceeded) and the action taken on the call (none, downgraded, block).',
    ['budget', 'state', 'action']
  )
);
export const budgetStoreErrorsTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_budget_store_errors_total',
    'Budget store operations that failed, by store (redis, memory) and operation (read, reserve, settle).',
    ['store', 'op']
  )
);
export const budgetSpendTokens = defaultRegistry.register(
  new Gauge(
    'ps_gateway_budget_spend_tokens',
    'Tokens counted against a token budget in its current window (settled plus in-flight reservations), as last seen by this replica.',
    ['budget', 'window']
  )
);
export const budgetLimitTokens = defaultRegistry.register(
  new Gauge(
    'ps_gateway_budget_limit_tokens',
    'Configured token budget limit.',
    ['budget', 'window']
  )
);
/**
 * A settle that moved a counter across a budget's warn share
 * (threshold="warn") or its limit (threshold="exceeded"). The write is
 * atomic, so with a shared Redis exactly one replica counts each crossing.
 */
export const budgetThresholdCrossingsTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_budget_threshold_crossings_total',
    "Budget counters that crossed a budget's warn share or its limit, by budget, window and threshold (warn, exceeded).",
    ['budget', 'window', 'threshold']
  )
);
