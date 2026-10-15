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
    delete process.env.MAX_FILE_PAYLOAD_SIZE_IN_MB;
  });

  describe('MAX_FILE_PAYLOAD_SIZE_IN_MB (multipart uploads)', () => {
    const multipart = 'multipart/form-data; boundary=x';

    test('rejects an upload over the limit with 413', async () => {
      process.env.MAX_FILE_PAYLOAD_SIZE_IN_MB = '25';
      const res = await post({
        'content-type': multipart,
        'content-length': String(26 * 1048576),
      });
      expect(res.status).toBe(413);
      expect(((await res.json()) as any).message).toContain('25MB');
    });

    test('allows an upload within the limit', async () => {
      process.env.MAX_FILE_PAYLOAD_SIZE_IN_MB = '25';
      const res = await post({
        'content-type': multipart,
        'content-length': String(25 * 1048576),
      });
      expect(res.status).toBe(200);
    });

    test.each([[undefined], [''], ['abc'], ['-1']])(
      'requires a valid content-length (%p) with 411',
      async (length) => {
        process.env.MAX_FILE_PAYLOAD_SIZE_IN_MB = '25';
        const headers: Record<string, string> = { 'content-type': multipart };
        if (length !== undefined) headers['content-length'] = length;
        const res = await post(headers);
        expect(res.status).toBe(411);
      }
    );

    test('also limits urlencoded forms, which formData() parses too', async () => {
      process.env.MAX_FILE_PAYLOAD_SIZE_IN_MB = '1';
      const res = await post({
        'content-type': 'application/x-www-form-urlencoded',
        'content-length': String(2 * 1048576),
      });
      expect(res.status).toBe(413);
    });

    test('does not apply to JSON bodies', async () => {
      process.env.MAX_FILE_PAYLOAD_SIZE_IN_MB = '1';
      const res = await post({
        'content-type': 'application/json',
        'content-length': String(5 * 1048576),
      });
      expect(res.status).toBe(200);
    });

    test('no limit and no content-length check when unset', async () => {
      const res = await post({ 'content-type': multipart });
      expect(res.status).toBe(200);
    });
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
