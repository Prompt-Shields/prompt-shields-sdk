/**
 * Cost-aware routing types.
 *
 * The SDK expresses *intent* (see gateway/src/middlewares/PS_README.md); the
 * gateway owns the decision. A RouterStrategy turns a RouteRequest into a
 * concrete RouteDecision against a config-driven RoutePolicy.
 */

export type Quality = 'draft' | 'balanced' | 'critical';

/** One concrete model plus its per-1k-token cost, tagged with its group. */
export interface ModelTier {
  model: string;
  group: string;
  inputCostPer1k: number;
  outputCostPer1k: number;
}

/** A named ladder of model groups plus the mappings that steer into them. */
export interface RoutePolicy {
  /** Group used when no hint applies — the transparent-by-default target. */
  defaultGroup: string;
  /** group name -> ordered tiers (cheapest-first is not required; we sort by cost). */
  groups: Record<string, ModelTier[]>;
  /** Maps an SDK quality intent to a model group. */
  qualityToGroup: Record<Quality, string>;
}

/** What the router is asked to decide for a single call. */
export interface RouteRequest {
  messages: any[];
  requestedModel: string; // "auto" or a concrete name (soft preference)
  approxInputTokens: number;
  /** Assumed completion length for cost estimation; defaults applied downstream. */
  maxOutputTokens?: number;
  quality?: Quality; // X-PS-Quality hint
  maxCost?: number; // X-PS-Max-Cost ceiling (USD)
  explicitGroup?: string; // X-PS-Route override — bypasses inference
}

/** The router's answer: a concrete model and why it was chosen. */
export interface RouteDecision {
  model: string;
  group: string;
  reason: string;
  estCost: number;
}

/** Pluggable decision function. Async so a learned strategy can call out. */
export interface RouterStrategy {
  route(req: RouteRequest, policy: RoutePolicy): Promise<RouteDecision>;
}

/** Hints parsed off the inbound X-PS-* headers. */
export interface ParsedRouteHints {
  quality?: Quality;
  maxCost?: number;
  explicitGroup?: string;
  cacheOff: boolean;
}
