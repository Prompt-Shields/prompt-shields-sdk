import { Context } from 'hono';
import {
  createLocalJWKSet,
  decodeProtectedHeader,
  jwtVerify,
  JSONWebKeySet,
  JWK,
  JWTPayload,
} from 'jose';
import { isSafeUpstreamURL } from '../requestValidator';
import { upstreamFetch } from '../../utils/fetch';
import { OidcConfig } from './config';

/**
 * JWT verification against the issuer's JWKS, with jose (WebCrypto, so the
 * same code runs on Node and Workers).
 *
 * JWKS and discovery documents are fetched with `upstreamFetch` (redirects
 * are never followed) after `isSafeUpstreamURL`, over https only, with a
 * timeout and a size cap. Keys are cached for the response's
 * `Cache-Control: max-age` (clamped to MIN_TTL_MS..MAX_TTL_MS). A token
 * whose `kid` is not in the cache triggers a refresh, but refreshes for any
 * reason happen at most once per REFRESH_COOLDOWN_MS per key set, so a flood
 * of tokens with random `kid`s cannot turn the gateway against the IdP.
 * When a refresh fails, the previous keys keep being used.
 */

export const MIN_TTL_MS = 60_000;
export const MAX_TTL_MS = 24 * 60 * 60_000;
export const DEFAULT_TTL_MS = 10 * 60_000;
export const REFRESH_COOLDOWN_MS = 30_000;
export const FETCH_TIMEOUT_MS = 5_000;
export const MAX_DOCUMENT_BYTES = 512 * 1024;
export const MAX_TOKEN_LENGTH = 16 * 1024;

/** Why a token was refused; `ps.auth.detail` in auth.failure events. */
export type TokenFailure =
  | 'malformed'
  | 'disallowed_alg'
  | 'unknown_key'
  | 'bad_signature'
  | 'expired'
  | 'not_yet_valid'
  | 'wrong_issuer'
  | 'wrong_audience'
  | 'invalid_claims'
  | 'jwks_unavailable';

export class TokenError extends Error {
  constructor(readonly reason: TokenFailure) {
    super(reason);
  }
}

let now = () => Date.now();

/** https, no credentials, not internal unless TRUSTED_CUSTOM_HOSTS lists it. */
export function isAllowedIdpUrl(url: string, c?: Context): boolean {
  try {
    return new URL(url).protocol === 'https:' && isSafeUpstreamURL(url, c);
  } catch {
    return false;
  }
}

/** max-age from Cache-Control, clamped; DEFAULT_TTL_MS without one. */
export function ttlFromCacheControl(header: string | null): number {
  if (!header) return DEFAULT_TTL_MS;
  const h = header.toLowerCase();
  if (/(^|[,\s])(no-store|no-cache)\b/.test(h)) return MIN_TTL_MS;
  const m = /(?:^|[,\s])max-age\s*=\s*"?(\d+)"?/.exec(h);
  if (!m) return DEFAULT_TTL_MS;
  const ms = Number(m[1]) * 1000;
  return Math.min(MAX_TTL_MS, Math.max(MIN_TTL_MS, ms));
}

async function fetchDocument(
  url: string,
  c?: Context
): Promise<{ json: any; ttlMs: number }> {
  if (!isAllowedIdpUrl(url, c)) throw new Error(`refused IdP URL ${url}`);
  const res = await upstreamFetch(url, {
    method: 'GET',
    headers: { accept: 'application/json' },
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
  if (!res.ok) {
    res.body?.cancel().catch(() => {});
    throw new Error(`IdP answered ${res.status} for ${url}`);
  }
  const declared = Number(res.headers.get('content-length'));
  if (Number.isFinite(declared) && declared > MAX_DOCUMENT_BYTES) {
    res.body?.cancel().catch(() => {});
    throw new Error(`IdP document too large: ${url}`);
  }
  const buf = await res.arrayBuffer();
  if (buf.byteLength > MAX_DOCUMENT_BYTES)
    throw new Error(`IdP document too large: ${url}`);
  const json = JSON.parse(new TextDecoder().decode(buf));
  return { json, ttlMs: ttlFromCacheControl(res.headers.get('cache-control')) };
}

type KeyResolver = ReturnType<typeof createLocalJWKSet>;

/** Only public asymmetric signing keys; never `oct` (shared-secret) keys. */
function usableKeys(jwks: any): JWK[] {
  if (!jwks || !Array.isArray(jwks.keys)) throw new Error('not a JWKS');
  return (jwks.keys as JWK[]).filter(
    (k) =>
      k &&
      typeof k === 'object' &&
      ['RSA', 'EC', 'OKP'].includes(k.kty as string) &&
      (k.use === undefined || k.use === 'sig') &&
      !('d' in k)
  );
}

export class JwksSource {
  private keys?: KeyResolver;
  private expiresAt = 0;
  private lastAttempt = -Infinity;
  private inflight?: Promise<void>;
  private jwksUri?: string;
  lastError?: string;

  constructor(
    private readonly issuer: string,
    private readonly configuredJwksUrl?: string
  ) {
    this.jwksUri = configuredJwksUrl;
  }

  private async resolveJwksUri(c?: Context): Promise<string> {
    if (this.jwksUri) return this.jwksUri;
    const base = this.issuer.replace(/\/+$/, '');
    const { json } = await fetchDocument(
      `${base}/.well-known/openid-configuration`,
      c
    );
    // OIDC Discovery 4.3: the document's issuer must be the one we expect.
    if (json?.issuer !== this.issuer)
      throw new Error('discovery issuer does not match PS_OIDC_ISSUER');
    if (typeof json?.jwks_uri !== 'string')
      throw new Error('discovery has no jwks_uri');
    this.jwksUri = json.jwks_uri;
    return json.jwks_uri;
  }

  /** Fetch now unless one is running or the cooldown has not passed. */
  refresh(c?: Context): Promise<void> {
    if (this.inflight) return this.inflight;
    if (now() - this.lastAttempt < REFRESH_COOLDOWN_MS)
      return Promise.resolve();
    this.lastAttempt = now();
    this.inflight = (async () => {
      try {
        const uri = await this.resolveJwksUri(c);
        const { json, ttlMs } = await fetchDocument(uri, c);
        const keys = usableKeys(json);
        if (!keys.length) throw new Error('JWKS has no usable signing keys');
        this.keys = createLocalJWKSet({ keys } as JSONWebKeySet);
        this.expiresAt = now() + ttlMs;
        this.lastError = undefined;
      } catch (e: any) {
        this.lastError = String(e?.message ?? e);
        // Discovery is retried with the next refresh.
        if (!this.configuredJwksUrl) this.jwksUri = undefined;
      } finally {
        this.inflight = undefined;
      }
    })();
    return this.inflight;
  }

  /** Current keys, refreshed first when missing or expired. */
  async resolver(c?: Context): Promise<KeyResolver> {
    if (!this.keys || now() >= this.expiresAt) await this.refresh(c);
    if (!this.keys) throw new TokenError('jwks_unavailable');
    return this.keys;
  }

  /** Refresh because a token named a key we do not have (rate-limited). */
  async refreshForUnknownKey(c?: Context): Promise<boolean> {
    const before = this.keys;
    await this.refresh(c);
    return this.keys !== before;
  }
}

const sources = new Map<string, JwksSource>();
const MAX_SOURCES = 16;

export function jwksSourceFor(config: OidcConfig): JwksSource {
  const key = `${config.issuer}\n${config.jwksUrl ?? ''}`;
  let source = sources.get(key);
  if (!source) {
    if (sources.size >= MAX_SOURCES) sources.clear();
    source = new JwksSource(config.issuer, config.jwksUrl);
    sources.set(key, source);
  }
  return source;
}

function failureOf(e: any): TokenFailure {
  if (e instanceof TokenError) return e.reason;
  switch (e?.code) {
    case 'ERR_JWT_EXPIRED':
      return 'expired';
    case 'ERR_JWT_CLAIM_VALIDATION_FAILED':
      if (e.claim === 'iss') return 'wrong_issuer';
      if (e.claim === 'aud') return 'wrong_audience';
      if (e.claim === 'nbf') return 'not_yet_valid';
      return 'invalid_claims';
    case 'ERR_JOSE_ALG_NOT_ALLOWED':
    case 'ERR_JOSE_NOT_SUPPORTED':
      return 'disallowed_alg';
    case 'ERR_JWKS_NO_MATCHING_KEY':
    case 'ERR_JWKS_MULTIPLE_MATCHING_KEYS':
      return 'unknown_key';
    case 'ERR_JWS_SIGNATURE_VERIFICATION_FAILED':
      return 'bad_signature';
    default:
      return 'malformed';
  }
}

/**
 * Verifies signature, `alg` allowlist, `iss`, `aud`, `exp`/`nbf` (with the
 * configured skew) and the presence of `exp` and `sub`. Throws TokenError.
 */
export async function verifyToken(
  token: string,
  config: OidcConfig,
  c?: Context
): Promise<JWTPayload> {
  if (
    !token ||
    token.length > MAX_TOKEN_LENGTH ||
    token.split('.').length !== 3
  )
    throw new TokenError('malformed');
  let alg: unknown;
  try {
    alg = decodeProtectedHeader(token).alg;
  } catch {
    throw new TokenError('malformed');
  }
  // Checked before any key lookup, so `none`/HS* tokens never cause a fetch.
  if (typeof alg !== 'string' || !config.algorithms.includes(alg))
    throw new TokenError('disallowed_alg');

  const source = jwksSourceFor(config);
  const options = {
    issuer: config.issuer,
    audience: config.audiences,
    algorithms: config.algorithms,
    clockTolerance: config.clockSkewSeconds,
    requiredClaims: ['exp', 'sub'],
  };
  try {
    const keys = await source.resolver(c);
    try {
      return (await jwtVerify(token, keys, options)).payload;
    } catch (e: any) {
      if (
        e?.code === 'ERR_JWKS_NO_MATCHING_KEY' &&
        (await source.refreshForUnknownKey(c))
      ) {
        return (await jwtVerify(token, await source.resolver(c), options))
          .payload;
      }
      throw e;
    }
  } catch (e: any) {
    throw new TokenError(failureOf(e));
  }
}

/** Test helpers. */
export function _resetJwksForTests() {
  sources.clear();
  now = () => Date.now();
}
export function _setClockForTests(fn: () => number) {
  now = fn;
}
