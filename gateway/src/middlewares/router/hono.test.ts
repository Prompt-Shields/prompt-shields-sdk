import { Hono } from 'hono';
import { psRouter } from './hono';

/** A tiny app: the router middleware, then a handler that echoes the body it sees. */
function makeApp() {
  const app = new Hono();
  app.use('/v1/chat/completions', psRouter());
  app.post('/v1/chat/completions', async (c) => {
    const body = await c.req.json();
    return c.json({ model: body.model, route: c.get('psRoute') ?? null });
  });
  app.get('/', (c) => c.text('ok'));
  return app;
}

const post = (app: Hono, body: any, headers: Record<string, string> = {}) =>
  app.request('/v1/chat/completions', {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify(body),
  });

describe('psRouter (Hono adapter)', () => {
  test('rewrites model="auto" and the handler sees the concrete model', async () => {
    const app = makeApp();
    const res = await post(app, {
      model: 'auto',
      messages: [{ role: 'user', content: 'hi' }],
    });
    const json: any = await res.json();
    expect(json.model).not.toBe('auto');
    expect(typeof json.model).toBe('string');
    expect(json.route.requestedModel).toBe('auto');
  });

  test('X-PS-Route override reaches the handler', async () => {
    const app = makeApp();
    const res = await post(
      app,
      { model: 'gpt-4o', messages: [{ role: 'user', content: 'hi' }] },
      { 'x-ps-route': 'cheap' }
    );
    const json: any = await res.json();
    expect(json.route.group).toBe('cheap');
  });

  test('a concrete model with no hint is untouched', async () => {
    const app = makeApp();
    const res = await post(app, {
      model: 'gpt-4o',
      messages: [{ role: 'user', content: 'hi' }],
    });
    const json: any = await res.json();
    expect(json.model).toBe('gpt-4o');
    expect(json.route).toBeNull();
  });

  test('non-JSON / GET requests pass through without error', async () => {
    const app = makeApp();
    const res = await app.request('/');
    expect(res.status).toBe(200);
  });
});
