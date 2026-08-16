/**
 * Prompt Shields Telemetry Middleware
 * Intercepts LLM requests/responses and sends discovery metadata
 * to the Prompt Shields collector service.
 */

interface PSConfig {
  collectorUrl: string;
  apiKey: string;
}

interface TelemetryEvent {
  vendor: string;
  model: string;
  source: string;
  tokens_in?: number;
  tokens_out?: number;
  latency_ms?: number;
  business_unit?: string;
  use_case_name?: string;
  owner_email?: string;
  data_classification?: string;
  environment?: string;
  // Cost-aware routing (populated when ps-router made a decision)
  requested_model?: string;
  served_model?: string;
  route_group?: string;
  route_reason?: string;
  route_est_cost?: number;
  // Exact-match cache (populated by emitCacheTelemetry)
  cache_status?: string;
  est_tokens_saved?: number;
}

const PS_COLLECTOR_URL =
  process.env.PS_COLLECTOR_URL || 'http://localhost:8000';
const PS_API_KEY = process.env.PS_API_KEY || '';

/**
 * Extract vendor from the request URL or provider config
 */
function detectVendor(url: string, provider?: string): string {
  if (provider) return provider.toLowerCase();
  if (url.includes('openai.com')) return 'openai';
  if (url.includes('anthropic.com')) return 'anthropic';
  if (url.includes('googleapis.com') || url.includes('generativelanguage'))
    return 'google';
  if (url.includes('cohere.com')) return 'cohere';
  if (url.includes('mistral.ai')) return 'mistral';
  return 'unknown';
}

/**
 * Extract PS metadata from request headers (X-PS-* headers)
 */
function extractPSHeaders(
  headers: Record<string, string | undefined>
): Partial<TelemetryEvent> {
  return {
    business_unit: headers['x-ps-business-unit'],
    use_case_name: headers['x-ps-use-case'],
    owner_email: headers['x-ps-owner'],
    data_classification: headers['x-ps-data-classification'],
    environment: headers['x-ps-environment'],
  };
}

/**
 * Send telemetry event to PS collector (fire-and-forget, fail-open)
 */
async function sendTelemetry(event: TelemetryEvent): Promise<void> {
  if (!PS_API_KEY) return; // Skip if no API key configured

  try {
    await fetch(`${PS_COLLECTOR_URL}/ingest/events`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${PS_API_KEY}`,
      },
      body: JSON.stringify({ events: [event] }),
    });
  } catch (err) {
    // Fail-open: never block LLM requests due to telemetry issues
    console.warn('[PS Telemetry] Failed to send event:', err);
  }
}

export function buildCacheEvent(
  cacheStatus: string,
  requestBody: any,
  vendor: string
): TelemetryEvent {
  const isHit = cacheStatus === 'HIT' || cacheStatus === 'SEMANTIC HIT';
  const estTokens = isHit ? Math.ceil(JSON.stringify(requestBody ?? {}).length / 4) : 0;
  return {
    vendor,
    model: requestBody?.model || 'unknown',
    source: 'gateway',
    cache_status: cacheStatus,
    est_tokens_saved: estTokens,
  };
}

// Fail-open emit from a spot that already knows cacheStatus (handlerUtils).
export function emitCacheTelemetry(cacheStatus: string, requestBody: any, vendor: string): void {
  try {
    if (!cacheStatus || cacheStatus === 'DISABLED') return;
    sendTelemetry(buildCacheEvent(cacheStatus, requestBody, vendor)).catch(() => {});
  } catch {
    /* fail-open */
  }
}

/**
 * Middleware that wraps request/response to capture telemetry
 */
export function psTelemetryMiddleware() {
  return {
    beforeRequest: (
      request: { url: string; headers: Record<string, string>; body: any },
      provider?: string
    ) => {
      // Attach start time for latency tracking
      (request as any)._psStartTime = Date.now();
      (request as any)._psHeaders = extractPSHeaders(request.headers);
      (request as any)._psProvider = provider;

      // Strip PS headers before forwarding to upstream provider
      const cleaned = { ...request.headers };
      Object.keys(cleaned).forEach((k) => {
        if (k.toLowerCase().startsWith('x-ps-')) delete cleaned[k];
      });
      request.headers = cleaned;

      return request;
    },

    afterResponse: (request: any, response: { body: any; status: number }) => {
      const latencyMs = Date.now() - (request._psStartTime || Date.now());
      const vendor = detectVendor(request.url, request._psProvider);
      const model = request.body?.model || response.body?.model || 'unknown';
      const psHeaders = request._psHeaders || {};
      const route = request._psRoute; // set by ps-router when it made a decision

      const event: TelemetryEvent = {
        vendor,
        model,
        source: 'gateway',
        tokens_in: response.body?.usage?.prompt_tokens,
        tokens_out: response.body?.usage?.completion_tokens,
        latency_ms: latencyMs,
        ...psHeaders,
      };

      // Cost-aware routing telemetry — proves requested-vs-served savings.
      if (route) {
        event.requested_model = route.requestedModel;
        // The provider echoes the concrete model it ran; prefer that as served.
        event.served_model = response.body?.model || route.servedModel;
        event.route_group = route.group;
        event.route_reason = route.reason;
        event.route_est_cost = route.estCost;
      }

      // Fire-and-forget
      sendTelemetry(event).catch(() => {});

      return response;
    },
  };
}

/**
 * Hono adapter — emits one telemetry event per request after the response is
 * produced. Reads token usage + served model from a non-streaming JSON body,
 * merges business `X-PS-*` headers, and folds in the routing decision from
 * `c.get('psRoute')` (set by ps-router). Fire-and-forget and fail-open.
 *
 * Mount in src/index.ts AFTER psRouter so `psRoute` is available:
 *   app.use('*', psTelemetry());
 */
export function psTelemetry() {
  return async (c: any, next: any) => {
    const startTime = Date.now();
    await next();

    // Only LLM POST traffic is telemetry-worthy; skip health checks / GETs.
    if (c.req.method !== 'POST') return;

    try {
      const url = c.req.url as string;
      const headers = Object.fromEntries(c.req.raw.headers) as Record<
        string,
        string
      >;
      const vendor = detectVendor(url);
      const psHeaders = extractPSHeaders(headers);
      const route = c.get('psRoute');

      // Parse the response body only when it is non-streaming JSON.
      let responseBody: any = undefined;
      const resType = c.res.headers.get('content-type') || '';
      if (resType.includes('application/json')) {
        try {
          responseBody = await c.res.clone().json();
        } catch {
          responseBody = undefined;
        }
      }

      // The requested model comes from the (possibly rewritten) request body.
      let requestBody: any = undefined;
      try {
        requestBody = await c.req.json();
      } catch {
        requestBody = undefined;
      }

      const model =
        route?.requestedModel ||
        requestBody?.model ||
        responseBody?.model ||
        'unknown';

      const event: TelemetryEvent = {
        vendor,
        model,
        source: 'gateway',
        tokens_in: responseBody?.usage?.prompt_tokens,
        tokens_out: responseBody?.usage?.completion_tokens,
        latency_ms: Date.now() - startTime,
        ...psHeaders,
      };

      if (route) {
        event.requested_model = route.requestedModel;
        event.served_model = responseBody?.model || route.servedModel;
        event.route_group = route.group;
        event.route_reason = route.reason;
        event.route_est_cost = route.estCost;
      }

      sendTelemetry(event).catch(() => {});
    } catch (err) {
      // Fail-open: telemetry must never affect the response.
      console.warn('[PS Telemetry] hono adapter skipped:', err);
    }
  };
}

export {
  TelemetryEvent,
  PSConfig,
  detectVendor,
  extractPSHeaders,
  sendTelemetry,
};
