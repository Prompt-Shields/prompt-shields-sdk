/**
 * PS fork: SSRF-safe egress for guardrail plugins.
 *
 * Guardrail config (inline in `x-portkey-config`, or a stored config id)
 * comes from the caller, and with it the webhook / log URLs, vendor base-URL
 * overrides (`apiEndpoint`, `customHost`, `baseUrl`, `domain`, ...) and the
 * parts of vendor hosts built from credentials (Azure resource name, AWS
 * region). Every plugin request goes through `pluginRequest`, which:
 *
 * - refuses a URL that fails `isSafePluginEgressURL` (metadata, loopback,
 *   private and reserved addresses, internal TLDs; only hosts listed in
 *   TRUSTED_CUSTOM_HOSTS are exempt, localhost is never trusted by default);
 * - sends with `redirect: 'manual'` and treats any 3xx as a failure, so an
 *   allowed host cannot bounce the request (and its headers) to an internal
 *   one;
 * - aborts after a timeout that covers reading the body;
 * - reads at most `maxResponseBytes` of the response.
 *
 * A refused request throws `PluginEgressError`, which each plugin handles
 * like any other network error (its `failOnError` / fail-open behaviour is
 * unchanged).
 */
import { isSafePluginEgressURL } from '../src/middlewares/requestValidator';
import { isRedirectResponse } from '../src/utils/fetch';

/** Default cap on a plugin response body (guardrail answers are small). */
export const PLUGIN_MAX_RESPONSE_BYTES = 5 * 1024 * 1024;
export const PLUGIN_DEFAULT_TIMEOUT_MS = 5000;

export class PluginEgressError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'PluginEgressError';
  }
}

export interface PluginRequestOptions {
  timeout?: number;
  maxResponseBytes?: number;
  /**
   * Skip the URL check. Only for fixed, operator-gated URLs that are internal
   * by design (the Azure IMDS token endpoint); never for caller input.
   */
  allowInternalHost?: boolean;
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return 'an invalid URL';
  }
}

/** Throw unless `url` may be reached by a guardrail plugin. */
export function assertPluginEgressURL(url: string): void {
  if (!isSafePluginEgressURL(url)) {
    throw new PluginEgressError(
      `Guardrail request to ${hostOf(url)} refused: internal, metadata and private hosts are not allowed (see TRUSTED_CUSTOM_HOSTS)`
    );
  }
}

async function readCapped(
  response: Response,
  maxBytes: number
): Promise<string> {
  const declared = Number(response.headers.get('content-length'));
  if (Number.isFinite(declared) && declared > maxBytes) {
    await response.body?.cancel().catch(() => {});
    throw new PluginEgressError(
      `Guardrail response too large (over ${maxBytes} bytes)`
    );
  }
  if (!response.body) return '';
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maxBytes) {
      await reader.cancel().catch(() => {});
      throw new PluginEgressError(
        `Guardrail response too large (over ${maxBytes} bytes)`
      );
    }
    chunks.push(value);
  }
  const all = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    all.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(all);
}

/**
 * Send one guardrail request and read its body as text. The returned
 * `response` has its body consumed; use `text` instead. A timeout rejects
 * with the fetch `AbortError` (name `AbortError`), as plain fetch did.
 */
export async function pluginRequest(
  url: string,
  init: RequestInit = {},
  options: PluginRequestOptions = {}
): Promise<{ response: Response; text: string }> {
  const {
    timeout = PLUGIN_DEFAULT_TIMEOUT_MS,
    maxResponseBytes = PLUGIN_MAX_RESPONSE_BYTES,
    allowInternalHost = false,
  } = options;
  if (!allowInternalHost) assertPluginEgressURL(url);

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, {
      ...init,
      redirect: 'manual',
      signal: controller.signal,
    });
    if (isRedirectResponse(response)) {
      await response.body?.cancel().catch(() => {});
      throw new PluginEgressError(
        `Guardrail request to ${hostOf(url)} answered with a redirect (${response.status || 'opaque'}), which is not followed`
      );
    }
    if (String(init.method).toUpperCase() === 'HEAD') {
      // Content-Length on a HEAD answer describes a body that is not sent.
      await response.body?.cancel().catch(() => {});
      return { response, text: '' };
    }
    const text = await readCapped(response, maxResponseBytes);
    return { response, text };
  } finally {
    clearTimeout(timer);
  }
}
