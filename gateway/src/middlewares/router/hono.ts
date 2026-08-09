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
 */
import { Context, Next } from 'hono';
import { psRouterMiddleware, PSRoute } from './middleware';
import { defaultPolicy } from './policy';
import { RoutePolicy, RouterStrategy } from './types';

// Expose the routing decision to downstream middleware (e.g. telemetry).
declare module 'hono' {
  interface ContextVariableMap {
    psRoute?: PSRoute;
  }
}

export function psRouter(policy?: RoutePolicy, strategy?: RouterStrategy) {
  const mw = psRouterMiddleware(
    policy ?? defaultPolicy,
    strategy as RouterStrategy
  );

  return async (c: Context, next: Next) => {
    try {
      const contentType = c.req.header('content-type') || '';
      if (c.req.method === 'POST' && contentType.includes('application/json')) {
        // Cached by Hono — the same object reference the handler will read.
        const body = await c.req.json();
        const headers = Object.fromEntries(c.req.raw.headers);
        const request = { url: c.req.url, headers, body };
        await mw.beforeRequest(request);
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
