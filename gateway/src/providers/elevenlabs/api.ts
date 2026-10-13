import { GatewayError } from '../../errors/GatewayError';
import { ProviderAPIConfig } from '../types';

// Fixed vendor host. Request fields only fill a validated path segment and
// query values, never the host.
export const ELEVENLABS_BASE_URL = 'https://api.elevenlabs.io/v1';

// ElevenLabs voice IDs are opaque alphanumeric IDs. Anything else is rejected
// so the voice cannot add path segments (e.g. `..`) to the upstream URL.
const VOICE_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

// OpenAI `response_format` values mapped to ElevenLabs `output_format`.
const OUTPUT_FORMATS: Record<string, string> = {
  mp3: 'mp3_44100_128',
  pcm: 'pcm_24000',
  opus: 'opus_48000_128',
};
const NATIVE_OUTPUT_FORMAT_PATTERN = /^(mp3|pcm|ulaw|alaw|opus)_\d+(_\d+)?$/;

export const getElevenLabsOutputFormat = (
  responseFormat?: string
): string | undefined => {
  if (!responseFormat) return undefined;
  if (OUTPUT_FORMATS[responseFormat]) return OUTPUT_FORMATS[responseFormat];
  if (NATIVE_OUTPUT_FORMAT_PATTERN.test(responseFormat)) return responseFormat;
  throw new GatewayError(
    `elevenlabs does not support response_format "${responseFormat}"`,
    400
  );
};

export const getElevenLabsSpeechPath = (
  params: Record<string, any>
): string => {
  const voice = params.voice;
  if (typeof voice !== 'string' || !VOICE_ID_PATTERN.test(voice)) {
    throw new GatewayError(
      'elevenlabs requires `voice` to be an ElevenLabs voice ID',
      400
    );
  }
  const outputFormat = getElevenLabsOutputFormat(params.response_format);
  const query = outputFormat
    ? `?${new URLSearchParams({ output_format: outputFormat })}`
    : '';
  return `/text-to-speech/${voice}${query}`;
};

const ElevenLabsAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => ELEVENLABS_BASE_URL,
  headers: ({ providerOptions }) => ({
    'xi-api-key': providerOptions.apiKey ?? '',
  }),
  getEndpoint: ({ fn, gatewayRequestBodyJSON }) => {
    switch (fn) {
      case 'createSpeech':
        return getElevenLabsSpeechPath(gatewayRequestBodyJSON);
      case 'createTranscription':
        return '/speech-to-text';
      default:
        return '';
    }
  },
};

export default ElevenLabsAPIConfig;
