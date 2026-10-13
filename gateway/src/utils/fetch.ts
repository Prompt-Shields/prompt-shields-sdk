/**
 * PS fork shim for Portkey 2.0.0's `src/utils/fetch.ts`. Upstream wraps
 * fetch with optional outbound-proxy agents from its agent store, which this
 * fork does not have; here both helpers are plain fetch, which is what the
 * call sites used before (#108). Ported provider files import these names.
 */
export async function externalServiceFetch(
  url: string,
  options?: RequestInit
): Promise<Response> {
  return fetch(url, options);
}

export async function internalServiceFetch(
  url: string,
  options: RequestInit
): Promise<Response> {
  return fetch(url, options);
}
