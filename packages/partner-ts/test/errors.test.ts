import {
  APIConnectionError,
  APIError,
  APITimeoutError,
  BadRequestError,
  InternalServerError,
  PartnerClient,
  PartnerError,
  PermissionDeniedError,
  RateLimitError,
} from '../src';
import { extractProblem, parseRetryAfter } from '../src/errors';
import type { FetchLike } from '../src';
import { json, mockFetch } from './helpers';

const tooMany = (retryAfter?: string) =>
  json(
    { detail: { type: 'about:blank', title: 'Too Many Requests', status: 429, detail: 'Rate limit exceeded. Retry after 23 seconds.' } },
    429,
    retryAfter === undefined ? {} : { 'Retry-After': retryAfter },
  );
const ok = () => json({ data: ['openai'] });

function setup(queue: Parameters<typeof mockFetch>, opts: Partial<ConstructorParameters<typeof PartnerClient>[0]> = {}) {
  const m = mockFetch(...queue);
  const sleeps: number[] = [];
  const client = new PartnerClient({
    apiKey: 'k',
    fetch: m.fetch,
    sleep: async (ms) => {
      sleeps.push(ms);
    },
    ...opts,
  });
  return { m, sleeps, client };
}

describe('rate limits', () => {
  it('waits out Retry-After and retries', async () => {
    const { m, sleeps, client } = setup([tooMany('23'), ok()]);
    expect(await client.vendors.list()).toEqual(['openai']);
    expect(sleeps).toEqual([23_000]);
    expect(m.calls).toHaveLength(2);
  });

  it('throws RateLimitError with retryAfter once retries run out', async () => {
    const { m, sleeps, client } = setup([tooMany('2'), tooMany('2'), tooMany('2')]);
    const err = await client.vendors.list().catch((e) => e);
    expect(err).toBeInstanceOf(RateLimitError);
    expect(err).toBeInstanceOf(APIError);
    expect(err.status).toBe(429);
    expect(err.retryAfter).toBe(2);
    expect(err.title).toBe('Too Many Requests');
    expect(err.detail).toBe('Rate limit exceeded. Retry after 23 seconds.');
    expect(err.headers['retry-after']).toBe('2');
    expect(sleeps).toEqual([2000, 2000]);
    expect(m.calls).toHaveLength(3);
  });

  it('does not wait longer than maxRetryAfterSeconds', async () => {
    const { m, sleeps, client } = setup([tooMany('600')], { maxRetryAfterSeconds: 60 });
    const err = await client.vendors.list().catch((e) => e);
    expect(err).toBeInstanceOf(RateLimitError);
    expect(err.retryAfter).toBe(600);
    expect(sleeps).toEqual([]);
    expect(m.calls).toHaveLength(1);
  });

  it('falls back to backoff when Retry-After is missing', async () => {
    const { sleeps, client } = setup([tooMany(), ok()]);
    await client.vendors.list();
    expect(sleeps).toHaveLength(1);
    expect(sleeps[0]).toBeGreaterThanOrEqual(500);
    expect(sleeps[0]).toBeLessThanOrEqual(625);
  });

  it('honours maxRetries: 0', async () => {
    const { m, client } = setup([tooMany('1')], { maxRetries: 0 });
    await expect(client.vendors.list()).rejects.toBeInstanceOf(RateLimitError);
    expect(m.calls).toHaveLength(1);
  });

  it('parses Retry-After as seconds or an HTTP date', () => {
    const now = Date.parse('2026-03-25T10:00:00Z');
    expect(parseRetryAfter('23')).toBe(23);
    expect(parseRetryAfter('Wed, 25 Mar 2026 10:00:30 GMT', now)).toBe(30);
    expect(parseRetryAfter('garbage')).toBeNull();
    expect(parseRetryAfter(null)).toBeNull();
  });
});

describe('errors', () => {
  it('retries 503 then succeeds', async () => {
    const { m, client } = setup([json({ detail: 'down' }, 503), ok()]);
    expect(await client.vendors.list()).toEqual(['openai']);
    expect(m.calls).toHaveLength(2);
  });

  it('throws InternalServerError for 500 without retrying', async () => {
    const { m, client } = setup([new Response('Internal Server Error', { status: 500 })]);
    const err = await client.vendors.list().catch((e) => e);
    expect(err).toBeInstanceOf(InternalServerError);
    expect(err.detail).toBe('Internal Server Error');
    expect(m.calls).toHaveLength(1);
  });

  it('maps 403 to PermissionDeniedError', async () => {
    const { client } = setup([
      json({ detail: { type: 'about:blank', title: 'Forbidden', status: 403, detail: 'Token lacks required scope: registry:read' } }, 403),
    ]);
    const err = await client.vendors.list().catch((e) => e);
    expect(err).toBeInstanceOf(PermissionDeniedError);
    expect(err.message).toBe('403 Forbidden: Token lacks required scope: registry:read');
  });

  it('maps FastAPI validation errors to BadRequestError', async () => {
    const { client } = setup([
      json({ detail: [{ loc: ['query', 'limit'], msg: 'Input should be less than or equal to 200', type: 'less_than_equal' }] }, 422),
    ]);
    const err = await client.assets.listPage({ limit: 500 }).catch((e) => e);
    expect(err).toBeInstanceOf(BadRequestError);
    expect(err.status).toBe(422);
    expect(err.detail).toBe('query.limit: Input should be less than or equal to 200');
  });

  it('reads top-level RFC 7807 bodies as documented', () => {
    expect(extractProblem({ type: 'about:blank', title: 'Not Found', status: 404, detail: 'gone' }, 404)).toEqual({
      type: 'about:blank',
      title: 'Not Found',
      detail: 'gone',
    });
    expect(extractProblem(undefined, 418)).toEqual({ title: 'HTTP 418', detail: 'HTTP 418' });
  });

  it('wraps network failures in APIConnectionError after retries', async () => {
    const { m, client } = setup([new TypeError('fetch failed'), new TypeError('fetch failed'), new TypeError('fetch failed')]);
    const err = await client.vendors.list().catch((e) => e);
    expect(err).toBeInstanceOf(APIConnectionError);
    expect(err.cause).toBeInstanceOf(TypeError);
    expect(m.calls).toHaveLength(3);
  });

  it('times out slow requests with APITimeoutError', async () => {
    const hang: FetchLike = (_url, init) =>
      new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new Error('aborted')));
      });
    const client = new PartnerClient({ apiKey: 'k', fetch: hang, timeoutMs: 10, maxRetries: 0 });
    const err = await client.vendors.list().catch((e) => e);
    expect(err).toBeInstanceOf(APITimeoutError);
    expect(err).toBeInstanceOf(APIConnectionError);
  });

  it('rejects a non-JSON success body', async () => {
    const { client } = setup([new Response('<html>', { status: 200 })]);
    await expect(client.vendors.list()).rejects.toBeInstanceOf(PartnerError);
  });
});
