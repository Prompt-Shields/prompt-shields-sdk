import { ELEVENLABS } from '../../globals';
import { ErrorResponse, RequestHandler } from '../types';
import { generateErrorResponse } from '../utils';
import { ELEVENLABS_BASE_URL } from './api';
import { ElevenLabsErrorResponseTransform } from './utils';
import { upstreamFetch } from '../../utils/fetch';

const DEFAULT_MODEL = 'scribe_v1';

// ElevenLabs-native fields forwarded as is.
const PASSTHROUGH_FIELDS = [
  'diarize',
  'num_speakers',
  'tag_audio_events',
  'timestamps_granularity',
];

/**
 * Maps an OpenAI-style transcription form (`file`, `model`, `language`,
 * `timestamp_granularities[]`) to the ElevenLabs speech-to-text form.
 */
export const toElevenLabsTranscriptionForm = (input: FormData): FormData => {
  const form = new FormData();
  const file = input.get('file');
  if (file !== null) form.append('file', file);

  const model = input.get('model_id') ?? input.get('model');
  form.append(
    'model_id',
    typeof model === 'string' && model ? model : DEFAULT_MODEL
  );

  const language = input.get('language_code') ?? input.get('language');
  if (typeof language === 'string' && language) {
    form.append('language_code', language);
  }

  const granularities = [
    ...input.getAll('timestamp_granularities[]'),
    ...input.getAll('timestamp_granularities'),
  ];
  if (!input.has('timestamps_granularity') && granularities.length) {
    form.append(
      'timestamps_granularity',
      granularities.includes('word') ? 'word' : 'none'
    );
  }

  for (const field of PASSTHROUGH_FIELDS) {
    const value = input.get(field);
    if (typeof value === 'string') form.append(field, value);
  }
  return form;
};

/**
 * The gateway forwards multipart bodies untouched, so the form is rebuilt
 * here and sent to the fixed ElevenLabs host. Custom hosts are not used.
 */
export const ElevenLabsCreateTranscriptionRequestHandler: RequestHandler<
  FormData
> = async ({ providerOptions, requestBody }) => {
  if (!(requestBody instanceof FormData)) {
    return new Response(
      JSON.stringify(
        generateErrorResponse(
          {
            message: 'transcription requests must be multipart/form-data',
            type: 'invalid_request_error',
            param: null,
            code: null,
          },
          ELEVENLABS
        )
      ),
      { status: 400, headers: { 'content-type': 'application/json' } }
    );
  }
  return upstreamFetch(`${ELEVENLABS_BASE_URL}/speech-to-text`, {
    method: 'POST',
    headers: { 'xi-api-key': providerOptions.apiKey ?? '' },
    body: toElevenLabsTranscriptionForm(requestBody),
  });
};

interface ElevenLabsWord {
  text: string;
  start?: number;
  end?: number;
  type?: string;
  speaker_id?: string;
}

/** Maps the ElevenLabs transcript to the OpenAI transcription shape. */
export const ElevenLabsCreateTranscriptionResponseTransform: (
  response: Record<string, any>,
  responseStatus: number
) => Record<string, any> | ErrorResponse = (response, responseStatus) => {
  if (responseStatus !== 200 || typeof response?.text !== 'string') {
    return ElevenLabsErrorResponseTransform(response);
  }
  const allWords: ElevenLabsWord[] = Array.isArray(response.words)
    ? response.words
    : [];
  const words = allWords.filter((w) => !w.type || w.type === 'word');
  const ends = allWords
    .map((w) => w.end)
    .filter((e): e is number => typeof e === 'number');
  const duration = ends.length ? Math.max(...ends) : undefined;

  return {
    text: response.text,
    ...(response.language_code && { language: response.language_code }),
    ...(duration !== undefined && { duration }),
    ...(words.length > 0 && {
      words: words.map((w) => ({ word: w.text, start: w.start, end: w.end })),
    }),
    // Non-token usage, in the OpenAI transcription `usage` shape.
    ...(duration !== undefined && {
      usage: { type: 'duration', seconds: duration },
    }),
  };
};
