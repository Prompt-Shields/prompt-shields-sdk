import { Context } from 'hono';
import { InMemoryLruStore } from './store';
import { isSensitiveKey } from '../../utils/redact';

const store = new InMemoryLruStore({
  maxEntries: parseInt(process.env.PS_CACHE_MAX_ENTRIES || '1000', 10),
});

// test-only hook
export const __resetCacheForTests = () => {
  store.clear();
};

const CACHE_STATUS = {
  HIT: 'HIT',
  SEMANTIC_HIT: 'SEMANTIC HIT',
  MISS: 'MISS',
  SEMANTIC_MISS: 'SEMANTIC MISS',
  REFRESH: 'REFRESH',
  DISABLED: 'DISABLED',
};

// Only JSON-shaped bodies are cacheable. FormData (audio transcriptions,
// uploads), Blob, ArrayBuffer/typed arrays and ReadableStream bodies all
// JSON.stringify to `{}` (or a lossy form), so they would share one key and
// serve each other's responses. Hashing their bytes would mean buffering
// arbitrarily large uploads (and a stream is already consumed by the
// upstream call), so we bypass the cache for them instead.
export const isCacheableBody = (body: unknown): boolean => {
  if (Array.isArray(body)) return true;
  if (body === null || typeof body !== 'object') return false;
  const proto = Object.getPrototypeOf(body);
  return proto === Object.prototype || proto === null;
};

const sha256Hex = async (value: string): Promise<string> => {
  const digest = await crypto.subtle.digest(
    { name: 'SHA-256' },
    new TextEncoder().encode(value)
  );
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
};

// PS fork: the cache key is scoped to the caller. Upstream keyed on body +
// endpoint only, so with caching on, caller B was served caller A's cached
// response for an identical body. The scope covers every provider option and
// incoming header that decides whose upstream account answers: anything
// `isSensitiveKey` treats as a credential (API keys, secrets, tokens,
// Authorization) plus the identity fields below (virtual key, service
// account, role ARN, tenant, org/project, account, auth mode).
const IDENTITY_KEY =
  /(virtual[-_]?key|auth|passphrase|fingerprint|tenan|organi[sz]ation|project|account|role[-_]?arn|external[-_]?id|client[-_]?id|workspace|oracle[-_]?user)/i;

const isIdentityKey = (key: string): boolean =>
  isSensitiveKey(key) || IDENTITY_KEY.test(key);

// With awsAuthType assumedRole/serviceRole the gateway resolves these itself
// and writes them back onto the provider options before the cache lookup.
// They rotate with the STS session, and the caller's identity is already in
// awsAuthType/awsRoleArn/awsExternalId, so leave them out to keep keys stable.
const GATEWAY_DERIVED_AWS_KEYS = [
  'awsAccessKeyId',
  'awsSecretAccessKey',
  'awsSessionToken',
];

const canonicalJSON = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonicalJSON).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canonicalJSON(record[k])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
};

const pickIdentity = (
  record: Record<string, unknown> | undefined | null,
  skip: string[] = []
): [string, string][] =>
  Object.entries(record ?? {})
    .filter(
      ([k, v]) =>
        isIdentityKey(k) &&
        !skip.includes(k) &&
        v !== undefined &&
        v !== null &&
        v !== ''
    )
    .map(([k, v]): [string, string] => [k.toLowerCase(), canonicalJSON(v)])
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));

// Incoming headers the gateway sends upstream (all non-x-portkey headers on
// /v1/proxy, plus `forward_headers`) can carry credentials under any name,
// e.g. Azure APIM's `Ocp-Apim-Subscription-Key`. Those are scoped by value
// except for these per-request, non-identity headers, which would otherwise
// make every key unique.
const VOLATILE_FORWARDED_HEADERS = new Set([
  'accept-encoding',
  'baggage',
  'cdn-loop',
  'connection',
  'content-length',
  'date',
  'host',
  'sentry-trace',
  'traceparent',
  'tracestate',
  'x-amzn-trace-id',
  'x-correlation-id',
  'x-forwarded-for',
  'x-forwarded-host',
  'x-forwarded-port',
  'x-forwarded-proto',
  'x-real-ip',
  'x-request-id',
]);

const pickForwarded = (
  requestHeaders: Record<string, string> | undefined | null,
  endpoint: string | undefined,
  forwardHeaders: string[] | undefined
): [string, string][] => {
  const forwarded = new Set(
    (forwardHeaders ?? []).map((h) => String(h).trim().toLowerCase())
  );
  return Object.entries(requestHeaders ?? {})
    .map(([k, v]): [string, string] => [k.toLowerCase(), String(v)])
    .filter(
      ([k]) =>
        (forwarded.has(k) ||
          (endpoint === 'proxy' && !k.startsWith('x-portkey-'))) &&
        !VOLATILE_FORWARDED_HEADERS.has(k) &&
        !k.startsWith('cf-')
    )
    .sort(([a, av], [b, bv]) =>
      a < b ? -1 : a > b ? 1 : av < bv ? -1 : av > bv ? 1 : 0
    );
};

export interface CacheScopeInput {
  provider?: string;
  providerOption?: Record<string, any> | null;
  requestHeaders?: Record<string, string> | null;
  endpoint?: string;
  requestURL?: string;
  forwardHeaders?: string[];
}

/**
 * SHA-256 of who is calling which upstream: provider, the caller's credential
 * and identity fields (provider options and incoming headers), headers the
 * gateway forwards upstream, and the upstream URL (custom host, path and
 * query). Only this digest reaches the cache key; raw credentials never do.
 */
export const computeCacheScope = async ({
  provider,
  providerOption,
  requestHeaders,
  endpoint,
  requestURL,
  forwardHeaders,
}: CacheScopeInput): Promise<string> => {
  const ambientAws = ['assumedRole', 'serviceRole'].includes(
    providerOption?.awsAuthType
  );
  return sha256Hex(
    canonicalJSON({
      v: 1,
      provider: provider || providerOption?.provider || '',
      options: pickIdentity(
        providerOption,
        ambientAws ? GATEWAY_DERIVED_AWS_KEYS : []
      ),
      headers: pickIdentity(requestHeaders),
      forwarded: pickForwarded(requestHeaders, endpoint, forwardHeaders),
      // Always the full upstream URL: with no credentials (e.g. a keyless
      // self-hosted model) two custom hosts must not share entries.
      upstream: requestURL ?? '',
    })
  );
};

const getCacheKey = async (requestBody: any, url: string, scope: string) =>
  sha256Hex(`${JSON.stringify(requestBody)}-${url}-${scope}`);

const CACHE_SCOPES_CONTEXT_KEY = 'psCacheScopes';

/** Records the scope a target's cache read used (see memoryCache). */
export const setCacheScopeForTarget = (
  c: Context,
  targetIndex: number | string | undefined,
  scope: string
) => {
  const scopes = c.get(CACHE_SCOPES_CONTEXT_KEY) ?? {};
  c.set(CACHE_SCOPES_CONTEXT_KEY, { ...scopes, [String(targetIndex)]: scope });
};

const getCacheScopeForTarget = (
  c: Context,
  targetIndex: number | string | undefined
): string | undefined => c.get(CACHE_SCOPES_CONTEXT_KEY)?.[String(targetIndex)];

// Cache Handling
export const getFromCache = async (
  env: any,
  requestHeaders: any,
  requestBody: any,
  url: string,
  organisationId: string,
  cacheMode: string,
  cacheMaxAge: number | null,
  cacheScope?: string
) => {
  // Without a caller scope there is no tenant isolation: never read.
  if (!cacheScope) {
    return [null, CACHE_STATUS.DISABLED, null];
  }
  if ('x-portkey-cache-force-refresh' in requestHeaders) {
    return [null, CACHE_STATUS.REFRESH, null];
  }
  if (!isCacheableBody(requestBody)) {
    return [null, CACHE_STATUS.DISABLED, null];
  }
  try {
    const cacheKey = await getCacheKey(requestBody, url, cacheScope);
    const entry = await store.get(cacheKey);
    if (entry) return [entry.responseBody, CACHE_STATUS.HIT, cacheKey];
    return [null, CACHE_STATUS.MISS, null];
  } catch (error) {
    console.error('getFromCache error: ', error);
    return [null, CACHE_STATUS.MISS, null];
  }
};

export const putInCache = async (
  env: any,
  requestHeaders: any,
  requestBody: any,
  responseBody: any,
  url: string,
  organisationId: string,
  cacheMode: string | null,
  cacheMaxAge: number | null,
  cacheScope?: string
) => {
  // Without a caller scope there is no tenant isolation: never write.
  if (!cacheScope) return;
  if (!isCacheableBody(requestBody) || requestBody.stream === true) {
    // Do not cache multipart/binary bodies (see isCacheableBody) or streamed responses
    return;
  }

  const cacheKey = await getCacheKey(requestBody, url, cacheScope);
  await store.set(cacheKey, {
    responseBody: JSON.stringify(responseBody),
    maxAge: cacheMaxAge,
  });
};

export const memoryCache = () => {
  return async (c: Context, next: any) => {
    c.set('getFromCache', getFromCache);

    await next();

    let requestOptions = c.get('requestOptions');

    if (
      requestOptions &&
      Array.isArray(requestOptions) &&
      requestOptions.length > 0 &&
      requestOptions[0].requestParams.stream !== true
    ) {
      requestOptions = requestOptions[0];
      // Reuse the scope the read computed for this target: the logged
      // provider options are a snapshot taken before credentials resolve.
      const cacheScope = getCacheScopeForTarget(
        c,
        requestOptions.lastUsedOptionIndex
      );
      if (requestOptions.cacheMode === 'simple' && cacheScope) {
        try {
          const parsed = await requestOptions.response.clone().json();
          await putInCache(
            null,
            null,
            requestOptions.transformedRequest.body,
            parsed,
            requestOptions.providerOptions.rubeusURL,
            '',
            null,
            new Date().getTime() +
              (requestOptions.cacheMaxAge || 24 * 60 * 60 * 1000),
            cacheScope
          );
        } catch (error) {
          console.error('memoryCache write-back error (fail-open):', error);
        }
      }
    }
  };
};
