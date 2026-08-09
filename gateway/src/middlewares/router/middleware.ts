/**
 * Prompt Shields cost-aware routing middleware.
 *
 * beforeRequest: parse X-PS-* routing hints, decide a concrete model, and
 * rewrite `body.model`. The decision is stashed on the request as `_psRoute`
 * so the telemetry middleware can emit requested_model vs served_model.
 *
 * Runs BEFORE ps-telemetry (which strips X-PS-* headers) and before the cache
 * middleware (so the cache key reflects the resolved model). Fail-open: any
 * error leaves the request untouched.
 */
import { HeuristicStrategy } from './heuristic';
import { defaultPolicy } from './policy';
import { buildRouteRequest, parseRouteHeaders, resolveRoute } from './index';
import { RoutePolicy, RouterStrategy } from './types';

export interface PSRoute {
  requestedModel: string;
  servedModel: string;
  group: string;
  reason: string;
  estCost: number;
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
        const decision = await resolveRoute(routeReq, policy, strategy);
        if (!decision) return request;

        (request as any)._psRoute = {
          requestedModel: routeReq.requestedModel,
          servedModel: decision.model,
          group: decision.group,
          reason: decision.reason,
          estCost: decision.estCost,
        } as PSRoute;

        body.model = decision.model;
      } catch (err) {
        // Fail-open: routing must never block an LLM request.
        console.warn('[PS Router] routing skipped:', err);
      }
      return request;
    },
  };
}
