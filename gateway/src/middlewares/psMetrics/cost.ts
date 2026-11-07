import { Counter, defaultRegistry } from './registry';
import { labelValue } from './labels';

/**
 * Gateway spend (#346): the estimated cost of every call the gateway reports
 * to the collector, in USD, as the call's own telemetry event prices it
 * (provider-reported cost, else the pricing table; 0 for a cache hit, whose
 * avoided cost is ps_gateway_cache_saved_cost_usd_total). Kept apart from
 * `./index` so ps-telemetry can count without importing the middleware.
 *
 * `team` is the caller's team. The gateway has no teams yet (principals and
 * virtual keys, #338), so it is `none` until they land. Label values go
 * through `labelValue` and the registry's series cap.
 */
export const costTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_cost_usd_total',
    'Estimated cost of gateway calls (USD), as reported on their telemetry events.',
    ['provider', 'model', 'team']
  )
);

/** The cost-relevant fields of a gateway telemetry event. */
export interface CostEvent {
  vendor?: string;
  model?: string;
  served_provider?: string;
  served_model?: string;
  cost?: number;
  team?: string;
}

/** Count one event's cost; best-effort, never throws. */
export function recordCost(event: CostEvent): void {
  try {
    const cost = event.cost;
    if (typeof cost !== 'number' || !Number.isFinite(cost) || cost <= 0) return;
    costTotal.inc(
      {
        provider: labelValue(event.served_provider || event.vendor, 'unknown'),
        model: labelValue(event.served_model || event.model, 'unknown'),
        team: labelValue(event.team),
      },
      cost
    );
  } catch {
    // Metrics must never affect telemetry.
  }
}
