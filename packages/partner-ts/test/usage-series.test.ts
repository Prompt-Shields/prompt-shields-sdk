/** client.usage.series and client.usage.seriesExport, against a mocked fetch. */
import { PartnerClient, PartnerError, UsageSeriesExport } from '../src';
import type { UsageSeries } from '../src';
import { json, mockFetch, noSleep } from './helpers';

const client = (fetch: ReturnType<typeof mockFetch>['fetch']) =>
  new PartnerClient({ apiKey: 'k', fetch, sleep: noSleep });

const POINT_WIRE = {
  bucket_start: '2026-10-01T00:00:00Z',
  key: 'h-3f2a',
  name: null,
  requests: 120,
  tokens_in: 48000,
  tokens_out: 9000,
  tokens_cached: 12000,
  tokens_cache_write: 0,
  tokens_reasoning: 0,
  cost_usd: 0.42,
  cache_saved_usd: null,
  cache_hit_rate: null,
  error_rate: 0.0083,
};

const POINT = {
  bucketStart: '2026-10-01T00:00:00Z',
  key: 'h-3f2a',
  name: null,
  requests: 120,
  tokensIn: 48000,
  tokensOut: 9000,
  tokensCached: 12000,
  tokensCacheWrite: 0,
  tokensReasoning: 0,
  costUsd: 0.42,
  cacheSavedUsd: null,
  cacheHitRate: null,
  errorRate: 0.0083,
};

describe('client.usage.series', () => {
  it('sends the query and camelCases the page', async () => {
    const m = mockFetch(
      json({
        data: [POINT_WIRE],
        meta: {
          group_by: 'user',
          granularity: 'day',
          from: '2026-10-01T00:00:00Z',
          to: '2026-10-02T00:00:00Z',
          rolled_up_at: '2026-10-02T10:00:00Z',
          has_more: true,
          next_cursor: 'bzoxMDAw',
        },
      }),
    );
    const page = await client(m.fetch).usage.series({
      groupBy: 'user',
      granularity: 'day',
      from: new Date('2026-10-01T00:00:00Z'),
      to: '2026-10-02',
      limit: 1000,
      cursor: 'abc',
    });
    const url = new URL(m.calls[0].url);
    expect(url.pathname).toBe('/partner/v1/usage/series');
    expect(Object.fromEntries(url.searchParams)).toEqual({
      group_by: 'user',
      granularity: 'day',
      from: '2026-10-01T00:00:00.000Z',
      to: '2026-10-02',
      limit: '1000',
      cursor: 'abc',
    });
    const expected: UsageSeries = {
      data: [POINT],
      meta: {
        groupBy: 'user',
        granularity: 'day',
        from: '2026-10-01T00:00:00Z',
        to: '2026-10-02T00:00:00Z',
        rolledUpAt: '2026-10-02T10:00:00Z',
        hasMore: true,
        nextCursor: 'bzoxMDAw',
      },
    };
    expect(page).toEqual(expected);
  });
});

describe('client.usage.seriesExport', () => {
  it('streams JSON Lines and parses camelCased points', async () => {
    const body = `${JSON.stringify(POINT_WIRE)}\n${JSON.stringify({ ...POINT_WIRE, key: null })}\n`;
    const m = mockFetch(
      new Response(body, {
        status: 200,
        headers: {
          'content-type': 'application/x-ndjson',
          'content-disposition': 'attachment; filename="usage-day-user-x.jsonl"',
        },
      }),
    );
    const exp = await client(m.fetch).usage.seriesExport({ format: 'jsonl', groupBy: 'user' });
    expect(new URL(m.calls[0].url).pathname).toBe('/partner/v1/usage/series/export');
    expect(m.calls[0].headers.Accept).toBe('application/x-ndjson');
    expect(exp).toBeInstanceOf(UsageSeriesExport);
    expect(exp.filename).toBe('usage-day-user-x.jsonl');
    const rows = [];
    for await (const row of exp.rows()) rows.push(row);
    expect(rows).toEqual([POINT, { ...POINT, key: null }]);
  });

  it('defaults to CSV, whose rows() throws', async () => {
    const m = mockFetch(new Response('bucket_start,key\n', { status: 200, headers: { 'content-type': 'text/csv' } }));
    const exp = await client(m.fetch).usage.seriesExport();
    expect(exp.format).toBe('csv');
    expect(m.calls[0].headers.Accept).toBe('text/csv');
    await expect(exp.rows().next()).rejects.toBeInstanceOf(PartnerError);
  });
});

describe('client.usage.showback', () => {
  it('sends the query and camelCases the report', async () => {
    const amounts = {
      cost_usd: '1.003999',
      cost_cents: 100,
      requests: 5,
      cost_requests: 4,
      tokens_in: 10,
      tokens_out: 2,
    };
    const m = mockFetch(
      json({
        data: [{ key: 't-1', name: 'payments', team_id: null, team_name: null, ...amounts }],
        unallocated: { ...amounts, cost_cents: 0 },
        total: amounts,
        meta: {
          group_by: 'team',
          period: '2026-11',
          from: '2026-11-01T00:00:00Z',
          to: '2026-12-01T00:00:00Z',
          currency: 'USD',
          rolled_up_at: null,
          partial: false,
        },
      }),
    );
    const report = await client(m.fetch).usage.showback({ groupBy: 'tag:env', period: '2026-11' });
    const url = new URL(m.calls[0].url);
    expect(url.pathname).toBe('/partner/v1/cost/showback');
    expect(Object.fromEntries(url.searchParams)).toEqual({ group_by: 'tag:env', period: '2026-11' });
    expect(report.data[0]).toMatchObject({ key: 't-1', name: 'payments', teamId: null, costCents: 100 });
    expect(report.unallocated.costCents).toBe(0);
    expect(report.meta.rolledUpAt).toBeNull();
  });
});
