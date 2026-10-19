/**
 * PS fork (privacy): `X-PS-*` request headers carry Prompt Shields business
 * metadata (business unit, data classification, owner, routing and cache
 * hints). They are read by the gateway's own middlewares (ps-router,
 * ps-telemetry, psMetrics, psOtel, cache) from the incoming request and must
 * never reach an upstream provider.
 *
 * Upstream headers are finalised in `constructRequestHeaders`
 * (handlers/handlerUtils.ts) for every proxy and unified route, and in the
 * realtime WebSocket handlers; each passes its outgoing headers through
 * `stripPSInternalHeaders`. The incoming request is left untouched, so the
 * middlewares still see the headers.
 */

export const PS_INTERNAL_HEADER_PREFIX = 'x-ps-';

/** True for an `X-PS-*` header name, in any case. */
export function isPSInternalHeader(name: string): boolean {
  return name.toLowerCase().startsWith(PS_INTERNAL_HEADER_PREFIX);
}

/** A copy of `headers` without any `X-PS-*` header. Never mutates the input. */
export function stripPSInternalHeaders<T>(
  headers: Record<string, T>
): Record<string, T> {
  const out: Record<string, T> = {};
  for (const key of Object.keys(headers)) {
    if (!isPSInternalHeader(key)) out[key] = headers[key];
  }
  return out;
}
