/**
 * Whether a gateway-held provider key may be attached to a request (psOidc's
 * PS_OIDC_PROVIDER_KEYS, psVirtualKeys' pool credentials, #343). Kept apart
 * from ./index.ts so callers do not load the JWT verifier.
 */

/**
 * `x-portkey-*` headers whose values never become part of an upstream URL.
 * Every other `x-portkey-*` header may (`x-portkey-huggingface-base-url`,
 * `x-portkey-azure-foundry-url`, `x-portkey-azure-resource-name`,
 * `x-portkey-vertex-region`, ... are interpolated into the provider's base
 * URL), so it must look like a plain name for the gateway key to be used.
 */
const NON_URL_HEADERS = new Set([
  'x-portkey-provider',
  'x-portkey-metadata',
  'x-portkey-trace-id',
  'x-portkey-forward-headers',
  'x-portkey-openai-beta',
  'x-portkey-anthropic-beta',
]);
const PLAIN_NAME = /^[A-Za-z0-9._-]*$/;

/**
 * Whether the gateway's own provider key (PS_OIDC_PROVIDER_KEYS) may be
 * attached to this request: header mode only, with no caller config or
 * custom host, and every other `x-portkey-*` value a plain name (letters,
 * digits, `.`, `_`, `-`). A name can only fill its slot in the provider's
 * URL template (`<resource>.openai.azure.com`), never replace the host the
 * way a URL, `/`, `?`, `#` or `@` can.
 */
export function gatewayKeyAllowed(headers: Headers): boolean {
  if (headers.has('x-portkey-config') || headers.has('x-portkey-custom-host'))
    return false;
  for (const [name, value] of headers) {
    if (!name.startsWith('x-portkey-') || NON_URL_HEADERS.has(name)) continue;
    if (!PLAIN_NAME.test(value.trim())) return false;
  }
  return true;
}
