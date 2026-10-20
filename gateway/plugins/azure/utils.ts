import { AzureCredentials } from './types';
import { pluginRequest, PluginEgressError } from '../egress';

const cacheExpiry = 15 * 60; // 15 minutes

const AZURE_RESOURCE_NAME = /^[a-z0-9][a-z0-9-]{0,62}$/i;

/**
 * PS fork (security): requests built as
 * `https://<resourceName>.cognitiveservices.azure.com` must stay on that
 * host; a resource name with `.`, `/`, `@`, `?` or `#` would swap in another
 * one. Call it inside the plugin's request `try`, so a bad name is reported
 * like a network error. (A `customHost` gets the plugin egress URL check.)
 */
export function assertAzureResourceName(resourceName?: string): void {
  if (!AZURE_RESOURCE_NAME.test(resourceName ?? '')) {
    throw new PluginEgressError(
      'Azure resourceName must match ^[a-z0-9][a-z0-9-]{0,62}$'
    );
  }
}
const COGNITIVE_SERVICES_RESOURCE = 'https://cognitiveservices.azure.com/';

/** Thrown by `pii`, whose token lookup reports errors by throwing. */
export class AzureHostIdentityDisabledError extends Error {}

export async function getAccessTokenFromEntraId(
  tenantId: string,
  clientId: string,
  clientSecret: string,
  scope = 'https://openai.azure.com/.default',
  check: string,
  options?: Record<string, any>,
  env?: Record<string, any>
) {
  const result: { token: string; error: string | null } = {
    token: '',
    error: null,
  };
  const cacheKey = `azure-plugin-entra-token-${check}-${tenantId}-${clientId}-${clientSecret}`;
  const cachedToken = await options?.getFromCacheByKey?.(env, cacheKey);
  if (cachedToken) {
    return { token: cachedToken, error: null };
  }
  try {
    const url = `https://login.microsoftonline.com/${tenantId}/oauth2/v2.0/token`;
    const params = new URLSearchParams({
      client_id: clientId,
      client_secret: clientSecret,
      scope: scope,
      grant_type: 'client_credentials',
    });

    const { response, text } = await pluginRequest(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body: params,
    });

    if (!response.ok) {
      result.error = text;
      return result;
    }
    const data: { access_token: string } = JSON.parse(text);
    result.token = data.access_token;
    await options?.putInCacheWithValue?.(
      env,
      cacheKey,
      result.token,
      cacheExpiry
    );
  } catch (error) {
    result.error = (error as Error).message;
  }
  return result;
}

export async function getAzureManagedIdentityToken(
  resource: string,
  check: string,
  clientId?: string,
  options?: Record<string, any>,
  env?: Record<string, any>
) {
  const result: { token: string; error: string | null } = {
    token: '',
    error: null,
  };
  const cacheKey = `azure-plugin-managed-identity-token-${check}-${resource}-${clientId}`;
  const cachedToken = await options?.getFromCacheByKey?.(env, cacheKey);
  if (cachedToken) {
    return { token: cachedToken, error: null };
  }
  try {
    // The fixed IMDS URL is internal by design (operator-gated by
    // AZURE_AMBIENT_CREDENTIALS_ENABLED); still no redirects, a timeout and
    // a body cap.
    const { response, text } = await pluginRequest(
      `http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=${encodeURIComponent(resource)}${clientId ? `&client_id=${encodeURIComponent(clientId)}` : ''}`,
      {
        method: 'GET',
        headers: {
          Metadata: 'true',
        },
      },
      { allowInternalHost: true }
    );
    if (!response.ok) {
      result.error = text;
      return result;
    }
    const data: { access_token: string } = JSON.parse(text);
    result.token = data.access_token;
    await options?.putInCacheWithValue?.(
      env,
      cacheKey,
      result.token,
      cacheExpiry
    );
    return result;
  } catch (error) {
    result.error = (error as Error).message;
  }
  return result;
}

export const getAccessToken = async (
  credentials: AzureCredentials,
  check: string,
  options?: Record<string, any>,
  env?: Record<string, any>
) => {
  if (credentials.apiKey) {
    return { token: credentials.apiKey, error: null };
  }
  const scope = 'https://cognitiveservices.azure.com/.default';

  const { clientId, clientSecret, tenantId, azureAuthMode } = credentials ?? {};

  let tokenResult: { token: string; error: string | null; denied?: boolean } = {
    token: '',
    error: null,
  };
  // client id is only set for managed identity
  if (azureAuthMode === 'managed') {
    // Prompt Shields: the guardrail config comes from the caller, and a
    // managed identity token is the gateway host's own Azure identity.
    // `denied` makes the guardrail fail closed instead of skipping the check.
    if (
      String(env?.AZURE_AMBIENT_CREDENTIALS_ENABLED).toLowerCase() !== 'true'
    ) {
      return {
        token: '',
        error: 'azureAuthMode managed is disabled on this gateway',
        denied: true,
      };
    }
    // The host token may only go to `<resourceName>.cognitiveservices.azure.com`:
    // no caller custom host (TLS checks are also off for those), and a
    // resource name that cannot rewrite the host. Upstream passed
    // resourceName as the IMDS resource too, which only produced a usable
    // token together with a custom host; ask IMDS for Cognitive Services.
    if (credentials.customHost) {
      return {
        token: '',
        error: 'azureAuthMode managed cannot be used with a custom host',
        denied: true,
      };
    }
    if (!AZURE_RESOURCE_NAME.test(credentials.resourceName ?? '')) {
      return {
        token: '',
        error: 'azureAuthMode managed requires a valid Azure resource name',
        denied: true,
      };
    }
    tokenResult = await getAzureManagedIdentityToken(
      COGNITIVE_SERVICES_RESOURCE,
      check,
      clientId,
      options,
      env
    );
  }

  if (azureAuthMode === 'entra') {
    tokenResult = await getAccessTokenFromEntraId(
      tenantId ?? '',
      clientId ?? '',
      clientSecret ?? '',
      scope,
      check,
      options,
      env
    );
  }

  return tokenResult;
};
