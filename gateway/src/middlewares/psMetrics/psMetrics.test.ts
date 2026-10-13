import { Hono } from 'hono';
import { metricsHandler, psMetrics } from '.';
import { Counter } from './registry';

function makeApp() {
  const app = new Hono();
  app.get('/metrics', metricsHandler);
  app.use('*', psMetrics());
  app.post('/v1/chat/completions', (c) =>
    c.json({
      model: 'gpt-4o-mini',
      usage: { prompt_tokens: 11, completion_tokens: 7 },
      choices: [{ message: { content: 'secret answer text' } }],
    })
  );
  app.get('/v1/files/:id', (c) => c.json({ id: c.req.param('id') }));
  // A later catch-all, like the gateway's proxy route, must not win.
  app.post('/v1/*', (c) => c.text('proxy'));
  return app;
}

const chat = (app: Hono, headers: Record<string, string> = {}) =>
  app.request('/v1/chat/completions', {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify({
      messages: [{ role: 'user', content: 'secret prompt text' }],
    }),
  });

describe('psMetrics', () => {
  afterEach(() => {
    delete process.env.PS_METRICS_ENABLED;
    delete process.env.PS_METRICS_TOKEN;
  });

  test('/metrics is 404 unless enabled', async () => {
    const res = await makeApp().request('/metrics');
    expect(res.status).toBe(404);
  });

  test('exports request, duration and token metrics with business labels', async () => {
    process.env.PS_METRICS_ENABLED = 'true';
    const app = makeApp();
    await chat(app, {
      'x-portkey-provider': 'openai',
      'x-ps-business-unit': 'HR',
      'x-ps-environment': 'production',
    });
    const res = await app.request('/metrics');
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toContain('text/plain');
    const text = await res.text();
    expect(text).toMatch(
      /ps_gateway_requests_total\{route="\/v1\/chat\/completions",method="POST",status="200",provider="openai",business_unit="HR",environment="production"\} \d+/
    );
    expect(text).toContain('ps_gateway_request_duration_seconds_bucket{');
    expect(text).toMatch(
      /ps_gateway_tokens_total\{provider="openai",model="gpt-4o-mini",direction="input",business_unit="HR"\} \d+/
    );
    // Privacy contract: no prompt or completion content in metrics.
    expect(text).not.toContain('secret prompt text');
    expect(text).not.toContain('secret answer text');
  });

  test('path parameters do not create new series', async () => {
    process.env.PS_METRICS_ENABLED = 'true';
    const app = makeApp();
    await app.request('/v1/files/file-abc');
    await app.request('/v1/files/file-def');
    const text = await (await app.request('/metrics')).text();
    expect(text).toContain('route="/v1/files/:id"');
    expect(text).not.toContain('file-abc');
  });

  test('PS_METRICS_TOKEN protects the endpoint', async () => {
    process.env.PS_METRICS_ENABLED = 'true';
    process.env.PS_METRICS_TOKEN = 'scrape-token';
    const app = makeApp();
    expect((await app.request('/metrics')).status).toBe(401);
    const ok = await app.request('/metrics', {
      headers: { authorization: 'Bearer scrape-token' },
    });
    expect(ok.status).toBe(200);
  });

  test('odd header values are bounded and escaped', async () => {
    process.env.PS_METRICS_ENABLED = 'true';
    const app = makeApp();
    await chat(app, { 'x-ps-business-unit': 'HR"} evil{x="1' });
    const text = await (await app.request('/metrics')).text();
    expect(text).toContain('business_unit="other"');
    expect(text).not.toContain('evil');
  });

  test('streamed responses record usage and outcome when the stream ends', async () => {
    process.env.PS_METRICS_ENABLED = 'true';
    const app = new Hono();
    app.get('/metrics', metricsHandler);
    app.use('*', psMetrics());
    const sse = (body: string) => () =>
      new Response(body, { headers: { 'content-type': 'text/event-stream' } });
    app.post(
      '/v1/messages',
      sse(
        'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-s","usage":{"input_tokens":9,"output_tokens":1}}}\n\n' +
          'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":4}}\n\n' +
          'event: message_stop\ndata: {"type":"message_stop"}\n\n'
      )
    );
    app.post(
      '/v1/chat/completions',
      sse(
        'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}\n\n'
      )
    );
    const call = (path: string) =>
      app.request(path, {
        method: 'POST',
        headers: { 'x-portkey-provider': 'anthropic' },
        body: '{}',
      });

    await (await call('/v1/messages')).text();
    await (await call('/v1/chat/completions')).text();
    await new Promise((r) => setTimeout(r, 0));

    const text = await (await app.request('/metrics')).text();
    expect(text).toMatch(
      /ps_gateway_streams_total\{route="\/v1\/messages",provider="anthropic",outcome="complete"\} \d+/
    );
    expect(text).toMatch(
      /ps_gateway_streams_total\{route="\/v1\/chat\/completions",provider="anthropic",outcome="truncated"\} \d+/
    );
    expect(text).toMatch(
      /ps_gateway_tokens_total\{provider="anthropic",model="claude-s",direction="output",business_unit="none"\} \d+/
    );
  });
});

describe('registry cardinality guard', () => {
  test('folds label sets past maxSeries into one overflow series', () => {
    const c = new Counter('t_total', 'test', ['k'], 2);
    c.inc({ k: 'a' });
    c.inc({ k: 'b' });
    c.inc({ k: 'c' });
    c.inc({ k: 'd' });
    const text = c.render();
    expect(text).toContain('t_total{k="a"} 1');
    expect(text).toContain('t_total{k="__overflow__"} 2');
    expect(text).not.toContain('k="d"');
  });
});
