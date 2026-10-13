import { Hono } from 'hono';
import {
  ADMIN_TOKEN_ENV,
  adminAuthLoginHandler,
  adminAuthMiddleware,
  adminAuthSessionStatusHandler,
} from '.';

const TOKEN = 'a-sufficiently-long-admin-token';

function makeApp() {
  const app = new Hono();
  app.post('/public/auth', adminAuthLoginHandler);
  app.get('/public/auth/session', adminAuthSessionStatusHandler);
  app.get('/log/stream', adminAuthMiddleware, (c) => c.text('streaming'));
  return app;
}

describe('adminAuth', () => {
  afterEach(() => {
    delete process.env[ADMIN_TOKEN_ENV];
  });

  test('log stream is disabled when no admin token is configured', async () => {
    const res = await makeApp().request('/log/stream');
    expect(res.status).toBe(500);
    expect(await res.text()).not.toContain('streaming');
  });

  test('a token shorter than 16 characters counts as not configured', async () => {
    process.env[ADMIN_TOKEN_ENV] = 'short';
    const res = await makeApp().request('/log/stream', {
      headers: { authorization: 'Bearer short' },
    });
    expect(res.status).toBe(500);
  });

  test('log stream returns 401 without credentials', async () => {
    process.env[ADMIN_TOKEN_ENV] = TOKEN;
    const res = await makeApp().request('/log/stream');
    expect(res.status).toBe(401);
  });

  test('log stream returns 401 for a wrong bearer token', async () => {
    process.env[ADMIN_TOKEN_ENV] = TOKEN;
    const res = await makeApp().request('/log/stream', {
      headers: { authorization: `Bearer ${TOKEN}x` },
    });
    expect(res.status).toBe(401);
  });

  test('log stream accepts the configured bearer token', async () => {
    process.env[ADMIN_TOKEN_ENV] = TOKEN;
    const res = await makeApp().request('/log/stream', {
      headers: { authorization: `Bearer ${TOKEN}` },
    });
    expect(res.status).toBe(200);
  });

  test('login issues an HttpOnly session cookie that unlocks the stream', async () => {
    process.env[ADMIN_TOKEN_ENV] = TOKEN;
    const app = makeApp();

    const bad = await app.request('/public/auth', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ admin_token: 'wrong' }),
    });
    expect(bad.status).toBe(401);

    const login = await app.request('/public/auth', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ admin_token: TOKEN }),
    });
    expect(login.status).toBe(200);
    const cookie = login.headers.get('set-cookie') || '';
    expect(cookie).toContain('HttpOnly');
    expect(cookie).toContain('SameSite=Strict');

    const session = cookie.split(';')[0];
    const res = await app.request('/log/stream', {
      headers: { cookie: session },
    });
    expect(res.status).toBe(200);
  });

  test('a malformed session cookie is a 401, not a 500', async () => {
    process.env[ADMIN_TOKEN_ENV] = TOKEN;
    const res = await makeApp().request('/log/stream', {
      headers: { cookie: 'portkey_admin_session=%E0%A4%A' },
    });
    expect(res.status).toBe(401);
  });
});
