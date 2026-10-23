import { HookEventType, PluginContext, PluginParameters } from '../types';
import { post, getText, getCurrentContentPart } from '../utils';
import { AzureCredentials } from './types';
import { assertAzureResourceName, getAccessToken } from './utils';

const contentText = (content: any): string =>
  Array.isArray(content)
    ? content.map((item: any) => item?.text).join('\n')
    : content;

/**
 * PS fork (#69): the request's system prompts, in order. Chat Completions:
 * the `system` messages. Responses (`createModelResponse`): `instructions`,
 * then the `system` / `developer` input messages (Responses' system roles).
 */
const getSystemPrompts = (context: PluginContext): string[] => {
  const json = context.request?.json;
  if (context.requestType === 'createModelResponse') {
    const prompts: string[] = [];
    if (typeof json?.instructions === 'string' && json.instructions) {
      prompts.push(json.instructions);
    }
    for (const item of Array.isArray(json?.input) ? json.input : []) {
      if (
        (item?.role === 'system' || item?.role === 'developer') &&
        (item.type === undefined || item.type === 'message')
      ) {
        prompts.push(contentText(item.content));
      }
    }
    return prompts;
  }
  const messages = Array.isArray(json?.messages) ? json.messages : [];
  return messages
    .filter((message: any) => message?.role === 'system')
    .map((message: any) => contentText(message.content));
};

/**
 * Shield Prompt handler for detecting jailbreak and prompt injection attacks.
 * Uses Azure AI Content Safety Prompt Shields API.
 *
 * @see https://learn.microsoft.com/en-us/azure/ai-services/content-safety/quickstart-jailbreak
 */
export const handler = async (
  context: PluginContext,
  parameters: PluginParameters<{ contentSafety: AzureCredentials }>,
  eventType: HookEventType,
  pluginOptions?: Record<string, any>
) => {
  let verdict = true;
  let data = null;

  if (eventType === 'afterRequestHook') {
    return {
      error: new Error('Shield Prompt is not supported for afterRequestHook'),
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
      error: new Error('Shield Prompt credentials must include resourceName'),
      verdict: false,
      data,
    };
  }

  // prefer api key over auth mode
  if (!credentials?.azureAuthMode && !credentials?.apiKey) {
    return {
      error: new Error(
        'Shield Prompt credentials must include either apiKey or azureAuthMode'
      ),
      verdict: false,
      data,
    };
  }

  // PS fork (#69): system prompts for Chat Completions and Responses, read
  // without assuming `messages` exists (a Responses request has none).
  const systemPrompts = getSystemPrompts(context);

  let userPrompt;

  // If system message, flatten them into a single string, if not, use the user prompt
  if (systemPrompts.length > 0) {
    userPrompt = systemPrompts.join('\n');
  } else {
    userPrompt = getText(context, eventType) || '';
  }

  const { textArray } = getCurrentContentPart(context, eventType);

  const request = {
    userPrompt,
    ...(systemPrompts.length > 0 ? { documents: textArray } : {}), // If system message, add user prompt as documents
  };

  const apiVersion = parameters.apiVersion || '2024-09-01';

  const url = `https://${credentials.resourceName}.cognitiveservices.azure.com/contentsafety/text:shieldPrompt?api-version=${apiVersion}`;

  const { token, error: tokenError } = await getAccessToken(
    credentials as any,
    'shieldPrompt',
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

    // Check if user prompt attack was detected
    const userPromptAttackDetected =
      response.userPromptAnalysis?.attackDetected === true;

    // Check if any document attack was detected
    const documentAttackDetected = response.documentsAnalysis?.some(
      (doc: { attackDetected: boolean }) => doc.attackDetected === true
    );

    // Verdict is false if any attack is detected
    verdict = !(userPromptAttackDetected || documentAttackDetected);
  }

  return {
    error: null,
    verdict,
    data,
  };
};
