import { Hono } from 'hono';
import { payloadSizeValidatorMiddleware } from './payloadSizeValidator';

function makeApp() {
  const app = new Hono();
  app.post('*', payloadSizeValidatorMiddleware());
  app.post('/v1/chat/completions', (c) => c.text('ok'));
  return app;
}

const post = (headers: Record<string, string>) =>
  makeApp().request('/v1/chat/completions', {
    method: 'POST',
    headers,
    body: '{}',
  });

describe('payloadSizeValidatorMiddleware', () => {
  afterEach(() => {
    delete process.env.MAX_JSON_PAYLOAD_SIZE_IN_MB;
  });

  test('no limit when MAX_JSON_PAYLOAD_SIZE_IN_MB is unset', async () => {
    const res = await post({
      'content-type': 'application/json',
      'content-length': String(500 * 1048576),
    });
    expect(res.status).toBe(200);
  });

  test('rejects a JSON body over the limit with 413', async () => {
    process.env.MAX_JSON_PAYLOAD_SIZE_IN_MB = '1';
    const res = await post({
      'content-type': 'application/json; charset=utf-8',
      'content-length': String(2 * 1048576),
    });
    expect(res.status).toBe(413);
  });

  test('allows a JSON body within the limit', async () => {
    process.env.MAX_JSON_PAYLOAD_SIZE_IN_MB = '1';
    const res = await post({
      'content-type': 'application/json',
      'content-length': '1024',
    });
    expect(res.status).toBe(200);
  });

  test('does not apply to non-JSON bodies such as file uploads', async () => {
    process.env.MAX_JSON_PAYLOAD_SIZE_IN_MB = '1';
    const res = await post({
      'content-type': 'multipart/form-data; boundary=x',
      'content-length': String(5 * 1048576),
    });
    expect(res.status).toBe(200);
  });
});
