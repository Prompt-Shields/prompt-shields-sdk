/**
 * A DELETE retried after a network error or 5xx treats a 404 as success
 * (the first attempt may have deleted the resource and lost its response);
 * a 404 on the first attempt, or after a 429, still throws.
 */
import { NotFoundError, PartnerClient } from '../src';
import { json, mockFetch, noSleep } from './helpers';

const client = (fetch: ReturnType<typeof mockFetch>['fetch']) =>
  new PartnerClient({ apiKey: 'k', fetch, sleep: noSleep });

const notFound = () => json({ title: 'Not Found', status: 404 }, 404);
const noContent = () => new Response(null, { status: 204 });

describe('DELETE retried into a 404', () => {
  it('still throws NotFoundError on a first-attempt 404', async () => {
    const m = mockFetch(notFound());
    await expect(client(m.fetch).providerKeyFingerprints.delete('anthropic', 'key_abc123')).rejects.toBeInstanceOf(
      NotFoundError,
    );
    expect(m.calls).toHaveLength(1);
  });

  it('resolves when a 5xx is followed by a 404 on the retry', async () => {
    const m = mockFetch(json({ title: 'Bad Gateway' }, 502), notFound());
    await expect(client(m.fetch).providerKeyFingerprints.delete('anthropic', 'key_abc123')).resolves.toBeUndefined();
    expect(m.calls.map((c) => c.method)).toEqual(['DELETE', 'DELETE']);
  });

  it('resolves when a network error is followed by a 404 on the retry', async () => {
    const m = mockFetch(new TypeError('socket hang up'), notFound());
    await expect(client(m.fetch).providerCredentials.delete('openai')).resolves.toBeUndefined();
    expect(m.calls).toHaveLength(2);
  });

  it('resolves for the generic send() DELETE and webhook subscriptions too', async () => {
    const m = mockFetch(json({}, 503), notFound(), new TypeError('reset'), notFound());
    const c = client(m.fetch);
    await expect(c.send('DELETE', '/partner/v1/whatever/1')).resolves.toBeUndefined();
    await expect(c.webhooks.subscriptions.delete('sub_1')).resolves.toBeUndefined();
    expect(m.remaining()).toBe(0);
  });

  it('still throws when the only earlier attempt was a 429 (not applied)', async () => {
    const m = mockFetch(json({ title: 'Too Many Requests' }, 429, { 'retry-after': '0' }), notFound());
    await expect(client(m.fetch).providerCredentials.delete('openai')).rejects.toBeInstanceOf(NotFoundError);
    expect(m.calls).toHaveLength(2);
  });

  it('treats a 404 as success once any earlier attempt may have applied', async () => {
    const m = mockFetch(json({}, 504), json({}, 429, { 'retry-after': '0' }), notFound());
    const c = new PartnerClient({ apiKey: 'k', fetch: m.fetch, sleep: noSleep, maxRetries: 3 });
    await expect(c.providerCredentials.delete('openai')).resolves.toBeUndefined();
  });

  it('resolves the same way when the retry itself succeeds', async () => {
    const m = mockFetch(json({}, 502), noContent());
    await expect(client(m.fetch).providerCredentials.delete('openai')).resolves.toBeUndefined();
  });

  it('does not turn a retried GET 404 into success', async () => {
    const m = mockFetch(json({}, 502), notFound());
    await expect(client(m.fetch).providerCredentials.get('openai')).rejects.toBeInstanceOf(NotFoundError);
  });

  it('does not apply when retries are off', async () => {
    const m = mockFetch(json({}, 502));
    const c = new PartnerClient({ apiKey: 'k', fetch: m.fetch, sleep: noSleep, maxRetries: 0 });
    await expect(c.providerCredentials.delete('openai')).rejects.toMatchObject({ status: 502 });
  });
});
