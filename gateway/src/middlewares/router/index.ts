/**
 * Cost-aware routing — entry point.
 *
 * Resolution precedence (see PS_README.md):
 *   explicitGroup (X-PS-Route)  >  quality / maxCost (hints)  >  transparent default.
 */
import {
  cheapestUnderBudget,
  defaultPolicy,
  estimateInputTokens,
  estimateTierCost,
  pickCheapestIn,
} from './policy';
import { HeuristicStrategy } from './heuristic';
import {
  ParsedRouteHints,
  Quality,
  RouteDecision,
  RoutePolicy,
  RouteRequest,
  RouterStrategy,
} from './types';

const QUALITIES: Quality[] = ['draft', 'balanced', 'critical'];

/** Case-insensitive lookup across a header bag. */
function header(
  headers: Record<string, any>,
  name: string
): string | undefined {
  const target = name.toLowerCase();
  for (const key of Object.keys(headers || {})) {
    if (key.toLowerCase() === target) {
      const v = headers[key];
      return v == null ? undefined : String(v);
    }
  }
  return undefined;
}

/** Parse the inbound X-PS-* routing headers into structured hints. */
export function parseRouteHeaders(
  headers: Record<string, any>
): ParsedRouteHints {
  const hints: ParsedRouteHints = { cacheOff: false };

  const quality = header(headers, 'x-ps-quality');
  if (quality && (QUALITIES as string[]).includes(quality)) {
    hints.quality = quality as Quality;
  }

  const maxCost = header(headers, 'x-ps-max-cost');
  if (maxCost != null) {
    const parsed = Number(maxCost);
    if (!Number.isNaN(parsed)) hints.maxCost = parsed;
  }

  const route = header(headers, 'x-ps-route');
  if (route) hints.explicitGroup = route;

  if ((header(headers, 'x-ps-cache') || '').toLowerCase() === 'off') {
    hints.cacheOff = true;
  }

  return hints;
}

/** Cheapest tier in one group, clamping down across groups when maxCost busts. */
function resolveInGroup(
  policy: RoutePolicy,
  group: string,
  req: RouteRequest,
  reasonPrefix: string
): RouteDecision {
  const tiers = policy.groups[group];
  if (!tiers || tiers.length === 0) {
    // Unknown / empty group → transparent default group.
    const fb = pickCheapestIn(policy.groups[policy.defaultGroup] || [], req);
    return {
      model: fb?.tier.model ?? req.requestedModel,
      group: policy.defaultGroup,
      reason: `${reasonPrefix}:unknown-group-default`,
      estCost: fb?.estCost ?? 0,
    };
  }

  const picked = pickCheapestIn(tiers, req)!;
  if (req.maxCost != null && picked.estCost > req.maxCost) {
    const clamped = cheapestUnderBudget(policy, req.maxCost, req);
    if (clamped) {
      return {
        model: clamped.tier.model,
        group: clamped.tier.group,
        reason: clamped.underBudget
          ? `${reasonPrefix}+budget-clamp`
          : `${reasonPrefix}+budget-unsatisfiable`,
        estCost: clamped.estCost,
      };
    }
  }

  return {
    model: picked.tier.model,
    group,
    reason: reasonPrefix,
    estCost: picked.estCost,
  };
}

/**
 * Resolve a concrete model for a request, honoring precedence. `strategy` is
 * only consulted on the transparent path (no explicit group, no quality hint).
 */
export async function resolveRoute(
  req: RouteRequest,
  policy: RoutePolicy,
  strategy: RouterStrategy
): Promise<RouteDecision> {
  // 1. Explicit override — pins the group, skips inference.
  if (req.explicitGroup) {
    return resolveInGroup(policy, req.explicitGroup, req, 'override');
  }

  // 2. Quality hint — maps to a group, strategy not consulted.
  if (req.quality) {
    const group = policy.qualityToGroup[req.quality] ?? policy.defaultGroup;
    return resolveInGroup(policy, group, req, `quality:${req.quality}`);
  }

  // 3. Transparent — the strategy infers difficulty and respects maxCost.
  return strategy.route(req, policy);
}

/** Build a RouteRequest from a raw request body + parsed hints. */
export function buildRouteRequest(
  body: any,
  hints: ParsedRouteHints
): RouteRequest {
  const messages = body?.messages || [];
  return {
    messages,
    requestedModel: body?.model || 'auto',
    approxInputTokens: estimateInputTokens(messages),
    maxOutputTokens: body?.max_tokens ?? body?.max_completion_tokens,
    quality: hints.quality,
    maxCost: hints.maxCost,
    explicitGroup: hints.explicitGroup,
  };
}

export {
  defaultPolicy,
  estimateInputTokens,
  estimateTierCost,
  HeuristicStrategy,
};
export * from './types';
