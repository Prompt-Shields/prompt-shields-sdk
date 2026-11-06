import { META_MODEL_API } from '../../globals';
import { MessagesResponse } from '../../types/messagesResponse';
import { getMessagesConfig } from '../anthropic-base/messages';
import { AnthropicErrorResponseTransform } from '../anthropic/utils';
import {
  chatCompleteParams,
  createModelResponseParams,
  OpenAICreateModelResponseTransformer,
  responseTransformers,
} from '../open-ai-base';
import { OpenAIErrorResponseTransform } from '../openai/utils';
import { ErrorResponse, ProviderConfigs } from '../types';
import { generateInvalidProviderResponseError } from '../utils';
import MetaModelAPIConfig from './api';

const MetaModelAPIMessagesConfig = getMessagesConfig({});

export const MetaModelAPIMessagesResponseTransform = (
  response: any,
  responseStatus: number
): MessagesResponse | ErrorResponse => {
  if (responseStatus !== 200 && response && 'error' in response) {
    return AnthropicErrorResponseTransform(response, META_MODEL_API);
  }
  if (response && 'model' in response) return response;
  return generateInvalidProviderResponseError(response, META_MODEL_API);
};

/** Files answers are OpenAI-shaped; only errors are rewritten. */
export const MetaModelAPIFilesResponseTransform = (
  response: Response | ErrorResponse,
  responseStatus: number
): Response | ErrorResponse => {
  if (responseStatus !== 200 && response && 'error' in response) {
    return OpenAIErrorResponseTransform(
      response as ErrorResponse,
      META_MODEL_API
    );
  }
  return response;
};

// Chat Completions, Responses and Messages keep the OpenAI and Anthropic
// shapes; only host, path and auth differ. File uploads pass the caller's
// multipart body through unchanged.
const MetaModelAPIProviderConfig: ProviderConfigs = {
  api: MetaModelAPIConfig,
  chatComplete: chatCompleteParams([]),
  createModelResponse: createModelResponseParams([]),
  messages: MetaModelAPIMessagesConfig,
  messagesCountTokens: MetaModelAPIMessagesConfig,
  requestTransforms: {
    uploadFile: (requestBody: ReadableStream) => requestBody,
  },
  responseTransforms: {
    ...responseTransformers(META_MODEL_API, { chatComplete: true }),
    createModelResponse: OpenAICreateModelResponseTransformer(META_MODEL_API),
    messages: MetaModelAPIMessagesResponseTransform,
    uploadFile: MetaModelAPIFilesResponseTransform,
    listFiles: MetaModelAPIFilesResponseTransform,
    retrieveFile: MetaModelAPIFilesResponseTransform,
    deleteFile: MetaModelAPIFilesResponseTransform,
  },
};

export default MetaModelAPIProviderConfig;
