import { ErrorResponse, ProviderConfig } from '../types';
import { ElevenLabsErrorResponseTransform } from './utils';

// `voice` and `response_format` go into the URL (see api.ts).
export const ElevenLabsCreateSpeechConfig: ProviderConfig = {
  input: {
    param: 'text',
    required: true,
  },
  model: {
    param: 'model_id',
    required: true,
    default: 'eleven_multilingual_v2',
  },
  language: {
    param: 'language_code',
  },
  speed: {
    param: 'voice_settings.speed',
  },
};

// Audio bytes pass through untouched; only JSON errors reach this transform.
export const ElevenLabsCreateSpeechResponseTransform: (
  response: Record<string, any>,
  responseStatus: number
) => Record<string, any> | ErrorResponse = (response, responseStatus) => {
  if (responseStatus !== 200) {
    return ElevenLabsErrorResponseTransform(response);
  }
  return response;
};
