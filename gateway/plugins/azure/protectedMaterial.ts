import { HookEventType, PluginContext, PluginParameters } from '../types';
import { post, getText } from '../utils';
import { AzureCredentials } from './types';
import { assertAzureResourceName, getAccessToken } from './utils';

/**
 * Protected Material handler for detecting copyrighted/protected text content.
 * Uses Azure AI Content Safety Protected Material Detection API.
 *
 * @see https://learn.microsoft.com/en-us/azure/ai-services/content-safety/concepts/protected-material
 */
export const handler = async (
  context: PluginContext,
  parameters: PluginParameters<{ contentSafety: AzureCredentials }>,
  eventType: HookEventType,
  pluginOptions?: Record<string, any>
) => {
  let verdict = true;
  let data = null;

  if (eventType === 'beforeRequestHook') {
    return {
      error: new Error(
        'Protected Material is not supported for beforeRequestHook'
      ),
      verdict: false,
      data,
    };
  }

  const credentials = parameters.credentials?.contentSafety;

  if (!credentials) {
    return {
      error: new Error('parameters.credentials must be set'),
      verdict: false,
      data,
    };
  }

  // Validate required credentials
  if (!credentials?.resourceName) {
    return {
      error: new Error(
        'Protected Material credentials must include resourceName'
      ),
      verdict: false,
      data,
    };
  }

  // prefer api key over auth mode
  if (!credentials?.azureAuthMode && !credentials?.apiKey) {
    return {
      error: new Error(
        'Protected Material credentials must include either apiKey or azureAuthMode'
      ),
      verdict: false,
      data,
    };
  }

  const text = getText(context, eventType);
  if (!text) {
    return {
      error: new Error('request or response text is empty'),
      verdict: false,
      data,
    };
  }

  const apiVersion = parameters.apiVersion || '2024-09-01';

  const url = `https://${credentials.resourceName}.cognitiveservices.azure.com/contentsafety/text:detectProtectedMaterial?api-version=${apiVersion}`;

  const { token, error: tokenError } = await getAccessToken(
    credentials as any,
    'protectedMaterial',
    pluginOptions,
    pluginOptions?.env
  );

  if (tokenError) {
    return {
      error: tokenError,
      verdict: false,
      data,
    };
  }

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    'User-Agent': 'portkey-ai-plugin/',
    'Ocp-Apim-Subscription-Key': token,
  };

  if (credentials?.azureAuthMode && credentials?.azureAuthMode !== 'apiKey') {
    headers['Authorization'] = `Bearer ${token}`;
    delete headers['Ocp-Apim-Subscription-Key'];
  }

  // Build request body
  const request = {
    text: text,
  };

  const timeout = parameters.timeout || 5000;
  let response;
  try {
    assertAzureResourceName(credentials.resourceName);
    response = await post(url, request, { headers }, timeout);
  } catch (e) {
    return { error: e, verdict: false, data };
  }

  if (response) {
    data = response;

    // Check if protected material was detected
    // The API returns protectedMaterialAnalysis with detected flag
    const protectedMaterialDetected =
      response.protectedMaterialAnalysis?.detected === true;

    // Verdict is false if protected material is detected
    verdict = !protectedMaterialDetected;
  }

  return {
    error: null,
    verdict,
    data,
  };
};
