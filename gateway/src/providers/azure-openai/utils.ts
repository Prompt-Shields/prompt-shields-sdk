import { AZURE_OPEN_AI, HEADER_KEYS } from '../../globals';
import { Options } from '../../types/requestBody';
import { OpenAIErrorResponseTransform } from '../openai/utils';
import { ErrorResponse } from '../types';
import { Environment } from '../../utils/env';
import { GatewayError } from '../../errors/GatewayError';

// Prompt Shields: callers pick `x-portkey-azure-auth-mode` per request, and
// `managed` / `workload` authenticate as the gateway host's own Azure
// identity, so they need an explicit operator opt-in.
export function ambientAzureCredentialsEnabled(
  env?: Record<string, any>
): boolean {
  return (
    String(Environment(env).AZURE_AMBIENT_CREDENTIALS_ENABLED).toLowerCase() ===
    'true'
  );
}

export function assertAzureAuthModeAllowed(
  azureAuthMode: string | undefined,
  env?: Record<string, any>
) {
  if (
    isAzureAmbientAuthMode(azureAuthMode) &&
    !ambientAzureCredentialsEnabled(env)
  ) {
    throw new GatewayError(
      `azureAuthMode ${azureAuthMode} is disabled on this gateway`,
      403
    );
  }
}

export function isAzureAmbientAuthMode(azureAuthMode: string | undefined) {
  return azureAuthMode === 'managed' || azureAuthMode === 'workload';
}

// Microsoft-run Azure AI hosts (public, US Gov and China clouds) plus Blob
// Storage for batch output. Deliberately not all of `.azure.com`: hosts such
// as `*.cloudapp.azure.com` (VM DNS labels) or `*.inference.ml.azure.com`
// (customer containers) are run by whoever created them.
const AZURE_AMBIENT_HOST_SUFFIXES = [
  '.openai.azure.com',
  '.cognitiveservices.azure.com',
  '.services.ai.azure.com',
  '.models.ai.azure.com',
  '.inference.ai.azure.com',
  '.openai.azure.us',
  '.cognitiveservices.azure.us',
  '.openai.azure.cn',
  '.cognitiveservices.azure.cn',
];

// Blob Storage only for the batch-output download in getBatchOutput.ts, which
// asks for a storage token itself; never for a caller-built URL.
const AZURE_AMBIENT_BLOB_SUFFIXES = [
  '.blob.core.windows.net',
  '.blob.core.usgovcloudapi.net',
  '.blob.core.chinacloudapi.cn',
];

// Audiences a caller may ask the host identity for (`azureEntraScope`). A
// caller-chosen audience on a caller-chosen Azure host makes the gateway a
// confused deputy (e.g. a storage token reading the operator's blobs), so
// only the Cognitive Services audience is allowed.
const AZURE_AMBIENT_SCOPES = [
  'https://cognitiveservices.azure.com',
  'https://cognitiveservices.azure.us',
  'https://cognitiveservices.azure.cn',
];
const AZURE_STORAGE_SCOPES = [
  'https://storage.azure.com',
  'https://storage.azure.us',
  'https://storage.azure.cn',
];

function normalizeAzureScope(scope: string) {
  return scope.replace(/\/\.default$/, '').replace(/\/$/, '');
}

const AZURE_RESOURCE_NAME = /^[a-z0-9][a-z0-9-]{0,62}$/i;

function isAzureAmbientUpstreamURL(
  rawUrl: string | undefined,
  suffixes: string[] = AZURE_AMBIENT_HOST_SUFFIXES
): boolean {
  if (typeof rawUrl !== 'string' || !rawUrl) return false;
  try {
    const url = new URL(rawUrl);
    if (url.protocol !== 'https:' && url.protocol !== 'wss:') return false;
    if (url.username || url.password || url.port) return false;
    const host = url.hostname.toLowerCase();
    return suffixes.some(
      (suffix) => host.endsWith(suffix) && host.length > suffix.length
    );
  } catch {
    return false;
  }
}

/**
 * `managed` / `workload` tokens are bearer tokens for the gateway host's own
 * Azure identity, so they may only go to Azure: refuse a caller custom host,
 * a resource name or Foundry URL that could rewrite the upstream host, and
 * any final upstream URL outside Microsoft's Azure AI hosts. Call before
 * minting the token. Caller-supplied credentials (api key, `azureAdToken`,
 * `entra` client secret) are not checked here.
 */
export function assertAzureAmbientUpstreamIsAzure(
  providerOptions: {
    customHost?: string;
    resourceName?: string;
    azureFoundryUrl?: string;
    azureEntraScope?: string;
  },
  transformedRequestUrl: string | undefined,
  c?: { req?: { header?: (name: string) => string | undefined } },
  fn?: string
) {
  const headerHost =
    typeof c?.req?.header === 'function'
      ? c.req.header(HEADER_KEYS.CUSTOM_HOST)
      : undefined;
  if (providerOptions.customHost || headerHost) {
    throw new GatewayError(
      'azureAuthMode managed and workload cannot be used with a custom host',
      400
    );
  }
  const { resourceName, azureFoundryUrl } = providerOptions;
  if (resourceName !== undefined && !AZURE_RESOURCE_NAME.test(resourceName)) {
    throw new GatewayError('Invalid Azure resource name', 400);
  }
  if (
    azureFoundryUrl !== undefined &&
    !isAzureAmbientUpstreamURL(azureFoundryUrl)
  ) {
    throw new GatewayError('Invalid Azure Foundry URL', 400);
  }
  const { azureEntraScope } = providerOptions;
  const scope =
    typeof azureEntraScope === 'string' && azureEntraScope
      ? normalizeAzureScope(azureEntraScope.trim().toLowerCase())
      : undefined;
  // The batch-output blob download: a storage token, to Blob Storage only.
  if (
    fn === 'retrieveFileContent' &&
    scope !== undefined &&
    AZURE_STORAGE_SCOPES.includes(scope)
  ) {
    if (
      !isAzureAmbientUpstreamURL(
        transformedRequestUrl,
        AZURE_AMBIENT_BLOB_SUFFIXES
      )
    ) {
      throw new GatewayError(
        'azureAuthMode managed and workload can only be used with Azure hosts',
        400
      );
    }
    return;
  }
  if (scope !== undefined && !AZURE_AMBIENT_SCOPES.includes(scope)) {
    throw new GatewayError(
      'azureAuthMode managed and workload only allow the Cognitive Services scope',
      400
    );
  }
  if (!isAzureAmbientUpstreamURL(transformedRequestUrl)) {
    throw new GatewayError(
      'azureAuthMode managed and workload can only be used with Azure hosts',
      400
    );
  }
}

export async function getAccessTokenFromEntraId(
  tenantId: string,
  clientId: string,
  clientSecret: string,
  scope = 'https://cognitiveservices.azure.com/.default'
) {
  try {
    const url = `https://login.microsoftonline.com/${tenantId}/oauth2/v2.0/token`;
    const params = new URLSearchParams({
      client_id: clientId,
      client_secret: clientSecret,
      scope: scope,
      grant_type: 'client_credentials',
    });

    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body: params,
    });

    if (!response.ok) {
      const errorMessage = await response.text();
      console.error('getAccessTokenFromEntraId error: ', {
        message: `Error from Entra ${errorMessage}`,
      });
      return undefined;
    }
    const data: { access_token: string } = await response.json();
    return data.access_token;
  } catch (error) {
    console.error('getAccessTokenFromEntraId error: ', error);
  }
}

// Prompt Shields: the two host-identity token helpers check the opt-in
// themselves, so a new caller cannot mint a host token without it.
function assertAmbientAzureCredentialsEnabled(env: Record<string, any>) {
  if (!ambientAzureCredentialsEnabled(env)) {
    throw new GatewayError(
      'Azure host identity is disabled on this gateway',
      403
    );
  }
}

export async function getAzureManagedIdentityToken(
  env: Record<string, any>,
  resource: string,
  clientId?: string
) {
  assertAmbientAzureCredentialsEnabled(env);
  try {
    const response = await fetch(
      `http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=${encodeURIComponent(resource)}${clientId ? `&client_id=${encodeURIComponent(clientId)}` : ''}`,
      {
        method: 'GET',
        headers: {
          Metadata: 'true',
        },
      }
    );
    if (!response.ok) {
      const errorMessage = await response.text();
      console.error('getAzureManagedIdentityToken error: ', {
        message: `Error from Managed ${errorMessage}`,
      });
      return undefined;
    }
    const data: { access_token: string } = await response.json();
    return data.access_token;
  } catch (error) {
    console.error('getAzureManagedIdentityToken error: ', error);
  }
}

export async function getAzureWorkloadIdentityToken(
  env: Record<string, any>,
  authorityHost: string,
  tenantId: string,
  clientId: string,
  federatedToken: string,
  scope = 'https://cognitiveservices.azure.com/.default'
) {
  assertAmbientAzureCredentialsEnabled(env);
  try {
    const url = `${authorityHost}/${tenantId}/oauth2/v2.0/token`;
    const params = new URLSearchParams({
      client_id: clientId,
      client_assertion: federatedToken,
      client_assertion_type:
        'urn:ietf:params:oauth:client-assertion-type:jwt-bearer',
      scope: scope,
      grant_type: 'client_credentials',
    });

    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body: params,
    });

    if (!response.ok) {
      const errorMessage = await response.text();
      console.error({ message: `Error from Entra ${errorMessage}` });
      return undefined;
    }
    const data: { access_token: string } = await response.json();
    return data.access_token;
  } catch (error) {
    console.error(error);
  }
}

export const AzureOpenAIFinetuneResponseTransform = (
  response: Response | ErrorResponse,
  responseStatus: number
): Response | ErrorResponse => {
  if (responseStatus !== 200 && 'error' in response) {
    return OpenAIErrorResponseTransform(response, AZURE_OPEN_AI);
  }

  const _response = { ...response } as any;

  if (['created', 'pending'].includes(_response.status)) {
    _response.status = 'queued';
  }

  return _response;
};

export const getAzureModelValue = (
  params: Params,
  providerOptions?: Options
) => {
  const { apiVersion: azureApiVersion, deploymentId: azureDeploymentName } =
    providerOptions ?? {};
  if (azureApiVersion && azureApiVersion.trim() === 'v1') {
    return azureDeploymentName;
  }
  return params.model || '';
};
