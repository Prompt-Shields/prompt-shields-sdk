import { Context, Next } from 'hono';
import { env } from 'hono/adapter';
import { providerFromHeaders, reportsOwnUsage } from '../ps-telemetry';
import { inputTokensOf, observeStream } from '../psStreamObserver';
import {
  Counter,
  defaultRegistry,
  Histogram,
  PROMETHEUS_CONTENT_TYPE,
} from './registry';
import { isMetricsEnabled, labelValue, routeOf } from './labels';
// Security and telemetry-health metrics register in the same registry.
import './security';
import { reportAuthFailure } from '../psSecurityEvents';

/**
 * PS gateway Prometheus metrics (#61). Opt-in with PS_METRICS_ENABLED=true;
 * when PS_METRICS_TOKEN is set, GET /metrics requires it as a bearer token.
 *
 * Privacy contract: labels carry provider, model, route, status and the
 * X-PS business context (business unit, environment) only. Prompt and
 * completion content never reach a label.
 */
export const registry = defaultRegistry;

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

const usageOf = (body: any): { input?: number; output?: number } => {
  const u = body?.usage;
  if (!u || typeof u !== 'object') return {};
  return {
    input: inputTokensOf(u),
    output: u.completion_tokens ?? u.output_tokens,
  };
};

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
        reportsOwnUsage(c.req.path) &&
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

export { isMetricsEnabled, labelValue, routeOf };

export function metricsHandler(c: Context) {
  if (!isMetricsEnabled(c))
    return c.json({ message: 'Not Found', ok: false }, 404);
  const token = env<Record<string, string | undefined>>(c).PS_METRICS_TOKEN;
  if (token && c.req.header('authorization') !== `Bearer ${token}`) {
    reportAuthFailure(
      c,
      'metrics',
      c.req.header('authorization')
        ? 'invalid_credentials'
        : 'missing_credentials'
    );
    return c.json({ status: 'failure', message: 'Unauthorized' }, 401);
  }
  c.header('Content-Type', PROMETHEUS_CONTENT_TYPE);
  return c.body(registry.render());
}
