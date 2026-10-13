import { DEEPGRAM } from '../../globals';
import { ErrorResponse, RequestHandler } from '../types';
import { generateErrorResponse } from '../utils';
import { DEEPGRAM_BASE_URL } from './api';
import { DeepgramErrorResponseTransform } from './utils';

const DEFAULT_MODEL = 'nova-3';

// Deepgram-native boolean options forwarded as query values.
const PASSTHROUGH_OPTIONS = [
  'smart_format',
  'punctuate',
  'diarize',
  'detect_language',
  'utterances',
  'paragraphs',
];

/**
 * Builds the Deepgram `/listen` query from an OpenAI-style transcription
 * form. Values only ever become query values.
 */
export const getDeepgramListenQuery = (input: FormData): URLSearchParams => {
  const query = new URLSearchParams();
  const model = input.get('model');
  query.set(
    'model',
    typeof model === 'string' && model ? model : DEFAULT_MODEL
  );
  const language = input.get('language');
  if (typeof language === 'string' && language) query.set('language', language);
  query.set('smart_format', 'true');
  for (const option of PASSTHROUGH_OPTIONS) {
    const value = input.get(option);
    if (value === 'true' || value === 'false') query.set(option, value);
  }
  return query;
};

const errorResponse = (message: string, status: number): Response =>
  new Response(
    JSON.stringify(
      generateErrorResponse(
        { message, type: 'invalid_request_error', param: null, code: null },
        DEEPGRAM
      )
    ),
    { status, headers: { 'content-type': 'application/json' } }
  );

/**
 * Deepgram takes the raw audio as the request body, so the file is pulled
 * out of the multipart form and sent to the fixed Deepgram host. Custom
 * hosts are not used.
 */
export const DeepgramCreateTranscriptionRequestHandler: RequestHandler<
  FormData
> = async ({ providerOptions, requestBody }) => {
  if (!(requestBody instanceof FormData)) {
    return errorResponse(
      'transcription requests must be multipart/form-data',
      400
    );
  }
  const file: unknown = requestBody.get('file');
  if (!(file instanceof Blob)) {
    return errorResponse('`file` is required', 400);
  }
  return fetch(
    `${DEEPGRAM_BASE_URL}/listen?${getDeepgramListenQuery(requestBody)}`,
    {
      method: 'POST',
      headers: {
        Authorization: `Token ${providerOptions.apiKey ?? ''}`,
        'content-type': file.type || 'application/octet-stream',
      },
      body: file,
    }
  );
};

interface DeepgramWord {
  word: string;
  punctuated_word?: string;
  start: number;
  end: number;
}

/** Maps the Deepgram `/listen` result to the OpenAI transcription shape. */
export const DeepgramCreateTranscriptionResponseTransform: (
  response: Record<string, any>,
  responseStatus: number
) => Record<string, any> | ErrorResponse = (response, responseStatus) => {
  const channel = response?.results?.channels?.[0];
  const alternative = channel?.alternatives?.[0];
  if (responseStatus !== 200 || typeof alternative?.transcript !== 'string') {
    return DeepgramErrorResponseTransform(response);
  }
  const duration = response?.metadata?.duration;
  const hasDuration = typeof duration === 'number';
  const words: DeepgramWord[] = Array.isArray(alternative.words)
    ? alternative.words
    : [];

  return {
    text: alternative.transcript,
    ...(channel.detected_language && { language: channel.detected_language }),
    ...(hasDuration && { duration }),
    ...(words.length > 0 && {
      words: words.map((w) => ({
        word: w.punctuated_word ?? w.word,
        start: w.start,
        end: w.end,
      })),
    }),
    // Non-token usage, in the OpenAI transcription `usage` shape.
    ...(hasDuration && { usage: { type: 'duration', seconds: duration } }),
  };
};
