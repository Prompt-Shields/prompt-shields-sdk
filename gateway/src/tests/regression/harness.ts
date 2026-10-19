/**
 * Offline harness for the ported-2.0 regression suites (#69).
 *
 * Drives the real gateway app (`src/index.ts`, every middleware mounted) with
 * `global.fetch` replaced by a fake that serves canned provider responses and
 * captures what the gateway sends to the Prompt Shields collector and the
 * OTLP endpoint. No network, no credentials files, no timers to wait out.
 *
 * Import this module BEFORE `../../index`: it sets the environment that
 * `src/index.ts` reads at load time (telemetry, router, OTel). Suites must
 * also `jest.mock('jose', () => ({}))`: the JWT guardrail plugin imports
 * jose 6, which ships ES modules only and is never called here.
 */

export const COLLECTOR = 'http://collector.test';
export const OTEL = 'http://otel.test/v1/traces';

process.env.PS_API_KEY = 'ps-regression-key';
process.env.PS_COLLECTOR_URL = COLLECTOR;
process.env.PS_ROUTER_ENABLED = 'true';
process.env.PS_OTEL_ENDPOINT = OTEL;
process.env.PS_OTEL_MAX_BATCH = '1';
process.env.PS_METRICS_ENABLED = 'true';
// Collector retries (#87) back off for 1 ms here, not 200 ms.
process.env.PS_TELEMETRY_RETRY_BASE_MS = '1';

export interface UpstreamCall {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: any;
}

type Responder = (call: UpstreamCall) => Response | Promise<Response>;

const routes: { match: RegExp; respond: Responder }[] = [];
export const upstreamCalls: UpstreamCall[] = [];
export const events: any[] = [];
export const otelPayloads: any[] = [];
/** The loss-accounting `metadata` of every collector request (#87). */
export const collectorMetadata: any[] = [];
/** Collector response override; a 200 `{}` (event stored) when unset. */
let collectorResponder: (() => Response | Promise<Response>) | undefined;

/** Answer collector requests with `respond` until the next resetHarness(). */
export function collector(respond: () => Response | Promise<Response>) {
  collectorResponder = respond;
}

function headersOf(init: any): Record<string, string> {
  const out: Record<string, string> = {};
  new Headers(init?.headers ?? {}).forEach((v, k) => (out[k] = v));
  return out;
}

function bodyOf(init: any): any {
  const raw = init?.body;
  if (typeof raw !== 'string') return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

/**
 * Real fetch follows a 3xx unless `redirect` is 'manual' or 'error'. The fake
 * does the same, as Node 22 does: a cross-origin hop drops only
 * `authorization`, `cookie` and `proxy-authorization`; a 303 (or a POST 301
 * or 302) turns into a body-less GET. Followed hops land in upstreamCalls.
 */
const REDIRECT_STATUSES = [301, 302, 303, 307, 308];
const CROSS_ORIGIN_DROPPED = ['authorization', 'cookie', 'proxy-authorization'];

global.fetch = jest.fn(async (input: any, init: any = {}) => {
  let url: string = typeof input === 'string' ? input : input.url;
  let current = init;
  for (let hop = 0; ; hop++) {
    const res = await fakeFetchOnce(url, current);
    const location = res.headers?.get('location');
    if (!REDIRECT_STATUSES.includes(res.status) || !location) return res;
    if (current.redirect === 'manual') return res;
    if (current.redirect === 'error' || hop >= 20) {
      throw new TypeError('fetch failed: redirect');
    }
    const next = new URL(location, url).toString();
    const headers = headersOf(current);
    if (new URL(next).origin !== new URL(url).origin) {
      CROSS_ORIGIN_DROPPED.forEach((h) => delete headers[h]);
    }
    const method = (current.method ?? 'GET').toUpperCase();
    const toGet =
      res.status === 303 ||
      ([301, 302].includes(res.status) && method === 'POST');
    current = {
      ...current,
      method: toGet ? 'GET' : current.method,
      headers,
      body: toGet ? undefined : current.body,
    };
    url = next;
  }
}) as any;

async function fakeFetchOnce(url: string, init: any): Promise<Response> {
  if (url.startsWith(COLLECTOR)) {
    const sent = JSON.parse(init.body);
    collectorMetadata.push(sent.metadata);
    if (collectorResponder) return collectorResponder();
    events.push(...sent.events);
    return new Response('{}', { status: 200 });
  }
  if (url.startsWith(OTEL)) {
    otelPayloads.push(JSON.parse(init.body));
    return new Response('{}', { status: 200 });
  }
  const call: UpstreamCall = {
    url,
    method: init.method ?? 'GET',
    headers: headersOf(init),
    body: bodyOf(init),
  };
  upstreamCalls.push(call);
  const route = routes.find((r) => r.match.test(url));
  if (!route) {
    // Fail loudly: an unexpected upstream URL is itself a regression.
    return new Response(JSON.stringify({ error: `unmocked ${url}` }), {
      status: 599,
      headers: { 'content-type': 'application/json' },
    });
  }
  return route.respond(call);
}

/** Serve `respond` for upstream URLs matching `match` (first match wins). */
export function upstream(match: RegExp, respond: Responder) {
  routes.unshift({ match, respond });
}

export function resetHarness() {
  routes.length = 0;
  upstreamCalls.length = 0;
  events.length = 0;
  otelPayloads.length = 0;
  collectorMetadata.length = 0;
  collectorResponder = undefined;
}

export const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

/** A text/event-stream response from raw SSE text. */
export const sse = (text: string, contentType = 'text/event-stream') =>
  new Response(text, { status: 200, headers: { 'content-type': contentType } });

/**
 * AWS event-stream body (Bedrock `converse-stream`): one frame per event,
 * with no headers. The gateway reads the frame lengths and the JSON payload;
 * it does not check the CRCs, so they are left zero.
 */
export function awsEventStream(eventsIn: object[]): Response {
  const enc = new TextEncoder();
  const frames = eventsIn.map((ev) => {
    const payload = enc.encode(JSON.stringify(ev));
    const total = 12 + payload.length + 4;
    const frame = new Uint8Array(total);
    new DataView(frame.buffer).setUint32(0, total);
    new DataView(frame.buffer).setUint32(4, 0);
    frame.set(payload, 12);
    return frame;
  });
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(f);
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { 'content-type': 'application/vnd.amazon.eventstream' },
  });
}

/** Wait (on real timers) until `cond` holds, failing after ~2 s. */
export async function until(cond: () => boolean, what = 'condition') {
  for (let i = 0; i < 200; i++) {
    if (cond()) return;
    await new Promise((r) => setTimeout(r, 10));
  }
  throw new Error(`timed out waiting for ${what}`);
}

/** The one collector event for this test. */
export async function telemetryEvent() {
  await until(() => events.length > 0, 'telemetry event');
  expect(events).toHaveLength(1);
  return events[0];
}

/** All OTel spans exported so far. */
export function otelSpans(): any[] {
  return otelPayloads.flatMap((p) =>
    (p.resourceSpans ?? []).flatMap((rs: any) =>
      (rs.scopeSpans ?? []).flatMap((ss: any) => ss.spans ?? [])
    )
  );
}

/** OTLP attributes as a plain object. */
export function spanAttrs(span: any): Record<string, any> {
  const out: Record<string, any> = {};
  for (const a of span.attributes ?? []) {
    const v = a.value ?? {};
    out[a.key] =
      v.stringValue ??
      (v.intValue !== undefined ? Number(v.intValue) : undefined) ??
      v.doubleValue ??
      v.boolValue ??
      v.arrayValue;
  }
  return out;
}

export function post(
  app: { request: (path: string, init: RequestInit) => any },
  path: string,
  body: unknown,
  headers: Record<string, string> = {}
): Promise<Response> {
  return app.request(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
}
