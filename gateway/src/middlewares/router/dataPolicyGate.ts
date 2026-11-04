/**
 * The data policy on every model call, not only routed ones (#19, PRD G2:
 * no call above the policy threshold is served outside its allowed
 * jurisdiction).
 *
 * Routing (resolvePolicyRoute) only filters the candidates of calls the
 * router decides (`model: "auto"` or a routing hint). A caller could
 * otherwise name a non-compliant model directly, or have a config's
 * `override_params.model` (or a fallback / load-balance target) replace the
 * routed one. So under a policy with a `dataPolicy` section the Hono adapter
 * classifies every request and stashes a gate on the context, and tryPost
 * checks each target's model (after override_params, plus the Azure
 * deployment) before hooks, cache or upstream, like the OIDC group policy.
 */
import { Context } from 'hono';
import { reportPolicyDenied } from '../psSecurityEvents';
import {
  classifySensitivity,
  dataPolicyRule,
  modelComplies,
} from './constraints';
import { parseRouteHeaders } from './index';
import { DataPolicyRule, RoutePolicy, SensitivitySource } from './types';

export const DATA_POLICY_GATE_KEY = 'psDataPolicyGate';

export interface DataPolicyGate {
  policy: RoutePolicy;
  level?: string;
  source: SensitivitySource;
  rule?: DataPolicyRule;
}

/** The gate for one request, or undefined when the policy has no dataPolicy. */
export function buildDataPolicyGate(
  policy: RoutePolicy,
  headers: Record<string, string>,
  piiDetected: readonly string[]
): DataPolicyGate | undefined {
  if (!policy.dataPolicy) return undefined;
  const { dataClassification } = parseRouteHeaders(headers);
  const { level, source } = classifySensitivity(
    policy,
    dataClassification,
    piiDetected
  );
  return { policy, level, source, rule: dataPolicyRule(policy, level) };
}

/**
 * The 403 when the data policy refuses a target model, else undefined.
 * `requireModel` (model endpoints): a target with no model name is checked
 * as a model without facts, i.e. it fails closed unless the rule allows any.
 */
export function dataPolicyTargetDenied(
  c: Context,
  provider: string | undefined,
  models: string[],
  requireModel: boolean
): Response | undefined {
  let gate: DataPolicyGate | undefined;
  try {
    gate = c.get(DATA_POLICY_GATE_KEY as any);
  } catch {
    gate = undefined;
  }
  if (!gate?.rule) return undefined;
  const names = models.filter((m) => typeof m === 'string' && m !== '');
  const toCheck: (string | undefined)[] = names.length
    ? names
    : requireModel
      ? [undefined]
      : [];
  for (const model of toCheck) {
    if (!modelComplies(gate.policy, model, gate.rule)) {
      return policyViolationResponse(c, {
        requestedModel: model ?? '',
        provider,
        sensitivityLevel: gate.level,
      });
    }
  }
  return undefined;
}

/** The 403 for a call the data policy refuses (fail closed). */
export function policyViolationResponse(
  c: Context,
  v: {
    requestedModel?: string;
    provider?: string;
    group?: string;
    sensitivityLevel?: string;
  }
): Response {
  reportPolicyDenied(c, 'no_compliant_candidate', {
    group: v.group,
    model: v.requestedModel || undefined,
    provider: v.provider,
  });
  const level = v.sensitivityLevel
    ? ` (sensitivity: ${v.sensitivityLevel})`
    : '';
  return c.json(
    {
      error: {
        message: `Forbidden by the routing data policy${level}: no allowed model can serve this request`,
        type: 'permission_error',
        param: null,
        code: 'policy_violation',
      },
    },
    403
  );
}
