/**
 * PS fork (#389): every streamed chat call reports usage. OpenAI-style
 * providers only send usage on a stream when the request sets
 * `stream_options.include_usage`, so for providers on an allowlist the
 * gateway sets it when the client did not, and removes what that adds from
 * the client's stream (the usage-only chunk, and `"usage":null` on the
 * other chunks), so the client gets the bytes it would have got without it.
 * The removed usage chunk is handed to the stream observer through a
 * sideband (`STREAM_USAGE_SIDEBAND_KEY`), so telemetry, budgets, rate
 * limits, metrics and spans still see it.
 *
 * Configuration (read per request, so Workers bindings work):
 * - `PS_STREAM_INCLUDE_USAGE=false` turns injection off.
 * - `PS_STREAM_INCLUDE_USAGE_PROVIDERS` replaces the provider allowlist
 *   (comma-separated gateway provider ids; default `DEFAULT_PROVIDERS`).
 * - `PS_STREAM_INCLUDE_USAGE_HOSTS`: exact hostnames of custom hosts
 *   (`x-portkey-custom-host`, e.g. a vLLM server) on an allowlisted
 *   provider that accept `stream_options`. Custom hosts are skipped
 *   otherwise, since some OpenAI-compatible servers reject the parameter.
 *
 * The allowlist is shared with the SDK: schemas/stream-usage/include-usage.v1.json.
 */

/** Set per upstream attempt: whether the gateway injected include_usage. */
export const STREAM_USAGE_INJECTED_KEY = 'psStreamUsageInjected';
/** Usage-only SSE data lines removed from the client stream. */
export const STREAM_USAGE_SIDEBAND_KEY = 'psStreamUsageSideband';

/**
 * Gateway providers that forward `stream_options` verbatim and return the
 * OpenAI chunk stream unchanged. Providers that send usage on every stream
 * anyway (Anthropic, Vertex, Bedrock, Groq, Together, Fireworks) need no
 * injection.
 */
export const DEFAULT_PROVIDERS = ['openai', 'azure-openai'];

/**
 * Azure OpenAI accepts `stream_options` from this API version on (and on
 * the dateless `v1` API); older versions reject it.
 */
export const AZURE_MIN_API_VERSION = '2024-07-01';

export function azureApiVersionAccepts(version: unknown): boolean {
  if (typeof version !== 'string' || !version.trim()) return false;
  const v = version.trim().toLowerCase();
  if (!/^\d/.test(v)) return ['v1', 'latest', 'preview'].includes(v);
  return v >= AZURE_MIN_API_VERSION;
}

const ENDPOINTS = new Set(['chatComplete', 'complete']);

// An SSE event larger than this is passed through unexamined.
const MAX_EVENT_CHARS = 1 << 20;
// At most this many usage lines are kept for the observer.
const MAX_SIDEBAND_LINES = 8;

export interface StreamUsageSideband {
  lines: string[];
}

type Env = Record<string, unknown>;

const list = (v: unknown): string[] | undefined =>
  typeof v === 'string' && v.trim()
    ? v
        .split(',')
        .map((s) => s.trim().toLowerCase())
        .filter(Boolean)
    : undefined;

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.toLowerCase();
  } catch {
    return '';
  }
}

/** Whether the gateway asks this upstream for stream usage. */
export function streamUsageEnabled(
  env: Env,
  provider: string,
  customHost?: string,
  apiVersion?: string
): boolean {
  if (String(env.PS_STREAM_INCLUDE_USAGE ?? '').toLowerCase() === 'false')
    return false;
  const providers =
    list(env.PS_STREAM_INCLUDE_USAGE_PROVIDERS) ?? DEFAULT_PROVIDERS;
  if (!providers.includes((provider || '').toLowerCase())) return false;
  if (customHost) {
    const hosts = list(env.PS_STREAM_INCLUDE_USAGE_HOSTS) ?? [];
    return hosts.includes(hostOf(customHost));
  }
  if (provider === 'azure-openai') return azureApiVersionAccepts(apiVersion);
  return true;
}

/**
 * Sets `stream_options.include_usage` on an upstream body (in place) when
 * it streams and the client did not set `include_usage` itself (true or
 * false). Returns whether it did.
 */
export function injectIncludeUsage(body: any): boolean {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return false;
  if (body.stream !== true) return false;
  const opts = body.stream_options;
  if (opts === undefined || opts === null) {
    body.stream_options = { include_usage: true };
    return true;
  }
  if (typeof opts === 'object' && !Array.isArray(opts)) {
    if ('include_usage' in opts) return false;
    body.stream_options = { ...opts, include_usage: true };
    return true;
  }
  return false;
}

/**
 * For one upstream attempt: injects include_usage when the provider is
 * allowlisted, and records the outcome on the context (a later attempt
 * overwrites it, so it describes the attempt that served the call).
 */
export function maybeInjectStreamUsage(
  c: any,
  env: Env,
  fn: string,
  provider: string,
  customHost: string | undefined,
  body: any,
  apiVersion?: string
): boolean {
  let injected = false;
  try {
    injected =
      ENDPOINTS.has(fn) &&
      streamUsageEnabled(env, provider, customHost, apiVersion) &&
      injectIncludeUsage(body);
  } catch {
    injected = false;
  }
  try {
    c.set(STREAM_USAGE_INJECTED_KEY, injected);
    c.set(STREAM_USAGE_SIDEBAND_KEY, undefined);
  } catch {
    /* no context */
  }
  return injected;
}

const EVENT_END = /(\r\n|\r|\n)(\r\n|\r|\n)/;
const USAGE_NULL_TAIL = ',"usage":null}';

/**
 * One SSE event with what injection added removed: undefined when the
 * whole event is the usage-only chunk (its data line goes to `onUsage`),
 * else the event without a `"usage":null` member. Anything that is not a
 * JSON chunk is returned unchanged.
 */
export function stripEvent(
  event: string,
  onUsage: (data: string) => void
): string | undefined {
  const m = /^data:[ ]?(.*?)(\r\n|\r|\n|$)/s.exec(event);
  if (!m) return event;
  const data = m[1];
  if (!data.startsWith('{')) return event;
  // Fast path: OpenAI puts "usage":null last on every other chunk.
  if (data.endsWith(USAGE_NULL_TAIL) && !data.includes('"usage":{')) {
    const fixed = data.slice(0, -USAGE_NULL_TAIL.length) + '}';
    return event.replace(data, () => fixed);
  }
  if (!data.includes('"usage"')) return event;
  let obj: any;
  try {
    obj = JSON.parse(data);
  } catch {
    return event;
  }
  if (!obj || typeof obj !== 'object' || !('usage' in obj)) return event;
  if (
    obj.usage &&
    typeof obj.usage === 'object' &&
    Array.isArray(obj.choices) &&
    obj.choices.length === 0
  ) {
    onUsage(data);
    return undefined;
  }
  // Only rewrite compact JSON, whose bytes JSON.stringify reproduces.
  if (JSON.stringify(obj) !== data) return event;
  if (obj.usage && typeof obj.usage === 'object') onUsage(data);
  delete obj.usage;
  const fixed = JSON.stringify(obj);
  return event.replace(data, () => fixed);
}

/**
 * Wraps a streamed response so the client gets it without what include
 * usage injection added. Usage lines go to `sideband` for the observer.
 */
export function stripInjectedUsage(
  res: Response,
  sideband: StreamUsageSideband
): Response {
  if (!res.body || res.status !== 200) return res;
  if (!(res.headers.get('content-type') ?? '').includes('text/event-stream'))
    return res;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  let pending = '';
  const onUsage = (data: string) => {
    if (sideband.lines.length < MAX_SIDEBAND_LINES) sideband.lines.push(data);
  };
  const take = (flush: boolean): string => {
    let out = '';
    for (;;) {
      const m = EVENT_END.exec(pending);
      if (!m) break;
      const end = m.index + m[0].length;
      const event = pending.slice(0, end);
      pending = pending.slice(end);
      let kept: string | undefined = event;
      try {
        kept = stripEvent(event, onUsage);
      } catch {
        kept = event;
      }
      if (kept !== undefined) out += kept;
    }
    if (flush || pending.length > MAX_EVENT_CHARS) {
      out += pending;
      pending = '';
    }
    return out;
  };
  const body = new ReadableStream<Uint8Array>({
    async pull(controller) {
      for (;;) {
        let result: ReadableStreamReadResult<Uint8Array>;
        try {
          result = await reader.read();
        } catch (err) {
          const rest = take(true);
          if (rest) controller.enqueue(encoder.encode(rest));
          controller.error(err);
          return;
        }
        if (result.done) {
          pending += decoder.decode();
          const rest = take(true);
          if (rest) controller.enqueue(encoder.encode(rest));
          controller.close();
          return;
        }
        pending += decoder.decode(result.value, { stream: true });
        const out = take(false);
        if (out) {
          controller.enqueue(encoder.encode(out));
          return;
        }
      }
    },
    cancel(reason) {
      return reader.cancel(reason);
    },
  });
  return new Response(body, res);
}

/**
 * The served attempt's response, stripped when that attempt injected
 * include_usage. The sideband is stored on the context for the observer.
 */
export function applyStreamUsageStrip(c: any, res: Response): Response {
  try {
    if (!c.get(STREAM_USAGE_INJECTED_KEY)) return res;
    const sideband: StreamUsageSideband = { lines: [] };
    const out = stripInjectedUsage(res, sideband);
    if (out !== res) c.set(STREAM_USAGE_SIDEBAND_KEY, sideband);
    return out;
  } catch {
    return res;
  }
}
