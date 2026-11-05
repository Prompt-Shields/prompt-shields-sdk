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
  /**
   * Sovereignty facts the data policy checks (route-policy.v1 `modelTier`).
   * Missing facts fail closed: see `tierComplies` in constraints.ts.
   */
  provider?: string;
  /** ISO 3166-1 alpha-2 code, a bloc such as `EEA`, or `any`. */
  jurisdiction?: string;
  /** Zero data retention is offered for this model. */
  zdr?: boolean;
  usCloudActExposure?: boolean;
}

/** Where a call's effective sensitivity level came from. */
export type SensitivitySource = 'declared' | 'pii_escalated' | 'default';

/** route-policy.v1 `sensitivity`. */
export interface SensitivityConfig {
  /** Least to most sensitive. Defaults to public < internal < confidential < restricted. */
  levels?: string[];
  /** Level used when the caller declares none (or an unknown one). */
  unclassifiedDefault: string;
  /** Detected PII category -> the minimum level it escalates a call to. */
  piiEscalation?: Record<string, string>;
}

/** route-policy.v1 `dataPolicyRule`: what a candidate must meet at one level. */
export interface DataPolicyRule {
  jurisdictions: string[];
  providers?: string[];
  requireZdr?: boolean;
  /** `false` requires candidates that state they have no US CLOUD Act exposure. */
  usCloudActExposure?: boolean;
}

/** route-policy.v1 `qualityBar`, per declared use case. */
export interface UseCaseBar {
  minGroup?: string;
  evalSet?: string;
  minParity?: number;
  eligibleModels?: string[];
}

/** A named ladder of model groups plus the mappings that steer into them. */
export interface RoutePolicy {
  /** Group used when no hint applies — the transparent-by-default target. */
  defaultGroup: string;
  /** group name -> ordered tiers (cheapest-first is not required; we sort by cost). */
  groups: Record<string, ModelTier[]>;
  /** Maps an SDK quality intent to a model group. */
  qualityToGroup: Record<Quality, string>;
  /** Groups from least to most capable (compared against a use case's minGroup). */
  groupOrder?: string[];
  /** Strategy on the transparent path; only `heuristic` is built in. */
  strategy?: string;
  /**
   * The three sections below turn on the policy stages (resolveRoute):
   * sensitivity classification, the fail-closed data policy and the
   * per-use-case quality bar. Without them routing is unchanged.
   */
  sensitivity?: SensitivityConfig;
  dataPolicy?: Record<string, DataPolicyRule>;
  useCases?: Record<string, UseCaseBar>;
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
  /** Declared sensitivity level (X-PS-Data-Classification). */
  dataClassification?: string;
  /** PII categories detected in the request (escalate-only). */
  piiDetected?: string[];
  /** Declared use case (X-PS-Use-Case), for the per-use-case quality bar. */
  useCase?: string;
}

/** The router's answer: a concrete model and why it was chosen. */
export interface RouteDecision {
  model: string;
  group: string;
  reason: string;
  estCost: number;
  /** Set when the policy has a sensitivity or data policy section. */
  sensitivityLevel?: string;
  sensitivitySource?: SensitivitySource;
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
  /** X-PS-Data-Classification. */
  dataClassification?: string;
  /** X-PS-Use-Case. */
  useCase?: string;
  cacheOff: boolean;
}
