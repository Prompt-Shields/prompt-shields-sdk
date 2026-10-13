// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../utils/env', () => ({ Environment: () => ({}) }));
// The plugin registry pulls in ESM-only deps (jose); no guardrails run here.
jest.mock('../../plugins', () => ({ plugins: {} }));

import { Hono } from 'hono';
import { createTranscriptionHandler } from './createTranscriptionHandler';
import { requestValidator } from '../middlewares/requestValidator';
import { hooks } from '../middlewares/hooks';
import { memoryCache } from '../middlewares/cache';

// POSTs served by a provider request handler (#75): the cache middleware reads
// requestOptions[0].requestParams after the handler, so it must be set.
function makeApp() {
  const app = new Hono();
  app.use('*', memoryCache());
  app.use('*', hooks);
  app.post(
    '/v1/audio/transcriptions',
    requestValidator,
    createTranscriptionHandler
  );
  return app;
}

describe('request-handler POSTs', () => {
  const realFetch = global.fetch;
  let fetchMock: jest.Mock;

  beforeEach(() => {
    fetchMock = jest.fn(
      async () =>
        new Response(
          JSON.stringify({
            metadata: { duration: 1.5 },
            results: { channels: [{ alternatives: [{ transcript: 'hi' }] }] },
          }),
          { status: 200, headers: { 'content-type': 'application/json' } }
        )
    );
    global.fetch = fetchMock as any;
  });

  afterEach(() => {
    global.fetch = realFetch;
  });

  test('deepgram transcription returns the mapped transcript', async () => {
    const form = new FormData();
    form.append('file', new Blob(['abc'], { type: 'audio/wav' }), 'a.wav');
    form.append('model', 'nova-3');

    const res = await makeApp().request('/v1/audio/transcriptions', {
      method: 'POST',
      headers: {
        'x-portkey-provider': 'deepgram',
        authorization: 'Bearer dg-test',
      },
      body: form,
    });

    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.text).toBe('hi');
    expect(body.usage).toEqual({ type: 'duration', seconds: 1.5 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toMatch(
      /^https:\/\/api\.deepgram\.com\/v1\/listen\?/
    );
  });
});
