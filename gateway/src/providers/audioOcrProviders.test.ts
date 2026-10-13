// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../utils/env', () => ({ Environment: () => ({}) }));

import ElevenLabsAPIConfig from './elevenlabs/api';
import ElevenLabsConfig from './elevenlabs';
import { ElevenLabsCreateSpeechConfig } from './elevenlabs/createSpeech';
import {
  ElevenLabsCreateTranscriptionRequestHandler,
  ElevenLabsCreateTranscriptionResponseTransform,
  toElevenLabsTranscriptionForm,
} from './elevenlabs/createTranscription';
import DeepgramAPIConfig from './deepgram/api';
import DeepgramConfig from './deepgram';
import {
  DeepgramCreateTranscriptionRequestHandler,
  DeepgramCreateTranscriptionResponseTransform,
  getDeepgramListenQuery,
} from './deepgram/createTranscription';
import MistralAIAPIConfig from './mistral-ai/api';
import {
  GetMistralAIOcrResponseTransform,
  MistralAIOcrConfig,
} from './mistral-ai/ocr';
import GoogleVertexAIConfig from './google-vertex-ai';
import AzureAIInferenceAPI from './azure-ai-inference/api';
import { transformUsingProviderConfig } from '../services/transformToProviderRequest';
import { isSafeUpstreamURL } from '../middlewares/requestValidator';
import {
  apiSurfaceOf,
  modalityOf,
  usageUnits,
} from '../middlewares/ps-telemetry';

const c: any = { get: () => undefined, env: {} };
const endpoint = (api: any, fn: string, body: any) =>
  api.getEndpoint({
    c,
    fn,
    providerOptions: {},
    gatewayRequestBodyJSON: body,
    gatewayRequestURL: 'https://gw.local/v1/ocr',
  });

const audioForm = () => {
  const form = new FormData();
  form.append(
    'file',
    new Blob([new Uint8Array([1, 2, 3])], { type: 'audio/wav' }),
    'a.wav'
  );
  return form;
};

const mockFetch = () => {
  const fetchMock = jest
    .fn()
    .mockResolvedValue(new Response('{}', { status: 200 }));
  (global as any).fetch = fetchMock;
  return fetchMock;
};

describe('elevenlabs', () => {
  test('fixed vendor host and xi-api-key auth', async () => {
    const url = await ElevenLabsAPIConfig.getBaseURL({
      providerOptions: { provider: 'elevenlabs', baseURL: 'https://evil' },
      c,
      gatewayRequestURL: '',
    } as any);
    expect(url).toBe('https://api.elevenlabs.io/v1');
    expect(isSafeUpstreamURL(url)).toBe(true);
    expect(
      await ElevenLabsAPIConfig.headers({
        providerOptions: { apiKey: 'k' },
      } as any)
    ).toEqual({ 'xi-api-key': 'k' });
  });

  test('speech request: voice in path, format in query, body mapped', () => {
    const params = {
      model: 'eleven_turbo_v2_5',
      input: 'Hello',
      voice: 'JBFqnCBsd6RMkjVDRZzb',
      response_format: 'mp3',
      speed: 1.1,
    };
    expect(endpoint(ElevenLabsAPIConfig, 'createSpeech', params)).toBe(
      '/text-to-speech/JBFqnCBsd6RMkjVDRZzb?output_format=mp3_44100_128'
    );
    expect(
      transformUsingProviderConfig(ElevenLabsCreateSpeechConfig, params as any)
    ).toEqual({
      text: 'Hello',
      model_id: 'eleven_turbo_v2_5',
      voice_settings: { speed: 1.1 },
    });
    expect(
      transformUsingProviderConfig(ElevenLabsCreateSpeechConfig, {
        input: 'Hi',
      } as any)
    ).toEqual({ text: 'Hi', model_id: 'eleven_multilingual_v2' });
  });

  test('voice cannot add path segments or change the host', () => {
    for (const voice of ['..', '../models', 'a/b', '%2e%2e', '@evil.com', '']) {
      expect(() =>
        endpoint(ElevenLabsAPIConfig, 'createSpeech', { voice, input: 'x' })
      ).toThrow('voice');
    }
    expect(() =>
      endpoint(ElevenLabsAPIConfig, 'createSpeech', {
        voice: 'abc',
        response_format: 'wav&x=1',
      })
    ).toThrow('response_format');
  });

  test('transcription form maps OpenAI fields to ElevenLabs fields', () => {
    const input = audioForm();
    input.append('model', 'scribe_v1');
    input.append('language', 'en');
    input.append('timestamp_granularities[]', 'word');
    input.append('diarize', 'true');
    input.append('prompt', 'dropped');
    const out = toElevenLabsTranscriptionForm(input);
    expect(out.get('file')).toBeInstanceOf(Blob);
    expect(out.get('model_id')).toBe('scribe_v1');
    expect(out.get('language_code')).toBe('en');
    expect(out.get('timestamps_granularity')).toBe('word');
    expect(out.get('diarize')).toBe('true');
    expect(out.has('prompt')).toBe(false);
    expect(toElevenLabsTranscriptionForm(audioForm()).get('model_id')).toBe(
      'scribe_v1'
    );
  });

  test('transcription handler posts to the fixed host', async () => {
    const fetchMock = mockFetch();
    await ElevenLabsCreateTranscriptionRequestHandler(<any>{
      c,
      providerOptions: { provider: 'elevenlabs', apiKey: 'k' },
      requestURL: '',
      requestHeaders: {},
      requestBody: audioForm(),
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('https://api.elevenlabs.io/v1/speech-to-text');
    expect(init.headers).toEqual({ 'xi-api-key': 'k' });
    expect(init.body).toBeInstanceOf(FormData);
    expect(ElevenLabsConfig.requestHandlers?.createTranscription).toBe(
      ElevenLabsCreateTranscriptionRequestHandler
    );
  });

  test('transcription response maps to OpenAI shape with duration usage', () => {
    const out = ElevenLabsCreateTranscriptionResponseTransform(
      {
        language_code: 'en',
        language_probability: 0.99,
        text: 'Hi there',
        words: [
          { text: 'Hi', start: 0, end: 0.4, type: 'word' },
          { text: ' ', start: 0.4, end: 0.5, type: 'spacing' },
          { text: 'there', start: 0.5, end: 1.25, type: 'word' },
        ],
      },
      200
    );
    expect(out).toEqual({
      text: 'Hi there',
      language: 'en',
      duration: 1.25,
      words: [
        { word: 'Hi', start: 0, end: 0.4 },
        { word: 'there', start: 0.5, end: 1.25 },
      ],
      usage: { type: 'duration', seconds: 1.25 },
    });
  });

  test('errors map to the gateway error shape', () => {
    const transform = ElevenLabsConfig.responseTransforms.createSpeech;
    expect(
      transform(
        { detail: { status: 'voice_not_found', message: 'Voice not found' } },
        404
      )
    ).toEqual({
      error: {
        message: 'elevenlabs error: Voice not found',
        type: null,
        param: null,
        code: 'voice_not_found',
      },
      provider: 'elevenlabs',
    });
    expect(
      ElevenLabsCreateTranscriptionResponseTransform(
        { detail: [{ msg: 'field required' }] },
        422
      ).error.message
    ).toBe('elevenlabs error: field required');
  });
});

describe('deepgram', () => {
  test('fixed vendor host and Token auth', async () => {
    const url = await DeepgramAPIConfig.getBaseURL({
      providerOptions: {},
      c,
      gatewayRequestURL: '',
    } as any);
    expect(url).toBe('https://api.deepgram.com/v1');
    expect(isSafeUpstreamURL(url)).toBe(true);
    expect(
      await DeepgramAPIConfig.headers({
        providerOptions: { apiKey: 'k' },
      } as any)
    ).toEqual({ Authorization: 'Token k' });
  });

  test('speech request: model and encoding in the query, text in the body', () => {
    expect(
      endpoint(DeepgramAPIConfig, 'createSpeech', {
        model: 'aura-2-thalia-en',
        voice: 'alloy',
        input: 'Hello',
        response_format: 'wav',
      })
    ).toBe('/speak?model=aura-2-thalia-en&encoding=linear16&container=wav');
    expect(
      endpoint(DeepgramAPIConfig, 'createSpeech', {
        model: 'tts-1',
        voice: 'aura-asteria-en',
      })
    ).toBe('/speak?model=aura-asteria-en');
    expect(
      transformUsingProviderConfig(DeepgramConfig.createSpeech, {
        model: 'aura-2-thalia-en',
        input: 'Hello',
      } as any)
    ).toEqual({ text: 'Hello' });
    expect(() =>
      endpoint(DeepgramAPIConfig, 'createSpeech', { response_format: 'x' })
    ).toThrow('response_format');
  });

  test('model values stay query values', () => {
    const q = endpoint(DeepgramAPIConfig, 'createSpeech', {
      model: 'aura-x&host=evil.com/#',
    });
    expect(q).toBe('/speak?model=aura-x%26host%3Devil.com%2F%23');
  });

  test('listen query from the transcription form', () => {
    const form = audioForm();
    form.append('model', 'nova-3');
    form.append('language', 'en');
    form.append('diarize', 'true');
    form.append('punctuate', 'maybe');
    expect(getDeepgramListenQuery(form).toString()).toBe(
      'model=nova-3&language=en&smart_format=true&diarize=true'
    );
    expect(getDeepgramListenQuery(audioForm()).get('model')).toBe('nova-3');
  });

  test('transcription handler sends the raw audio to the fixed host', async () => {
    const fetchMock = mockFetch();
    await DeepgramCreateTranscriptionRequestHandler(<any>{
      c,
      providerOptions: { provider: 'deepgram', apiKey: 'k' },
      requestURL: '',
      requestHeaders: {},
      requestBody: audioForm(),
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(
      'https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true'
    );
    expect(init.headers).toEqual({
      Authorization: 'Token k',
      'content-type': 'audio/wav',
    });
    expect(init.body).toBeInstanceOf(Blob);

    const missing = await DeepgramCreateTranscriptionRequestHandler(<any>{
      c,
      providerOptions: {},
      requestURL: '',
      requestHeaders: {},
      requestBody: new FormData(),
    });
    expect(missing.status).toBe(400);
  });

  test('transcription response maps to OpenAI shape with duration usage', () => {
    const out = DeepgramCreateTranscriptionResponseTransform(
      {
        metadata: { request_id: 'r', duration: 2.5 },
        results: {
          channels: [
            {
              detected_language: 'en',
              alternatives: [
                {
                  transcript: 'Hello world.',
                  confidence: 0.98,
                  words: [
                    {
                      word: 'hello',
                      punctuated_word: 'Hello',
                      start: 0.1,
                      end: 0.5,
                    },
                    {
                      word: 'world',
                      punctuated_word: 'world.',
                      start: 0.6,
                      end: 1.0,
                    },
                  ],
                },
              ],
            },
          ],
        },
      },
      200
    );
    expect(out).toEqual({
      text: 'Hello world.',
      language: 'en',
      duration: 2.5,
      words: [
        { word: 'Hello', start: 0.1, end: 0.5 },
        { word: 'world.', start: 0.6, end: 1.0 },
      ],
      usage: { type: 'duration', seconds: 2.5 },
    });
  });

  test('errors map to the gateway error shape', () => {
    expect(
      DeepgramCreateTranscriptionResponseTransform(
        { err_code: 'INVALID_AUTH', err_msg: 'Invalid credentials.' },
        401
      )
    ).toEqual({
      error: {
        message: 'deepgram error: Invalid credentials.',
        type: null,
        param: null,
        code: 'INVALID_AUTH',
      },
      provider: 'deepgram',
    });
  });
});

describe('ocr', () => {
  const ocrRequest = {
    model: 'mistral-ocr-latest',
    document: { type: 'document_url', document_url: 'https://x/doc.pdf' },
    include_image_base64: true,
    unknown_field: 'dropped',
  };

  test('mistral routes /v1/ocr to /ocr', () => {
    expect(endpoint(MistralAIAPIConfig, 'ocr', ocrRequest)).toBe('/ocr');
  });

  test('request keeps the Mistral OCR shape and drops unknown fields', () => {
    expect(
      transformUsingProviderConfig(MistralAIOcrConfig, ocrRequest as any)
    ).toEqual({
      model: 'mistral-ocr-latest',
      document: ocrRequest.document,
      include_image_base64: true,
    });
    expect(
      transformUsingProviderConfig(MistralAIOcrConfig, {
        model: 'mistralai.mistral-ocr-2505',
        document: ocrRequest.document,
      } as any).model
    ).toBe('mistral-ocr-2505');
  });

  test('vertex-hosted mistral OCR uses rawPredict', () => {
    const config = GoogleVertexAIConfig.getConfig!({
      params: { model: 'mistralai.mistral-ocr-2505' },
      providerOptions: {} as any,
    });
    expect(config.ocr).toBe(MistralAIOcrConfig);
    expect(typeof config.responseTransforms.ocr).toBe('function');
    expect(
      config.api.getEndpoint({
        c,
        fn: 'ocr',
        providerOptions: { vertexProjectId: 'p', vertexRegion: 'us-central1' },
        gatewayRequestBodyJSON: { model: 'mistralai.mistral-ocr-2505' },
        gatewayRequestURL: 'https://gw.local/v1/ocr',
      })
    ).toBe(
      '/v1/projects/p/locations/us-central1/publishers/mistralai/models/mistral-ocr-2505:rawPredict'
    );
  });

  test('azure ai foundry routes /v1/ocr to /ocr', () => {
    expect(
      AzureAIInferenceAPI.getEndpoint({
        c,
        fn: 'ocr',
        providerOptions: { azureApiVersion: '2024-05-01-preview' },
        gatewayRequestBodyJSON: ocrRequest,
        gatewayRequestURL: 'https://gw.local/v1/ocr',
      } as any)
    ).toBe('/ocr?api-version=2024-05-01-preview');
  });

  test('response passes through with provider, errors are normalised', () => {
    const transform = GetMistralAIOcrResponseTransform('mistral-ai');
    const ok = {
      pages: [{ index: 0, markdown: '# Hi', images: [], dimensions: null }],
      model: 'mistral-ocr-2505',
      usage_info: { pages_processed: 1, doc_size_bytes: 10 },
    };
    expect(transform(ok, 200)).toEqual({ ...ok, provider: 'mistral-ai' });
    expect(
      transform(
        { object: 'error', message: 'bad doc', type: 'invalid_request' },
        400
      )
    ).toEqual({
      error: {
        message: 'mistral-ai error: bad doc',
        type: 'invalid_request',
        param: null,
        code: null,
      },
      provider: 'mistral-ai',
    });
  });
});

describe('telemetry modality and units', () => {
  test('modality from path', () => {
    expect(apiSurfaceOf('/v1/ocr')).toBe('ocr');
    expect(modalityOf('/v1/ocr')).toBe('document');
    expect(modalityOf('/v1/audio/speech')).toBe('audio');
    expect(modalityOf('/v1/audio/transcriptions')).toBe('audio');
    expect(modalityOf('/v1/images/generations')).toBe('image');
    expect(modalityOf('/v1/chat/completions')).toBe('text');
    expect(modalityOf('/v1/files')).toBeUndefined();
  });

  test('non-token units', () => {
    expect(
      usageUnits(
        '/v1/audio/transcriptions',
        { text: 'x', usage: { type: 'duration', seconds: 2.5 } },
        undefined
      )
    ).toEqual({ usage_units: 2.5, usage_unit_type: 'seconds' });
    expect(
      usageUnits('/v1/ocr', { usage_info: { pages_processed: 3 } }, {})
    ).toEqual({ usage_units: 3, usage_unit_type: 'pages' });
    expect(
      usageUnits('/v1/audio/speech', undefined, { input: 'Hello' })
    ).toEqual({ usage_units: 5, usage_unit_type: 'characters' });
    expect(usageUnits('/v1/chat/completions', { usage: {} }, {})).toEqual({});
  });
});
