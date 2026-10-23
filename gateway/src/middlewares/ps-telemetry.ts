/**
 * Prompt Shields Telemetry Middleware
 * Intercepts LLM requests/responses and sends discovery metadata
 * to the Prompt Shields collector service.
 */

import {
  inputTokensOf,
  observeStream,
  StreamSummary,
  UsageDetails,
  usageDetailsOf,
} from './psStreamObserver';
import { supportsResponsesApiNatively } from '../adapters/responses';
import {
  AttemptRecord,
  attemptCountOf,
  attemptsOf,
  traceIdOf,
} from './psAttempts';
import { PSTelemetrySender } from './psTelemetrySender';
import { isMcpProxyPath } from '../mcp/paths';
import { stripPSInternalHeaders } from '../utils/psInternalHeaders';
import { oidcTelemetryFields } from './psOidc/telemetry';

// Set by psOidc (./psOidc/index.ts OIDC_IDENTITY_KEY).
const OIDC_IDENTITY_KEY = 'psOidcIdentity';

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
  // Usage detail (#9), as the SDK reports it: cached, cache-write (and its
  // 1-hour subset) and reasoning counts, the provider's service tier, and a
  // cost only when the provider reported one (cost_source says so).
  tokens_cached?: number;
  tokens_cache_write?: number;
  tokens_cache_write_1h?: number;
  tokens_reasoning?: number;
  service_tier?: string;
  cost?: number;
  cost_source?: string;
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
  // Sensitivity-aware routing (#18): the call's effective level and whether
  // it was declared, the policy default, or raised by detected PII.
  route_sensitivity_level?: string;
  route_sensitivity_source?: string;
  // Exact-match cache (populated by emitCacheTelemetry)
  cache_status?: string;
  est_tokens_saved?: number;
  // Which API the caller spoke (chat, messages, responses, ...). With the
  // Messages/Responses adapters (#59) this can differ from what the provider
  // served, so it is recorded separately from vendor.
  api_surface?: string;
  // What the asset works on (text, audio, image, document), so assets found
  // on the audio and OCR endpoints are registered with the right modality.
  modality?: string;
  // Non-token usage: audio seconds, OCR pages or speech characters.
  usage_units?: number;
  usage_unit_type?: string;
  // Streamed (SSE) responses (#72): true when the upstream closed without a
  // terminal event ([DONE], message_stop, response.completed, ...), and
  // when the client disconnected before the stream ended.
  stream?: boolean;
  stream_truncated?: boolean;
  stream_cancelled?: boolean;
  // OIDC (PS_OIDC_ENABLED): the caller's pseudonymous subject and the name
  // of the gateway policy group that applied. See psOidc/telemetry.ts.
  user_id?: string;
  user_group?: string;
  // Retries and fallbacks (#78): every upstream attempt, in order, linked by
  // trace_id. retry_count is the number of attempts - 1; attempts holds at
  // most MAX_ATTEMPT_RECORDS of them.
  trace_id?: string;
  retry_count?: number;
  attempts?: AttemptRecord[];
}

/** The API surface of a gateway request, from its path. */
export function apiSurfaceOf(path: string): string | undefined {
  const m = path.match(
    /^\/v1\/(chat\/completions|completions|messages|responses|embeddings|rerank|images|audio|ocr)/
  );
  if (!m) return undefined;
  return m[1] === 'chat/completions' ? 'chat' : m[1];
}

/** The modality of a gateway request, from its path. */
export function modalityOf(path: string): string | undefined {
  const surface = apiSurfaceOf(path);
  if (!surface) return undefined;
  if (surface === 'audio') return 'audio';
  if (surface === 'images') return 'image';
  if (surface === 'ocr') return 'document';
  return 'text';
}

/**
 * Non-token usage: transcription `usage: {type: 'duration', seconds}`,
 * Mistral OCR `usage_info.pages_processed`, rerank `usage.search_units`
 * (Cohere search units, Voyage/Jina tokens, Pinecone rerank units, as the
 * provider bills them), or text-to-speech characters from the request `input`.
 */
export function usageUnits(
  path: string,
  responseBody: any,
  requestBody: any
): { usage_units?: number; usage_unit_type?: string } {
  const u = responseBody?.usage;
  if (u?.type === 'duration' && typeof u.seconds === 'number') {
    return { usage_units: u.seconds, usage_unit_type: 'seconds' };
  }
  if (path.startsWith('/v1/rerank') && typeof u?.search_units === 'number') {
    return { usage_units: u.search_units, usage_unit_type: 'search_units' };
  }
  const pages = responseBody?.usage_info?.pages_processed;
  if (typeof pages === 'number') {
    return { usage_units: pages, usage_unit_type: 'pages' };
  }
  if (
    path.startsWith('/v1/audio/speech') &&
    typeof requestBody?.input === 'string'
  ) {
    return {
      usage_units: requestBody.input.length,
      usage_unit_type: 'characters',
    };
  }
  return {};
}

/**
 * The request body as telemetry needs it, read after the handler ran. JSON
 * bodies come from Hono's body cache. For a multipart upload (audio
 * transcription or translation, image edit) only the `model` field is taken from the
 * form the handler already parsed; the form is never re-read or serialised,
 * and no other field (file, prompt) is returned.
 */
export async function requestBodyForTelemetry(c: any): Promise<any> {
  const contentType = (c.req.header('content-type') || '').toLowerCase();
  // Any parsed form (multipart or urlencoded): never serialise it back via
  // json(), which would copy the whole upload into a string.
  const cached = c.req.bodyCache?.formData;
  if (cached || contentType.startsWith('multipart/form-data')) {
    if (!cached) return undefined;
    try {
      const form = await cached;
      const model = form?.get?.('model');
      return typeof model === 'string' && model ? { model } : undefined;
    } catch {
      return undefined;
    }
  }
  try {
    return await c.req.json();
  } catch {
    return undefined;
  }
}

/**
 * Cancelling a response (#73) returns the Response object with the usage of
 * the generation it stopped; that usage was already reported by the create
 * call, so it is not counted again.
 */
export function reportsOwnUsage(path: string): boolean {
  return !/^\/v1\/responses\/[^/]+\/cancel\/?$/.test(path);
}

/** Token usage from Chat Completions or Messages/Responses-style usage. */
export function usageTokens(body: any): {
  tokens_in?: number;
  tokens_out?: number;
} {
  const u = body?.usage;
  if (!u || typeof u !== 'object') return {};
  return {
    tokens_in: inputTokensOf(u),
    tokens_out: u.completion_tokens ?? u.output_tokens,
  };
}

/** The SDK's `cost_source` for a cost the provider billed (pricing.py). */
export const COST_SOURCE_PROVIDER_REPORTED = 'provider_reported';

/**
 * Usage details of a non-streamed response body. The tier is top level on
 * Chat Completions and Responses bodies, inside `usage` on Anthropic's.
 * `nativeTier` is false when the body was built by the Responses adapter,
 * which echoes the requested tier (or `default`) rather than a served one.
 */
export function usageDetails(
  body: any,
  nativeTier = true,
  zeroFilled = false
): UsageDetails {
  const details = usageDetailsOf(body?.usage);
  const tier = body?.service_tier;
  if (typeof tier === 'string' && tier) details.service_tier = tier;
  if (!nativeTier) delete details.service_tier;
  return zeroFilled ? withoutFilledZeros(details) : details;
}

/**
 * Providers whose usage the gateway rebuilds in Chat Completions shape and
 * fills a count the provider left out with 0: Gemini and Vertex
 * (`thoughtsTokenCount`, `cachedContentTokenCount`) and Bedrock Converse
 * (`cacheReadInputTokens`). The Responses adapter does the same for any
 * provider. The SDK omits a count the provider did not report.
 */
const ZERO_FILLED_VENDORS = new Set(['google', 'vertex-ai', 'bedrock']);

/** Whether a zero cached or reasoning count may be a filled-in default. */
export function usageZeroFilled(vendor: string, adapted: boolean): boolean {
  return adapted || ZERO_FILLED_VENDORS.has(vendor);
}

/** `details` without zero cached and reasoning counts. */
export function withoutFilledZeros(details: UsageDetails): UsageDetails {
  const out = { ...details };
  if (out.tokens_cached === 0) delete out.tokens_cached;
  if (out.tokens_reasoning === 0) delete out.tokens_reasoning;
  return out;
}

/**
 * Event fields for usage details. Like the SDK's `add_usage_detail_fields`,
 * `cost_source` accompanies a cost only: the gateway prices nothing itself,
 * so a cost (and `cost_source: provider_reported`) is sent only when the
 * provider reported what it billed.
 */
export function usageDetailFields(
  details: UsageDetails | undefined
): Partial<TelemetryEvent> {
  if (!details) return {};
  const { provider_cost, ...rest } = details;
  const out: Partial<TelemetryEvent> = { ...rest };
  if (provider_cost !== undefined) {
    out.cost = provider_cost;
    out.cost_source = COST_SOURCE_PROVIDER_REPORTED;
  }
  return out;
}

/**
 * One sender per process: its client id, batch seq and drop counters are
 * what the collector reconciles in /telemetry/loss (#87).
 */
export const psTelemetrySender = PSTelemetrySender.fromEnv(process.env);

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
 * The provider the gateway will call, from `x-portkey-provider` or the
 * provider of an `x-portkey-config` (top level, else its first target).
 * The gateway's own URL never names the vendor, so without this every
 * gateway event would be reported as `unknown`.
 */
export function providerFromHeaders(
  headers: Record<string, string | undefined>
): string | undefined {
  const direct = headers['x-portkey-provider'];
  if (direct) return direct;
  const rawConfig = headers['x-portkey-config'];
  if (!rawConfig) return undefined;
  try {
    const config = JSON.parse(rawConfig);
    const provider = config?.provider ?? config?.targets?.[0]?.provider;
    return typeof provider === 'string' ? provider : undefined;
  } catch {
    return undefined;
  }
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
 * Send telemetry event to PS collector (fire-and-forget, fail-open). Retries
 * and loss accounting are in psTelemetrySender; this never rejects.
 */
async function sendTelemetry(event: TelemetryEvent): Promise<void> {
  // Fail-open: never block LLM requests due to telemetry issues
  await psTelemetrySender.send(event);
}

export function buildCacheEvent(
  cacheStatus: string,
  requestBody: any,
  vendor: string
): TelemetryEvent {
  const isHit = cacheStatus === 'HIT' || cacheStatus === 'SEMANTIC HIT';
  const estTokens = isHit
    ? Math.ceil(JSON.stringify(requestBody ?? {}).length / 4)
    : 0;
  return {
    vendor,
    model: requestBody?.model || 'unknown',
    source: 'gateway',
    cache_status: cacheStatus,
    est_tokens_saved: estTokens,
  };
}

// Fail-open emit from a spot that already knows cacheStatus (handlerUtils).
export function emitCacheTelemetry(
  cacheStatus: string,
  requestBody: any,
  vendor: string
): void {
  try {
    if (!cacheStatus || cacheStatus === 'DISABLED') return;
    sendTelemetry(buildCacheEvent(cacheStatus, requestBody, vendor)).catch(
      () => {}
    );
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

      // Strip PS headers from this request object. Note: this does not reach
      // the real upstream call; the gateway strips X-PS-* centrally where
      // upstream headers are finalised (utils/psInternalHeaders.ts).
      request.headers = stripPSInternalHeaders(request.headers);

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
        ...usageTokens(response.body),
        ...usageDetailFields(
          usageDetails(response.body, true, usageZeroFilled(vendor, false))
        ),
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
        if (route.sensitivityLevel)
          event.route_sensitivity_level = route.sensitivityLevel;
        if (route.sensitivitySource)
          event.route_sensitivity_source = route.sensitivitySource;
      }

      // Fire-and-forget
      sendTelemetry(event).catch(() => {});

      return response;
    },
  };
}

/** Folds a stream's usage and completion state into its telemetry event. */
export function withStreamSummary(
  event: TelemetryEvent,
  summary: StreamSummary,
  routed = false,
  nativeTier = true,
  zeroFilled = false
): TelemetryEvent {
  const out: TelemetryEvent = { ...event, stream: true };
  if (summary.tokens_in !== undefined) out.tokens_in = summary.tokens_in;
  if (summary.tokens_out !== undefined) out.tokens_out = summary.tokens_out;
  let details = summary.details && { ...summary.details };
  if (details && !nativeTier) delete details.service_tier;
  if (details && zeroFilled) details = withoutFilledZeros(details);
  Object.assign(out, usageDetailFields(details));
  if (routed && summary.model) out.served_model = summary.model;
  if (summary.truncated) out.stream_truncated = true;
  if (summary.cancelled) out.stream_cancelled = true;
  return out;
}

/**
 * Hono adapter — emits one telemetry event per request after the response is
 * produced. Reads token usage + served model from a JSON body or, for a
 * streamed (SSE) response, from the stream as it passes to the client (the
 * event is then sent when the stream ends, marked if it was truncated). It
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
    // MCP proxy calls are reported to /ingest/mcp by the MCP handler.
    if (isMcpProxyPath(c.req.path)) return;

    try {
      const url = c.req.url as string;
      const headers = Object.fromEntries(c.req.raw.headers) as Record<
        string,
        string
      >;
      const vendor = detectVendor(url, providerFromHeaders(headers));
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
      const requestBody = await requestBodyForTelemetry(c);

      const model =
        route?.requestedModel ||
        requestBody?.model ||
        responseBody?.model ||
        'unknown';

      const path = new URL(url).pathname;
      const surface = apiSurfaceOf(path);
      // The Responses adapter (#59) reports no served tier of its own.
      const adapted =
        surface === 'responses' && !supportsResponsesApiNatively(vendor);
      const zeroFilled = usageZeroFilled(vendor, adapted);
      const event: TelemetryEvent = {
        vendor,
        model,
        source: 'gateway',
        ...(reportsOwnUsage(path)
          ? {
              ...usageTokens(responseBody),
              ...usageDetailFields(
                usageDetails(responseBody, !adapted, zeroFilled)
              ),
            }
          : {}),
        api_surface: surface,
        modality: modalityOf(path),
        ...usageUnits(path, responseBody, requestBody),
        latency_ms: Date.now() - startTime,
        ...psHeaders,
        ...oidcTelemetryFields(c.get(OIDC_IDENTITY_KEY)),
      };

      if (route) {
        event.requested_model = route.requestedModel;
        event.served_model = responseBody?.model || route.servedModel;
        event.route_group = route.group;
        event.route_reason = route.reason;
        event.route_est_cost = route.estCost;
        if (route.sensitivityLevel)
          event.route_sensitivity_level = route.sensitivityLevel;
        if (route.sensitivitySource)
          event.route_sensitivity_source = route.sensitivitySource;
      }

      const attempts = attemptsOf(c);
      if (attempts.length) {
        // The caller's trace id links the event to the gateway logs.
        event.trace_id = traceIdOf(headers['x-portkey-trace-id']);
        event.retry_count = Math.max(attemptCountOf(c), attempts.length) - 1;
        event.attempts = attempts.map((a) => ({ ...a }));
      }

      // A streamed response carries its usage at the end of the stream, so
      // the event is sent once the client has received the whole stream.
      // On Workers, keep the isolate alive until the send settles (Node has
      // no execution context: the sender is flushed at shutdown instead).
      let waitUntil: (p: Promise<unknown>) => void = () => {};
      try {
        const ctx = c.executionCtx;
        if (ctx?.waitUntil) waitUntil = (p) => ctx.waitUntil(p);
      } catch {
        /* no execution context */
      }
      const stream = observeStream(c);
      if (!stream) {
        waitUntil(sendTelemetry(event).catch(() => {}));
        return;
      }
      waitUntil(
        stream
          .then((summary) =>
            sendTelemetry(
              withStreamSummary(event, summary, !!route, !adapted, zeroFilled)
            )
          )
          .catch(() => {})
      );
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
