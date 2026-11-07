/**
 * The audit trail read API (client.audit), against a mocked fetch.
 */
import { PartnerClient } from '../src';
import { json, mockFetch, noSleep } from './helpers';

const client = (fetch: ReturnType<typeof mockFetch>['fetch']) =>
  new PartnerClient({ apiKey: 'k', fetch, sleep: noSleep });

const query = (url: string) => Object.fromEntries(new URL(url).searchParams);

const row = (seq: number) => ({
  schema_version: '1.0',
  id: `019e1a2b-0000-7000-8000-00000000000${seq}`,
  tenant_id: '019e1a2b-0000-7000-8000-0000000000aa',
  seq,
  created_at: '2026-11-06T10:00:00.000000Z',
  action: 'user.erase',
  outcome: 'success',
  actor: { type: 'tenant_key', id: 'abc123' },
  target: { type: 'user', id: 'f'.repeat(64) },
  request_id: null,
  status_code: 200,
  details: { input: 'user_id', rows_erased: { ai_usage_events: 3, call_feedback: 1 } },
  prev_hash: '0'.repeat(64),
  row_hash: '1'.repeat(64),
  hash_version: 1,
});

describe('client.audit', () => {
  it('lists a page with filters and keeps details as stored', async () => {
    const m = mockFetch(json({ data: [row(1)], meta: { has_more: false, next_cursor: '1' } }));
    const page = await client(m.fetch).audit.listPage({
      action: 'user.erase',
      since: new Date('2026-11-01T00:00:00Z'),
      limit: 50,
    });
    expect(m.calls[0].url).toContain('/partner/v1/audit?');
    expect(query(m.calls[0].url)).toEqual({
      action: 'user.erase',
      since: '2026-11-01T00:00:00.000Z',
      limit: '50',
    });
    const [event] = page.data;
    expect(event.rowHash).toBe('1'.repeat(64));
    expect(event.target).toEqual({ type: 'user', id: 'f'.repeat(64) });
    // Chained as stored: snake_case keys kept, so the hash can be recomputed.
    expect(event.details).toEqual({ input: 'user_id', rows_erased: { ai_usage_events: 3, call_feedback: 1 } });
    expect(page.meta).toEqual({ hasMore: false, nextCursor: '1' });
  });

  it('iterates every page by cursor', async () => {
    const m = mockFetch(
      json({ data: [row(1), row(2)], meta: { has_more: true, next_cursor: '2' } }),
      json({ data: [row(3)], meta: { has_more: false, next_cursor: '3' } }),
    );
    const all = await client(m.fetch).audit.list({ limit: 2 });
    expect(all.map((r) => r.seq)).toEqual([1, 2, 3]);
    expect(query(m.calls[1].url)).toEqual({ limit: '2', cursor: '2' });
  });
});
