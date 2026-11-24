import { Counter, defaultRegistry } from './registry';
import { labelValue } from './labels';

/**
 * Gateway spend (#346): the estimated cost of every call the gateway reports
 * to the collector, in USD, as the call's own telemetry event prices it
 * (provider-reported cost, else the pricing table; 0 for a cache hit, whose
 * avoided cost is ps_gateway_cache_saved_cost_usd_total). Kept apart from
 * `./index` so ps-telemetry can count without importing the middleware.
 *
 * `team` is the caller's team as declared in `X-PS-Team` (#347; the
 * collector resolves it against the tenant's teams, the gateway cannot), or
 * `none`. The label is bounded: values go through `labelValue`, only the
 * first `MAX_TEAM_LABELS` distinct teams a process sees get their own value
 * (later ones count as `other`), and the registry's series cap still
 * applies.
 */
export const costTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_cost_usd_total',
    'Estimated cost of gateway calls (USD), as reported on their telemetry events.',
    ['provider', 'model', 'team']
  )
);

/** Distinct `team` label values per process before new ones become `other`. */
export const MAX_TEAM_LABELS = 100;
const seenTeams = new Set<string>();

/** The `team` label of a declared team: bounded, see above. */
export function teamLabel(raw: unknown): string {
  const value = labelValue(raw);
  if (value === 'none' || value === 'other' || seenTeams.has(value)) {
    return value;
  }
  if (seenTeams.size >= MAX_TEAM_LABELS) return 'other';
  seenTeams.add(value);
  return value;
}

/** Tests only: forget the teams seen so far. */
export function resetTeamLabels(): void {
  seenTeams.clear();
}

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
        team: teamLabel(event.team),
      },
      cost
    );
  } catch {
    // Metrics must never affect telemetry.
  }
}
