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
