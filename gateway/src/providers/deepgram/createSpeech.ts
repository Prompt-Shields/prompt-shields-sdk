import { ErrorResponse, ProviderConfig } from '../types';
import { DeepgramErrorResponseTransform } from './utils';

// `model`, `voice` and `response_format` go into the query (see api.ts).
export const DeepgramCreateSpeechConfig: ProviderConfig = {
  input: {
    param: 'text',
    required: true,
  },
};

// Audio bytes pass through untouched; only JSON errors reach this transform.
export const DeepgramCreateSpeechResponseTransform: (
  response: Record<string, any>,
  responseStatus: number
) => Record<string, any> | ErrorResponse = (response, responseStatus) => {
  if (responseStatus !== 200) {
    return DeepgramErrorResponseTransform(response);
  }
  return response;
};
