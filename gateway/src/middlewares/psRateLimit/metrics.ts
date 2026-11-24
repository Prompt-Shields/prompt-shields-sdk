import { Counter, defaultRegistry } from '../psMetrics/registry';

/**
 * Rate-limit metrics (#344). `rule` is the policy's rule id and `scope` its
 * scope, so cardinality is bounded by the policy (at most 200 rules per
 * section). Refusals also count in ps_gateway_rate_limited_total
 * {source="gateway"} with every other 429 the gateway answers.
 */
export const rateLimitHitsTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_rate_limit_hits_total',
    'Calls refused by a rate-limit policy rule, by scope, rule id and limit (rpm, tpm, tpd, concurrency).',
    ['scope', 'rule', 'limit']
  )
);
export const rateLimitStoreErrorsTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_rate_limit_store_errors_total',
    'Rate-limit store operations that failed, by operation (acquire, settle) and what was done (memory_fallback, refused, skipped).',
    ['op', 'outcome']
  )
);
