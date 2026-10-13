// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../utils/env', () => ({ Environment: () => ({}) }));
// The plugin registry pulls in ESM-only deps (jose); no guardrails run here.
jest.mock('../../plugins', () => ({ plugins: {} }));

import { Hono } from 'hono';
import { proxyHandler } from './proxyHandler';
import { requestValidator } from '../middlewares/requestValidator';
import { hooks } from '../middlewares/hooks';

// Mirrors the catch-all proxy routes registered in src/index.ts.
function makeApp() {
  const app = new Hono();
  app.use('*', hooks);
  app.post('/v1/*', requestValidator, proxyHandler);
  app.get('/v1/:path{(?!realtime).*}', requestValidator, proxyHandler);
  app.delete('/v1/*', requestValidator, proxyHandler);
  return app;
}

const baseHeaders = {
  'x-portkey-provider': 'openai',
  authorization: 'Bearer sk-test',
};

describe('proxyHandler body-less requests', () => {
  const realFetch = global.fetch;
  let fetchMock: jest.Mock;

  beforeEach(() => {
    fetchMock = jest.fn(
      async () =>
        new Response(JSON.stringify({ object: 'list', data: [] }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
    );
    global.fetch = fetchMock as any;
  });

  afterEach(() => {
    global.fetch = realFetch;
  });

  test.each(['GET', 'DELETE'])(
    '%s without content-type proxies with no body',
    async (method) => {
      const res = await makeApp().request('/v1/files', {
        method,
        headers: baseHeaders,
      });

      expect(res.status).toBe(200);
      expect(await res.json()).toEqual({ object: 'list', data: [] });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0];
      expect(String(url)).toBe('https://api.openai.com/v1/files');
      expect(init.method).toBe(method);
      expect(init.body).toBeUndefined();
    }
  );

  test('GET with application/json content-type still works', async () => {
    const res = await makeApp().request('/v1/files', {
      method: 'GET',
      headers: { ...baseHeaders, 'content-type': 'application/json' },
    });
    expect(res.status).toBe(200);
    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
  });

  test('POST with a JSON body is forwarded unchanged', async () => {
    const payload = { purpose: 'batch', foo: 'bar' };
    const res = await makeApp().request('/v1/files', {
      method: 'POST',
      headers: { ...baseHeaders, 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    });
    expect(res.status).toBe(200);
    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual(payload);
  });
});
