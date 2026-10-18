import { NotFoundError, PartnerClient, PartnerError } from '../src';
import type { AIAsset, ChangeEvent } from '../src';
import { ASSET_WIRE, asset, json, mockFetch } from './helpers';

function pageOf(items: unknown[], nextCursor: string | null, extraMeta: Record<string, unknown> = {}) {
  return json({
    data: items,
    meta: { total: 99, has_more: nextCursor !== null, next_cursor: nextCursor, ...extraMeta },
  });
}

const client = (fetch: ReturnType<typeof mockFetch>['fetch']) => new PartnerClient({ apiKey: 'k', fetch });

describe('assets', () => {
  it('maps camelCase params to snake_case query strings', async () => {
    const m = mockFetch(pageOf([], null));
    await client(m.fetch).assets.listPage({
      businessUnit: 'HR',
      confidence: 'high',
      dataClassification: 'confidential',
      discoverySource: 'sdk',
      since: new Date('2026-03-01T00:00:00Z'),
      limit: 200,
      vendor: undefined,
    });
    const url = new URL(m.calls[0].url);
    expect(url.pathname).toBe('/partner/v1/assets');
    expect(Object.fromEntries(url.searchParams)).toEqual({
      business_unit: 'HR',
      confidence: 'high',
      data_classification: 'confidential',
      discovery_source: 'sdk',
      since: '2026-03-01T00:00:00.000Z',
      limit: '200',
    });
  });

  it('returns camelCase objects', async () => {
    const m = mockFetch(pageOf([ASSET_WIRE], null));
    const page = await client(m.fetch).assets.listPage();
    const a: AIAsset = page.data[0];
    expect(a).toEqual({
      id: ASSET_WIRE.id,
      vendor: 'openai',
      model: 'gpt-4o',
      useCaseName: 'interview-screening',
      businessUnit: 'HR',
      ownerEmail: 'jane.doe@acme.com',
      environment: 'production',
      status: 'active',
      dataClassification: 'confidential',
      discoverySource: ['sdk', 'browser_extension'],
      confidence: 'verified',
      firstSeen: '2026-03-01T10:00:00Z',
      lastSeen: '2026-03-25T14:22:00Z',
    });
    expect(page.meta).toEqual({ total: 99, hasMore: false, nextCursor: null });
  });

  it('walks every page with for await, passing the cursor as `after`', async () => {
    const m = mockFetch(
      pageOf([asset('1'), asset('2')], '2'),
      pageOf([asset('3'), asset('4')], '4'),
      pageOf([asset('5')], null),
    );
    const ids: string[] = [];
    for await (const a of client(m.fetch).assets.list({ limit: 2, vendor: 'openai' })) ids.push(a.id);
    expect(ids).toEqual(['1', '2', '3', '4', '5']);
    const params = m.calls.map((c) => Object.fromEntries(new URL(c.url).searchParams));
    expect(params).toEqual([
      { limit: '2', vendor: 'openai' },
      { limit: '2', vendor: 'openai', after: '2' },
      { limit: '2', vendor: 'openai', after: '4' },
    ]);
  });

  it('collects all pages with await and toArray()', async () => {
    const m = mockFetch(pageOf([asset('1')], '1'), pageOf([asset('2')], null), pageOf([asset('1')], '1'), pageOf([asset('2')], null));
    const c = client(m.fetch);
    expect((await c.assets.list()).map((a) => a.id)).toEqual(['1', '2']);
    expect((await c.assets.list().toArray()).map((a) => a.id)).toEqual(['1', '2']);
  });

  it('is lazy: nothing is fetched until iterated', async () => {
    const m = mockFetch();
    client(m.fetch).assets.list();
    expect(m.calls).toHaveLength(0);
  });

  it('starts from a caller-supplied `after` cursor', async () => {
    const m = mockFetch(pageOf([asset('9')], null));
    await client(m.fetch).assets.list({ after: '8' });
    expect(new URL(m.calls[0].url).searchParams.get('after')).toBe('8');
  });

  it('stops if the server repeats a cursor', async () => {
    const m = mockFetch(pageOf([asset('1')], 'x'), pageOf([asset('2')], 'x'));
    await expect(client(m.fetch).assets.list().toArray()).rejects.toThrow(/cursor repeated/);
  });

  it('stops when has_more is true but next_cursor is null', async () => {
    const m = mockFetch(json({ data: [asset('1')], meta: { total: 5, has_more: true, next_cursor: null } }));
    expect(await client(m.fetch).assets.list()).toHaveLength(1);
  });

  it('supports the manual pagination loop from the docs', async () => {
    const m = mockFetch(pageOf([asset('1')], '1'), pageOf([asset('2')], null));
    const c = client(m.fetch);
    const seen: string[] = [];
    let page = await c.assets.listPage({ limit: 50 });
    for (;;) {
      for (const a of page.data) seen.push(a.id);
      if (!page.meta.hasMore) break;
      page = await c.assets.listPage({ limit: 50, after: page.meta.nextCursor! });
    }
    expect(seen).toEqual(['1', '2']);
  });

  it('gets one asset and URL-encodes the id', async () => {
    const m = mockFetch(json(asset('a/b')));
    const a = await client(m.fetch).assets.get('a/b');
    expect(a.id).toBe('a/b');
    expect(m.calls[0].url).toBe('https://api.promptshields.io/partner/v1/assets/a%2Fb');
  });

  it('rejects an empty asset id without a request', () => {
    const m = mockFetch();
    expect(() => client(m.fetch).assets.get('')).toThrow(PartnerError);
  });

  it('throws NotFoundError for a missing asset', async () => {
    const m = mockFetch(
      json({ detail: { type: 'about:blank', title: 'Not Found', status: 404, detail: 'Asset not found' } }, 404),
    );
    const err = await client(m.fetch).assets.get('nope').catch((e) => e);
    expect(err).toBeInstanceOf(NotFoundError);
    expect(err.detail).toBe('Asset not found');
  });

  it('returns data flows and risks as arrays', async () => {
    const m = mockFetch(
      json({
        data: [
          {
            id: 'f1',
            source_system: 'candidates_db',
            destination_system: 'hiring_dashboard',
            data_classification: 'confidential',
            direction: 'input',
            detected_pii_types: ['name', 'email'],
          },
        ],
      }),
      json({ data: [{ id: 'r1', risk_category: 'compliance', risk_level: 'high', framework: 'EU_AI_ACT' }] }),
    );
    const c = client(m.fetch);
    const flows = await c.assets.dataFlows('id1');
    const risks = await c.assets.risks('id1');
    expect(flows).toEqual([
      {
        id: 'f1',
        sourceSystem: 'candidates_db',
        destinationSystem: 'hiring_dashboard',
        dataClassification: 'confidential',
        direction: 'input',
        detectedPiiTypes: ['name', 'email'],
      },
    ]);
    expect(risks).toEqual([{ id: 'r1', riskCategory: 'compliance', riskLevel: 'high', framework: 'EU_AI_ACT' }]);
    expect(m.calls.map((x) => new URL(x.url).pathname)).toEqual([
      '/partner/v1/assets/id1/data-flows',
      '/partner/v1/assets/id1/risks',
    ]);
  });

  it('fetches a usage summary with a period', async () => {
    const m = mockFetch(
      json({
        total_requests: 15420,
        total_tokens_in: 3250000,
        total_tokens_out: 6100000,
        total_tokens_cached: 1200000,
        total_tokens_cache_write: 80000,
        total_tokens_reasoning: 900000,
        estimated_cost_usd: 482.5,
        unique_sessions: 312,
        avg_latency_ms: 380,
        period: '7d',
      }),
    );
    const summary = await client(m.fetch).assets.usageSummary('id1', { period: '7d' });
    expect(summary).toEqual({
      totalRequests: 15420,
      totalTokensIn: 3250000,
      totalTokensOut: 6100000,
      totalTokensCached: 1200000,
      totalTokensCacheWrite: 80000,
      totalTokensReasoning: 900000,
      estimatedCostUsd: 482.5,
      uniqueSessions: 312,
      avgLatencyMs: 380,
      period: '7d',
    });
    expect(m.calls[0].url).toBe('https://api.promptshields.io/partner/v1/assets/id1/usage-summary?period=7d');
  });
});

describe('taxonomy', () => {
  it('lists vendors, models and business units', async () => {
    const m = mockFetch(json({ data: ['anthropic', 'openai'] }), json({ data: ['gpt-4o'] }), json({ data: ['HR'] }));
    const c = client(m.fetch);
    expect(await c.vendors.list()).toEqual(['anthropic', 'openai']);
    expect(await c.models.list()).toEqual(['gpt-4o']);
    expect(await c.businessUnits.list()).toEqual(['HR']);
    expect(m.calls.map((x) => new URL(x.url).pathname)).toEqual([
      '/partner/v1/vendors',
      '/partner/v1/models',
      '/partner/v1/business-units',
    ]);
  });
});

describe('sync', () => {
  const change = (id: string, type: string) => ({
    change_type: type,
    asset: asset(id),
    changed_at: '2026-03-25T15:01:00Z',
    changed_fields: type === 'updated' ? ['confidence'] : null,
  });

  it('requires `since` for the changes feed', () => {
    const c = client(mockFetch().fetch);
    expect(() => c.changes.list({} as never)).toThrow(/since/);
    expect(() => c.changes.listPage({ since: '' })).toThrow(/since/);
  });

  it('iterates the changes feed and exposes the last sync token', async () => {
    const m = mockFetch(
      pageOf([change('1', 'created')], '1', { sync_token: '2026-03-25T14:00:00' }),
      pageOf([change('2', 'updated'), change('3', 'deactivated')], null, { sync_token: '2026-03-25T15:01:00' }),
    );
    const iter = client(m.fetch).changes.list({ since: '2026-03-25T10:00:00Z', limit: 1 });
    const changes: ChangeEvent[] = await iter.toArray();
    expect(changes.map((c) => [c.changeType, c.asset.id])).toEqual([
      ['created', '1'],
      ['updated', '2'],
      ['deactivated', '3'],
    ]);
    expect(changes[1].changedFields).toEqual(['confidence']);
    expect(changes[1].asset.useCaseName).toBe('interview-screening');
    expect(iter.lastPage?.meta.syncToken).toBe('2026-03-25T15:01:00');
    expect(Object.fromEntries(new URL(m.calls[1].url).searchParams)).toEqual({
      since: '2026-03-25T10:00:00Z',
      limit: '1',
      after: '1',
    });
  });

  it('exports the full registry', async () => {
    const m = mockFetch(
      json({
        data: [asset('1')],
        meta: { total: 1, exported_at: '2026-03-25T15:00:00Z', sync_token: '2026-03-25T15:00:00Z' },
      }),
    );
    const res = await client(m.fetch).export.get();
    expect(res.meta).toEqual({ total: 1, exportedAt: '2026-03-25T15:00:00Z', syncToken: '2026-03-25T15:00:00Z' });
    expect(res.data[0].businessUnit).toBe('HR');
    expect(m.calls[0].url).toBe('https://api.promptshields.io/partner/v1/export');
  });
});
