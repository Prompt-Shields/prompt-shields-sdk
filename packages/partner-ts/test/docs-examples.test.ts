/**
 * The examples from docs/sdks/typescript.mdx, run against a mocked fetch.
 * Code inside each block is copied from the docs; keep them in step.
 */
import { PartnerClient, APIError, RateLimitError } from '../src';
import type {
  AIAsset,
  DataFlow,
  RiskMapping,
  ChangeEvent,
  UsageSummary,
  Attestation,
  WebhookSubscription,
} from '../src';
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

  it('Risks, Attestations and Usage', async () => {
    const status = { state: 'attested', attested_by: 'ciso@acme.com', attested_at: null, comment: null, value_changed: false };
    const posted: unknown[] = [];
    jest.spyOn(globalThis, 'fetch').mockImplementation(async (input: unknown, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname === '/oauth/token') return token('docs.jwt.token');
      if (url.pathname.endsWith('/risks')) {
        const suggested = url.searchParams.get('status') === 'suggested';
        return json({
          data: suggested
            ? [{ id: 'r1', risk_category: 'employment', risk_level: 'high', framework: 'EU_AI_ACT', status: 'suggested', source: 'rules', framework_ref: 'Annex III, point 4(a)', evidence: { summary: 'use_case matched resume*' } }]
            : [],
        });
      }
      if (url.pathname.endsWith('/attestations') && init?.method === 'POST') {
        posted.push(JSON.parse(String(init.body)));
        return json({ data: {}, meta: { confidence: 'high', current: {} } }, 201);
      }
      if (url.pathname.endsWith('/attestations')) {
        expect(url.searchParams.get('field')).toBe('owner');
        return json({ data: [], meta: { total: 0, confidence: 'high', current: { owner: status } } });
      }
      if (url.pathname.endsWith('/usage-summary')) {
        return json({ total_requests: 1, total_tokens_in: 1000, total_tokens_out: 50, total_tokens_cached: 600, total_tokens_cache_write: 0, total_tokens_reasoning: 20, estimated_cost_usd: 0.1, unique_sessions: 1, avg_latency_ms: 10, savings_usd: null, fallback_rate: null, period: '7d' });
      }
      throw new Error(`unexpected ${url}`);
    });
    const client = new PartnerClient({ clientId: 'id', clientSecret: 'secret' });

    // ---- begin docs ----
    const assetId = '019e1a2b-3c4d-7000-8000-000000000001';

    // Confirmed risk mappings (the default), or rule-based suggestions awaiting review
    const confirmed = await client.assets.risks(assetId);
    const suggested = await client.assets.risks(assetId, { status: 'suggested' });
    for (const risk of suggested) {
      console.log(`${risk.framework} ${risk.frameworkRef}: ${risk.evidence?.summary}`);
    }

    // Attestation history, and the current state of each field
    const history = await client.assets.attestations(assetId, { field: 'owner' });
    console.log(`owner: ${history.meta.current.owner?.state}`);

    // Record an attestation (needs the attestations:write scope)
    await client.assets.attest(assetId, {
      field: 'owner',
      state: 'attested',
      attestedBy: 'ciso@acme.com',
    });

    // Usage, including prompt-cache and reasoning tokens
    const usage = await client.assets.usageSummary(assetId, { period: '7d' });
    console.log(`${usage.totalTokensIn} in (${usage.totalTokensCached} cached), ${usage.totalTokensReasoning} reasoning`);
    // ---- end docs ----

    expect(confirmed).toEqual([]);
    expect(logs).toEqual([
      'EU_AI_ACT Annex III, point 4(a): use_case matched resume*',
      'owner: attested',
      '1000 in (600 cached), 20 reasoning',
    ]);
    expect(posted).toEqual([{ field: 'owner', state: 'attested', attested_by: 'ciso@acme.com' }]);
  });

  it('Webhook Subscriptions', async () => {
    const sub = { id: 's1', partner_id: 'p1', url: 'https://ea-tool.example.com/hooks/prompt-shields', event_types: [], description: null, is_active: true, rotation_grace_ends_at: null, created_at: null, updated_at: null };
    const calls: string[] = [];
    jest.spyOn(globalThis, 'fetch').mockImplementation(async (input: unknown, init?: RequestInit) => {
      const url = new URL(String(input));
      expect(url.origin).toBe('https://collector.example.com');
      expect((init?.headers as Record<string, string>).Authorization).toBe('Bearer ps-tenant-key');
      calls.push(`${init?.method} ${url.pathname} ${init?.body ?? ''}`.trim());
      if (url.pathname.endsWith('/deliveries')) return json({ data: [{ id: 'd1', status: 'dead' }] });
      return json({ ...sub, secret: 'whsec_abc', warning: 'w' });
    });
    const saved = process.env.PS_API_KEY;
    process.env.PS_API_KEY = 'ps-tenant-key';
    try {
      // ---- begin docs ----
      const admin = new PartnerClient({ apiKey: process.env.PS_API_KEY, baseUrl: 'https://collector.example.com' });

      const sub = await admin.webhooks.subscriptions.create({
        partnerId: '<partner credential id>',
        url: 'https://ea-tool.example.com/hooks/prompt-shields',
        eventTypes: ['asset.created', 'asset.changed', 'asset.retired'],
      });
      console.log(`Give the partner ${sub.secret} over a secure channel`);

      // Pause, resume, or change the URL or events
      await admin.webhooks.subscriptions.update(sub.id, { isActive: false });
      await admin.webhooks.subscriptions.update(sub.id, { isActive: true, url: 'https://ea-tool.example.com/hooks/v2' });

      // New signing secret; the old one keeps signing for a day
      const rotated = await admin.webhooks.subscriptions.rotateSecret(sub.id, { gracePeriodSeconds: 86400 });

      // Dead letters, and sending one again
      for (const delivery of await admin.webhooks.deliveries.list({ status: 'dead' })) {
        await admin.webhooks.deliveries.redeliver(delivery.id);
      }
      // ---- end docs ----
      expect(rotated.secret).toBe('whsec_abc');
    } finally {
      if (saved === undefined) delete process.env.PS_API_KEY;
      else process.env.PS_API_KEY = saved;
    }

    expect(logs).toEqual(['Give the partner whsec_abc over a secure channel']);
    const S = '/api/v1/admin/webhooks/subscriptions';
    expect(calls).toEqual([
      `POST ${S} {"partner_id":"<partner credential id>","url":"https://ea-tool.example.com/hooks/prompt-shields","event_types":["asset.created","asset.changed","asset.retired"]}`,
      `PATCH ${S}/s1 {"is_active":false}`,
      `PATCH ${S}/s1 {"is_active":true,"url":"https://ea-tool.example.com/hooks/v2"}`,
      `POST ${S}/s1/rotate-secret {"grace_period_seconds":86400}`,
      'GET /api/v1/admin/webhooks/deliveries',
      'POST /api/v1/admin/webhooks/deliveries/d1/redeliver',
    ]);
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
    const t: [AIAsset?, DataFlow?, RiskMapping?, ChangeEvent?, UsageSummary?, Attestation?, WebhookSubscription?] = [];
    expect(t).toEqual([]);
  });
});
