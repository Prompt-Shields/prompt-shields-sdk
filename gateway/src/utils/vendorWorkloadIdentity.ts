/**
 * PS fork (#76): workload identity federation for the `openai` and
 * `anthropic` providers. Re-implemented from the vendors' published
 * protocols, not from Portkey code:
 *
 * - OpenAI: RFC 8693 token exchange, JSON POST to
 *   https://auth.openai.com/oauth/token with `grant_type`
 *   `urn:ietf:params:oauth:grant-type:token-exchange`, `subject_token`,
 *   `subject_token_type` (`...:token-type:jwt` or `...:token-type:id_token`),
 *   `identity_provider_id` and `service_account_id`
 *   (developers.openai.com/api/docs/guides/workload-identity-federation;
 *   `openai.auth._workload` in the official Python SDK).
 * - Anthropic: RFC 7523 jwt-bearer grant, JSON POST to
 *   https://api.anthropic.com/v1/oauth/token with `assertion`,
 *   `federation_rule_id`, `organization_id`, `service_account_id` and an
 *   optional `workspace_id`, and `anthropic-beta:
 *   oauth-2025-04-20,oidc-federation-2026-04-01`. The minted token is sent
 *   as `Authorization: Bearer` with `anthropic-beta: oauth-2025-04-20`
 *   (platform.claude.com/docs/en/manage-claude/wif-reference).
 *
 * The subject token is the gateway HOST's identity (a projected Kubernetes
 * service-account token, an EKS/GKE/AKS token file, ...), read from a file on
 * every exchange so rotated tokens are picked up. Like the other host-identity
 * modes (AWS serviceRole, Azure managed/workload, Vertex workload) it is
 * refused unless the operator opts in, and every federation parameter comes
 * from the operator's environment, never from the caller: a caller only picks
 * `openaiAuthType` / `anthropicAuthType: workload`.
 *
 * Security notes:
 * - The minted token belongs to the operator's vendor service account, so it
 *   is only sent to the vendor's own API: a caller custom host is refused
 *   before any exchange.
 * - The token URL (the vendor default, or an operator override) goes through
 *   assertSafeTokenUrl, the same safe-egress check as caller OAuth token URLs;
 *   the vendor's default host passes OAUTH_TOKEN_URL_ALLOWED_HOSTS.
 *   Redirects are not followed and token endpoint bodies are dropped.
 * - Tokens are cached in memory per (vendor, token URL, federation ids) and
 *   refreshed shortly before they expire (5 minutes, or half the lifetime for
 *   short tokens); concurrent requests share one exchange; failures are not
 *   cached. Neither the subject token nor the minted token reaches errors,
 *   logs or telemetry.
 */
import { getRuntimeKey } from 'hono/adapter';
import { GatewayError } from '../errors/GatewayError';
import { HEADER_KEYS } from '../globals';
import { Environment } from './env';
import { externalServiceFetch } from './fetch';
import { assertSafeTokenUrl } from './oauthClientCredentials';

export type WorkloadVendor = 'openai' | 'anthropic';

export const OPENAI_DEFAULT_TOKEN_URL = 'https://auth.openai.com/oauth/token';
export const ANTHROPIC_DEFAULT_TOKEN_URL =
  'https://api.anthropic.com/v1/oauth/token';
/** API beta that enables `Authorization: Bearer` on the Anthropic API. */
export const ANTHROPIC_OAUTH_BETA = 'oauth-2025-04-20';
const ANTHROPIC_FEDERATION_BETA = 'oidc-federation-2026-04-01';

const OPENAI_SUBJECT_TOKEN_TYPES: Record<string, string> = {
  jwt: 'urn:ietf:params:oauth:token-type:jwt',
  id: 'urn:ietf:params:oauth:token-type:id_token',
};
// Both vendors reject larger assertions; it also stops a misconfigured path
// (a key or certificate file) from being posted.
const MAX_SUBJECT_TOKEN_BYTES = 16 * 1024;
const MAX_REFRESH_SKEW_MS = 5 * 60 * 1000;
const DEFAULT_LIFETIME_MS = 5 * 60 * 1000;

const tokenCache = new Map<string, { token: string; expiresAt: number }>();
const inflight = new Map<string, Promise<string>>();

type Env = Record<string, any> | undefined;
type Ctx = { req?: { header?: (name: string) => string | undefined } };

const PREFIX: Record<WorkloadVendor, string> = {
  openai: 'OPENAI',
  anthropic: 'ANTHROPIC',
};

function envValue(env: Env, name: string): string | undefined {
  const v = Environment(env as any)[name];
  return typeof v === 'string' && v.trim() ? v.trim() : undefined;
}

export function ambientWorkloadIdentityEnabled(
  vendor: WorkloadVendor,
  env?: Env
): boolean {
  return (
    String(
      Environment(env as any)[`${PREFIX[vendor]}_AMBIENT_CREDENTIALS_ENABLED`]
    ).toLowerCase() === 'true'
  );
}

/**
 * Validates `openaiAuthType` / `anthropicAuthType`. Returns true for
 * `workload`, false when unset; anything else is a 400, and `workload` is a
 * 403 unless the operator opted in.
 */
export function isWorkloadAuth(
  vendor: WorkloadVendor,
  authType: string | undefined,
  env?: Env
): boolean {
  if (authType === undefined || authType === '') return false;
  if (authType !== 'workload') {
    throw new GatewayError(`Unsupported ${vendor}AuthType`, 400);
  }
  if (!ambientWorkloadIdentityEnabled(vendor, env)) {
    throw new GatewayError(
      `${vendor}AuthType workload is disabled on this gateway`,
      403
    );
  }
  return true;
}

export interface WorkloadIdentityConfig {
  vendor: WorkloadVendor;
  tokenUrl: string;
  tokenFile: string;
  /** OpenAI */
  identityProviderId?: string;
  subjectTokenType?: string;
  /** OpenAI and Anthropic */
  serviceAccountId: string;
  /** Anthropic */
  federationRuleId?: string;
  organizationId?: string;
  workspaceId?: string;
}

/** The operator's federation settings, or a 500 naming what is missing. */
export function workloadIdentityConfig(
  vendor: WorkloadVendor,
  env?: Env
): WorkloadIdentityConfig {
  const p = PREFIX[vendor];
  const missing: string[] = [];
  const need = (name: string) => {
    const v = envValue(env, name);
    if (!v) missing.push(name);
    return v ?? '';
  };
  const tokenFile = need(`${p}_IDENTITY_TOKEN_FILE`);
  const serviceAccountId = need(`${p}_SERVICE_ACCOUNT_ID`);
  let config: WorkloadIdentityConfig;
  if (vendor === 'openai') {
    const identityProviderId = need('OPENAI_IDENTITY_PROVIDER_ID');
    const kind = envValue(env, 'OPENAI_SUBJECT_TOKEN_TYPE') ?? 'jwt';
    const subjectTokenType = OPENAI_SUBJECT_TOKEN_TYPES[kind];
    if (!subjectTokenType) missing.push('OPENAI_SUBJECT_TOKEN_TYPE (jwt|id)');
    config = {
      vendor,
      tokenUrl:
        envValue(env, 'OPENAI_WIF_TOKEN_URL') ?? OPENAI_DEFAULT_TOKEN_URL,
      tokenFile,
      identityProviderId,
      subjectTokenType,
      serviceAccountId,
    };
  } else {
    config = {
      vendor,
      tokenUrl:
        envValue(env, 'ANTHROPIC_WIF_TOKEN_URL') ?? ANTHROPIC_DEFAULT_TOKEN_URL,
      tokenFile,
      serviceAccountId,
      federationRuleId: need('ANTHROPIC_FEDERATION_RULE_ID'),
      organizationId: need('ANTHROPIC_ORGANIZATION_ID'),
      workspaceId: envValue(env, 'ANTHROPIC_WORKSPACE_ID'),
    };
  }
  if (missing.length) {
    // Operator configuration, not caller input; names only, no values.
    throw new GatewayError(
      `${vendor}AuthType workload is not configured on this gateway (missing ${missing.join(', ')})`,
      500
    );
  }
  return config;
}

/**
 * The minted token is a bearer token for the operator's vendor service
 * account, so it may only go to the vendor's own API. Call before minting.
 */
export function assertWorkloadUpstreamIsVendor(
  vendor: WorkloadVendor,
  providerOptions: { customHost?: string },
  c?: Ctx
) {
  const headerHost =
    typeof c?.req?.header === 'function'
      ? c.req.header(HEADER_KEYS.CUSTOM_HOST)
      : undefined;
  if (providerOptions.customHost || headerHost) {
    throw new GatewayError(
      `${vendor}AuthType workload cannot be used with a custom host`,
      400
    );
  }
}

async function readSubjectToken(file: string): Promise<string> {
  if (getRuntimeKey() !== 'node') {
    throw new GatewayError(
      'Workload identity needs the Node runtime (an identity token file)',
      501
    );
  }
  let token: string;
  try {
    const fs = await import('fs');
    token = fs.readFileSync(file, 'utf8').trim();
  } catch {
    // No path in the message: it is operator configuration.
    throw new GatewayError('Workload identity token file is unreadable', 500);
  }
  if (!token) {
    throw new GatewayError('Workload identity token file is empty', 500);
  }
  if (new TextEncoder().encode(token).length > MAX_SUBJECT_TOKEN_BYTES) {
    throw new GatewayError('Workload identity token is too large', 500);
  }
  return token;
}

function exchangeRequest(
  config: WorkloadIdentityConfig,
  subjectToken: string
): { headers: Record<string, string>; body: Record<string, string> } {
  if (config.vendor === 'openai') {
    return {
      headers: {},
      body: {
        grant_type: 'urn:ietf:params:oauth:grant-type:token-exchange',
        subject_token: subjectToken,
        subject_token_type: config.subjectTokenType!,
        identity_provider_id: config.identityProviderId!,
        service_account_id: config.serviceAccountId,
      },
    };
  }
  const body: Record<string, string> = {
    grant_type: 'urn:ietf:params:oauth:grant-type:jwt-bearer',
    assertion: subjectToken,
    federation_rule_id: config.federationRuleId!,
    organization_id: config.organizationId!,
    service_account_id: config.serviceAccountId,
  };
  if (config.workspaceId) body.workspace_id = config.workspaceId;
  return {
    headers: {
      'anthropic-beta': `${ANTHROPIC_OAUTH_BETA},${ANTHROPIC_FEDERATION_BETA}`,
    },
    body,
  };
}

async function exchange(
  config: WorkloadIdentityConfig
): Promise<{ token: string; lifetimeMs: number }> {
  const subjectToken = await readSubjectToken(config.tokenFile);
  const { headers, body } = exchangeRequest(config, subjectToken);
  let response: Response;
  try {
    response = await externalServiceFetch(config.tokenUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        ...headers,
      },
      body: JSON.stringify(body),
      redirect: 'manual',
    });
  } catch {
    throw new GatewayError('Workload identity token exchange failed', 502);
  }
  if (!response.ok) {
    // Deliberately drop the body: token endpoints may echo the assertion.
    throw new GatewayError(
      `Workload identity token exchange failed with status ${response.status}`,
      502
    );
  }
  let json: Record<string, any>;
  try {
    json = await response.json();
  } catch {
    throw new GatewayError('Workload identity token response was invalid', 502);
  }
  const token = json?.access_token;
  if (typeof token !== 'string' || !token) {
    throw new GatewayError('Workload identity token response was invalid', 502);
  }
  const expiresIn = Number(json.expires_in);
  const lifetimeMs =
    Number.isFinite(expiresIn) && expiresIn > 0
      ? expiresIn * 1000
      : DEFAULT_LIFETIME_MS;
  return { token, lifetimeMs };
}

/**
 * A vendor access token for the operator's configured service account,
 * cached until shortly before it expires. Validates the token URL before
 * reading the identity token or making any network call.
 */
export async function getWorkloadIdentityToken(
  vendor: WorkloadVendor,
  env?: Env
): Promise<string> {
  const config = workloadIdentityConfig(vendor, env);
  const defaultHost = new URL(
    vendor === 'openai' ? OPENAI_DEFAULT_TOKEN_URL : ANTHROPIC_DEFAULT_TOKEN_URL
  ).hostname;
  assertSafeTokenUrl(config.tokenUrl, env as any, [defaultHost]);

  const key = JSON.stringify([
    vendor,
    config.tokenUrl,
    config.tokenFile,
    config.identityProviderId,
    config.subjectTokenType,
    config.serviceAccountId,
    config.federationRuleId,
    config.organizationId,
    config.workspaceId,
  ]);
  const cached = tokenCache.get(key);
  if (cached && cached.expiresAt > Date.now()) return cached.token;
  if (cached) tokenCache.delete(key);

  let pending = inflight.get(key);
  if (!pending) {
    pending = exchange(config)
      .then(({ token, lifetimeMs }) => {
        const skew = Math.min(MAX_REFRESH_SKEW_MS, lifetimeMs / 2);
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

/** Adds the OAuth API beta to an `anthropic-beta` value, once. */
export function withAnthropicOAuthBeta(
  beta: string | string[] | undefined
): string {
  const flags = (Array.isArray(beta) ? beta.join(',') : beta ?? '')
    .split(',')
    .map((f) => f.trim())
    .filter(Boolean);
  if (!flags.includes(ANTHROPIC_OAUTH_BETA)) flags.push(ANTHROPIC_OAUTH_BETA);
  return flags.join(',');
}

/** Test hook. */
export function _resetWorkloadIdentityTokenCache() {
  tokenCache.clear();
  inflight.clear();
}
