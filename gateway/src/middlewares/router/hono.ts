/**
 * Hono adapter for the cost-aware router.
 *
 * Wraps `psRouterMiddleware` as a Hono middleware and mounts it ahead of the
 * chat/messages handlers in src/index.ts. Hono (4.x) re-parses the body per
 * `c.req.json()` call, so we overwrite `c.req.bodyCache.json` with the rewritten
 * object — the downstream handler's own `await c.req.json()` then reads the
 * resolved model. Only done when a routing decision was actually made.
 *
 * The decision is exposed on the context as `psRoute` (c.get('psRoute')) so a
 * telemetry middleware can emit requested_model vs served_model.
 *
 * Fail-open: a non-JSON body, parse error, or routing error passes through
 * untouched. Only requests carrying a `messages` array are ever routed.
 *
 * Fail-closed under a data policy (#19): when no candidate meets the policy
 * for the call's sensitivity level, the request is refused with a 403
 * (`error.code: policy_violation`) before any upstream call, and a
 * `policy.denied` security event (reason `no_compliant_candidate`) is
 * recorded. Every other request (a concrete model, a non-chat endpoint, a
 * multipart upload) gets a data policy gate on the context that tryPost
 * checks against each target's model (dataPolicyGate.ts); realtime, which
 * never reaches tryPost, is checked here.
 */
import { Context, Next } from 'hono';
import { isMcpProxyPath } from '../../mcp/paths';
import { constructConfigFromRequestHeaders } from '../../handlers/handlerUtils';
import { targetModels } from '../psOidc/identity';
import {
  buildDataPolicyGate,
  DATA_POLICY_GATE_KEY,
  dataPolicyTargetDenied,
  policyViolationResponse,
} from './dataPolicyGate';
import {
  escalatesOnPii,
  psRouterMiddleware,
  PSRoute,
  PSRouteViolation,
} from './middleware';
import { scanRequestBody } from './pii';
import { PS_ROUTE_POLICY_KEY } from '../cache/semantic';
import { defaultPolicy } from './policy';
import { RoutePolicy, RouterStrategy } from './types';

// Expose the routing decision to downstream middleware (e.g. telemetry).
declare module 'hono' {
  interface ContextVariableMap {
    psRoute?: PSRoute;
  }
}

/**
 * `policy` is a RoutePolicy, or a function returning the active one per
 * request (PS_ROUTE_POLICY_FILE, #336, which can be reloaded). A function
 * that returns undefined means the policy is not loaded: model calls are
 * refused with a 503 rather than routed without it.
 */
export function psRouter(
  policy?: RoutePolicy | (() => RoutePolicy | undefined),
  strategy?: RouterStrategy
) {
  const provide =
    typeof policy === 'function' ? policy : () => policy ?? defaultPolicy;
  let built: RoutePolicy | undefined;
  let mw: ReturnType<typeof psRouterMiddleware> | undefined;

  return async (c: Context, next: Next) => {
    // MCP proxy traffic is not a model call (#63).
    if (isMcpProxyPath(c.req.path)) return next();
    const active = provide();
    if (!active) {
      return c.json(
        {
          error: {
            message: 'route policy not loaded; refusing the call',
            type: 'policy_unavailable',
          },
        },
        503
      );
    }
    if (!mw || built !== active) {
      mw = psRouterMiddleware(active, strategy as RouterStrategy);
      built = active;
    }
    const router = mw;
    // The semantic cache keys entries by the active policy's version (#33),
    // and ps-telemetry looks up served_jurisdiction in it (#20).
    c.set(PS_ROUTE_POLICY_KEY as any, active);
    const contentType = c.req.header('content-type') || '';
    const isJsonPost =
      c.req.method === 'POST' && contentType.includes('application/json');

    // Data policy gate for every model call (fail closed: an error here
    // refuses the call rather than letting it through unchecked).
    let pii: string[] | undefined;
    if (active.dataPolicy) {
      try {
        if (isJsonPost && escalatesOnPii(active)) {
          let body: unknown;
          try {
            body = await c.req.json();
          } catch {
            body = undefined; // not JSON: the handler rejects it
          }
          pii = scanRequestBody(body);
        }
        const gate = buildDataPolicyGate(
          active,
          Object.fromEntries(c.req.raw.headers),
          pii ?? []
        );
        c.set(DATA_POLICY_GATE_KEY as any, gate);
        if (c.req.path === '/v1/realtime') {
          const options = constructConfigFromRequestHeaders(
            Object.fromEntries(c.req.raw.headers)
          );
          const denied = dataPolicyTargetDenied(
            c,
            typeof options?.provider === 'string'
              ? options.provider
              : undefined,
            [
              c.req.query('model') || '',
              ...targetModels(options as any, undefined),
            ],
            true,
            typeof (options as any)?.customHost === 'string'
              ? (options as any).customHost
              : undefined
          );
          if (denied) return denied;
        }
      } catch (err) {
        console.warn('[PS Router] data policy gate failed, refusing:', err);
        return policyViolationResponse(c, {});
      }
    }

    try {
      if (isJsonPost) {
        // Cached by Hono — the same object reference the handler will read.
        const body = await c.req.json();
        const headers = Object.fromEntries(c.req.raw.headers);
        const request: any = { url: c.req.url, headers, body };
        if (pii) request._psPii = pii;
        await router.beforeRequest(request);
        const violation: PSRouteViolation | undefined =
          request._psRouteViolation;
        if (violation) return policyViolationResponse(c, violation);
        const route = (request as any)._psRoute;
        if (route) {
          c.set('psRoute', route);
          // `body` was mutated in place; force the handler's json() to read it.
          (c.req as any).bodyCache = { json: Promise.resolve(body) };
        }
      }
    } catch (err) {
      // Fail-open: routing must never block an LLM request.
      console.warn('[PS Router] hono adapter skipped:', err);
    }
    await next();
  };
}
