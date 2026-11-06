/**
 * PS fork (#20): which provider served a call, and the jurisdiction the
 * route policy states for it.
 *
 * tryPost records the target of each successful upstream call (provider and
 * any custom host) on the Hono context; the last one is the target that
 * served the response. ps-telemetry reports it as `served_provider` and,
 * when the active route policy has a tier for the served model at that
 * provider, the tier's `jurisdiction` as `served_jurisdiction`.
 *
 * The same matching is used by the data policy gate: a policy tier that
 * names a provider only vouches for a target sent to that provider at its
 * own base URL. A target with a custom host (any provider) or sent to
 * another provider gets no facts from that tier, so it fails closed.
 * Tiers without a provider keep stating facts about the model alone.
 */
import { BERGET_BASE_URL } from '../providers/berget/api';
import { INFERCOM_BASE_URL } from '../providers/infercom/api';
import { IONOS_BASE_URL } from '../providers/ionos/api';
import { REGOLO_BASE_URL } from '../providers/regolo/api';
import { SCALEWAY_BASE_URL } from '../providers/scaleway/api';
import { STACKIT_BASE_URL } from '../providers/stackit/api';
import { ModelTier, RoutePolicy } from './router/types';

export const SERVED_TARGET_KEY = 'psServedTarget';
// Same key as cache/semantic.ts PS_ROUTE_POLICY_KEY (set by router/hono.ts);
// not imported, to keep the cache module out of the gate's import graph.
export const ROUTE_POLICY_KEY = 'psRoutePolicy';

export interface ServedTarget {
  provider?: string;
  customHost?: string;
  /** The model sent upstream (after override_params). */
  model?: string;
}

/**
 * Gateway provider id -> the id route policies and the model catalog use,
 * where they differ.
 */
const POLICY_PROVIDER_IDS: Record<string, string> = {
  'mistral-ai': 'mistral',
};

/**
 * Other route-policy ids a gateway provider answers to. `mistral-eu` is the
 * id the shipped example policies (nordic-sovereign) use for Mistral's
 * EU-hosted La Plateforme, which is the gateway's `mistral-ai` base URL.
 */
const POLICY_PROVIDER_ALIASES: Record<string, string[]> = {
  'mistral-ai': ['mistral-eu'],
};

/**
 * The fixed base URL of providers whose facts can apply to a target with a
 * custom host, when that host is the provider's own.
 */
const PROVIDER_BASE_URLS: Record<string, string> = {
  openai: 'https://api.openai.com/v1',
  anthropic: 'https://api.anthropic.com/v1',
  'mistral-ai': 'https://api.mistral.ai/v1',
  berget: BERGET_BASE_URL,
  infercom: INFERCOM_BASE_URL,
  ionos: IONOS_BASE_URL,
  regolo: REGOLO_BASE_URL,
  scaleway: SCALEWAY_BASE_URL,
  stackit: STACKIT_BASE_URL,
  ovhcloud: 'https://oai.endpoints.kepler.ai.cloud.ovh.net/v1',
};

/** The route-policy / catalog id of a gateway provider. */
export function policyProviderId(provider: string): string {
  const id = provider.toLowerCase();
  return POLICY_PROVIDER_IDS[id] ?? id;
}

function originOf(url: string): string | undefined {
  try {
    return new URL(url.trim()).origin.toLowerCase();
  } catch {
    return undefined;
  }
}

/**
 * True when `tier`'s facts apply to a call sent to `target`. A tier with no
 * provider applies to any target; one that names a provider applies only to
 * that provider, and with a custom host only when the host is the
 * provider's own base URL origin. An unknown target provider is not checked.
 */
export function tierServesTarget(
  tier: ModelTier,
  target: ServedTarget | undefined
): boolean {
  if (tier.provider === undefined || !target?.provider) return true;
  const gatewayId = target.provider.toLowerCase();
  if (
    tier.provider !== gatewayId &&
    tier.provider !== policyProviderId(gatewayId) &&
    !(POLICY_PROVIDER_ALIASES[gatewayId] || []).includes(tier.provider)
  )
    return false;
  if (!target.customHost) return true;
  const own = PROVIDER_BASE_URLS[gatewayId];
  return !!own && originOf(target.customHost) === originOf(own);
}

/**
 * The jurisdiction the policy states for `model` served at `target`: the
 * tiers listing the model that apply to the target must agree on one, else
 * undefined (unknown).
 */
export function servedJurisdiction(
  policy: RoutePolicy | undefined,
  model: string | undefined,
  target: ServedTarget | undefined
): string | undefined {
  if (!policy?.groups || !model) return undefined;
  const found = new Set<string | undefined>();
  for (const group of Object.keys(policy.groups)) {
    for (const tier of policy.groups[group] || []) {
      if (tier.model === model && tierServesTarget(tier, target))
        found.add(tier.jurisdiction);
    }
  }
  if (found.size !== 1) return undefined;
  return [...found][0];
}

/** Records the target of a successful upstream call (tryPost). */
export function recordServedTarget(
  c: any,
  provider: string | undefined,
  customHost: string | undefined,
  model?: string
): void {
  try {
    const target: ServedTarget = {};
    if (typeof provider === 'string' && provider) target.provider = provider;
    if (typeof model === 'string' && model) target.model = model;
    if (typeof customHost === 'string' && customHost)
      target.customHost = customHost;
    c.set(SERVED_TARGET_KEY, target);
  } catch {
    /* no context: telemetry falls back to the request's provider */
  }
}

export function servedTargetOf(c: any): ServedTarget | undefined {
  try {
    return c.get(SERVED_TARGET_KEY);
  } catch {
    return undefined;
  }
}
