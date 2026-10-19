/**
 * PS fork shim for Portkey 2.0.0's `src/utils/fetch.ts`. Upstream wraps
 * fetch with optional outbound-proxy agents from its agent store, which this
 * fork does not have; here both helpers are plain fetch, which is what the
 * call sites used before (#108). Ported provider files import these names.
 *
 * PS fork (security): every upstream call goes out with `redirect: 'manual'`
 * and a 3xx answer is replaced by a 502 gateway error. A followed redirect
 * would re-send the body and the provider key headers (`api-key`,
 * `x-api-key`, `x-goog-api-key`; fetch only drops `Authorization` on a
 * cross-origin hop) to whatever host the `Location` names, skipping the
 * isSafeUpstreamURL and origin checks the first URL passed. No provider API
 * the gateway calls answers with a redirect; presigned storage URLs (Azure
 * blob batch output, Cohere dataset parts, Fireworks uploads) are given in a
 * response body and fetched directly, not reached through a 3xx.
 */
export const UPSTREAM_REDIRECT_MODE: RequestInit['redirect'] = 'manual';

/** A response that would have been a followed redirect in `follow` mode. */
export function isRedirectResponse(response: Response): boolean {
  // Browsers hand back an opaque status-0 response; Node and Workers the 3xx.
  if ((response as { type?: string }).type === 'opaqueredirect') return true;
  const { status } = response;
  return status >= 300 && status < 400 && status !== 304;
}

/** The 502 a 3xx upstream answer becomes. It never carries the Location. */
export function upstreamRedirectErrorResponse(status: number): Response {
  return new Response(
    JSON.stringify({
      error: {
        message: `The upstream answered with a redirect (${status || 'opaque'}), which the gateway does not follow`,
        type: 'upstream_redirect',
        param: null,
        code: null,
      },
    }),
    { status: 502, headers: { 'content-type': 'application/json' } }
  );
}

/** Pass `response` through, or replace a 3xx with the 502 above. */
export function rejectUpstreamRedirect(response: Response): Response {
  if (!isRedirectResponse(response)) return response;
  try {
    response.body?.cancel().catch(() => {});
  } catch {
    /* already consumed or locked */
  }
  return upstreamRedirectErrorResponse(response.status);
}

/** fetch for upstream (provider, IdP, storage) calls: never follows a 3xx. */
export async function upstreamFetch(
  url: string,
  options: RequestInit = {}
): Promise<Response> {
  const response = await fetch(url, {
    ...options,
    redirect: UPSTREAM_REDIRECT_MODE,
  });
  return rejectUpstreamRedirect(response);
}

export async function externalServiceFetch(
  url: string,
  options?: RequestInit
): Promise<Response> {
  return upstreamFetch(url, options);
}

export async function internalServiceFetch(
  url: string,
  options: RequestInit
): Promise<Response> {
  return upstreamFetch(url, options);
}
