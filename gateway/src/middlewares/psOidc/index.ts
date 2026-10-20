import { Context, Next } from 'hono';
import { reportAuthFailure } from '../psSecurityEvents';
import { constructConfigFromRequestHeaders } from '../../handlers/handlerUtils';
import { oidcSettings } from './config';
import {
  errorBody,
  OIDC_IDENTITY_KEY,
  OidcIdentity,
  oidcTargetDenied,
  policyDeniedResponse,
  targetModels,
} from './identity';
import { TokenError, verifyToken } from './jwks';
import {
  groupsFromClaims,
  hasGroupsOverage,
  matchGroup,
  subjectHash,
  takeRateLimit,
} from './policy';

/**
 * PS gateway OIDC/JWT authentication and group policy (opt-in,
 * `PS_OIDC_ENABLED=true`; see docs/guides/gateway-oidc.mdx).
 *
 * On every `/v1/*` route except `/v1/health`:
 * 1. `Authorization: Bearer <IdP JWT>` is verified (./jwks.ts). Missing or
 *    invalid: 401 and an `auth.failure` event (realm `oidc`).
 * 2. The first policy entry matching the groups claim applies; no match with
 *    `default: deny` is a 403 and a `policy.denied` event. The entry's rate
 *    limit is applied here (429).
 * 3. The request is rebuilt without the IdP token, so it can never reach a
 *    provider: not as the header-mode API key, not through the proxy routes'
 *    header pass-through, not through `forward_headers`. When
 *    PS_OIDC_PROVIDER_KEYS has a key for the `x-portkey-provider`, it is set
 *    as the provider credential instead (header mode only; never with
 *    `x-portkey-config`, `x-portkey-custom-host` or any other header that
 *    can move the upstream host, see gatewayKeyAllowed, so a caller cannot
 *    send the gateway's key to a host of their choosing). `x-ps-auth-subject`
 *    (the subject hash) is set for the cache scope, so cached responses are
 *    per user; `x-ps-*` headers are never sent upstream.
 * 4. The provider/model allowlist is enforced per target, on the provider and
 *    model actually called (after config, override_params and routing), by
 *    `oidcTargetDenied` in tryPost, before hooks, cache or upstream.
 */

export const SUBJECT_HEADER = 'x-ps-auth-subject';
export const GROUP_HEADER = 'x-ps-auth-group';

export {
  OIDC_IDENTITY_KEY,
  oidcIdentityOf,
  oidcTargetDenied,
  requestModel,
} from './identity';
export type { OidcIdentity } from './identity';

/** Routes that need a token when OIDC is on. */
export const isProtectedPath = (path: string): boolean =>
  path.startsWith('/v1/') && path !== '/v1/health';

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

const bearerToken = (header: string | undefined): string | undefined => {
  const m = /^Bearer\s+(\S+)\s*$/i.exec(header ?? '');
  return m?.[1];
};

let lastConfigError: string | undefined;

export function psOidc() {
  return async (c: Context, next: Next) => {
    const settings = oidcSettings(c);
    if (!settings.enabled || !isProtectedPath(c.req.path)) return next();

    if (settings.error !== undefined) {
      if (lastConfigError !== settings.error) {
        lastConfigError = settings.error;
        console.error(`[PS OIDC] invalid configuration: ${settings.error}`);
      }
      return c.json(
        errorBody(
          'Gateway authentication is misconfigured',
          'server_error',
          'oidc_misconfigured'
        ),
        500
      );
    }
    const config = settings.config;

    const token = bearerToken(c.req.header('authorization'));
    if (!token) {
      reportAuthFailure(c, 'oidc', 'missing_credentials', 401);
      c.header('WWW-Authenticate', 'Bearer realm="prompt-shields-gateway"');
      return c.json(
        errorBody(
          'Authentication required: send an access token from your identity provider as Authorization: Bearer <token>',
          'authentication_error',
          'missing_token'
        ),
        401
      );
    }

    let payload;
    try {
      payload = await verifyToken(token, config, c);
    } catch (e) {
      const reason = e instanceof TokenError ? e.reason : 'malformed';
      if (reason === 'jwks_unavailable') {
        reportAuthFailure(c, 'oidc', 'invalid_credentials', 503, reason);
        c.header('Retry-After', '30');
        return c.json(
          errorBody(
            'The identity provider signing keys are unavailable; try again shortly',
            'server_error',
            'idp_unavailable'
          ),
          503
        );
      }
      reportAuthFailure(c, 'oidc', 'invalid_credentials', 401, reason);
      c.header(
        'WWW-Authenticate',
        'Bearer realm="prompt-shields-gateway", error="invalid_token"'
      );
      return c.json(
        errorBody(
          `Invalid access token (${reason})`,
          'authentication_error',
          'invalid_token'
        ),
        401
      );
    }

    const identity: OidcIdentity = {
      subjectHash: await subjectHash(config, String(payload.sub)),
      subject: String(payload.sub),
      config,
    };

    if (config.policy) {
      identity.group = matchGroup(
        config.policy,
        groupsFromClaims(payload, config.groupsClaim)
      );
      if (!identity.group && config.policy.defaultAction === 'deny')
        return policyDeniedResponse(
          c,
          identity,
          'no_matching_group',
          undefined,
          undefined,
          hasGroupsOverage(payload, config.groupsClaim)
        );
    }
    c.set(OIDC_IDENTITY_KEY as any, identity);

    const headers = new Headers(c.req.raw.headers);

    // Realtime is a WebSocket that never reaches tryPost: check it here, on
    // the provider options the realtime handlers build (a config wins over
    // x-portkey-provider there, so it must here too).
    if (c.req.path === '/v1/realtime') {
      let options: any;
      try {
        options = constructConfigFromRequestHeaders(
          Object.fromEntries(headers)
        );
      } catch {
        options = undefined;
      }
      const provider =
        typeof options?.provider === 'string' ? options.provider : undefined;
      const response = oidcTargetDenied(c, provider, [
        c.req.query('model') || undefined,
        ...targetModels(options, undefined),
      ]);
      if (response) return response;
    }

    if (identity.group) {
      const retryAfter = takeRateLimit(identity.group, identity.subjectHash);
      if (retryAfter !== undefined) {
        c.header('Retry-After', String(retryAfter));
        return c.json(
          errorBody(
            `Rate limit for gateway policy group "${identity.group.name}" exceeded`,
            'rate_limit_error',
            'rate_limit_exceeded'
          ),
          429
        );
      }
    }

    // Rebuild the request without the IdP token (see the module comment).
    headers.delete('authorization');
    for (const name of [...headers.keys()])
      if (name.startsWith('x-ps-auth-')) headers.delete(name);
    headers.set(SUBJECT_HEADER, identity.subjectHash);
    if (identity.group) headers.set(GROUP_HEADER, identity.group.name);
    const provider = headers.get('x-portkey-provider')?.trim().toLowerCase();
    const providerKey =
      provider && Object.hasOwn(config.providerKeys, provider)
        ? config.providerKeys[provider]
        : undefined;
    if (providerKey && gatewayKeyAllowed(headers)) {
      headers.set('authorization', `Bearer ${providerKey}`);
    }
    c.req.raw = new Request(c.req.raw, { headers });

    return next();
  };
}
