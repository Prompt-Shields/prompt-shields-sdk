/**
 * Failover between routed models in the gateway (#21, PRD 0001 §7.5).
 *
 * When the router decided a call (`psRoute` on the context), it also lists
 * the ordered failover candidates: the decided model, then the other models
 * it could have picked under the same constraints (routeCandidates). The
 * chat and messages handlers call `tryTargetsWithRouteFailover` instead of
 * `tryTargetsRecursively`; it runs the request through the psFailover engine
 * once per candidate, with `body.model` set to the candidate's model and
 * the request's own provider and credentials.
 *
 * - A 5xx, 429 (after at most one Retry-After wait) or timeout moves to the
 *   next candidate and counts against the candidate provider's circuit
 *   breaker. Breakers are process-wide, so each is scoped to the caller
 *   (provider, credentials and upstream host, see routeBreakerScope): one
 *   caller's failing key or custom host never opens another caller's. Any other 4xx, a content-filter refusal, a guardrail denial or
 *   a data-policy 403 is returned as is: no fallback.
 * - Only already-compliant candidates are listed. Each attempt still goes
 *   through tryPost, so the data-policy gate checks the model the attempt
 *   actually runs, and the engine's `allow` re-checks each candidate before
 *   it is called (defence in depth). A non-compliant one is never called.
 * - The engine returns once the upstream answered with its headers; a
 *   streamed body is read after that, so nothing is retried once response
 *   bytes may have reached the client.
 * - Every upstream attempt is recorded by tryPost (psAttempts), so the
 *   telemetry event carries `attempts` and `fallback_chain`, and
 *   `served_model` is the candidate that answered.
 *
 * Precedence with Portkey-native routing (`x-portkey-config`): a config
 * with its own `strategy` / `targets` (fallback, load balance, conditional),
 * `retry` attempts, or an `override_params.model` owns retries and
 * fallbacks for the call. The router then only sets the decided model, as
 * before #21, and does not fail over, so a call is never retried by both.
 */
import { Context } from 'hono';
import { tryTargetsRecursively } from '../../handlers/handlerUtils';
import { endpointStrings } from '../../providers/types';
import { computeCacheScope } from '../cache';
import {
  BreakerRegistry,
  Candidate,
  defaultBreakers,
  FailoverOptions,
  FailureInfo,
  NoCompliantCandidate,
  retryAfterOf,
  runFailover,
  UpstreamError,
} from '../psFailover';
import { modelComplies } from './constraints';
import {
  DATA_POLICY_GATE_KEY,
  DataPolicyGate,
  policyViolationResponse,
} from './dataPolicyGate';
import { PSRoute } from './middleware';

let breakers: BreakerRegistry = defaultBreakers;

/** Tests only: a fresh breaker registry. */
export function _resetRouteBreakersForTests(
  registry: BreakerRegistry = new BreakerRegistry()
): BreakerRegistry {
  breakers = registry;
  return registry;
}

/** A failed upstream answer, kept so it can be returned to the client. */
export class RouteAttemptError extends UpstreamError {
  constructor(
    readonly response: Response,
    info: FailureInfo
  ) {
    super(info);
    this.name = 'RouteAttemptError';
  }
}

/** True unless the request's Portkey config owns retries or fallbacks. */
export function configAllowsRouteFailover(config: any): boolean {
  if (!config || typeof config !== 'object') return true;
  if (config.strategy) return false;
  if (Array.isArray(config.targets) && config.targets.length) return false;
  if (config.retry && Number(config.retry.attempts) > 0) return false;
  if (config.overrideParams && config.overrideParams.model !== undefined)
    return false;
  return true;
}

const MAX_ERROR_BODY = 64 * 1024;

/** Status, Retry-After and the error code/message of a failed answer. The
 * code and message are only classified, never stored. */
async function failureOfResponse(res: Response): Promise<FailureInfo> {
  const info: FailureInfo = { status: res.status };
  const retryAfter = retryAfterOf(res.headers);
  if (retryAfter !== undefined) info.retryAfter = retryAfter;
  if (res.status < 500) {
    try {
      const text = await res.clone().text();
      if (text.length <= MAX_ERROR_BODY) {
        const parsed = JSON.parse(text);
        const err = parsed?.error ?? parsed;
        const code = err?.code ?? err?.type;
        if (typeof code === 'string') info.code = code;
        if (typeof err?.message === 'string') info.message = err.message;
      }
    } catch {
      /* not JSON: classified by status alone */
    }
  }
  return info;
}

/**
 * Who the attempts go out as: a digest of the provider, the caller's
 * credential and identity fields and the upstream host (the cache's caller
 * scope). Breakers are keyed `<provider>#<scope>`, so a caller whose own key
 * is rate limited or whose custom host fails trips only its own breaker,
 * never every caller's. The metric label keeps only `<provider>`.
 */
export async function routeBreakerScope(
  targetGroup: any,
  requestHeaders: Record<string, string>
): Promise<string> {
  const scope = await computeCacheScope({
    provider:
      typeof targetGroup?.provider === 'string' ? targetGroup.provider : '',
    providerOption: targetGroup,
    requestHeaders,
    requestURL:
      typeof targetGroup?.customHost === 'string' ? targetGroup.customHost : '',
  });
  return scope.slice(0, 32);
}

function gateOf(c: Context): DataPolicyGate | undefined {
  try {
    return c.get(DATA_POLICY_GATE_KEY as any);
  } catch {
    return undefined;
  }
}

/**
 * `tryTargetsRecursively` with failover across the router's candidates.
 * Without a routed decision with more than one candidate, a non-JSON body,
 * or when the Portkey config owns retries, it is `tryTargetsRecursively`.
 */
export async function tryTargetsWithRouteFailover(
  c: Context,
  targetGroup: any,
  request: any,
  requestHeaders: Record<string, string>,
  fn: endpointStrings,
  method: string,
  jsonPath: string,
  opts: Omit<FailoverOptions<unknown>, 'breakers' | 'allow'> = {}
): Promise<Response> {
  let route: PSRoute | undefined;
  try {
    route = c.get('psRoute');
  } catch {
    route = undefined;
  }
  const candidates = route?.candidates;
  if (
    !route ||
    !Array.isArray(candidates) ||
    candidates.length < 2 ||
    !request ||
    typeof request !== 'object' ||
    request instanceof FormData ||
    request instanceof ReadableStream ||
    !configAllowsRouteFailover(targetGroup)
  ) {
    return tryTargetsRecursively(
      c,
      targetGroup,
      request,
      requestHeaders,
      fn,
      method,
      jsonPath
    );
  }

  // The provider the call actually goes to keys the breaker when the tier
  // states none.
  const gatewayProvider =
    typeof targetGroup?.provider === 'string' ? targetGroup.provider : '';
  let scope: string;
  try {
    scope = await routeBreakerScope(targetGroup, requestHeaders);
  } catch (err) {
    // No caller scope, no shared breaker state: one attempt, as before #21.
    console.warn('[PS Router] failover skipped (breaker scope):', err);
    return tryTargetsRecursively(
      c,
      targetGroup,
      request,
      requestHeaders,
      fn,
      method,
      jsonPath
    );
  }
  const list: Candidate[] = candidates.map((cand) => ({
    provider: `${cand.provider || gatewayProvider || 'unknown'}#${scope}`,
    model: cand.model,
  }));
  const gate = gateOf(c);
  const servedRoute = route;

  try {
    const result = await runFailover(
      list,
      async (cand) => {
        servedRoute.servedModel = cand.model;
        const res = await tryTargetsRecursively(
          c,
          targetGroup,
          { ...request, model: cand.model },
          requestHeaders,
          fn,
          method,
          jsonPath
        );
        if (res.status >= 200 && res.status < 300) return res;
        throw new RouteAttemptError(res, await failureOfResponse(res));
      },
      {
        ...opts,
        breakers,
        allow: gate?.rule
          ? (cand) => modelComplies(gate.policy, cand.model, gate.rule)
          : undefined,
      }
    );
    servedRoute.servedModel = result.candidate.model;
    return result.response;
  } catch (err) {
    // A failure the matrix does not fail over on: the upstream's answer.
    if (err instanceof RouteAttemptError) return err.response;
    if (err instanceof NoCompliantCandidate) {
      if (err.lastError instanceof RouteAttemptError)
        return err.lastError.response;
      if (
        err.skipped.length &&
        err.skipped.every((s) => s.reason === 'non_compliant')
      ) {
        return policyViolationResponse(c, {
          requestedModel: servedRoute.requestedModel,
          group: servedRoute.group,
          sensitivityLevel: gate?.level,
        });
      }
      return c.json(
        {
          error: {
            message:
              'No routed model is available: the circuit breaker is open for every allowed candidate',
            type: 'service_unavailable',
            param: null,
            code: 'no_available_candidate',
          },
        },
        503
      );
    }
    throw err;
  }
}
