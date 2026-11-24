/**
 * PS fork (#343): gateway virtual keys (opt-in, PS_VIRTUAL_KEYS_ENABLED;
 * see docs/guides/gateway-virtual-keys.mdx).
 *
 * A call may carry a virtual key (`ps-vk-...`) instead of a provider key:
 * in `X-PS-Virtual-Key`, as `Authorization: Bearer ps-vk-...`, or in
 * `x-api-key` (Anthropic SDKs). With OIDC on (psOidc runs first and needs
 * the IdP token in Authorization), send it in `X-PS-Virtual-Key`. Then:
 *
 * 1. It is resolved with the collector (./resolver.ts; cached 30 s, so a
 *    revocation reaches every replica within 30 s). Unknown, revoked or
 *    expired: 401 `invalid_virtual_key` and an `auth.failure` event (realm
 *    `virtual_key`). Collector unreachable: 503, fail closed.
 * 2. Header mode only: with `x-portkey-config`, `x-portkey-custom-host` or
 *    any other header that can move the upstream host (psOidc
 *    gatewayKeyAllowed), 400, so pooled keys can never be sent to a host of
 *    the caller's choosing. Realtime (WebSocket) is refused (400).
 * 3. `x-portkey-provider` may be left out when the pool holds one provider.
 *    A provider the pool has no credential for: 403.
 * 4. The request is rebuilt: the virtual key is removed; Authorization
 *    carries the first credential to try (so handlers that do not go
 *    through tryPost still work), and X-PS-Team / X-PS-Project are set from
 *    the key's binding, replacing whatever the caller declared (removed
 *    when the key has none), so rate limits, budgets, guardrails and
 *    telemetry attribute the call to the key's team and project. The
 *    resolution is kept on the context (VIRTUAL_KEY_CONTEXT), never in a
 *    header.
 * 5. Per target, in tryPost (handlers/handlerUtils.ts): the model actually
 *    sent must match the key's `allowed_models` (403
 *    `virtual_key_model_not_allowed`), the target's provider must be in the
 *    pool (403), and the target is sent with a pool credential of its
 *    provider (./pool.ts order). On 429 the call is retried with the
 *    provider's next credential before anything else changes (no model
 *    change, no other provider); 401 and 403 also move on and mark the
 *    credential unhealthy. Streamed and multipart bodies are not replayed.
 *    Telemetry's `api_key_fingerprint` is the serving credential's
 *    fingerprint (sha256(key)[:16], as the vault and SDK compute it), and
 *    `user_id` is the key's principal (`principal:<id>`) when the call has
 *    no OIDC subject.
 *
 * Provider keys exist only in memory: never in logs, telemetry, errors or
 * responses (the redaction in utils/redact.ts already covers Authorization
 * and api keys). Without a virtual key on the request, nothing changes:
 * header mode with the caller's own key keeps working.
 */
import { Context, Next } from 'hono';
import { env } from 'hono/adapter';
import { reportAuthFailure } from '../psSecurityEvents';
import { gatewayKeyAllowed } from '../psOidc/gatewayKey';
import { errorBody } from '../psOidc/identity';
import { CREDENTIAL_FINGERPRINT_KEY } from '../../utils/credentialFingerprint';
import {
  defaultResolver,
  Resolution,
  VirtualKeyInfo,
  virtualKeyOf,
  virtualKeySettings,
  VirtualKeyResolver,
} from './resolver';
import { modelAllowed, PoolCredential, poolState, PoolState } from './pool';

export const VIRTUAL_KEY_CONTEXT = 'psVirtualKey';
export const VIRTUAL_KEY_ID_HEADER = 'x-ps-virtual-key-id';
export const OIDC_IDENTITY_KEY = 'psOidcIdentity';
/** Statuses that move a call to the provider's next pool credential. */
export const FAILOVER_STATUSES = new Set([401, 403, 429]);

export interface VirtualKeyContext {
  virtualKey: VirtualKeyInfo;
  credentials: PoolCredential[];
  /** The credential that served (or last tried) the call. */
  servedBy?: PoolCredential;
}

export function virtualKeyOfContext(c: {
  get: (key: string) => unknown;
}): VirtualKeyContext | undefined {
  const v = c.get(VIRTUAL_KEY_CONTEXT) as VirtualKeyContext | undefined;
  return v && typeof v === 'object' && v.virtualKey ? v : undefined;
}

let lastConfigError: string | undefined;

export function psVirtualKeys(resolver: VirtualKeyResolver = defaultResolver) {
  return async (c: Context, next: Next) => {
    if (!c.req.path.startsWith('/v1/') || c.req.path === '/v1/health')
      return next();
    const settings = virtualKeySettings(env(c) as Record<string, any>);
    if (!settings.enabled) return next();
    if (c.req.raw.headers.has(VIRTUAL_KEY_ID_HEADER)) {
      const stripped = new Headers(c.req.raw.headers);
      stripped.delete(VIRTUAL_KEY_ID_HEADER);
      c.req.raw = new Request(c.req.raw, { headers: stripped });
    }
    const found = virtualKeyOf(c.req.raw.headers);
    if (!found) return next();

    if (settings.error !== undefined) {
      if (lastConfigError !== settings.error) {
        lastConfigError = settings.error;
        console.error(
          `[PS virtual keys] invalid configuration: ${settings.error}`
        );
      }
      return c.json(
        errorBody(
          'Virtual keys are misconfigured on this gateway',
          'server_error',
          'virtual_keys_misconfigured'
        ),
        500
      );
    }

    const headers = new Headers(c.req.raw.headers);
    if (c.req.path === '/v1/realtime')
      return c.json(
        errorBody(
          'Virtual keys are not supported on /v1/realtime',
          'invalid_request_error',
          'virtual_key_unsupported_route'
        ),
        400
      );
    if (!gatewayKeyAllowed(headers))
      return c.json(
        errorBody(
          'A virtual key works in header mode only: x-portkey-config, x-portkey-custom-host and URL-valued x-portkey-* headers cannot be combined with it',
          'invalid_request_error',
          'virtual_key_header_mode_only'
        ),
        400
      );

    let resolution: Resolution;
    try {
      resolution = await resolver.resolve(found.key, settings);
    } catch (e: any) {
      console.warn(
        `[PS virtual keys] resolution failed: ${String(e?.message ?? e).slice(0, 200)}`
      );
      c.header('Retry-After', '5');
      return c.json(
        errorBody(
          'Virtual key resolution is unavailable; try again shortly',
          'server_error',
          'virtual_key_resolution_unavailable'
        ),
        503
      );
    }
    if (!resolution.active) {
      reportAuthFailure(c, 'virtual_key', 'invalid_credentials', 401);
      c.header('WWW-Authenticate', 'Bearer realm="prompt-shields-gateway"');
      return c.json(
        errorBody(
          'Invalid virtual key (unknown, revoked or expired)',
          'authentication_error',
          'invalid_virtual_key'
        ),
        401
      );
    }

    let provider = headers.get('x-portkey-provider')?.trim().toLowerCase();
    const providers = [
      ...new Set(resolution.credentials.map((x) => x.provider)),
    ];
    if (!provider) {
      if (providers.length !== 1)
        return c.json(
          errorBody(
            providers.length
              ? `Set x-portkey-provider: this virtual key's pool holds ${providers.join(', ')}`
              : 'This virtual key has no usable credential',
            providers.length ? 'invalid_request_error' : 'server_error',
            providers.length
              ? 'provider_required'
              : 'virtual_key_no_credentials'
          ),
          providers.length ? 400 : 503
        );
      provider = providers[0];
      headers.set('x-portkey-provider', provider);
    }
    // The provider's first credential in pool order (stable, so the cache
    // scope does not change call to call); tryPost picks per strategy.
    const first = resolution.credentials.find((x) => x.provider === provider);
    if (!first)
      return c.json(
        errorBody(
          `This virtual key has no usable credential for provider "${provider}"`,
          'permission_error',
          'virtual_key_provider_not_allowed'
        ),
        403
      );

    const ctx: VirtualKeyContext = {
      virtualKey: resolution.virtualKey,
      credentials: resolution.credentials,
      servedBy: first,
    };
    c.set(VIRTUAL_KEY_CONTEXT as any, ctx);
    c.set(CREDENTIAL_FINGERPRINT_KEY as any, first.key_fingerprint);

    headers.delete('x-ps-virtual-key');
    // For the cache scope (middlewares/cache: a per-key cache), never for
    // trust: rate limits read the context. x-ps-* never goes upstream.
    headers.set(VIRTUAL_KEY_ID_HEADER, resolution.virtualKey.id);
    if (found.header === 'x-api-key') headers.delete('x-api-key');
    headers.set('authorization', `Bearer ${first.api_key}`);
    for (const [name, value] of [
      ['x-ps-team', resolution.virtualKey.team_id],
      ['x-ps-project', resolution.virtualKey.project_id],
    ] as const) {
      if (value) headers.set(name, value);
      else headers.delete(name);
    }
    c.req.raw = new Request(c.req.raw, { headers });
    return next();
  };
}

/**
 * tryPost's check for one target (module doc, step 5): a Response to
 * return instead of calling the provider, or undefined. Only on model
 * endpoints is the model checked; every endpoint needs a pooled provider.
 */
export function virtualKeyTargetDenied(
  c: Context,
  provider: string | undefined,
  models: (string | undefined)[],
  isModelEndpoint: boolean
): Response | undefined {
  const ctx = virtualKeyOfContext(c);
  if (!ctx) return undefined;
  const p = (provider ?? '').toLowerCase();
  if (!ctx.credentials.some((x) => x.provider === p))
    return c.json(
      errorBody(
        `This virtual key has no credential for provider "${provider ?? ''}"`,
        'permission_error',
        'virtual_key_provider_not_allowed'
      ),
      403
    );
  if (isModelEndpoint && ctx.virtualKey.allowed_models !== null) {
    const named = models.filter((m): m is string => !!m);
    const denied = named.length
      ? named.find((m) => !modelAllowed(ctx.virtualKey.allowed_models, m))
      : '(none)';
    if (denied !== undefined)
      return c.json(
        errorBody(
          `Model "${denied}" is not allowed for this virtual key`,
          'permission_error',
          'virtual_key_model_not_allowed'
        ),
        403
      );
  }
  return undefined;
}

/**
 * Calls `run` with the target's pool credentials in order until one
 * answers something other than 401/403/429, or none is left (the last
 * answer is returned). `replayable` false (a streamed or multipart body)
 * makes a single attempt. Without a virtual key, `run` is called once
 * with undefined.
 */
export async function withPoolCredentials(
  c: Context,
  provider: string | undefined,
  replayable: boolean,
  run: (credential: PoolCredential | undefined) => Promise<Response>,
  state: PoolState = poolState
): Promise<Response> {
  const ctx = virtualKeyOfContext(c);
  if (!ctx) return run(undefined);
  const order = state.order(
    ctx.virtualKey.id,
    ctx.virtualKey.strategy,
    (provider ?? '').toLowerCase(),
    ctx.credentials
  );
  const candidates = replayable ? order : order.slice(0, 1);
  let response: Response | undefined;
  for (let i = 0; i < candidates.length; i++) {
    const cred = candidates[i];
    ctx.servedBy = cred;
    c.set(CREDENTIAL_FINGERPRINT_KEY as any, cred.key_fingerprint);
    response = await run(cred);
    const status = response.status;
    if (status === 429) state.markLimited(cred.id);
    else if (status === 401 || status === 403) state.markUnhealthy(cred.id);
    else if (response.ok) state.markHealthy(cred.id);
    if (!FAILOVER_STATUSES.has(status) || i === candidates.length - 1)
      return response;
    // Moving on: drop this answer's body. Not awaited: the mapped body can
    // be a transform whose cancel settles only when its source does.
    void response.body?.cancel().catch(() => undefined);
  }
  return response as Response;
}

/**
 * Telemetry fields for a call made with a virtual key: `user_id` (the
 * key's principal) when the call has no OIDC identity, whose subject wins.
 */
export function virtualKeyTelemetryFields(c: {
  get: (key: string) => unknown;
}): { user_id?: string } {
  const ctx = virtualKeyOfContext(c);
  if (!ctx?.virtualKey.user_id || c.get(OIDC_IDENTITY_KEY)) return {};
  return { user_id: ctx.virtualKey.user_id };
}
