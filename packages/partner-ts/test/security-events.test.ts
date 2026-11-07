/**
 * The gateway security events read API (client.securityEvents), against a
 * mocked fetch.
 */
import { PartnerClient } from '../src';
import { json, mockFetch, noSleep } from './helpers';

const client = (fetch: ReturnType<typeof mockFetch>['fetch']) =>
  new PartnerClient({ apiKey: 'k', fetch, sleep: noSleep });

const query = (url: string) => Object.fromEntries(new URL(url).searchParams);

const row = (n: number) => ({
  id: `019e1a2b-0000-7000-8000-00000000000${n}`,
  received_at: '2026-11-06T12:00:01Z',
  event: {
    '@timestamp': '2026-11-06T12:00:00Z',
    'event.id': `e-${n}`,
    'event.name': 'injection.detected',
    severity: 'high',
    action: 'blocked',
    'ps.guardrail.check_id': 'azure.shieldPrompt',
    'http.response.status_code': 446,
  },
});

describe('client.securityEvents', () => {
  it('lists a page with filters and keeps the event fields as sent', async () => {
    const m = mockFetch(json({ data: [row(1)], meta: { has_more: false, next_cursor: 'abc' } }));
    const page = await client(m.fetch).securityEvents.listPage({
      name: 'injection.detected',
      severity: 'high',
      since: new Date('2026-11-01T00:00:00Z'),
      limit: 50,
    });
    expect(new URL(m.calls[0].url).pathname).toBe('/partner/v1/security-events');
    expect(query(m.calls[0].url)).toEqual({
      name: 'injection.detected',
      severity: 'high',
      since: '2026-11-01T00:00:00.000Z',
      limit: '50',
    });
    const [ev] = page.data;
    expect(ev.receivedAt).toBe('2026-11-06T12:00:01Z');
    expect(ev.event['event.name']).toBe('injection.detected');
    expect(ev.event['ps.guardrail.check_id']).toBe('azure.shieldPrompt');
    expect(ev.event['http.response.status_code']).toBe(446);
    expect(page.meta).toEqual({ hasMore: false, nextCursor: 'abc' });
  });

  it('iterates every page by cursor', async () => {
    const m = mockFetch(
      json({ data: [row(1), row(2)], meta: { has_more: true, next_cursor: 'c2' } }),
      json({ data: [row(3)], meta: { has_more: false, next_cursor: 'c3' } }),
    );
    const all = await client(m.fetch).securityEvents.list({ limit: 2 });
    expect(all.map((r) => r.event['event.id'])).toEqual(['e-1', 'e-2', 'e-3']);
    expect(query(m.calls[1].url)).toEqual({ limit: '2', cursor: 'c2' });
  });
});
