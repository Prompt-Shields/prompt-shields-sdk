import { Context, Next } from 'hono';
import { env } from 'hono/adapter';
import { providerFromHeaders } from '../ps-telemetry';
import { inputTokensOf, observeStream } from '../psStreamObserver';
import {
  Counter,
  Histogram,
  PROMETHEUS_CONTENT_TYPE,
  Registry,
} from './registry';

/**
 * PS gateway Prometheus metrics (#61). Opt-in with PS_METRICS_ENABLED=true;
 * when PS_METRICS_TOKEN is set, GET /metrics requires it as a bearer token.
 *
 * Privacy contract: labels carry provider, model, route, status and the
 * X-PS business context (business unit, environment) only. Prompt and
 * completion content never reach a label.
 */
export const registry = new Registry();

const LABELS = [
  'route',
  'method',
  'status',
  'provider',
  'business_unit',
  'environment',
];

export const requestsTotal = registry.register(
  new Counter('ps_gateway_requests_total', 'Gateway requests handled.', LABELS)
);
export const requestDuration = registry.register(
  new Histogram(
    'ps_gateway_request_duration_seconds',
    'Gateway request duration, including the upstream call.',
    ['route', 'status', 'provider']
  )
);
export const tokensTotal = registry.register(
  new Counter('ps_gateway_tokens_total', 'Tokens reported by providers.', [
    'provider',
    'model',
    'direction',
    'business_unit',
  ])
);
export const streamsTotal = registry.register(
  new Counter(
    'ps_gateway_streams_total',
    'Streamed responses by outcome: complete, truncated (upstream closed without a terminal event) or cancelled (client went away).',
    ['route', 'provider', 'outcome']
  )
);

// Bounded, printable label values; anything else collapses to "other".
const labelValue = (raw: unknown, fallback = 'none'): string => {
  if (typeof raw !== 'string' || !raw) return fallback;
  const v = raw.trim().slice(0, 64);
  return /^[A-Za-z0-9 _.:\/@-]+$/.test(v) ? v : 'other';
};

const routeOf = (c: Context): string => {
  // The first concrete route that matched is the one that handled the
  // request (later catch-alls such as /v1/* also match). Using its pattern,
  // e.g. /v1/files/:id, keeps the label low-cardinality for paths with ids.
  const matched = (c.req as any).matchedRoutes as
    | { path: string; method: string }[]
    | undefined;
  const route = matched?.find(
    (r) => r.method !== 'ALL' && r.path !== '*' && r.path !== '/*'
  )?.path;
  return route ?? 'unmatched';
};

const usageOf = (body: any): { input?: number; output?: number } => {
  const u = body?.usage;
  if (!u || typeof u !== 'object') return {};
  return {
    input: inputTokensOf(u),
    output: u.completion_tokens ?? u.output_tokens,
  };
};

export const isMetricsEnabled = (c: Context) =>
  env<Record<string, string | undefined>>(c).PS_METRICS_ENABLED === 'true';

export function psMetrics() {
  return async (c: Context, next: Next) => {
    if (!isMetricsEnabled(c) || c.req.path === '/metrics') return next();
    const start = Date.now();
    await next();

    try {
      const headers = Object.fromEntries(c.req.raw.headers) as Record<
        string,
        string
      >;
      const provider = labelValue(providerFromHeaders(headers), 'unknown');
      const businessUnit = labelValue(headers['x-ps-business-unit']);
      const route = routeOf(c);
      const status = String(c.res.status);

      requestsTotal.inc({
        route,
        method: c.req.method,
        status,
        provider,
        business_unit: businessUnit,
        environment: labelValue(headers['x-ps-environment']),
      });
      requestDuration.observe(
        { route, status, provider },
        (Date.now() - start) / 1000
      );

      const countTokens = (
        model: string,
        input: number | undefined,
        output: number | undefined
      ) => {
        if (input)
          tokensTotal.inc(
            {
              provider,
              model,
              direction: 'input',
              business_unit: businessUnit,
            },
            input
          );
        if (output)
          tokensTotal.inc(
            {
              provider,
              model,
              direction: 'output',
              business_unit: businessUnit,
            },
            output
          );
      };

      // Streamed responses: usage and outcome are known only once the stream
      // ends, so they are recorded then, without holding the response.
      const stream = observeStream(c);
      if (stream) {
        stream
          .then((s) => {
            streamsTotal.inc({
              route,
              provider,
              outcome: s.cancelled
                ? 'cancelled'
                : s.truncated
                  ? 'truncated'
                  : 'complete',
            });
            countTokens(
              labelValue(s.model, 'unknown'),
              s.tokens_in,
              s.tokens_out
            );
          })
          .catch(() => {});
      } else if (
        (c.res.headers.get('content-type') ?? '').includes('application/json')
      ) {
        const body: any = await c.res
          .clone()
          .json()
          .catch(() => undefined);
        const { input, output } = usageOf(body);
        countTokens(labelValue(body?.model, 'unknown'), input, output);
      }
    } catch {
      // Metrics are best-effort and must never affect the response.
    }
  };
}

export function metricsHandler(c: Context) {
  if (!isMetricsEnabled(c))
    return c.json({ message: 'Not Found', ok: false }, 404);
  const token = env<Record<string, string | undefined>>(c).PS_METRICS_TOKEN;
  if (token && c.req.header('authorization') !== `Bearer ${token}`) {
    return c.json({ status: 'failure', message: 'Unauthorized' }, 401);
  }
  c.header('Content-Type', PROMETHEUS_CONTENT_TYPE);
  return c.body(registry.render());
}
