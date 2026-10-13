/**
 * PS fork: OAuth 2.0 client credentials (RFC 6749 section 4.4) for
 * OpenAI-compatible upstreams, so tenants need not store static provider keys.
 * Re-implemented from Portkey's enterprise changelog (#76).
 *
 * Security notes:
 * - The token URL comes from the caller, so it must be https and pass
 *   isSafeUpstreamURL (no internal or metadata hosts). When the operator sets
 *   OAUTH_TOKEN_URL_ALLOWED_HOSTS the host must be on that list; otherwise IP
 *   literals and loopback / internal names are refused too. Redirects are not
 *   followed.
 * - Tokens are cached per (token URL, client id, client secret, scope,
 *   audience) via a SHA-256 key, so a caller only gets a cached token if it
 *   presented the same secret. Nothing is shared across clients.
 * - The client secret and token never reach error messages or logs; upstream
 *   error bodies are dropped.
 */
import { Context } from 'hono';
import { GatewayError } from '../errors/GatewayError';
import { isSafeUpstreamURL } from '../middlewares/requestValidator';
import { Environment } from './env';
import { externalServiceFetch } from './fetch';

export interface OAuthClientCredentialsOptions {
  oauthTokenUrl?: string;
  oauthClientId?: string;
  oauthClientSecret?: string;
  oauthScope?: string;
  oauthAudience?: string;
}

const INTERNAL_SUFFIXES = ['.internal', '.local', '.localdomain'];
const MAX_CACHE_ENTRIES = 1000;
const MAX_REFRESH_SKEW_MS = 60 * 1000;
const DEFAULT_LIFETIME_MS = 5 * 60 * 1000;

const tokenCache = new Map<string, { token: string; expiresAt: number }>();
const inflight = new Map<string, Promise<string>>();

export function hasOAuthClientCredentials(
  options: OAuthClientCredentialsOptions
): boolean {
  return !!(
    options.oauthTokenUrl ||
    options.oauthClientId ||
    options.oauthClientSecret
  );
}

function allowedHosts(c?: Context | Record<string, any>): string[] {
  const raw = Environment(c as any).OAUTH_TOKEN_URL_ALLOWED_HOSTS;
  if (!raw) return [];
  return String(raw)
    .split(',')
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean);
}

export function assertSafeTokenUrl(
  tokenUrl: string,
  c?: Context | Record<string, any>
): void {
  let parsed: URL;
  try {
    parsed = new URL(tokenUrl);
  } catch {
    throw new GatewayError('Invalid OAuth token URL', 400);
  }
  if (
    parsed.protocol !== 'https:' ||
    parsed.username ||
    parsed.password ||
    !isSafeUpstreamURL(tokenUrl, c as Context)
  ) {
    throw new GatewayError('Invalid OAuth token URL', 400);
  }
  const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, '');
  const allow = allowedHosts(c);
  if (allow.length) {
    // An explicit operator allowlist may name an internal IdP.
    if (!allow.includes(host)) {
      throw new GatewayError('OAuth token URL host is not allowed', 403);
    }
    return;
  }
  // isSafeUpstreamURL lets TRUSTED_CUSTOM_HOSTS through (localhost and
  // 127.0.0.1 by default, for custom_host dev setups). A caller-chosen token
  // endpoint gets no such exemption: no IP literals, no loopback names.
  if (
    /^[\d.]+$/.test(host) ||
    host.includes(':') ||
    host === 'localhost' ||
    host.endsWith('.localhost') ||
    host === 'host.docker.internal' ||
    INTERNAL_SUFFIXES.some((s) => host.endsWith(s))
  ) {
    throw new GatewayError('Invalid OAuth token URL', 400);
  }
}

async function sha256Hex(input: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(input)
  );
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

/** Cache key: never contains the secret itself, only its hash. */
export async function oauthCacheKey(
  tokenUrl: string,
  clientId: string,
  clientSecret: string,
  scope = '',
  audience = ''
): Promise<string> {
  const secretHash = await sha256Hex(clientSecret);
  return sha256Hex(
    JSON.stringify([tokenUrl, clientId, secretHash, scope, audience])
  );
}

async function requestToken(
  tokenUrl: string,
  clientId: string,
  clientSecret: string,
  scope?: string,
  audience?: string
): Promise<{ token: string; lifetimeMs: number }> {
  const body = new URLSearchParams({
    grant_type: 'client_credentials',
    client_id: clientId,
    client_secret: clientSecret,
  });
  if (scope) body.set('scope', scope);
  if (audience) body.set('audience', audience);

  let response: Response;
  try {
    response = await externalServiceFetch(tokenUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        Accept: 'application/json',
      },
      body,
      redirect: 'manual',
    });
  } catch {
    throw new GatewayError('OAuth token request failed', 502);
  }
  if (!response.ok) {
    // Deliberately drop the body: some servers echo request parameters.
    throw new GatewayError(
      `OAuth token request failed with status ${response.status}`,
      502
    );
  }
  let json: Record<string, any>;
  try {
    json = await response.json();
  } catch {
    throw new GatewayError('OAuth token response was invalid', 502);
  }
  const token = json?.access_token;
  if (typeof token !== 'string' || !token) {
    throw new GatewayError('OAuth token response was invalid', 502);
  }
  const expiresIn = Number(json.expires_in);
  const lifetimeMs =
    Number.isFinite(expiresIn) && expiresIn > 0
      ? expiresIn * 1000
      : DEFAULT_LIFETIME_MS;
  return { token, lifetimeMs };
}

/**
 * Exchanges caller-supplied client credentials for an access token, cached
 * until shortly before it expires. Validates the token URL before any
 * network call.
 */
export async function getOAuthClientCredentialsToken(
  options: OAuthClientCredentialsOptions,
  c?: Context | Record<string, any>
): Promise<string> {
  const { oauthTokenUrl, oauthClientId, oauthClientSecret } = options;
  if (!oauthTokenUrl || !oauthClientId || !oauthClientSecret) {
    throw new GatewayError(
      'OAuth client credentials require a token URL, client id and client secret',
      400
    );
  }
  assertSafeTokenUrl(oauthTokenUrl, c);

  const key = await oauthCacheKey(
    oauthTokenUrl,
    oauthClientId,
    oauthClientSecret,
    options.oauthScope,
    options.oauthAudience
  );
  const cached = tokenCache.get(key);
  if (cached && cached.expiresAt > Date.now()) return cached.token;
  if (cached) tokenCache.delete(key);

  let pending = inflight.get(key);
  if (!pending) {
    pending = requestToken(
      oauthTokenUrl,
      oauthClientId,
      oauthClientSecret,
      options.oauthScope,
      options.oauthAudience
    )
      .then(({ token, lifetimeMs }) => {
        const skew = Math.min(MAX_REFRESH_SKEW_MS, lifetimeMs / 2);
        if (tokenCache.size >= MAX_CACHE_ENTRIES) {
          const oldest = tokenCache.keys().next().value;
          if (oldest !== undefined) tokenCache.delete(oldest);
        }
        tokenCache.set(key, {
          token,
          expiresAt: Date.now() + lifetimeMs - skew,
        });
        return token;
      })
      .finally(() => inflight.delete(key));
    inflight.set(key, pending);
  }
  return pending;
}

/** Test hook. */
export function _resetOAuthTokenCache() {
  tokenCache.clear();
  inflight.clear();
}
