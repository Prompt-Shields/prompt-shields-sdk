/**
 * PS fork (#73 follow-up): Anthropic `POST /v1/messages/count_tokens` for
 * Gemini models, on Google AI Studio (`google`) and Vertex AI
 * (`google-vertex-ai`). The Messages request is turned into the same
 * `contents` / `systemInstruction` / `tools` the provider's chatComplete
 * config sends (via the Messages adapter's Chat Completions form), so the
 * count covers what a real call would send, and the provider's own
 * `countTokens` answers with `totalTokens`.
 *
 * - AI Studio: `POST /v1beta/models/{model}:countTokens` with
 *   `{ generateContentRequest: { model: "models/{model}", contents, ... } }`
 *   https://ai.google.dev/api/tokens#method:-models.counttokens
 * - Vertex AI: `POST .../publishers/google/models/{model}:countTokens` with
 *   `{ contents, systemInstruction, tools }`
 *   https://cloud.google.com/vertex-ai/generative-ai/docs/model-reference/count-tokens
 */
import { transformMessagesToChatCompletions } from '../../adapters/messages/requestTransform';
import { transformUsingProviderConfig } from '../../services/transformToProviderRequest';
import { Options, Params } from '../../types/requestBody';
import { ErrorResponse, ProviderConfig } from '../types';
import { generateInvalidProviderResponseError } from '../utils';

type GeminiBody = {
  contents?: unknown;
  systemInstruction?: unknown;
  tools?: unknown;
};

function geminiBody(
  chatConfig: ProviderConfig,
  params: Params,
  providerOptions?: Options
): GeminiBody {
  const chatParams = transformMessagesToChatCompletions(params);
  const body = transformUsingProviderConfig(
    chatConfig,
    chatParams,
    providerOptions
  );
  return {
    contents: body.contents,
    systemInstruction: body.systemInstruction,
    tools: body.tools,
  };
}

/** AI Studio: everything goes inside `generateContentRequest`. */
export const googleMessagesCountTokensConfig = (
  chatConfig: ProviderConfig
): ProviderConfig => ({
  messages: {
    param: 'generateContentRequest',
    required: true,
    transform: (params: Params, providerOptions?: Options) => ({
      model: `models/${params.model ?? ''}`,
      ...geminiBody(chatConfig, params, providerOptions),
    }),
  },
});

/** Vertex AI: `contents`, `systemInstruction` and `tools` at the top level. */
export const vertexGeminiMessagesCountTokensConfig = (
  chatConfig: ProviderConfig
): ProviderConfig => ({
  messages: (['contents', 'systemInstruction', 'tools'] as const).map(
    (key) => ({
      param: key,
      required: key === 'contents',
      transform: (params: Params, providerOptions?: Options) =>
        geminiBody(chatConfig, params, providerOptions)[key],
    })
  ),
});

/** `{ totalTokens }` → Anthropic's `{ input_tokens }`. */
export const geminiMessagesCountTokensResponseTransform =
  (
    provider: string,
    errorTransform: (
      response: any,
      provider?: string
    ) => ErrorResponse | undefined
  ) =>
  (response: any, responseStatus: number) => {
    if (responseStatus !== 200) {
      return (
        errorTransform(response, provider) ||
        generateInvalidProviderResponseError(response, provider)
      );
    }
    if (typeof response?.totalTokens === 'number') {
      return { input_tokens: response.totalTokens };
    }
    return generateInvalidProviderResponseError(response, provider);
  };
