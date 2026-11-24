/**
 * Observes a streamed (SSE) gateway response on its way to the client, to
 * recover token usage and detect truncation (#72). Bytes are forwarded as
 * they arrive, unchanged; only the current partial line is kept in memory.
 *
 * Shared by ps-telemetry and psMetrics: the first middleware to call
 * `observeStream(c)` wraps the response, later callers get the same summary.
 */

import {
  MAX_STOP_REASONS,
  hasRefusal,
  stopReasonsOf,
} from './psOutcomeSignals';
import {
  STREAM_USAGE_SIDEBAND_KEY,
  StreamUsageSideband,
} from './psStreamUsage';
import {
  TokenCounter,
  estimateRequestTokens,
  tokenFamily,
} from './psUsageEstimate';

/**
 * Where a stream's token counts came from (#389): `provider` when the
 * provider reported them, `estimated` when any of them is the gateway's
 * estimate (psUsageEstimate.ts) because the provider sent none, or the
 * stream was cut before its final usage.
 */
export type UsageSource = 'provider' | 'estimated';

export interface StreamSummary {
  tokens_in?: number;
  tokens_out?: number;
  /** The model the provider reported in the stream, if any. */
  model?: string;
  /** Cached, cache-write and reasoning counts, service tier, reported cost (#9). */
  details?: UsageDetails;
  /** A terminal event ([DONE], message_stop, response.completed, ...) was seen. */
  terminal: boolean;
  /** The upstream ended (or errored) without a terminal event. */
  truncated: boolean;
  /** The client went away before the stream ended. */
  cancelled: boolean;
  /** Stop reasons the stream reported (finish_reason, stop_reason, ...). */
  stopReasons?: string[];
  /** The stream carried an explicit refusal (#44). */
  refusal?: boolean;
  /** Where tokens_in / tokens_out came from (#389), once finalised. */
  usageSource?: UsageSource;
}

// A single SSE line larger than this is not parsed (usage lines are tiny).
const MAX_LINE_CHARS = 1 << 20;

const RESPONSES_TERMINAL = new Set([
  'response.completed',
  'response.incomplete',
  'response.failed',
]);

const num = (v: unknown) => (typeof v === 'number' ? v : undefined);

/**
 * Total input tokens from a usage object. Chat Completions `prompt_tokens`
 * and Responses `input_tokens` already include cached tokens; Anthropic
 * Messages `input_tokens` excludes cache reads and writes, which it reports
 * as `cache_read_input_tokens` / `cache_creation_input_tokens`, so they are
 * added back (#73).
 */
export function inputTokensOf(u: any): number | undefined {
  if (!u || typeof u !== 'object') return undefined;
  const prompt = num(u.prompt_tokens);
  if (prompt !== undefined) return prompt;
  const input = num(u.input_tokens);
  if (input === undefined) return undefined;
  return (
    input +
    (num(u.cache_read_input_tokens) ?? 0) +
    (num(u.cache_creation_input_tokens) ?? 0)
  );
}

/**
 * Usage beyond the input/output totals (#9), named as the SDK sends them.
 * Each count is a subset of `tokens_in` (cached, cache writes) or
 * `tokens_out` (reasoning), as in the SDK's provider adapters.
 */
export interface UsageDetails {
  tokens_cached?: number;
  tokens_cache_write?: number;
  /** 1-hour TTL cache writes, a subset of tokens_cache_write (Anthropic). */
  tokens_cache_write_1h?: number;
  tokens_reasoning?: number;
  service_tier?: string;
  /** Cost the provider billed, in USD (OpenRouter `usage.cost`). */
  provider_cost?: number;
}

/** Costs at or above this (USD) do not fit the collector's cost column. */
export const MAX_PROVIDER_COST = 10_000;

const count = (v: unknown) =>
  typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : undefined;

/**
 * Usage details from one usage object, in any shape the gateway returns:
 * Chat Completions (`prompt_tokens_details.cached_tokens`,
 * `completion_tokens_details.reasoning_tokens`; Gemini, Vertex and Bedrock
 * are normalised to it, Anthropic and Bedrock also keep top-level
 * `cache_*_input_tokens`), Responses (`input_tokens_details`,
 * `output_tokens_details`) and Anthropic Messages (`cache_read_input_tokens`,
 * `cache_creation_input_tokens`, `cache_creation.ephemeral_1h_input_tokens`,
 * `service_tier`). Absent values are left out.
 */
export function usageDetailsOf(u: any): UsageDetails {
  if (!u || typeof u !== 'object') return {};
  const out: UsageDetails = {
    tokens_cached:
      count(u.prompt_tokens_details?.cached_tokens) ??
      count(u.input_tokens_details?.cached_tokens) ??
      count(u.cache_read_input_tokens),
    tokens_cache_write:
      count(u.cache_creation_input_tokens) ??
      count(u.prompt_tokens_details?.cache_creation_tokens) ??
      count(u.input_tokens_details?.cache_creation_tokens),
    // As in the SDK, a zero 1-hour count is not reported.
    tokens_cache_write_1h:
      count(u.cache_creation?.ephemeral_1h_input_tokens) || undefined,
    tokens_reasoning:
      count(u.completion_tokens_details?.reasoning_tokens) ??
      count(u.output_tokens_details?.reasoning_tokens),
    service_tier:
      typeof u.service_tier === 'string' && u.service_tier
        ? u.service_tier
        : undefined,
    // Bounded by the collector's cost column (Numeric(10, 6)): a larger
    // value would fail the insert and lose the whole event.
    provider_cost:
      typeof u.cost === 'number' &&
      Number.isFinite(u.cost) &&
      u.cost >= 0 &&
      u.cost < MAX_PROVIDER_COST
        ? u.cost
        : undefined,
  };
  return definedOnly(out);
}

/** `a` with every defined value of `b` laid over it. */
export function mergeUsageDetails(
  a: UsageDetails | undefined,
  b: UsageDetails
): UsageDetails {
  return { ...(a ?? {}), ...definedOnly(b) };
}

function definedOnly<T extends object>(o: T): T {
  const out: any = {};
  for (const [k, v] of Object.entries(o)) if (v !== undefined) out[k] = v;
  return out;
}

/** Incremental SSE parser that records usage, model and terminal events. */
export class SSEUsageTracker {
  private decoder = new TextDecoder();
  private partial = '';
  // Latest Anthropic-style counts, which may arrive in different events
  private input?: number;
  private cacheRead?: number;
  private cacheCreation?: number;
  // Text the stream generated, for the estimate when usage is missing.
  private output = new TokenCounter();
  // tokens_out came from a final usage report (not message_start's).
  private outputFinal = false;
  readonly summary: StreamSummary = {
    terminal: false,
    truncated: false,
    cancelled: false,
  };

  push(chunk: Uint8Array | string) {
    const text =
      typeof chunk === 'string'
        ? chunk
        : this.decoder.decode(chunk, { stream: true });
    const lines = (this.partial + text).split(/\r\n|\r|\n/);
    this.partial = lines.pop() ?? '';
    if (this.partial.length > MAX_LINE_CHARS) this.partial = '';
    for (const line of lines) this.line(line);
  }

  /** Feeds a data payload the client never saw (psStreamUsage sideband). */
  pushData(data: string) {
    this.line(`data: ${data}`);
  }

  /** Estimated output tokens of the text the stream generated. */
  outputTokens(family: Parameters<TokenCounter['tokens']>[0]): number {
    return this.output.tokens(family);
  }

  /** Whether tokens_out is the provider's final count. */
  get outputReported(): boolean {
    return this.summary.tokens_out !== undefined && this.outputFinal;
  }

  /** Flushes the last unterminated line and returns the summary. */
  end(): StreamSummary {
    this.push(this.decoder.decode());
    if (this.partial) this.line(this.partial);
    this.partial = '';
    return this.summary;
  }

  private line(line: string) {
    if (line.startsWith('event:')) {
      const name = line.slice(6).trim();
      if (name === 'message_stop' || RESPONSES_TERMINAL.has(name))
        this.summary.terminal = true;
      return;
    }
    if (!line.startsWith('data:')) return;
    const data = line.slice(5).trim();
    if (!data) return;
    if (data === '[DONE]') {
      this.summary.terminal = true;
      return;
    }
    if (data.length > MAX_LINE_CHARS) return;
    let obj: any;
    try {
      obj = JSON.parse(data);
    } catch {
      return;
    }
    if (!obj || typeof obj !== 'object') return;

    const type = obj.type;
    if (type === 'message_stop' || RESPONSES_TERMINAL.has(type)) {
      this.summary.terminal = true;
    }
    // OpenAI-style chunk: a finish_reason ends the generation even when the
    // provider does not send [DONE].
    if (
      Array.isArray(obj.choices) &&
      obj.choices.some((ch: any) => ch?.finish_reason)
    ) {
      this.summary.terminal = true;
    }

    this.countOutput(obj);

    // Usage: chat chunks / message_delta carry `usage`, Anthropic
    // message_start nests it in `message`, Responses events in `response`.
    const outBefore = this.summary.tokens_out;
    this.usage(obj.usage);
    this.usage(obj.message?.usage);
    this.usage(obj.response?.usage);
    // message_start's output count is a placeholder; any other report of
    // output tokens is the provider's count so far.
    if (
      this.summary.tokens_out !== outBefore ||
      obj.usage?.output_tokens !== undefined ||
      obj.usage?.completion_tokens !== undefined
    )
      this.outputFinal = type !== 'message_start';

    // Chat chunks and Responses events report the tier outside `usage`.
    const tier = obj.service_tier ?? obj.response?.service_tier;
    if (typeof tier === 'string' && tier) this.details({ service_tier: tier });

    // Outcome signals (#44): stop reasons and refusals, never the text.
    for (const r of stopReasonsOf(obj)) {
      const reasons = (this.summary.stopReasons ??= []);
      if (!reasons.includes(r) && reasons.length < MAX_STOP_REASONS)
        reasons.push(r);
    }
    if (!this.summary.refusal && hasRefusal(obj)) this.summary.refusal = true;

    const model = obj.model ?? obj.message?.model ?? obj.response?.model;
    if (typeof model === 'string' && model) this.summary.model = model;
  }

  /** Feeds the text a chunk generated to the output estimate. */
  private countOutput(obj: any) {
    const out = this.output;
    if (Array.isArray(obj.choices)) {
      for (const ch of obj.choices) {
        if (!ch || typeof ch !== 'object') continue;
        out.feed(ch.text);
        const d = ch.delta;
        if (!d || typeof d !== 'object') continue;
        out.feed(d.content);
        out.feed(d.reasoning_content);
        out.feed(d.reasoning);
        out.feed(d.refusal);
        if (Array.isArray(d.tool_calls)) {
          for (const tc of d.tool_calls) {
            out.feed(tc?.function?.name);
            out.feed(tc?.function?.arguments);
          }
        }
      }
      return;
    }
    const type = obj.type;
    if (type === 'content_block_delta') {
      const d = obj.delta;
      out.feed(d?.text);
      out.feed(d?.partial_json);
      out.feed(d?.thinking);
      return;
    }
    if (type === 'content_block_start') {
      out.feed(obj.content_block?.name);
      return;
    }
    if (typeof type === 'string' && type.endsWith('.delta')) {
      out.feed(obj.delta);
    }
  }

  private details(d: UsageDetails) {
    if (Object.keys(d).length)
      this.summary.details = mergeUsageDetails(this.summary.details, d);
  }

  private usage(u: any) {
    if (!u || typeof u !== 'object') return;
    // Counts are cumulative (Anthropic repeats them on message_delta), so
    // the latest reported value of each wins.
    this.details(usageDetailsOf(u));
    const tokensOut = num(u.completion_tokens) ?? num(u.output_tokens);
    // Anthropic reports cumulative counts, so the latest value wins.
    const prompt = num(u.prompt_tokens);
    if (prompt !== undefined) {
      this.summary.tokens_in = prompt;
    } else {
      this.input = num(u.input_tokens) ?? this.input;
      this.cacheRead = num(u.cache_read_input_tokens) ?? this.cacheRead;
      this.cacheCreation =
        num(u.cache_creation_input_tokens) ?? this.cacheCreation;
      if (this.input !== undefined) {
        this.summary.tokens_in = inputTokensOf({
          input_tokens: this.input,
          cache_read_input_tokens: this.cacheRead,
          cache_creation_input_tokens: this.cacheCreation,
        });
      }
    }
    if (tokensOut !== undefined) this.summary.tokens_out = tokensOut;
  }
}

/**
 * Wraps an SSE response so the tracker sees every chunk the client gets.
 * `onDone` fires once: when the upstream ends, errors, or the client cancels.
 */
export function observeSSE(
  res: Response,
  onDone: (summary: StreamSummary, tracker: SSEUsageTracker) => void,
  sideband?: () => StreamUsageSideband | undefined
): Response {
  if (!res.body) return res;
  const reader = res.body.getReader();
  const tracker = new SSEUsageTracker();
  let finished = false;
  const finish = (how: 'ended' | 'errored' | 'cancelled') => {
    if (finished) return;
    finished = true;
    let summary = tracker.summary;
    try {
      summary = tracker.end();
      for (const data of sideband?.()?.lines ?? []) tracker.pushData(data);
    } catch {
      /* fail-open */
    }
    if (how === 'cancelled') summary.cancelled = true;
    else summary.truncated = !summary.terminal;
    try {
      onDone(summary, tracker);
    } catch {
      /* fail-open */
    }
  };

  const body = new ReadableStream<Uint8Array>({
    async pull(controller) {
      let result: ReadableStreamReadResult<Uint8Array>;
      try {
        result = await reader.read();
      } catch (err) {
        finish('errored');
        controller.error(err);
        return;
      }
      if (result.done) {
        controller.close();
        finish('ended');
        return;
      }
      controller.enqueue(result.value);
      try {
        tracker.push(result.value);
      } catch {
        /* fail-open: never break the stream over parsing */
      }
    },
    cancel(reason) {
      finish('cancelled');
      return reader.cancel(reason);
    },
  });

  return new Response(body, res);
}

const isSSE = (res: Response | undefined) =>
  !!res?.body &&
  (res.headers.get('content-type') ?? '').includes('text/event-stream');

// Paths whose streams generate text, where a missing count is estimated.
const GENERATION_PATH =
  /\/(chat\/completions|completions|messages|responses)\/?$/;

/**
 * Fills the counts the provider did not report with estimates (#389) and
 * sets `usageSource`. Input comes from the request body; output from the
 * text the stream carried. A cut stream (cancelled or truncated) whose
 * final output count never arrived reports the larger of the provider's
 * partial count and the estimate of the text sent before the cut.
 */
export function finaliseUsage(
  summary: StreamSummary,
  tracker: SSEUsageTracker,
  requestBody: any
): StreamSummary {
  const family = tokenFamily(summary.model ?? requestBody?.model);
  let estimated = false;
  if (summary.tokens_in === undefined) {
    const est = estimateRequestTokens(requestBody, family);
    if (est !== undefined) {
      summary.tokens_in = est;
      estimated = true;
    }
  }
  if (!tracker.outputReported) {
    const est = tracker.outputTokens(family);
    if (summary.tokens_out === undefined || est > summary.tokens_out) {
      summary.tokens_out = est;
      estimated = true;
    }
  }
  summary.usageSource = estimated ? 'estimated' : 'provider';
  return summary;
}

async function requestJson(c: any): Promise<any> {
  try {
    const type = (c.req.header('content-type') || '').toLowerCase();
    if (!type.includes('json')) return undefined;
    return await c.req.json();
  } catch {
    return undefined;
  }
}

/**
 * For a streamed Hono response, wraps `c.res` once and returns a promise of
 * its summary, resolved when the stream ends. Returns undefined otherwise.
 * On text-generation paths, missing counts are estimated (finaliseUsage).
 */
export function observeStream(c: any): Promise<StreamSummary> | undefined {
  const existing = c.get('psStreamSummary');
  if (existing) return existing;
  if (!isSSE(c.res)) return undefined;
  let resolve!: (s: StreamSummary) => void;
  const promise = new Promise<StreamSummary>((r) => (resolve = r));
  let generation = false;
  try {
    generation = GENERATION_PATH.test(new URL(c.req.url).pathname);
  } catch {
    generation = false;
  }
  c.res = observeSSE(
    c.res,
    (summary, tracker) => {
      if (!generation) return resolve(summary);
      requestJson(c)
        .then((body) => finaliseUsage(summary, tracker, body))
        .catch(() => summary)
        .then(resolve, () => resolve(summary));
    },
    () => {
      try {
        return c.get(STREAM_USAGE_SIDEBAND_KEY);
      } catch {
        return undefined;
      }
    }
  );
  c.set('psStreamSummary', promise);
  return promise;
}
