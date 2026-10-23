/**
 * OpenTelemetry GenAI spans from the gateway (#70).
 *
 * Opt-in: nothing is built or sent unless `PS_OTEL_ENDPOINT` (or the standard
 * `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`) is set. One CLIENT span per LLM POST,
 * with attributes from the OTel GenAI semantic conventions 1.40
 * (`gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.*`,
 * `gen_ai.response.*`, `gen_ai.usage.*`, `error.type`), exported as
 * OTLP/HTTP JSON. The collector's `/ingest/otel/v1/traces` reads the same
 * names (`packages/collector/collector/otel_mapping.py`).
 *
 * Privacy: prompt and completion content (`gen_ai.input.messages`,
 * `gen_ai.output.messages`, `gen_ai.system_instructions`) is only attached
 * when `PS_OTEL_CAPTURE_CONTENT=true` (or the standard
 * `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`), matching the
 * SDK's `send_prompt_text=False` default.
 *
 * No dependencies: spans are plain OTLP JSON and sent with `fetch`, so this
 * runs on Node and Cloudflare Workers alike. Export is batched and fail-open.
 */

import {
  apiSurfaceOf,
  extractPSHeaders,
  providerFromHeaders,
  reportsOwnUsage,
  requestBodyForTelemetry,
} from './ps-telemetry';
import {
  inputTokensOf,
  observeStream,
  StreamSummary,
} from './psStreamObserver';
import { telemetryDroppedTotal } from './psMetrics/security';

export interface OtelConfig {
  endpoint: string;
  headers: Record<string, string>;
  serviceName: string;
  captureContent: boolean;
  maxBatch: number;
  flushMs: number;
  maxQueue: number;
}

const truthy = (v?: string) => /^(1|true|yes|on)$/i.test((v || '').trim());

const posInt = (v: string | undefined, dflt: number) => {
  const n = Number(v);
  return Number.isInteger(n) && n > 0 ? n : dflt;
};

/** `k1=v1,k2=v2` as in OTEL_EXPORTER_OTLP_HEADERS. */
export function parseHeaderList(raw?: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const pair of (raw || '').split(',')) {
    const i = pair.indexOf('=');
    if (i <= 0) continue;
    const k = pair.slice(0, i).trim();
    const v = pair.slice(i + 1).trim();
    if (!k) continue;
    try {
      out[k] = decodeURIComponent(v);
    } catch {
      // Malformed %-escape: keep the raw value rather than crash startup.
      out[k] = v;
    }
  }
  return out;
}

/** Exporter settings from the environment, or null when OTel is off. */
export function otelConfig(
  env: Record<string, string | undefined> = process.env
): OtelConfig | null {
  const psEndpoint = (env.PS_OTEL_ENDPOINT || '').trim();
  const endpoint =
    psEndpoint || (env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT || '').trim();
  if (!endpoint) return null;
  const headers = parseHeaderList(
    env.PS_OTEL_HEADERS || env.OTEL_EXPORTER_OTLP_TRACES_HEADERS
  );
  // Sending to the PS collector: reuse the gateway's tenant ingest key. Only
  // for the PS-specific PS_OTEL_ENDPOINT: the generic OTEL_* endpoint is often
  // set platform-wide for a third-party backend, which must never receive it.
  if (
    psEndpoint &&
    env.PS_API_KEY &&
    !Object.keys(headers).some((k) => k.toLowerCase() === 'authorization')
  ) {
    headers.Authorization = `Bearer ${env.PS_API_KEY}`;
  }
  return {
    endpoint,
    headers,
    serviceName: env.OTEL_SERVICE_NAME || 'prompt-shields-gateway',
    captureContent:
      truthy(env.PS_OTEL_CAPTURE_CONTENT) ||
      truthy(env.OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT),
    maxBatch: posInt(env.PS_OTEL_MAX_BATCH, 64),
    flushMs: posInt(env.PS_OTEL_FLUSH_MS, 5000),
    maxQueue: posInt(env.PS_OTEL_MAX_QUEUE, 2048),
  };
}

// ---------------------------------------------------------------------------
// Semantic-convention mapping
// ---------------------------------------------------------------------------

/**
 * Gateway provider ids to `gen_ai.provider.name` well-known values (semconv
 * 1.40). Anything else is passed through as-is.
 */
const PROVIDER_NAMES: Record<string, string> = {
  openai: 'openai',
  anthropic: 'anthropic',
  bedrock: 'aws.bedrock',
  'azure-openai': 'azure.ai.openai',
  'azure-ai': 'azure.ai.inference',
  google: 'gcp.gemini',
  'vertex-ai': 'gcp.vertex_ai',
  cohere: 'cohere',
  deepseek: 'deepseek',
  groq: 'groq',
  'mistral-ai': 'mistral_ai',
  'perplexity-ai': 'perplexity',
  'x-ai': 'x_ai',
  watsonx: 'ibm.watsonx.ai',
};

export function semconvProviderName(provider?: string): string {
  const p = (provider || '').trim().toLowerCase();
  if (!p) return 'unknown';
  return PROVIDER_NAMES[p] ?? p;
}

/** `gen_ai.operation.name` for a gateway API surface. */
const OPERATION_NAMES: Record<string, string> = {
  chat: 'chat',
  messages: 'chat',
  responses: 'chat',
  completions: 'text_completion',
  embeddings: 'embeddings',
  images: 'generate_content',
};

export function semconvOperationName(surface?: string): string | undefined {
  if (!surface) return undefined;
  return OPERATION_NAMES[surface] ?? surface;
}

/**
 * Provider finish/stop reasons normalised to the semconv output-message
 * values: stop, length, content_filter, tool_call, error.
 */
export function normalizeFinishReason(reason: unknown): string | undefined {
  if (typeof reason !== 'string' || !reason) return undefined;
  switch (reason.toLowerCase()) {
    case 'stop':
    case 'end_turn':
    case 'stop_sequence':
    case 'completed':
    case 'finish_reason_stop':
      return 'stop';
    case 'length':
    case 'max_tokens':
    case 'max_output_tokens':
    case 'model_context_window_exceeded':
      return 'length';
    case 'tool_calls':
    case 'tool_use':
    case 'function_call':
      return 'tool_call';
    case 'content_filter':
    case 'refusal':
    case 'safety':
      return 'content_filter';
    case 'error':
    case 'failed':
      return 'error';
    default:
      return reason.toLowerCase();
  }
}

/** Finish reasons from Chat Completions, Messages or Responses bodies. */
export function finishReasonsOf(body: any): string[] {
  const raw: unknown[] = [];
  if (Array.isArray(body?.choices)) {
    for (const ch of body.choices) raw.push(ch?.finish_reason);
  } else if (body?.stop_reason !== undefined) {
    raw.push(body.stop_reason);
  } else if (body?.object === 'response') {
    raw.push(body.incomplete_details?.reason ?? body.status);
  }
  return raw.map(normalizeFinishReason).filter((r): r is string => !!r);
}

/** `error.type` for a failed request: the provider's error type, else status. */
export function errorTypeOf(status: number, body: any): string | undefined {
  if (status < 400) return undefined;
  const t = body?.error?.type ?? body?.error?.code;
  return typeof t === 'string' && t ? t : String(status);
}

// ---- Content (opt-in only) -------------------------------------------------

function textParts(content: any): any[] {
  if (typeof content === 'string') return [{ type: 'text', content }];
  if (!Array.isArray(content)) return [];
  return content.map((p: any) => {
    if (typeof p === 'string') return { type: 'text', content: p };
    if (
      p?.type === 'text' ||
      p?.type === 'input_text' ||
      p?.type === 'output_text'
    )
      return { type: 'text', content: p.text };
    if (p?.type === 'tool_use')
      return { type: 'tool_call', id: p.id, name: p.name, arguments: p.input };
    if (p?.type === 'tool_result')
      return {
        type: 'tool_call_response',
        id: p.tool_use_id,
        response: p.content,
      };
    return { type: p?.type ?? 'unknown' };
  });
}

/** A chat / Messages message in the semconv 1.40 structured shape. */
function toSemconvMessage(m: any): any {
  const parts = textParts(m?.content);
  for (const tc of Array.isArray(m?.tool_calls) ? m.tool_calls : []) {
    let args: any = tc?.function?.arguments;
    try {
      args = typeof args === 'string' ? JSON.parse(args) : args;
    } catch {
      /* keep the raw string */
    }
    parts.push({
      type: 'tool_call',
      id: tc?.id,
      name: tc?.function?.name,
      arguments: args,
    });
  }
  if (m?.role === 'tool') {
    return {
      role: 'tool',
      parts: [
        { type: 'tool_call_response', id: m.tool_call_id, response: m.content },
      ],
    };
  }
  return { role: m?.role ?? 'user', parts };
}

/** Content attributes (only called when content capture is on). */
export function contentAttributes(
  requestBody: any,
  responseBody: any
): Record<string, string> {
  const out: Record<string, string> = {};
  const system: any[] = [];
  const input: any[] = [];
  if (requestBody?.system !== undefined)
    system.push(...textParts(requestBody.system));
  if (typeof requestBody?.instructions === 'string')
    system.push({ type: 'text', content: requestBody.instructions });
  if (Array.isArray(requestBody?.messages)) {
    for (const m of requestBody.messages) {
      if (m?.role === 'system' || m?.role === 'developer')
        system.push(...textParts(m.content));
      else input.push(toSemconvMessage(m));
    }
  } else if (
    requestBody?.input !== undefined ||
    requestBody?.prompt !== undefined
  ) {
    const raw = requestBody.input ?? requestBody.prompt;
    if (Array.isArray(raw) && raw.every((x: any) => x?.role))
      input.push(...raw.map(toSemconvMessage));
    else
      input.push({
        role: 'user',
        parts: textParts(Array.isArray(raw) ? raw : String(raw)),
      });
  }
  const output: any[] = [];
  if (Array.isArray(responseBody?.choices)) {
    for (const ch of responseBody.choices) {
      const msg = ch?.message
        ? toSemconvMessage(ch.message)
        : { role: 'assistant', parts: textParts(ch?.text ?? '') };
      output.push({
        ...msg,
        finish_reason: normalizeFinishReason(ch?.finish_reason),
      });
    }
  } else if (
    responseBody?.type === 'message' &&
    Array.isArray(responseBody.content)
  ) {
    output.push({
      role: 'assistant',
      parts: textParts(responseBody.content),
      finish_reason: normalizeFinishReason(responseBody.stop_reason),
    });
  } else if (
    responseBody?.object === 'response' &&
    Array.isArray(responseBody.output)
  ) {
    for (const item of responseBody.output) {
      if (item?.type === 'message')
        output.push({ role: 'assistant', parts: textParts(item.content) });
      else if (item?.type === 'function_call')
        output.push({
          role: 'assistant',
          parts: [
            {
              type: 'tool_call',
              id: item.call_id,
              name: item.name,
              arguments: item.arguments,
            },
          ],
        });
    }
  }
  if (system.length) out['gen_ai.system_instructions'] = JSON.stringify(system);
  if (input.length) out['gen_ai.input.messages'] = JSON.stringify(input);
  if (output.length) out['gen_ai.output.messages'] = JSON.stringify(output);
  return out;
}

// ---------------------------------------------------------------------------
// Span building
// ---------------------------------------------------------------------------

type AttrValue = string | number | boolean | string[];

export function toOtlpAttributes(attrs: Record<string, AttrValue | undefined>) {
  const out: any[] = [];
  for (const [key, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === '') continue;
    let value: any;
    if (Array.isArray(v)) {
      if (!v.length) continue;
      value = { arrayValue: { values: v.map((s) => ({ stringValue: s })) } };
    } else if (typeof v === 'boolean') value = { boolValue: v };
    else if (typeof v === 'number')
      value = Number.isInteger(v)
        ? { intValue: String(v) }
        : { doubleValue: v };
    else value = { stringValue: v };
    out.push({ key, value });
  }
  return out;
}

const hex = (bytes: number) => {
  const b = new Uint8Array(bytes);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
};

/** W3C `traceparent` → trace id and parent span id, when well-formed. */
export function parseTraceparent(
  header?: string
): { traceId: string; parentSpanId: string } | undefined {
  const m = (header || '')
    .trim()
    .match(/^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$/);
  if (!m || /^0+$/.test(m[1]) || /^0+$/.test(m[2])) return undefined;
  return { traceId: m[1], parentSpanId: m[2] };
}

const nanos = (ms: number) => `${Math.floor(ms)}000000`;

export interface SpanInput {
  path: string;
  headers: Record<string, string | undefined>;
  status: number;
  startMs: number;
  endMs: number;
  requestBody?: any;
  responseBody?: any;
  stream?: StreamSummary;
  route?: {
    requestedModel?: string;
    group?: string;
    reason?: string;
    sensitivityLevel?: string;
    sensitivitySource?: string;
  };
  captureContent?: boolean;
}

export interface BuiltSpan {
  span: any;
  /** Per-request `deployment.environment.name` (resource attribute). */
  environment?: string;
}

/** One OTLP JSON span for a gateway LLM call, or undefined if not one. */
export function buildGenAISpan(input: SpanInput): BuiltSpan | undefined {
  const surface = apiSurfaceOf(input.path);
  const operation = semconvOperationName(surface);
  if (!operation) return undefined;

  const req = input.requestBody;
  const res = input.responseBody;
  const requestModel =
    input.route?.requestedModel ||
    (typeof req?.model === 'string' ? req.model : undefined);
  const usage = res?.usage;
  const tokensIn = input.stream?.tokens_in ?? inputTokensOf(usage);
  const tokensOut =
    input.stream?.tokens_out ??
    usage?.completion_tokens ??
    usage?.output_tokens;
  const errorType =
    errorTypeOf(input.status, res) ??
    (input.stream?.truncated ? 'stream_truncated' : undefined);
  const ps = extractPSHeaders(input.headers);

  const attrs: Record<string, AttrValue | undefined> = {
    'gen_ai.operation.name': operation,
    'gen_ai.provider.name': semconvProviderName(
      providerFromHeaders(input.headers)
    ),
    'gen_ai.request.model': requestModel,
    'gen_ai.request.max_tokens':
      req?.max_tokens ?? req?.max_completion_tokens ?? req?.max_output_tokens,
    'gen_ai.request.temperature': req?.temperature,
    'gen_ai.request.top_p': req?.top_p,
    'gen_ai.request.top_k': req?.top_k,
    'gen_ai.request.frequency_penalty': req?.frequency_penalty,
    'gen_ai.request.presence_penalty': req?.presence_penalty,
    'gen_ai.request.seed': req?.seed,
    'gen_ai.request.choice.count':
      typeof req?.n === 'number' && req.n !== 1 ? req.n : undefined,
    'gen_ai.response.model':
      input.stream?.model ??
      (typeof res?.model === 'string' ? res.model : undefined),
    'gen_ai.response.id': typeof res?.id === 'string' ? res.id : undefined,
    'gen_ai.response.finish_reasons': finishReasonsOf(res),
    'gen_ai.usage.input_tokens': tokensIn,
    'gen_ai.usage.output_tokens': tokensOut,
    'gen_ai.usage.cache_read.input_tokens':
      usage?.cache_read_input_tokens ??
      usage?.prompt_tokens_details?.cached_tokens ??
      usage?.input_tokens_details?.cached_tokens,
    'gen_ai.usage.cache_creation.input_tokens':
      usage?.cache_creation_input_tokens,
    'http.response.status_code': input.status,
    'error.type': errorType,
    // Prompt Shields business context (X-PS-* headers) and routing.
    'ps.api_surface': surface,
    'ps.business_unit': ps.business_unit,
    'ps.use_case': ps.use_case_name,
    'ps.owner': ps.owner_email,
    'ps.data_classification': ps.data_classification,
    'ps.route.group': input.route?.group,
    'ps.route.reason': input.route?.reason,
    'ps.route.sensitivity_level': input.route?.sensitivityLevel,
    'ps.route.sensitivity_source': input.route?.sensitivitySource,
    'ps.stream': input.stream ? true : undefined,
    'ps.stream.truncated': input.stream?.truncated || undefined,
    'ps.stream.cancelled': input.stream?.cancelled || undefined,
  };
  // Numeric attributes only take finite numbers (clients send anything).
  for (const k of Object.keys(attrs)) {
    const numeric =
      (k.startsWith('gen_ai.request.') && k !== 'gen_ai.request.model') ||
      k.startsWith('gen_ai.usage.');
    const v = attrs[k];
    if (numeric && !(typeof v === 'number' && Number.isFinite(v)))
      attrs[k] = undefined;
  }
  if (input.captureContent && !input.stream) {
    Object.assign(attrs, contentAttributes(req, res));
  }

  const parent = parseTraceparent(input.headers['traceparent']);
  const span: any = {
    traceId: parent?.traceId ?? hex(16),
    spanId: hex(8),
    name: requestModel ? `${operation} ${requestModel}` : operation,
    kind: 3, // SPAN_KIND_CLIENT
    startTimeUnixNano: nanos(input.startMs),
    endTimeUnixNano: nanos(Math.max(input.endMs, input.startMs)),
    attributes: toOtlpAttributes(attrs),
    status: errorType ? { code: 2, message: errorType } : { code: 0 },
  };
  if (parent) span.parentSpanId = parent.parentSpanId;
  return { span, environment: ps.environment };
}

// ---------------------------------------------------------------------------
// Batched OTLP/HTTP JSON exporter
// ---------------------------------------------------------------------------

const SCOPE = { name: 'prompt-shields-gateway', version: '1' };
const SCHEMA_URL = 'https://opentelemetry.io/schemas/1.40.0';

/** ExportTraceServiceRequest JSON, one resource per environment. */
export function buildExportRequest(spans: BuiltSpan[], serviceName: string) {
  const byEnv = new Map<string, any[]>();
  for (const s of spans) {
    const k = s.environment ?? '';
    if (!byEnv.has(k)) byEnv.set(k, []);
    byEnv.get(k)!.push(s.span);
  }
  return {
    resourceSpans: [...byEnv.entries()].map(([environment, list]) => ({
      resource: {
        attributes: toOtlpAttributes({
          'service.name': serviceName,
          'deployment.environment.name': environment || undefined,
        }),
      },
      scopeSpans: [{ scope: SCOPE, schemaUrl: SCHEMA_URL, spans: list }],
      schemaUrl: SCHEMA_URL,
    })),
  };
}

export class OtlpJsonExporter {
  private queue: BuiltSpan[] = [];
  private timer: any;
  private warned = false;

  constructor(
    private config: OtelConfig,
    private fetchFn: typeof fetch = (...a) => fetch(...a)
  ) {}

  get pending() {
    return this.queue.length;
  }

  /**
   * Queue a span. When `waitUntil` is given (Workers), the queue is flushed
   * now and the send is kept alive past the response, because an isolate's
   * timers are not guaranteed to run between requests. On Node, spans are
   * flushed when `maxBatch` is reached or after `flushMs`.
   */
  enqueue(span: BuiltSpan, waitUntil?: (p: Promise<unknown>) => void) {
    this.queue.push(span);
    if (this.queue.length > this.config.maxQueue) {
      const dropped = this.queue.length - this.config.maxQueue;
      this.queue.splice(0, dropped);
      telemetryDroppedTotal.inc(
        { sink: 'otel', reason: 'queue_full' },
        dropped
      );
    }
    if (waitUntil) {
      waitUntil(this.flush());
    } else if (this.queue.length >= this.config.maxBatch) {
      this.flush();
    } else if (!this.timer) {
      this.timer = setTimeout(() => {
        this.timer = undefined;
        this.flush();
      }, this.config.flushMs);
      this.timer?.unref?.();
    }
  }

  /**
   * Send everything queued. Never throws; failed batches are dropped and
   * counted in ps_gateway_telemetry_dropped_total{sink="otel"}.
   */
  async flush(): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = undefined;
    }
    const sends: Promise<void>[] = [];
    while (this.queue.length) {
      sends.push(this.send(this.queue.splice(0, this.config.maxBatch)));
    }
    await Promise.all(sends);
  }

  private async send(batch: BuiltSpan[]): Promise<void> {
    try {
      const timeout = (AbortSignal as any).timeout?.(10_000);
      const res = await this.fetchFn(this.config.endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...this.config.headers },
        body: JSON.stringify(
          buildExportRequest(batch, this.config.serviceName)
        ),
        ...(timeout ? { signal: timeout } : {}),
      });
      if (!res.ok) {
        telemetryDroppedTotal.inc(
          { sink: 'otel', reason: 'http_error' },
          batch.length
        );
        this.warn(`HTTP ${res.status}`);
      }
    } catch (err) {
      telemetryDroppedTotal.inc(
        { sink: 'otel', reason: 'send_failed' },
        batch.length
      );
      this.warn(err);
    }
  }

  private warn(detail: unknown) {
    if (this.warned) return;
    this.warned = true;
    console.warn(
      '[PS OTel] span export failed (further failures muted):',
      detail
    );
  }
}

// ---------------------------------------------------------------------------
// Hono middleware
// ---------------------------------------------------------------------------

/**
 * Emits one GenAI span per LLM POST. Mount only when `otelConfig()` is
 * non-null (see src/index.ts). Fail-open: never affects the response.
 */
export function psOtel(
  config: OtelConfig,
  exporter = new OtlpJsonExporter(config)
) {
  return async (c: any, next: any) => {
    const startMs = Date.now();
    await next();
    if (c.req.method !== 'POST') return;
    try {
      const url = new URL(c.req.url);
      if (!semconvOperationName(apiSurfaceOf(url.pathname))) return;
      // A cancel (#73) is not a model call; its create call has the span.
      if (!reportsOwnUsage(url.pathname)) return;
      const headers = Object.fromEntries(c.req.raw.headers) as Record<
        string,
        string
      >;

      let responseBody: any;
      if (
        (c.res.headers.get('content-type') || '').includes('application/json')
      ) {
        try {
          responseBody = await c.res.clone().json();
        } catch {
          responseBody = undefined;
        }
      }
      // Multipart uploads yield only their `model`; the form is not re-read.
      const requestBody = await requestBodyForTelemetry(c);
      let waitUntil: ((p: Promise<unknown>) => void) | undefined;
      try {
        const ctx = c.executionCtx;
        if (ctx?.waitUntil) waitUntil = (p) => ctx.waitUntil(p);
      } catch {
        /* no execution context on Node */
      }

      const base: SpanInput = {
        path: url.pathname,
        headers,
        status: c.res.status,
        startMs,
        endMs: Date.now(),
        requestBody,
        responseBody,
        route: c.get('psRoute'),
        captureContent: config.captureContent,
      };
      const emit = (input: SpanInput) => {
        const built = buildGenAISpan(input);
        if (built) exporter.enqueue(built, waitUntil);
      };

      const stream = observeStream(c);
      if (!stream) return emit(base);
      stream
        .then((summary) =>
          emit({ ...base, stream: summary, endMs: Date.now() })
        )
        .catch(() => {});
    } catch (err) {
      console.warn('[PS OTel] span skipped:', err);
    }
  };
}
