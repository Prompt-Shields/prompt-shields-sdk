/**
 * W3C trace context for every gateway request (#385).
 *
 * Each request gets one gateway span id. Its trace id is the caller's
 * (inbound `traceparent`, so an app's own tracer and the Prompt Shields SDK
 * keep one trace end to end) or a new one. The gateway's OTel spans
 * (psOtel.ts, mcp/otel.ts) use this context, and every response carries
 * `traceparent: 00-<trace id>-<gateway span id>-<flags>`, so a caller
 * without a tracer can still look the trace up
 * (`GET /partner/v1/traces/{trace_id}`).
 *
 * Upstream propagation is opt-in: with `PS_OTEL_PROPAGATE_UPSTREAM=true`
 * the gateway sends `traceparent` (its span as the parent) and the caller's
 * `tracestate` to the provider or MCP server. Off by default: a trace id
 * sent to a provider lets it correlate a tenant's calls, and providers have
 * no use for it unless they trace too. When off, the gateway adds nothing;
 * headers the caller forwards explicitly (`forward_headers`, the `/v1/proxy`
 * pass-through) behave as before.
 */

import type { Context } from 'hono';

export interface TraceContext {
  traceId: string;
  /** The gateway's span for this request. */
  spanId: string;
  /** The caller's span (inbound `traceparent`), if any. */
  parentSpanId?: string;
  /** Trace flags, two hex digits (`01`: sampled). */
  flags: string;
  /** The caller's `tracestate`, when well-formed. */
  tracestate?: string;
}

const KEY = 'psTraceContext';
const TRACEPARENT_RE =
  /^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})(-.*)?$/;
// W3C: at most 32 list members; keep it bounded and printable.
const TRACESTATE_RE = /^[\x20-\x7e]{1,512}$/;

const truthy = (v?: string) => /^(1|true|yes|on)$/i.test((v || '').trim());

function hex(bytes: number): string {
  const b = new Uint8Array(bytes);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
}

/** An inbound `traceparent`, when well-formed (W3C trace context level 1). */
export function parseTraceparentHeader(
  header?: string | null
): { traceId: string; parentSpanId: string; flags: string } | undefined {
  const m = (header || '').trim().match(TRACEPARENT_RE);
  if (!m) return undefined;
  const [, version, traceId, parentSpanId, flags, rest] = m;
  // Version ff is invalid; version 00 has no trailing fields.
  if (version === 'ff' || (version === '00' && rest)) return undefined;
  if (/^0+$/.test(traceId) || /^0+$/.test(parentSpanId)) return undefined;
  return { traceId, parentSpanId, flags };
}

/** A new trace context from the request's headers. */
export function newTraceContext(
  traceparent?: string | null,
  tracestate?: string | null
): TraceContext {
  const parent = parseTraceparentHeader(traceparent);
  if (!parent) return { traceId: hex(16), spanId: hex(8), flags: '01' };
  const state = (tracestate || '').trim();
  return {
    traceId: parent.traceId,
    spanId: hex(8),
    parentSpanId: parent.parentSpanId,
    flags: parent.flags,
    ...(state && TRACESTATE_RE.test(state) ? { tracestate: state } : {}),
  };
}

/** The request's trace context, created on first use. */
export function traceContextOf(c: Context): TraceContext {
  let ctx = c.get(KEY as any) as TraceContext | undefined;
  if (!ctx) {
    ctx = newTraceContext(
      c.req.header('traceparent'),
      c.req.header('tracestate')
    );
    c.set(KEY as any, ctx);
  }
  return ctx;
}

export function formatTraceparent(ctx: TraceContext): string {
  return `00-${ctx.traceId}-${ctx.spanId}-${ctx.flags}`;
}

/** `PS_OTEL_PROPAGATE_UPSTREAM`: send trace context to providers. */
export function propagatesUpstream(
  env: Record<string, string | undefined> | undefined = typeof process !==
  'undefined'
    ? process.env
    : undefined
): boolean {
  return truthy(env?.PS_OTEL_PROPAGATE_UPSTREAM);
}

/**
 * Headers to add to an upstream request: `traceparent` (and the caller's
 * `tracestate`) when upstream propagation is on, else none.
 */
export function upstreamTraceHeaders(
  c: Context | undefined,
  env?: Record<string, string | undefined>
): Record<string, string> {
  if (!c || !propagatesUpstream(env)) return {};
  try {
    const ctx = traceContextOf(c);
    return {
      traceparent: formatTraceparent(ctx),
      ...(ctx.tracestate ? { tracestate: ctx.tracestate } : {}),
    };
  } catch {
    return {};
  }
}

function setHeader(c: Context, name: string, value: string): void {
  try {
    c.res.headers.set(name, value);
  } catch {
    try {
      // Immutable headers (a fetched Response): copy, then set.
      const res = new Response(c.res.body, c.res);
      res.headers.set(name, value);
      c.res = res;
    } catch {
      /* fail-open */
    }
  }
}

/** Sets `traceparent` on every response. Fail-open. */
export function psTraceContext() {
  return async (c: Context, next: () => Promise<void>) => {
    let ctx: TraceContext | undefined;
    try {
      ctx = traceContextOf(c);
    } catch {
      /* fail-open */
    }
    await next();
    if (!ctx || c.req.header('upgrade') === 'websocket') return;
    setHeader(c, 'traceparent', formatTraceparent(ctx));
  };
}
