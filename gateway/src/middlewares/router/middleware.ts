/**
 * Prompt Shields cost-aware routing middleware.
 *
 * beforeRequest: parse X-PS-* routing hints, decide a concrete model, and
 * rewrite `body.model`. The decision is stashed on the request as `_psRoute`
 * so the telemetry middleware can emit requested_model vs served_model.
 *
 * Runs BEFORE ps-telemetry (which strips X-PS-* headers) and before the cache
 * middleware (so the cache key reflects the resolved model). Fail-open: any
 * error leaves the request untouched, except under a data policy, which
 * fails closed: a PolicyViolation (or any routing error while the policy has
 * a dataPolicy section) is stashed as `_psRouteViolation` and the request
 * must be refused (the Hono adapter answers 403 `policy_violation`).
 */
import { HeuristicStrategy } from './heuristic';
import { defaultPolicy } from './policy';
import { PolicyViolation } from './constraints';
import { scanRequestBody } from './pii';
import { buildRouteRequest, parseRouteHeaders, resolveRoute } from './index';
import { RoutePolicy, RouterStrategy, SensitivitySource } from './types';

export interface PSRoute {
  requestedModel: string;
  servedModel: string;
  group: string;
  reason: string;
  estCost: number;
  /** Effective sensitivity level, when the policy classifies calls. */
  sensitivityLevel?: string;
  sensitivitySource?: SensitivitySource;
}

/** Why a routed call was refused under the data policy. */
export interface PSRouteViolation {
  requestedModel: string;
  message: string;
  group?: string;
  sensitivityLevel?: string;
  sensitivitySource?: SensitivitySource;
}

/** True when the policy escalates sensitivity on detected PII. */
export function escalatesOnPii(policy: RoutePolicy): boolean {
  const map = policy.sensitivity?.piiEscalation;
  return !!map && Object.keys(map).length > 0;
}

export function psRouterMiddleware(
  policy: RoutePolicy = defaultPolicy,
  strategy: RouterStrategy = new HeuristicStrategy()
) {
  return {
    beforeRequest: async (request: {
      url: string;
      headers: Record<string, string>;
      body: any;
    }) => {
      try {
        const body = request.body;
        if (!body || !Array.isArray(body.messages)) return request;

        const hints = parseRouteHeaders(request.headers || {});
        const wantsAuto = body.model === 'auto';
        const hasHint =
          hints.quality != null ||
          hints.explicitGroup != null ||
          hints.maxCost != null;

        // Transparent routing only engages for model="auto"; a concrete model
        // with no hint is an explicit choice we leave alone.
        if (!wantsAuto && !hasHint) return request;

        const routeReq = buildRouteRequest(body, hints);
        if (escalatesOnPii(policy)) {
          // The Hono adapter may have scanned the body already (data policy).
          const pre = (request as any)._psPii;
          routeReq.piiDetected = Array.isArray(pre)
            ? pre
            : scanRequestBody(body);
        }
        let decision;
        try {
          decision = await resolveRoute(routeReq, policy, strategy);
        } catch (err) {
          // Fail closed: never serve a call the data policy cannot vouch for.
          if (err instanceof PolicyViolation || policy.dataPolicy) {
            const v = err instanceof PolicyViolation ? err : undefined;
            (request as any)._psRouteViolation = {
              requestedModel: routeReq.requestedModel,
              message: v
                ? v.message
                : 'routing failed under a data policy; refusing the call',
              group: v?.group,
              sensitivityLevel: v?.sensitivityLevel,
              sensitivitySource: v?.sensitivitySource,
            } as PSRouteViolation;
            return request;
          }
          throw err;
        }
        if (!decision) return request;

        const route: PSRoute = {
          requestedModel: routeReq.requestedModel,
          servedModel: decision.model,
          group: decision.group,
          reason: decision.reason,
          estCost: decision.estCost,
        };
        if (decision.sensitivityLevel !== undefined)
          route.sensitivityLevel = decision.sensitivityLevel;
        if (decision.sensitivitySource !== undefined)
          route.sensitivitySource = decision.sensitivitySource;
        (request as any)._psRoute = route;

        body.model = decision.model;
      } catch (err) {
        // Fail-open: routing must never block an LLM request (no data policy).
        console.warn('[PS Router] routing skipped:', err);
      }
      return request;
    },
  };
}
