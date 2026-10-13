/**
 * PS fork: keyless Vertex AI auth with the gateway host's own Google identity
 * (GCE / Cloud Run / GKE Workload Identity metadata server). Re-implemented
 * from Portkey's enterprise changelog (#76); upstream code is not open source.
 *
 * Callers pick `vertexAuthType: workload` per request, and this mints a token
 * for the HOST's service account, so it is refused unless the operator sets
 * GCP_AMBIENT_CREDENTIALS_ENABLED=true (same pattern as AWS #111, Azure #115).
 */
import { GatewayError } from '../errors/GatewayError';
import { HEADER_KEYS } from '../globals';
import { Environment } from './env';
import { externalServiceFetch } from './fetch';

const METADATA_TOKEN_URL =
  'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token';

// Refresh this long before the metadata server's expiry.
const REFRESH_SKEW_MS = 5 * 60 * 1000;

let hostTokenCache: { token: string; expiresAt: number } | undefined;
let hostTokenInflight: Promise<string> | undefined;

export function ambientGcpCredentialsEnabled(
  env?: Record<string, any>
): boolean {
  return (
    String(Environment(env).GCP_AMBIENT_CREDENTIALS_ENABLED).toLowerCase() ===
    'true'
  );
}

export function assertVertexAuthTypeAllowed(
  vertexAuthType: string | undefined,
  env?: Record<string, any>
) {
  if (vertexAuthType === undefined || vertexAuthType === '') return;
  if (vertexAuthType !== 'workload') {
    throw new GatewayError('Unsupported vertexAuthType', 400);
  }
  if (!ambientGcpCredentialsEnabled(env)) {
    throw new GatewayError(
      'vertexAuthType workload is disabled on this gateway',
      403
    );
  }
}

/**
 * The host token is a bearer token for the gateway's own service account, so
 * it may only go to Google: refuse a caller custom host, and a region that
 * could rewrite the `https://<region>-aiplatform.googleapis.com` host.
 * Call before minting the token.
 */
export function assertWorkloadUpstreamIsGoogle(
  providerOptions: { customHost?: string; vertexRegion?: string },
  c?: { req?: { header?: (name: string) => string | undefined } }
) {
  const headerHost =
    typeof c?.req?.header === 'function'
      ? c.req.header(HEADER_KEYS.CUSTOM_HOST)
      : undefined;
  if (providerOptions.customHost || headerHost) {
    throw new GatewayError(
      'vertexAuthType workload cannot be used with a custom host',
      400
    );
  }
  const region = providerOptions.vertexRegion;
  if (region !== undefined && !/^[a-z0-9-]{1,63}$/.test(region)) {
    throw new GatewayError('Invalid vertexRegion', 400);
  }
}

async function fetchHostToken(): Promise<string> {
  let response: Response;
  try {
    response = await externalServiceFetch(METADATA_TOKEN_URL, {
      method: 'GET',
      headers: { 'Metadata-Flavor': 'Google' },
      redirect: 'manual',
    });
  } catch {
    throw new GatewayError('GCP metadata token request failed', 502);
  }
  if (!response.ok) {
    throw new GatewayError(
      `GCP metadata token request failed with status ${response.status}`,
      502
    );
  }
  let json: Record<string, any>;
  try {
    json = await response.json();
  } catch {
    throw new GatewayError('GCP metadata token response was invalid', 502);
  }
  const token = json?.access_token;
  if (typeof token !== 'string' || !token) {
    throw new GatewayError('GCP metadata token response was invalid', 502);
  }
  const expiresIn = Number(json.expires_in);
  const lifetimeMs =
    Number.isFinite(expiresIn) && expiresIn > 0 ? expiresIn * 1000 : 300_000;
  hostTokenCache = {
    token,
    expiresAt: Date.now() + Math.max(lifetimeMs - REFRESH_SKEW_MS, 0),
  };
  return token;
}

/**
 * Returns an access token for the gateway host's default Google service
 * account. Throws 403 before any network call when the gate is off.
 */
export async function getGcpHostAccessToken(
  env?: Record<string, any>
): Promise<string> {
  assertVertexAuthTypeAllowed('workload', env);
  if (hostTokenCache && hostTokenCache.expiresAt > Date.now()) {
    return hostTokenCache.token;
  }
  if (!hostTokenInflight) {
    hostTokenInflight = fetchHostToken().finally(() => {
      hostTokenInflight = undefined;
    });
  }
  return hostTokenInflight;
}

/** Test hook. */
export function _resetGcpHostTokenCache() {
  hostTokenCache = undefined;
  hostTokenInflight = undefined;
}
