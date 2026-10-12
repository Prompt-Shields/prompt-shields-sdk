/**
 * PS fork: cap the size of responses from custom-host upstreams. A custom
 * host is any server the caller points the gateway at, so it must not be able
 * to stream an unbounded body into gateway memory. Mirrors the limit Portkey's
 * enterprise gateway added in 2.8 (closed source, so this is our own code).
 */

export const DEFAULT_MAX_CUSTOM_HOST_RESPONSE_BYTES = 50 * 1024 * 1024;

export function maxCustomHostResponseBytes(raw: unknown): number {
  const n = Number(raw);
  return Number.isInteger(n) && n > 0
    ? n
    : DEFAULT_MAX_CUSTOM_HOST_RESPONSE_BYTES;
}

export function limitResponseSize(
  response: Response,
  maxBytes: number
): Response {
  const declared = Number(response.headers.get('content-length'));
  if (Number.isFinite(declared) && declared > maxBytes) {
    return new Response(
      JSON.stringify({
        status: 'failure',
        message: `Upstream response of ${declared} bytes exceeds the ${maxBytes} byte limit for custom hosts`,
      }),
      { status: 502, headers: { 'content-type': 'application/json' } }
    );
  }
  if (!response.body) return response;

  let seen = 0;
  const counter = new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      seen += chunk.byteLength;
      if (seen > maxBytes) {
        controller.error(
          new Error(
            `Upstream response exceeded the ${maxBytes} byte limit for custom hosts`
          )
        );
        return;
      }
      controller.enqueue(chunk);
    },
  });

  return new Response(response.body.pipeThrough(counter), {
    status: response.status,
    statusText: response.statusText,
    headers: response.headers,
  });
}
