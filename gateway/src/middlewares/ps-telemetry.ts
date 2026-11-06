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
import {
  CALL_ID_HEADER,
  callIdOf,
  FEEDBACK_PATH,
  setCallIdHeader,
} from './psFeedback';
import { applyBaseline, getBaseline } from './router/baseline';
import { RESPONSE_HEADER_KEYS } from '../globals';
import {
  OUTCOME_SIGNALS_VERSION,
  OutcomeInputs,
  hasRefusal,
  outcomeSignals,
  stopReasonsOf,
} from './psOutcomeSignals';
import {
  ROUTE_POLICY_KEY,
  ServedTarget,
  servedJurisdiction,
  servedTargetOf,
} from './psServedTarget';
import { RoutePolicy } from './router/types';
import {
  COST_SOURCE_CACHE,
  COST_SOURCE_PROVIDER_REPORTED,
  costFields,
  PricingUsage,
} from './psPricing';

// Set by psOidc (./psOidc/index.ts OIDC_IDENTITY_KEY).
const OIDC_IDENTITY_KEY = 'psOidcIdentity';
// Set by recordCredentialFingerprint (../utils/credentialFingerprint.ts).
const CREDENTIAL_FINGERPRINT_KEY = 'psCredentialFingerprint';

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
  // cost: the provider-reported one, else the SDK's table price (withCost);
  // cost_source says which.
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
  // gateway when ps-router made the decision (PRD 0001 §7.12).
  route_mode?: string;
  route_group?: string;
  route_reason?: string;
  route_est_cost?: number;
  // Sensitivity-aware routing (#18): the call's effective level and whether
  // it was declared, the policy default, or raised by detected PII.
  route_sensitivity_level?: string;
  route_sensitivity_source?: string;
  // The provider that served the call (#20) and the jurisdiction the active
  // route policy states for the served model there (psServedTarget.ts).
  served_provider?: string;
  served_jurisdiction?: string;
  // Verified savings (#24): the frozen baseline (PS_BASELINE_FILE) and what
  // the call's tokens would have cost on its baseline model, in USD.
  baseline_id?: string;
  counterfactual_cost?: number;
  // Response cache (#33): the call's lookup, see cacheLookupFields.
  cache_status?: string;
  est_tokens_saved?: number;
  // On a cache hit (#33): the upstream cost the hit avoided, in USD.
  cache_saved_cost?: number;
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
  // Stable fingerprint of the upstream credential's identity (#76), only
  // with PS_TELEMETRY_CREDENTIAL_FINGERPRINT=true; see
  // utils/credentialFingerprint.ts. Never a key or token.
  api_key_fingerprint?: string;
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
  // Per-call id (#43): the caller's X-PS-Call-Id or a new UUID, also
  // returned as the X-PS-Call-Id response header. POST /v1/feedback names
  // the call by it.
  call_id?: string;
  // The error class of each failed attempt, in order (PRD 0001 §7.12):
  // empty when the first attempt succeeded. Classes only, never messages.
  fallback_chain?: string[];
  // Spend budgets (#28, psBudget; schemas/budget/README.md): the call's
  // budget state (ok, warn, unknown, exceeded), the budget that set it, and
  // what the gateway did (none, downgraded, or blocked on the event of a
  // refused call, see budgetBlockedEvent).
  budget_state?: string;
  budget_id?: string;
  budget_action?: string;
  // Implicit outcome signals (#44): the classes the gateway can see
  // (fallback, max_tokens, refusal, stream_error, abandoned), with the
  // SDK's version number. Class names only; see psOutcomeSignals.ts.
  outcome_signals?: string[];
  outcome_signals_version?: number;
}

/**
 * `served_provider` and `served_jurisdiction` (#20): the target tryPost
 * recorded as serving the call, else the request's provider; the
 * jurisdiction only when the active route policy states one for the served
 * model at that target.
 */
export function servedFields(
  c: any,
  vendor: string,
  fallbackModel?: string
): Pick<TelemetryEvent, 'served_provider' | 'served_jurisdiction'> {
  const target: ServedTarget = { ...(servedTargetOf(c) || {}) };
  if (!target.provider && vendor && vendor !== 'unknown')
    target.provider = vendor;
  if (!target.provider) return {};
  const out: Pick<TelemetryEvent, 'served_provider' | 'served_jurisdiction'> = {
    served_provider: target.provider.toLowerCase(),
  };
  let policy: RoutePolicy | undefined;
  try {
    policy = c.get(ROUTE_POLICY_KEY);
  } catch {
    policy = undefined;
  }
  const jurisdiction = servedJurisdiction(
    policy,
    target.model || fallbackModel,
    target
  );
  if (jurisdiction) out.served_jurisdiction = jurisdiction;
  return out;
}

/** The error classes of the failed attempts, in order. */
export function fallbackChainOf(attempts: AttemptRecord[]): string[] {
  return attempts
    .map((a) => a.error_class)
    .filter((c): c is string => typeof c === 'string');
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

export { COST_SOURCE_PROVIDER_REPORTED };

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
 * `cost_source` accompanies a cost only. A cost the provider reported is
 * sent here (`cost_source: provider_reported`); otherwise `withCost` prices
 * the event from its usage once the served model is known.
 */
export function usageDetailFields(
  details: UsageDetails | undefined
): Partial<TelemetryEvent> {
  if (!details) return {};
  const { provider_cost, ...rest } = details;
  const out: Partial<TelemetryEvent> = { ...rest };
  if (provider_cost !== undefined) {
    Object.assign(out, costFields('', undefined, undefined, { provider_cost }));
  }
  return out;
}

/**
 * `event` with its `cost` and `cost_source: pricing_table` (#71), priced as
 * the SDK prices the same usage (psPricing.ts, checked against the shared
 * cost vectors): at the served model, then the served id without its
 * version date, then the requested model. A provider-reported cost is kept;
 * a call with no price stays without a cost (unmetered, not zero).
 */
export function withCost(
  event: TelemetryEvent,
  servedModel?: string
): TelemetryEvent {
  if (event.cost !== undefined) return event;
  const usage: PricingUsage = {
    tokens_in: event.tokens_in,
    tokens_out: event.tokens_out,
    tokens_cached: event.tokens_cached,
    tokens_cache_write: event.tokens_cache_write,
    tokens_cache_write_1h: event.tokens_cache_write_1h,
    service_tier: event.service_tier,
    usage_units: event.usage_units,
    usage_unit_type: event.usage_unit_type,
  };
  const model = event.model === 'unknown' ? undefined : event.model;
  return {
    ...event,
    ...costFields(event.vendor, model, servedModel, usage),
  };
}

/**
 * A response served from the gateway cache (#33): `cost` 0 with
 * `cost_source: cache`, the cache status, and `cache_saved_cost`, the price
 * of the cached response's usage (what the hit avoided), when it has one.
 */
export function withCacheHit(
  event: TelemetryEvent,
  cacheStatus: string,
  servedModel?: string
): TelemetryEvent {
  const saved = withCost(event, servedModel).cost;
  const out: TelemetryEvent = {
    ...event,
    cache_status: cacheStatus,
    cost: 0,
    cost_source: COST_SOURCE_CACHE,
  };
  if (saved !== undefined) out.cache_saved_cost = saved;
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
  await psTelemetrySender.send(withBaseline(event));
}

/**
 * A routed event (ps-router made the decision) with `baseline_id` and
 * `counterfactual_cost` from the gateway's baseline, when one is loaded.
 * Applied at send time, so streamed events are priced on their final usage.
 */
export function withBaseline(event: TelemetryEvent): TelemetryEvent {
  const baseline = getBaseline();
  if (!baseline || event.route_mode !== 'gateway') return event;
  try {
    const out = { ...event };
    applyBaseline(baseline, out);
    return out;
  } catch {
    return event; // fail-open
  }
}

/**
 * The cache lookup of a call (#33), for its own event: `cache_status` for
 * every lookup the cache made (not `DISABLED`), and `est_tokens_saved`, a
 * rough estimate of the prompt tokens a hit did not send (request JSON
 * characters / 4; 0 on a miss). The call's event carries it, once per
 * request, so a lookup is reported with the call's attribution and is never
 * a second event: there is no separate cache-only event (it used to be sent
 * per lookup, and per failover attempt). `status` is the final response's
 * cache status header, so a call that tried several targets reports the
 * lookup of the attempt that answered.
 */
export function cacheLookupFields(
  status: string,
  requestBody: any
): Pick<TelemetryEvent, 'cache_status' | 'est_tokens_saved'> {
  if (!status || status === 'DISABLED') return {};
  return {
    cache_status: status,
    est_tokens_saved: status.endsWith('HIT')
      ? Math.ceil(JSON.stringify(requestBody ?? {}).length / 4)
      : 0,
  };
}

/** The final cache status of a response (the last one, if repeated). */
export function cacheStatusOf(res: Response): string {
  const raw = res.headers.get(RESPONSE_HEADER_KEYS.CACHE_STATUS) || '';
  const parts = raw.split(',');
  return parts[parts.length - 1].trim();
}

/** `waitUntil` of the Workers execution context; a no-op on Node. */
function waitUntilOf(c: any): (p: Promise<unknown>) => void {
  try {
    const ctx = c.executionCtx;
    if (ctx?.waitUntil) return (p) => ctx.waitUntil(p);
  } catch {
    /* no execution context */
  }
  return () => {};
}

/**
 * The event of a call psBudget refused (#28): HTTP 429 `budget_exceeded`
 * (`budget_state: exceeded`) or 503 `budget_store_unavailable` with
 * `on_store_error: closed` (`budget_state: unknown`), both with
 * `budget_action: blocked`. No provider was called, so it has no usage,
 * latency, cost, served provider or outcome signals; it carries the call's
 * attribution, the requested model and the call id (also set on the
 * response), so the collector can count blocked calls per asset. It is not
 * a security event (psSecurityEvents counts budget 429s in a metric only).
 */
export function budgetBlockedEvent(
  c: any,
  opts: {
    vendor: string;
    model?: string;
    state: 'exceeded' | 'unknown';
    budgetId: string;
  }
): TelemetryEvent {
  const headers = Object.fromEntries(c.req.raw.headers) as Record<
    string,
    string
  >;
  const path = new URL(c.req.url).pathname;
  const event: TelemetryEvent = {
    vendor: opts.vendor,
    model: opts.model || 'unknown',
    source: 'gateway',
    api_surface: apiSurfaceOf(path),
    modality: modalityOf(path),
    ...extractPSHeaders(headers),
    ...oidcTelemetryFields(c.get(OIDC_IDENTITY_KEY)),
    call_id: callIdOf(c),
    budget_state: opts.state,
    budget_id: opts.budgetId,
    budget_action: 'blocked',
  };
  const fingerprint = c.get(CREDENTIAL_FINGERPRINT_KEY);
  if (typeof fingerprint === 'string' && fingerprint) {
    event.api_key_fingerprint = fingerprint;
  }
  return event;
}

/**
 * Sends the event of a refused call (budgetBlockedEvent) and sets its
 * X-PS-Call-Id on `res`. Fire-and-forget and fail-open: returns `res`.
 */
export function emitBudgetBlocked(
  c: any,
  res: Response,
  opts: Parameters<typeof budgetBlockedEvent>[1]
): Response {
  try {
    const event = budgetBlockedEvent(c, opts);
    try {
      res.headers.set(CALL_ID_HEADER, event.call_id!);
    } catch {
      /* immutable headers: the event still names the call */
    }
    waitUntilOf(c)(sendTelemetry(event).catch(() => {}));
  } catch (err) {
    console.warn('[PS Telemetry] budget refusal event skipped:', err);
  }
  return res;
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
        event.route_mode = 'gateway';
        event.route_reason = route.reason;
        event.route_est_cost = route.estCost;
        if (route.sensitivityLevel)
          event.route_sensitivity_level = route.sensitivityLevel;
        if (route.sensitivitySource)
          event.route_sensitivity_source = route.sensitivitySource;
      }

      // Fire-and-forget
      sendTelemetry(
        withCost(event, response.body?.model || route?.servedModel)
      ).catch(() => {});

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

// API surfaces that generate text, where outcome signals mean something.
const GENERATION_SURFACES = new Set([
  'chat',
  'completions',
  'messages',
  'responses',
]);

/** A streamed call's event with its outcome signals, on generation surfaces. */
export function streamOutcome(
  event: TelemetryEvent,
  summary: StreamSummary,
  generation: boolean
): TelemetryEvent {
  if (!generation) return event;
  return withOutcomeSignals(event, {
    stopReasons: summary.stopReasons,
    refusal: summary.refusal,
    fallbackChain: event.fallback_chain,
    streamTruncated: summary.truncated,
    streamCancelled: summary.cancelled,
  });
}

/** Sets `outcome_signals` (and its version) on an LLM call's event. */
export function withOutcomeSignals(
  event: TelemetryEvent,
  inputs: OutcomeInputs
): TelemetryEvent {
  return {
    ...event,
    outcome_signals: outcomeSignals(inputs),
    outcome_signals_version: OUTCOME_SIGNALS_VERSION,
  };
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
    // Feedback is not an LLM call (psFeedback.ts forwards it).
    if (c.req.path === FEEDBACK_PATH) return;

    try {
      const callId = callIdOf(c);
      setCallIdHeader(c, callId);

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
        call_id: callId,
      };

      const fingerprint = c.get(CREDENTIAL_FINGERPRINT_KEY);
      if (typeof fingerprint === 'string' && fingerprint) {
        event.api_key_fingerprint = fingerprint;
      }

      if (route) {
        event.requested_model = route.requestedModel;
        event.served_model = responseBody?.model || route.servedModel;
        event.route_group = route.group;
        event.route_mode = 'gateway';
        event.route_reason = route.reason;
        event.route_est_cost = route.estCost;
        if (route.sensitivityLevel)
          event.route_sensitivity_level = route.sensitivityLevel;
        if (route.sensitivitySource)
          event.route_sensitivity_source = route.sensitivitySource;
      }

      const budget = c.get('psBudget');
      if (budget) {
        event.budget_state = budget.state;
        event.budget_action = budget.action;
        if (budget.budgetId) event.budget_id = budget.budgetId;
      }

      Object.assign(
        event,
        servedFields(
          c,
          vendor,
          route?.servedModel || requestBody?.model || responseBody?.model
        )
      );

      const attempts = attemptsOf(c);
      if (attempts.length) {
        // The caller's trace id links the event to the gateway logs.
        event.trace_id = traceIdOf(headers['x-portkey-trace-id']);
        event.retry_count = Math.max(attemptCountOf(c), attempts.length) - 1;
        event.attempts = attempts.map((a) => ({ ...a }));
        event.fallback_chain = fallbackChainOf(attempts);
      }

      // A streamed response carries its usage at the end of the stream, so
      // the event is sent once the client has received the whole stream.
      // On Workers, keep the isolate alive until the send settles (Node has
      // no execution context: the sender is flushed at shutdown instead).
      const waitUntil = waitUntilOf(c);
      // The model that ran prices the call (SDK: served, then requested).
      // A response served from the gateway cache cost nothing upstream: it
      // is recorded at cost 0 with `cost_source: cache` and what it saved.
      const served = responseBody?.model || route?.servedModel;
      const cacheStatus = cacheStatusOf(c.res);
      const cacheHit = cacheStatus.endsWith('HIT');
      Object.assign(event, cacheLookupFields(cacheStatus, requestBody));
      const priced = (e: TelemetryEvent, m?: string) =>
        cacheHit ? withCacheHit(e, cacheStatus, m) : withCost(e, m);
      const stream = observeStream(c);
      // A cache hit is not a model call: its stop reasons and refusals
      // belong to the call that filled the cache (which reported them), so
      // it carries no outcome signals rather than counting them twice.
      const generation =
        !!surface && GENERATION_SURFACES.has(surface) && !cacheHit;
      if (!stream) {
        const outcome = generation
          ? withOutcomeSignals(event, {
              stopReasons: stopReasonsOf(responseBody),
              refusal: hasRefusal(responseBody),
              fallbackChain: event.fallback_chain,
            })
          : event;
        waitUntil(sendTelemetry(priced(outcome, served)).catch(() => {}));
        return;
      }
      waitUntil(
        stream
          .then((summary) =>
            sendTelemetry(
              priced(
                streamOutcome(
                  withStreamSummary(
                    event,
                    summary,
                    !!route,
                    !adapted,
                    zeroFilled
                  ),
                  summary,
                  generation
                ),
                summary.model || served
              )
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
