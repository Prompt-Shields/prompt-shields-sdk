import {
  DEFAULT_MAX_CUSTOM_HOST_RESPONSE_BYTES,
  limitResponseSize,
  maxCustomHostResponseBytes,
} from './responseSizeLimit';

describe('limitResponseSize', () => {
  test('passes small responses through unchanged', async () => {
    const res = limitResponseSize(
      new Response('{"ok":true}', { status: 201 }),
      1024
    );
    expect(res.status).toBe(201);
    expect(await res.json()).toEqual({ ok: true });
  });

  test('rejects a declared content-length over the limit with 502', async () => {
    const res = limitResponseSize(
      new Response('x', { headers: { 'content-length': '2048' } }),
      1024
    );
    expect(res.status).toBe(502);
    expect(((await res.json()) as any).message).toContain('exceeds');
  });

  test('errors a streamed body once it passes the limit', async () => {
    const chunk = new Uint8Array(600);
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(chunk);
        controller.enqueue(chunk);
        controller.close();
      },
    });
    const res = limitResponseSize(new Response(body), 1024);
    await expect(res.text()).rejects.toThrow();
  });

  test('limit comes from env with a safe default', () => {
    expect(maxCustomHostResponseBytes('1000')).toBe(1000);
    expect(maxCustomHostResponseBytes(undefined)).toBe(
      DEFAULT_MAX_CUSTOM_HOST_RESPONSE_BYTES
    );
    expect(maxCustomHostResponseBytes('-5')).toBe(
      DEFAULT_MAX_CUSTOM_HOST_RESPONSE_BYTES
    );
  });
});
