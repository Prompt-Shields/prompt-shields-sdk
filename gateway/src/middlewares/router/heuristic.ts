import { cheapestUnderBudget, pickCheapestIn } from './policy';
import {
  RouteDecision,
  RoutePolicy,
  RouteRequest,
  RouterStrategy,
} from './types';

/** Input-token count above which a request is treated as inherently complex. */
const COMPLEX_TOKEN_THRESHOLD = 2000;

function hasCodeFence(messages: any[]): boolean {
  return messages.some(
    (m) => typeof m?.content === 'string' && m.content.includes('```')
  );
}

function demandsJsonSchema(messages: any[]): boolean {
  return messages.some(
    (m) =>
      typeof m?.content === 'string' &&
      /json schema|response_format|strict json/i.test(m.content)
  );
}

/**
 * Cheap, explainable, <1ms difficulty router. Simple prompts go to the cheap
 * group; long / code / schema-shaped prompts go to frontier. A max_cost ceiling
 * clamps the choice down across groups when the natural tier busts the budget.
 *
 * This is the ship-first strategy. A learned strategy (embed -> classifier,
 * RouteLLM-style) can replace it behind the same RouterStrategy interface,
 * trained on the gateway's own telemetry, with no handler changes.
 */
export class HeuristicStrategy implements RouterStrategy {
  async route(req: RouteRequest, policy: RoutePolicy): Promise<RouteDecision> {
    const complex =
      req.approxInputTokens > COMPLEX_TOKEN_THRESHOLD ||
      hasCodeFence(req.messages) ||
      demandsJsonSchema(req.messages);

    const group = complex ? 'frontier' : 'cheap';
    const picked = pickCheapestIn(policy.groups[group] || [], req);

    if (!picked) {
      // Misconfigured group — fall back to the policy default group.
      const fallback = pickCheapestIn(
        policy.groups[policy.defaultGroup] || [],
        req
      );
      return {
        model: fallback?.tier.model ?? req.requestedModel,
        group: policy.defaultGroup,
        reason: 'heuristic:empty-group-fallback',
        estCost: fallback?.estCost ?? 0,
      };
    }

    // Budget clamp: if the natural tier exceeds max_cost, downgrade across groups.
    if (req.maxCost != null && picked.estCost > req.maxCost) {
      const clamped = cheapestUnderBudget(policy, req.maxCost, req);
      if (clamped) {
        return {
          model: clamped.tier.model,
          group: clamped.tier.group,
          reason: clamped.underBudget
            ? `heuristic:${complex ? 'complex' : 'simple'}+budget-clamp`
            : `heuristic:budget-unsatisfiable-cheapest`,
          estCost: clamped.estCost,
        };
      }
    }

    return {
      model: picked.tier.model,
      group,
      reason: `heuristic:${complex ? 'complex' : 'simple'}`,
      estCost: picked.estCost,
    };
  }
}
