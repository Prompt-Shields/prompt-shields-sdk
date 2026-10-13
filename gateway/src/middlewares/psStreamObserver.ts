/**
 * Observes a streamed (SSE) gateway response on its way to the client, to
 * recover token usage and detect truncation (#72). Bytes are forwarded as
 * they arrive, unchanged; only the current partial line is kept in memory.
 *
 * Shared by ps-telemetry and psMetrics: the first middleware to call
 * `observeStream(c)` wraps the response, later callers get the same summary.
 */

export interface StreamSummary {
  tokens_in?: number;
  tokens_out?: number;
  /** The model the provider reported in the stream, if any. */
  model?: string;
  /** A terminal event ([DONE], message_stop, response.completed, ...) was seen. */
  terminal: boolean;
  /** The upstream ended (or errored) without a terminal event. */
  truncated: boolean;
  /** The client went away before the stream ended. */
  cancelled: boolean;
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

/** Incremental SSE parser that records usage, model and terminal events. */
export class SSEUsageTracker {
  private decoder = new TextDecoder();
  private partial = '';
  // Latest Anthropic-style counts, which may arrive in different events
  private input?: number;
  private cacheRead?: number;
  private cacheCreation?: number;
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

    // Usage: chat chunks / message_delta carry `usage`, Anthropic
    // message_start nests it in `message`, Responses events in `response`.
    this.usage(obj.usage);
    this.usage(obj.message?.usage);
    this.usage(obj.response?.usage);

    const model = obj.model ?? obj.message?.model ?? obj.response?.model;
    if (typeof model === 'string' && model) this.summary.model = model;
  }

  private usage(u: any) {
    if (!u || typeof u !== 'object') return;
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
  onDone: (summary: StreamSummary) => void
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
    } catch {
      /* fail-open */
    }
    if (how === 'cancelled') summary.cancelled = true;
    else summary.truncated = !summary.terminal;
    try {
      onDone(summary);
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

/**
 * For a streamed Hono response, wraps `c.res` once and returns a promise of
 * its summary, resolved when the stream ends. Returns undefined otherwise.
 */
export function observeStream(c: any): Promise<StreamSummary> | undefined {
  const existing = c.get('psStreamSummary');
  if (existing) return existing;
  if (!isSSE(c.res)) return undefined;
  let resolve!: (s: StreamSummary) => void;
  const promise = new Promise<StreamSummary>((r) => (resolve = r));
  c.res = observeSSE(c.res, resolve);
  c.set('psStreamSummary', promise);
  return promise;
}
