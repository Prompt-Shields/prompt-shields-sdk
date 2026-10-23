/**
 * Cost-aware routing — entry point.
 *
 * Resolution precedence (see PS_README.md):
 *   data policy  >  explicitGroup (X-PS-Route)  >  quality / maxCost (hints)
 *   >  use-case quality bar  >  transparent default.
 *
 * The data policy and the quality bar only apply when the policy has a
 * `sensitivity`, `dataPolicy` or `useCases` section (resolvePolicyRoute).
 */
import {
  classifySensitivity,
  dataPolicyRule,
  groupRank,
  hasPolicyStages,
  PolicyViolation,
  tierComplies,
  useCaseBar,
} from './constraints';
import {
  cheapestUnderBudget,
  defaultPolicy,
  estimateInputTokens,
  estimateTierCost,
  groupTiers,
  pickCheapestIn,
} from './policy';
import { HeuristicStrategy, isComplex } from './heuristic';
import {
  ModelTier,
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

  const classification = header(headers, 'x-ps-data-classification');
  if (classification) hints.dataClassification = classification;

  const useCase = header(headers, 'x-ps-use-case');
  if (useCase) hints.useCase = useCase;

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
  const tiers = groupTiers(policy, group);
  if (!tiers || tiers.length === 0) {
    // Unknown / empty group → transparent default group.
    const fb = pickCheapestIn(
      groupTiers(policy, policy.defaultGroup) || [],
      req
    );
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
  if (hasPolicyStages(policy)) {
    return resolvePolicyRoute(req, policy, strategy);
  }

  // 1. Explicit override — pins the group, skips inference.
  if (req.explicitGroup) {
    return resolveInGroup(policy, req.explicitGroup, req, 'override');
  }

  // 2. Quality hint — maps to a group, strategy not consulted.
  if (req.quality) {
    const mapped = Object.hasOwn(policy.qualityToGroup, req.quality)
      ? policy.qualityToGroup[req.quality]
      : undefined;
    const group = mapped ?? policy.defaultGroup;
    return resolveInGroup(policy, group, req, `quality:${req.quality}`);
  }

  // 3. Transparent — the strategy infers difficulty and respects maxCost.
  return strategy.route(req, policy);
}

/**
 * Routing under the whole policy. A port of `resolve_policy_route`
 * (packages/sdk/prompt_shields/route_policy.py); both run the golden vectors.
 *
 * 1. classifySensitivity gives the effective level (#18).
 * 2. Only candidates that meet the level's data policy rule are kept, in
 *    every group and in the budget clamp (#19).
 * 3. The group comes from the explicit group, else the quality hint, else
 *    the strategy; an unknown or empty group (as written in the policy,
 *    before filtering) falls back to defaultGroup.
 * 4. Unless the group was explicit, the use case's bar applies: minGroup
 *    raises the group (by groupOrder) and an evalSet limits candidates to
 *    eligibleModels, trying the next groups up when none is left (#23).
 * 5. Cheapest remaining candidate, then the maxCost clamp over all allowed
 *    candidates.
 *
 * Throws PolicyViolation when no allowed candidate is left: it never falls
 * back to a non-compliant model.
 */
export async function resolvePolicyRoute(
  req: RouteRequest,
  policy: RoutePolicy,
  strategy: RouterStrategy
): Promise<RouteDecision> {
  const { level, source } = classifySensitivity(
    policy,
    req.dataClassification,
    req.piiDetected
  );
  const rule = dataPolicyRule(policy, level);

  const bar = req.explicitGroup ? undefined : useCaseBar(policy, req.useCase);
  const eligible = bar?.evalSet ? new Set(bar.eligibleModels || []) : undefined;
  const allow = (tier: ModelTier) =>
    (!eligible || eligible.has(tier.model)) && tierComplies(tier, rule);
  const tiersOf = (g: string) => groupTiers(policy, g) || [];

  // 3. Pick the group as resolveRoute would.
  let group: string;
  let reason: string;
  if (req.explicitGroup) {
    group = req.explicitGroup;
    reason = 'override';
    if (tiersOf(group).length === 0) {
      group = policy.defaultGroup;
      reason = 'override:unknown-group-default';
    }
  } else if (req.quality) {
    const mapped = Object.hasOwn(policy.qualityToGroup, req.quality)
      ? policy.qualityToGroup[req.quality]
      : undefined;
    group = mapped || policy.defaultGroup;
    reason = `quality:${req.quality}`;
    if (tiersOf(group).length === 0) {
      group = policy.defaultGroup;
      reason += ':unknown-group-default';
    }
  } else if (strategy instanceof HeuristicStrategy) {
    const label = isComplex(req) ? 'complex' : 'simple';
    group = label === 'complex' ? 'frontier' : 'cheap';
    reason = `heuristic:${label}`;
    if (tiersOf(group).length === 0) {
      group = policy.defaultGroup;
      reason = 'heuristic:empty-group-fallback';
    }
  } else {
    // A custom strategy picks the group; it only ever sees allowed tiers.
    const view: RoutePolicy = { ...policy, groups: {} };
    for (const g of Object.keys(policy.groups)) {
      view.groups[g] = policy.groups[g].filter(allow);
    }
    const picked = await strategy.route(req, view);
    group = picked.group;
    reason = picked.reason;
    if (tiersOf(group).length === 0) {
      group = policy.defaultGroup;
      reason += ':unknown-group-default';
    }
  }

  // 4. Use-case quality bar (not on the explicit path).
  if (bar?.minGroup) {
    const have = groupRank(policy, group);
    const need = groupRank(policy, bar.minGroup);
    if (need !== undefined && (have === undefined || have < need)) {
      group = bar.minGroup;
      reason += '+use-case-min-group';
    }
  }

  let candidates = tiersOf(group).filter(allow);
  if (candidates.length === 0 && eligible) {
    const order = policy.groupOrder || [];
    const start = groupRank(policy, group);
    for (const higher of order.slice(
      start !== undefined ? start + 1 : order.length
    )) {
      candidates = tiersOf(higher).filter(allow);
      if (candidates.length) {
        group = higher;
        reason += '+use-case-eligible';
        break;
      }
    }
  }
  if (candidates.length === 0) {
    throw new PolicyViolation(
      `no candidate in group '${group}' is allowed at sensitivity '${level}'`,
      { sensitivityLevel: level, sensitivitySource: source, group }
    );
  }

  // 5. Cheapest allowed candidate, then the budget clamp.
  let picked = pickCheapestIn(candidates, req)!;
  let servedGroup = group;
  if (req.maxCost != null && picked.estCost > req.maxCost) {
    const clamped = cheapestUnderBudget(policy, req.maxCost, req, allow);
    if (clamped) {
      // Same reason strings as resolveRoute for the same path.
      if (clamped.underBudget) reason += '+budget-clamp';
      else if (reason.startsWith('heuristic:') && !reason.includes('+'))
        reason = 'heuristic:budget-unsatisfiable-cheapest';
      else reason += '+budget-unsatisfiable';
      picked = clamped;
      servedGroup = clamped.tier.group;
    }
  }

  const decision: RouteDecision = {
    model: picked.tier.model,
    group: servedGroup,
    reason,
    estCost: picked.estCost,
  };
  if (level !== undefined) decision.sensitivityLevel = level;
  decision.sensitivitySource = source;
  return decision;
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
    dataClassification: hints.dataClassification,
    useCase: hints.useCase,
  };
}

export {
  classifySensitivity,
  fromRoutePolicyDoc,
  PolicyViolation,
  tierComplies,
} from './constraints';
export { detectPiiCategories } from './pii';
export {
  defaultPolicy,
  estimateInputTokens,
  estimateTierCost,
  HeuristicStrategy,
};
export * from './types';
