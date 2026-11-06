/**
 * The cheaper model a `downgrade` budget moves a call to: the route policy's
 * max_cost clamp (schemas/budget/README.md, "Decision"), with the budget's
 * remaining amount as the ceiling, over the candidates the router itself
 * allows: tiers that meet the data policy rule for the call's sensitivity
 * level and, when the use case has an `evalSet`, its `eligibleModels`. It
 * never falls back to a non-compliant model: with no compliant cheaper
 * candidate it returns null and the call keeps its model.
 */
import { tierComplies, useCaseBar } from '../router/constraints';
import { DEFAULT_OUTPUT_TOKENS, estimateTierCost } from '../router/policy';
import { DataPolicyRule, ModelTier, RoutePolicy } from '../router/types';

export interface DowngradeRequest {
  approxInputTokens: number;
  maxOutputTokens?: number;
  useCase?: string;
}

export interface DowngradePick {
  tier: ModelTier;
  estCost: number;
}

/**
 * Among allowed tiers strictly cheaper than `currentEst`: the most capable
 * (highest estimate) at or under `ceiling`, else the cheapest; else null.
 */
export function pickDowngrade(
  policy: RoutePolicy,
  req: DowngradeRequest,
  rule: DataPolicyRule | undefined,
  currentEst: number,
  ceiling: number
): DowngradePick | null {
  const bar = useCaseBar(policy, req.useCase);
  const eligible = bar?.evalSet ? new Set(bar.eligibleModels || []) : null;
  const out = req.maxOutputTokens ?? DEFAULT_OUTPUT_TOKENS;

  let fits: DowngradePick | null = null;
  let cheapest: DowngradePick | null = null;
  for (const group of Object.keys(policy.groups)) {
    for (const tier of policy.groups[group]) {
      if (!tierComplies(tier, rule)) continue;
      if (eligible && !eligible.has(tier.model)) continue;
      const estCost = estimateTierCost(tier, req.approxInputTokens, out);
      if (!(estCost < currentEst)) continue;
      if (!cheapest || estCost < cheapest.estCost) cheapest = { tier, estCost };
      if (estCost <= ceiling && (!fits || estCost > fits.estCost))
        fits = { tier, estCost };
    }
  }
  return fits ?? cheapest;
}
