import { BEDROCK_MANTLE } from '../../globals';
import { AnthropicErrorResponseTransform } from '../anthropic/utils';
import { getMessagesConfig } from '../anthropic-base/messages';
import { MessagesResponse } from '../../types/messagesResponse';
import {
  chatCompleteParams,
  createModelResponseParams,
  OpenAICreateModelResponseTransformer,
  responseTransformers,
} from '../open-ai-base';
import { ErrorResponse, ProviderConfigs } from '../types';
import { generateInvalidProviderResponseError } from '../utils';
import BedrockMantleAPIConfig from './api';

// Messages for Claude models: the Anthropic request shape, plus `safeguards`
// (enterprise 2.27).
const BedrockMantleMessagesConfig = getMessagesConfig({
  extra: { safeguards: { param: 'safeguards', required: false } },
});

const BedrockMantleMessagesResponseTransform = (
  response: any,
  responseStatus: number
): MessagesResponse | ErrorResponse => {
  if (responseStatus !== 200 && 'error' in response) {
    return AnthropicErrorResponseTransform(response, BEDROCK_MANTLE);
  }
  if ('model' in response) return response;
  return generateInvalidProviderResponseError(response, BEDROCK_MANTLE);
};

// Chat Completions and Responses use the OpenAI shapes unchanged; only host,
// path and auth differ.
const BedrockMantleConfig: ProviderConfigs = {
  api: BedrockMantleAPIConfig,
  chatComplete: chatCompleteParams([]),
  createModelResponse: createModelResponseParams([]),
  messages: BedrockMantleMessagesConfig,
  responseTransforms: {
    ...responseTransformers(BEDROCK_MANTLE, { chatComplete: true }),
    createModelResponse: OpenAICreateModelResponseTransformer(BEDROCK_MANTLE),
    messages: BedrockMantleMessagesResponseTransform,
  },
};

export default BedrockMantleConfig;
