/**
 * The examples from docs/sdks/typescript.mdx, run against a mocked fetch.
 * Code inside each block is copied from the docs; keep them in step.
 */
import { PartnerClient, APIError, RateLimitError } from '../src';
import type { AIAsset, DataFlow, RiskMapping, ChangeEvent, UsageSummary } from '../src';
import { asset, json, token } from './helpers';

type Handler = (url: URL) => Response;

function routeFetch(handler: Handler) {
  return jest.spyOn(globalThis, 'fetch').mockImplementation(async (input: unknown) => {
    const url = new URL(String(input));
    if (url.pathname === '/oauth/token') return token('docs.jwt.token');
    return handler(url);
  });
}

const onePage = (data: unknown[], meta: Record<string, unknown> = {}) =>
  json({ data, meta: { total: data.length, has_more: false, next_cursor: null, ...meta } });

describe('docs/sdks/typescript.mdx', () => {
  let logs: string[];
  beforeEach(() => {
    logs = [];
    jest.spyOn(console, 'log').mockImplementation((...args) => {
      logs.push(args.join(' '));
    });
  });
  afterEach(() => jest.restoreAllMocks());

  it('Quick Start', async () => {
    routeFetch((url) => {
      if (url.pathname === '/partner/v1/assets') {
        expect(url.searchParams.get('business_unit')).toBe('HR');
        expect(url.searchParams.get('confidence')).toBe('high');
        return onePage([asset('1')]);
      }
      if (url.pathname === '/partner/v1/changes') {
        expect(url.searchParams.get('since')).toBe('2026-03-25T10:00:00Z');
        return onePage([{ change_type: 'created', asset: asset('2', { use_case_name: 'contract-review' }), changed_at: null, changed_fields: null }]);
      }
      throw new Error(`unexpected ${url}`);
    });

    // ---- begin docs ----
    const client = new PartnerClient({
      clientId: 'ps-partner-ardoq-abc123',
      clientSecret: 'sk-partner-secret-xyz789',
      // Or use API key:
      // apiKey: 'ps-pk-your-key-here',
    });

    // List all assets
    const assets = await client.assets.list({ businessUnit: 'HR', confidence: 'high' });
    for (const asset of assets) {
      console.log(`${asset.vendor}/${asset.model} - ${asset.useCaseName}`);
    }

    // Get changes since last sync
    const changes = await client.changes.list({ since: '2026-03-25T10:00:00Z' });
    for (const change of changes) {
      console.log(`${change.changeType}: ${change.asset.useCaseName}`);
    }
    // ---- end docs ----

    expect(logs).toEqual(['openai/gpt-4o - interview-screening', 'created: contract-review']);
  });

  it('Pagination', async () => {
    let calls = 0;
    routeFetch(() => {
      calls++;
      return calls % 2 === 1
        ? json({ data: [asset('1')], meta: { total: 2, has_more: true, next_cursor: '1' } })
        : onePage([asset('2')]);
    });
    const client = new PartnerClient({ apiKey: 'ps-pk-your-key-here' });
    const seen: string[] = [];
    const process = (a: AIAsset) => seen.push(a.id);

    // ---- begin docs ----
    // Auto-pagination (async iterator)
    for await (const asset of client.assets.list({ limit: 200 })) {
      process(asset);
    }

    // Collect all
    const allAssets = await client.assets.list({ limit: 200 }).toArray();

    // Manual pagination
    let page = await client.assets.listPage({ limit: 50 });
    while (true) {
      for (const asset of page.data) {
        process(asset);
      }
      if (!page.meta.hasMore) break;
      page = await client.assets.listPage({ limit: 50, after: page.meta.nextCursor });
    }
    // ---- end docs ----

    expect(allAssets.map((a) => a.id)).toEqual(['1', '2']);
    expect(seen).toEqual(['1', '2', '1', '2']);
  });

  it('Full Sync Example', async () => {
    routeFetch((url) => {
      if (url.pathname === '/partner/v1/export') {
        return json({ data: [asset('1')], meta: { total: 1, exported_at: '2026-03-25T15:00:00Z', sync_token: '2026-03-25T15:00:00Z' } });
      }
      expect(url.searchParams.get('since')).toBe('2026-03-25T15:00:00Z');
      return onePage([
        { change_type: 'created', asset: asset('2'), changed_at: null, changed_fields: null },
        { change_type: 'updated', asset: asset('1'), changed_at: null, changed_fields: ['confidence'] },
        { change_type: 'deactivated', asset: asset('3'), changed_at: null, changed_fields: null },
      ]);
    });
    const actions: string[] = [];
    const createInEATool = async (a: AIAsset) => void actions.push(`create ${a.id}`);
    const updateInEATool = async (a: AIAsset, f: string[] | null) => void actions.push(`update ${a.id} ${f}`);
    const deactivateInEATool = async (a: AIAsset) => void actions.push(`deactivate ${a.id}`);

    // ---- begin docs ----
    const client = new PartnerClient({ apiKey: 'ps-pk-your-key-here' });

    // Initial full export
    const exportResult = await client.export.get();
    let syncToken = exportResult.meta.syncToken;
    console.log(`Exported ${exportResult.meta.total} assets`);

    // Delta sync (next run)
    const changes = await client.changes.list({ since: syncToken }).toArray();
    for (const change of changes) {
      switch (change.changeType) {
        case 'created':
          await createInEATool(change.asset);
          break;
        case 'updated':
          await updateInEATool(change.asset, change.changedFields);
          break;
        case 'deactivated':
          await deactivateInEATool(change.asset);
          break;
      }
    }
    // ---- end docs ----

    expect(logs).toEqual(['Exported 1 assets']);
    expect(actions).toEqual(['create 2', 'update 1 confidence', 'deactivate 3']);
  });

  it('Error Handling', async () => {
    routeFetch(() =>
      json(
        { detail: { type: 'about:blank', title: 'Too Many Requests', status: 429, detail: 'Rate limit exceeded. Retry after 600 seconds.' } },
        429,
        { 'Retry-After': '600' },
      ),
    );

    // ---- begin docs ----
    const client = new PartnerClient({ apiKey: 'ps-pk-your-key-here' });

    try {
      const asset = await client.assets.get('019e1a2b-...');
    } catch (error) {
      if (error instanceof RateLimitError) {
        console.log(`Rate limited. Retry after ${error.retryAfter} seconds`);
      } else if (error instanceof APIError) {
        console.log(`API error ${error.status}: ${error.title} - ${error.detail}`);
      }
    }
    // ---- end docs ----

    expect(logs).toEqual(['Rate limited. Retry after 600 seconds']);
  });

  it('exports the documented types', () => {
    const t: [AIAsset?, DataFlow?, RiskMapping?, ChangeEvent?, UsageSummary?] = [];
    expect(t).toEqual([]);
  });
});
