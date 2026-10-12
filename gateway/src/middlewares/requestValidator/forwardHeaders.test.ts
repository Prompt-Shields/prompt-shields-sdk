// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import { Hono } from 'hono';
import { requestValidator } from '.';

function makeApp() {
  const app = new Hono();
  app.post('/v1/chat/completions', requestValidator, (c) => c.text('ok'));
  return app;
}

const post = (headers: Record<string, string>) =>
  makeApp().request('/v1/chat/completions', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-portkey-provider': 'openai',
      ...headers,
    },
    body: JSON.stringify({ model: 'gpt-4o', messages: [] }),
  });

describe('x-portkey-forward-headers loop guard', () => {
  test('rejects a request that forwards the forward-headers header itself', async () => {
    const res = await post({
      'x-portkey-forward-headers': 'x-trace-id, X-Portkey-Forward-Headers',
    });
    expect(res.status).toBe(400);
    expect(await res.text()).toContain('forward_headers must not contain');
  });

  test('rejects a config whose forward_headers lists the header', async () => {
    const res = await post({
      'x-portkey-config': JSON.stringify({
        provider: 'openai',
        forward_headers: ['x-portkey-forward-headers'],
      }),
    });
    expect(res.status).toBe(400);
  });

  test('allows ordinary forwarded headers', async () => {
    const res = await post({ 'x-portkey-forward-headers': 'x-trace-id' });
    expect(res.status).toBe(200);
  });
});
