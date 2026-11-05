/**
 * Policy stages that run before any routing strategy (PRD 0001, P1):
 * sensitivity classification (#18) and the fail-closed data policy (#19).
 *
 * A port of `classify_sensitivity`, `data_policy_rule` and `tier_complies`
 * in packages/sdk/prompt_shields/route_policy.py. Both implementations run
 * the golden vectors in schemas/route-policy/vectors/ and must agree.
 *
 * Sovereignty facts (provider, jurisdiction, zdr, us_cloud_act_exposure)
 * are read from the policy's model tiers only. A fact the policy does not
 * state counts against the candidate: the stage fails closed.
 */
import {
  DataPolicyRule,
  ModelTier,
  RoutePolicy,
  SensitivitySource,
  UseCaseBar,
} from './types';

export const DEFAULT_LEVELS = [
  'public',
  'internal',
  'confidential',
  'restricted',
];

/**
 * Country codes inside the European Economic Area. A rule that allows `EEA`
 * also allows a candidate in one of these. The reverse is not true: a rule
 * that allows only `NO` does not allow a candidate only known to be in `EEA`.
 */
export const EEA_COUNTRIES = new Set([
  'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR', 'DE', 'GR',
  'HU', 'IE', 'IT', 'LV', 'LT', 'LU', 'MT', 'NL', 'PL', 'PT', 'RO', 'SK',
  'SI', 'ES', 'SE', 'IS', 'LI', 'NO',
]); // prettier-ignore

/**
 * No candidate the data policy (and use-case bar) allows. The call is never
 * served by a non-compliant model (`fallback.no_compliant_candidate` is
 * always `fail_closed`).
 */
export class PolicyViolation extends Error {
  readonly sensitivityLevel?: string;
  readonly sensitivitySource?: SensitivitySource;
  readonly group?: string;

  constructor(
    message: string,
    details: {
      sensitivityLevel?: string;
      sensitivitySource?: SensitivitySource;
      group?: string;
    } = {}
  ) {
    super(message);
    this.name = 'PolicyViolation';
    this.sensitivityLevel = details.sensitivityLevel;
    this.sensitivitySource = details.sensitivitySource;
    this.group = details.group;
  }
}

const own = <T>(obj: Record<string, T> | undefined, key: string) =>
  obj && Object.hasOwn(obj, key) ? obj[key] : undefined;

/** True when the policy has any stage that runs before the strategy. */
export function hasPolicyStages(policy: RoutePolicy): boolean {
  return !!(policy.sensitivity || policy.dataPolicy || policy.useCases);
}

/**
 * Effective sensitivity level of one call and where it came from.
 *
 * `max(declared, or unclassifiedDefault when none, the piiEscalation level of
 * every detected category)`. Escalate-only: detection never lowers a level.
 * A declared level that is not one of the policy's levels counts as
 * unclassified. The source is `pii_escalated` only when a detection raised
 * the level. Without a sensitivity section the declared value passes
 * through and nothing escalates.
 */
export function classifySensitivity(
  policy: RoutePolicy,
  declared: string | undefined,
  piiDetected: readonly string[] = []
): { level?: string; source: SensitivitySource } {
  const sensitivity = policy.sensitivity;
  if (!sensitivity) {
    return declared
      ? { level: declared, source: 'declared' }
      : { source: 'default' };
  }
  const levels = sensitivity.levels?.length
    ? sensitivity.levels
    : DEFAULT_LEVELS;
  const rank = (l: string | undefined) =>
    l === undefined ? -1 : levels.indexOf(l);

  let level: string;
  let source: SensitivitySource;
  if (declared !== undefined && rank(declared) >= 0) {
    level = declared;
    source = 'declared';
  } else {
    level = sensitivity.unclassifiedDefault;
    source = 'default';
  }
  for (const category of piiDetected) {
    const escalated = own(sensitivity.piiEscalation, category);
    if (
      escalated !== undefined &&
      rank(escalated) >= 0 &&
      rank(escalated) > rank(level)
    ) {
      level = escalated;
      source = 'pii_escalated';
    }
  }
  return { level, source };
}

/** The data policy rule for `level`; undefined means no constraint. */
export function dataPolicyRule(
  policy: RoutePolicy,
  level: string | undefined
): DataPolicyRule | undefined {
  return level === undefined ? undefined : own(policy.dataPolicy, level);
}

function jurisdictionAllowed(
  jurisdiction: string | undefined,
  allowed: readonly string[]
): boolean {
  if (allowed.includes('any')) return true;
  if (typeof jurisdiction !== 'string') return false;
  return (
    allowed.includes(jurisdiction) ||
    (allowed.includes('EEA') && EEA_COUNTRIES.has(jurisdiction))
  );
}

/**
 * True when a tier meets a data policy rule. Fails closed on missing facts:
 * a tier with no jurisdiction only passes `any`, one with no provider never
 * passes a provider allow-list, `requireZdr` needs `zdr: true`, and
 * `usCloudActExposure: false` needs the tier to state `false`.
 */
export function tierComplies(
  tier: ModelTier,
  rule: DataPolicyRule | undefined
): boolean {
  if (!rule) return true;
  if (!jurisdictionAllowed(tier.jurisdiction, rule.jurisdictions || []))
    return false;
  if (
    rule.providers != null &&
    (tier.provider === undefined || !rule.providers.includes(tier.provider))
  )
    return false;
  if (rule.requireZdr && tier.zdr !== true) return false;
  return !(
    rule.usCloudActExposure === false && tier.usCloudActExposure !== false
  );
}

/**
 * True when a model named directly (not chosen by the router: a concrete
 * body model, a config's `override_params.model`, an Azure deployment) meets
 * the rule. The facts come from the policy tiers listing that model; a model
 * the policy does not list has no facts and fails closed (only an `any`
 * jurisdiction rule with no other condition lets it through).
 */
export function modelComplies(
  policy: RoutePolicy,
  model: string | undefined,
  rule: DataPolicyRule | undefined
): boolean {
  if (!rule) return true;
  const tiers: ModelTier[] = [];
  if (model) {
    for (const group of Object.keys(policy.groups)) {
      for (const tier of policy.groups[group])
        if (tier.model === model) tiers.push(tier);
    }
  }
  if (!tiers.length) {
    return tierComplies(
      { model: model ?? '', group: '', inputCostPer1k: 0, outputCostPer1k: 0 },
      rule
    );
  }
  return tiers.some((tier) => tierComplies(tier, rule));
}

/** Index of a group in `groupOrder`, or undefined when it is not listed. */
export function groupRank(
  policy: RoutePolicy,
  group: string
): number | undefined {
  const i = (policy.groupOrder || []).indexOf(group);
  return i < 0 ? undefined : i;
}

/** The quality bar for a declared use case, if the policy has one. */
export function useCaseBar(
  policy: RoutePolicy,
  useCase: string | undefined
): UseCaseBar | undefined {
  return useCase === undefined ? undefined : own(policy.useCases, useCase);
}

/**
 * Convert a route-policy.v1 document (snake_case JSON, see
 * schemas/route-policy/) into a RoutePolicy, keeping every sovereignty fact.
 */
export function fromRoutePolicyDoc(doc: any): RoutePolicy {
  const groups: RoutePolicy['groups'] = {};
  for (const [name, tiers] of Object.entries<any[]>(doc.groups)) {
    groups[name] = tiers.map((t) => {
      const tier: ModelTier = {
        model: t.model,
        group: name,
        inputCostPer1k: t.input_cost_per_1k,
        outputCostPer1k: t.output_cost_per_1k,
      };
      if (t.provider !== undefined) tier.provider = t.provider;
      if (t.jurisdiction !== undefined) tier.jurisdiction = t.jurisdiction;
      if (t.zdr !== undefined) tier.zdr = t.zdr;
      if (t.us_cloud_act_exposure !== undefined)
        tier.usCloudActExposure = t.us_cloud_act_exposure;
      return tier;
    });
  }
  const policy: RoutePolicy = {
    defaultGroup: doc.default_group,
    qualityToGroup: doc.quality_to_group,
    groups,
  };
  if (doc.group_order) policy.groupOrder = doc.group_order;
  if (doc.strategy) policy.strategy = doc.strategy;
  if (doc.sensitivity) {
    policy.sensitivity = {
      levels: doc.sensitivity.levels,
      unclassifiedDefault: doc.sensitivity.unclassified_default,
      piiEscalation: doc.sensitivity.pii_escalation,
    };
  }
  if (doc.data_policy) {
    policy.dataPolicy = {};
    for (const [level, r] of Object.entries<any>(doc.data_policy)) {
      const rule: DataPolicyRule = { jurisdictions: r.jurisdictions };
      if (r.providers !== undefined) rule.providers = r.providers;
      if (r.require_zdr !== undefined) rule.requireZdr = r.require_zdr;
      if (r.us_cloud_act_exposure !== undefined)
        rule.usCloudActExposure = r.us_cloud_act_exposure;
      policy.dataPolicy[level] = rule;
    }
  }
  if (doc.use_cases) {
    policy.useCases = {};
    for (const [name, b] of Object.entries<any>(doc.use_cases)) {
      const bar: UseCaseBar = {};
      if (b.min_group !== undefined) bar.minGroup = b.min_group;
      if (b.eval_set !== undefined) bar.evalSet = b.eval_set;
      if (b.min_parity !== undefined) bar.minParity = b.min_parity;
      if (b.eligible_models !== undefined)
        bar.eligibleModels = b.eligible_models;
      policy.useCases[name] = bar;
    }
  }
  return policy;
}
