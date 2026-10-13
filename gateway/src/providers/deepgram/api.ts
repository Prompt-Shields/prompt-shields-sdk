import { GatewayError } from '../../errors/GatewayError';
import { ProviderAPIConfig } from '../types';

// Fixed vendor host. Request fields only fill query values, never the host.
export const DEEPGRAM_BASE_URL = 'https://api.deepgram.com/v1';

const DEFAULT_SPEECH_MODEL = 'aura-2-thalia-en';

// OpenAI `response_format` values mapped to Deepgram encoding/container.
const SPEECH_FORMATS: Record<string, Record<string, string>> = {
  mp3: { encoding: 'mp3' },
  opus: { encoding: 'opus' },
  aac: { encoding: 'aac' },
  flac: { encoding: 'flac' },
  wav: { encoding: 'linear16', container: 'wav' },
  pcm: { encoding: 'linear16', container: 'none' },
};

/**
 * Deepgram Aura voices are models (e.g. `aura-2-thalia-en`), so the voice
 * may be passed as either `model` or `voice`.
 */
export const getDeepgramSpeechQuery = (
  params: Record<string, any>
): URLSearchParams => {
  const { model, voice, response_format } = params;
  const query = new URLSearchParams();
  const isAura = (v: unknown): v is string =>
    typeof v === 'string' && v.startsWith('aura');
  query.set(
    'model',
    isAura(model)
      ? model
      : isAura(voice)
        ? voice
        : model || DEFAULT_SPEECH_MODEL
  );
  if (response_format) {
    const format = SPEECH_FORMATS[response_format];
    if (!format) {
      throw new GatewayError(
        `deepgram does not support response_format "${response_format}"`,
        400
      );
    }
    for (const [k, v] of Object.entries(format)) query.set(k, v);
  }
  return query;
};

const DeepgramAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => DEEPGRAM_BASE_URL,
  headers: ({ providerOptions }) => ({
    Authorization: `Token ${providerOptions.apiKey ?? ''}`,
  }),
  getEndpoint: ({ fn, gatewayRequestBodyJSON }) => {
    switch (fn) {
      case 'createSpeech':
        return `/speak?${getDeepgramSpeechQuery(gatewayRequestBodyJSON)}`;
      case 'createTranscription':
        return '/listen';
      default:
        return '';
    }
  },
};

export default DeepgramAPIConfig;
