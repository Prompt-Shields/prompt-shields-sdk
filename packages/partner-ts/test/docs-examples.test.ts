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
  SpendAlert,
  FocusCostRow,
  McpTool,
  LearnerRecommendation,
  SiemRecord,
} from '../src';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
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

  it('Reviewing Risk Suggestions', async () => {
    const mappingWire = { id: 'r1', risk_category: 'compliance', risk_level: 'high', framework: 'EU_AI_ACT', status: 'confirmed', source: 'rules', framework_ref: 'Annex III, point 4(a)' };
    const reviewWire = { id: 'rev1', risk_mapping_id: 'r1', asset_id: 'a1', status: 'confirmed', previous_status: 'suggested', comment: null, reviewed_by: 'dpo@acme.com', reviewed_at: '2026-11-04T10:00:00Z', actor_type: 'partner', partner_id: 'p1', mapping: null };
    const posted: unknown[] = [];
    jest.spyOn(globalThis, 'fetch').mockImplementation(async (input: unknown, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname === '/oauth/token') return token('docs.jwt.token');
      if (url.pathname.endsWith('/risks')) return json({ data: [{ ...mappingWire, status: 'suggested' }] });
      if (url.pathname.endsWith('/risks/r1/review') && init?.method === 'POST') {
        posted.push(JSON.parse(String(init.body)));
        return json({ data: reviewWire, meta: { current: mappingWire } }, 201);
      }
      if (url.pathname.endsWith('/reviews')) return json({ data: [reviewWire], meta: { total: 1, current: mappingWire } });
      throw new Error(`unexpected ${url}`);
    });
    const client = new PartnerClient({ clientId: 'id', clientSecret: 'secret' });
    const assetId = '019e1a2b-3c4d-7000-8000-000000000001';

    // ---- begin docs ----
    // Confirm or reject rule-based suggestions (needs the risks:review scope)
    for (const risk of await client.assets.risks(assetId, { status: 'suggested' })) {
      const { meta } = await client.assets.reviewRisk(assetId, risk.id, {
        status: 'confirmed',
        reviewedBy: 'dpo@acme.com',
        comment: 'Candidate screening is an Annex III use',
      });
      console.log(`${meta.current.frameworkRef}: ${meta.current.status}`);
    }

    // Who decided what, newest first (needs risks:read)
    const reviews = await client.assets.riskReviews(assetId, '<risk mapping id>');
    for (const review of reviews.data) {
      console.log(`${review.reviewedBy}: ${review.previousStatus} -> ${review.status}`);
    }
    // ---- end docs ----

    expect(posted).toEqual([{ status: 'confirmed', reviewed_by: 'dpo@acme.com', comment: 'Candidate screening is an Annex III use' }]);
    expect(logs).toEqual(['Annex III, point 4(a): confirmed', 'dpo@acme.com: suggested -> confirmed']);
  });

  it('Spend Alerts and Cost Export', async () => {
    const alertWire = { id: 'al1', group_by: 'use_case', group_key: 'interview-screening', day: '2026-11-02', observed_cost: 412.5, baseline_cost: 80.1, baseline_sigma: 21.4, z_score: 15.5, threshold_sigma: 3, history_days: 28, severity: 'critical', status: 'open', acknowledged_at: null, acknowledged_by: null, created_at: '2026-11-03T02:00:04Z' };
    const acked: string[] = [];
    const focusHeaders = { 'x-focus-version': '1.4', 'x-export-window-start': '2026-10-01T00:00:00Z', 'x-export-window-end': '2026-11-01T00:00:00Z' };
    jest.spyOn(globalThis, 'fetch').mockImplementation(async (input: unknown, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname === '/oauth/token') return token('docs.jwt.token');
      if (url.pathname === '/partner/v1/spend-alerts') {
        expect(url.searchParams.get('status')).toBe('open');
        return json({ data: [alertWire], meta: { total: 1 } });
      }
      if (url.pathname.endsWith('/acknowledge') && init?.method === 'POST') {
        acked.push(url.pathname);
        return json({ data: { ...alertWire, status: 'acknowledged' } });
      }
      if (url.pathname === '/partner/v1/usage/focus-export') {
        if (url.searchParams.get('format') === 'jsonl') {
          const rows = [{ x_EstimatedCost: 1.25 }, { x_EstimatedCost: 2.5 }].map((r) => JSON.stringify(r)).join('\n');
          return new Response(rows + '\n', { headers: { 'content-type': 'application/x-ndjson', ...focusHeaders } });
        }
        expect(url.searchParams.get('cost_mode')).toBe('merge_safe');
        return new Response('BilledCost,x_EstimatedCost\n0,1.25\n', {
          headers: { 'content-type': 'text/csv', 'content-disposition': 'attachment; filename="focus-cost.csv"', ...focusHeaders },
        });
      }
      throw new Error(`unexpected ${url}`);
    });
    const client = new PartnerClient({ clientId: 'id', clientSecret: 'secret' });
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ps-docs-'));
    const cwd = process.cwd();
    process.chdir(dir);
    try {
      // ---- begin docs ----
      // Spend anomaly alerts (needs spend-alerts:read; acknowledging needs spend-alerts:acknowledge)
      const { data: alerts } = await client.spendAlerts.list({ status: 'open', severity: 'critical' });
      for (const alert of alerts) {
        console.log(`${alert.day} ${alert.groupKey}: $${alert.observedCost} against a $${alert.baselineCost} baseline`);
        await client.spendAlerts.acknowledge(alert.id);
      }

      // FinOps FOCUS v1.4 cost export, streamed row by row (needs usage:export)
      const focus = await client.usage.focusExport({ start: '2026-10-01', end: '2026-11-01', format: 'jsonl' });
      console.log(`FOCUS ${focus.focusVersion} from ${focus.windowStart} to ${focus.windowEnd}`);
      let estimated = 0;
      for await (const row of focus.rows()) {
        estimated += row.x_EstimatedCost;
      }
      console.log(`Estimated cost: $${estimated.toFixed(2)}`);

      // Or stream the CSV straight into a file (Node)
      const csv = await client.usage.focusExport({ start: '2026-10-01', end: '2026-11-01', costMode: 'merge_safe' });
      await csv.saveTo(csv.filename ?? 'focus-cost.csv');
      // ---- end docs ----

      expect(fs.readFileSync(path.join(dir, 'focus-cost.csv'), 'utf8')).toBe('BilledCost,x_EstimatedCost\n0,1.25\n');
    } finally {
      process.chdir(cwd);
      fs.rmSync(dir, { recursive: true, force: true });
    }
    expect(acked).toEqual(['/partner/v1/spend-alerts/al1/acknowledge']);
    expect(logs).toEqual([
      '2026-11-02 interview-screening: $412.5 against a $80.1 baseline',
      'FOCUS 1.4 from 2026-10-01T00:00:00Z to 2026-11-01T00:00:00Z',
      'Estimated cost: $3.75',
    ]);
  });

  it('MCP Tools, Routing Learner and SIEM Pull', async () => {
    const tool = { server_name: 'github', tool_name: 'create_issue', source: 'gateway', calling_service: null, environment: null, transport: null, call_count: 42, error_count: 0, blocked_count: 2, user_hash: null, user_group: null, avg_latency_ms: 100, first_seen: null, last_seen: null, asset_id: null, owner_email: 'platform@acme.com' };
    const day = { day: '2026-11-05', source: 'gateway', server_name: 'github', tool_name: 'create_issue', call_count: 10, error_count: 0, blocked_count: 3, latency_ms_total: 0, latency_count: 0, avg_latency_ms: null, request_bytes: 0, response_bytes: 0, bytes_count: 0, asset_id: null, owner_email: null };
    const rec = { id: 'rec1', snapshot_id: 's1', learner_version: 'lv1-0123456789abcdef', use_case: 'support-triage', sensitivity_level: 'internal', current_model: 'openai:gpt-4o', recommended_model: 'mistral:mistral-small', candidates: [], projection: { savings_pct: 75 }, status: 'pending', reviewed_by: null, reviewed_at: null, review_comment: null, reviewer_actor_type: null, created_at: '2026-11-05T08:00:00Z', realised: null };
    const posted: unknown[] = [];
    const siemQueries: string[] = [];
    const call = (n: number) => JSON.stringify({ TimeGenerated: 't', EventUid: `e${n}`, EventType: 'AICall', PsTenantId: 't1' });
    jest.spyOn(globalThis, 'fetch').mockImplementation(async (input: unknown, init?: RequestInit) => {
      const url = new URL(String(input));
      if (url.pathname === '/oauth/token') return token('docs.jwt.token');
      if (url.pathname === '/partner/v1/mcp/tools') {
        expect(url.searchParams.get('source')).toBe('gateway');
        return url.searchParams.get('cursor')
          ? json({ data: [{ ...tool, tool_name: 'list_repos', call_count: 5 }], meta: { has_more: false, next_cursor: null } })
          : json({ data: [tool], meta: { has_more: true, next_cursor: 'c1' } });
      }
      if (url.pathname === '/partner/v1/mcp/usage') {
        expect(url.searchParams.get('server_name')).toBe('github');
        return json({ data: [day, day], meta: { since: '2026-10-01', until: null, has_more: false, next_cursor: null } });
      }
      if (url.pathname === '/partner/v1/learner/recommendations') {
        expect(url.searchParams.get('status')).toBe('pending');
        return json({ data: [rec], meta: { total: 1 } });
      }
      if (url.pathname === '/partner/v1/learner/recommendations/rec1/review' && init?.method === 'POST') {
        posted.push(JSON.parse(String(init.body)));
        return json({ data: { ...rec, status: 'accepted', reviewed_by: 'finops@acme.com' } });
      }
      if (url.pathname === '/partner/v1/siem/calls') {
        siemQueries.push(url.search);
        const first = url.searchParams.get('cursor') === 'saved';
        return new Response(first ? `${call(1)}\n${call(2)}\n` : `${call(3)}\n`, {
          headers: { 'content-type': 'application/x-ndjson', 'x-ps-cursor': first ? 'c2' : 'c3', 'x-ps-has-more': String(first) },
        });
      }
      throw new Error(`unexpected ${url}`);
    });
    const client = new PartnerClient({ clientId: 'id', clientSecret: 'secret' });
    let savedCursor: string | null = 'saved';
    const forwarded: string[] = [];
    const forwardToSiem = async (r: SiemRecord) => void forwarded.push(r.EventUid);

    // ---- begin docs ----
    // MCP tools seen in telemetry, and daily tool-call metering (needs mcp:read)
    for await (const tool of client.mcp.listTools({ source: 'gateway' })) {
      console.log(`${tool.serverName}/${tool.toolName}: ${tool.callCount} calls, owner ${tool.ownerEmail}`);
    }
    const days = await client.mcp.listUsage({ since: '2026-10-01', serverName: 'github' });
    console.log(`${days.reduce((n, d) => n + d.blockedCount, 0)} blocked calls`);

    // Learner recommendations awaiting review (learner:read), accepted or rejected with learner:review
    const { data: pending } = await client.learner.listRecommendations({ status: 'pending' });
    for (const rec of pending) {
      console.log(`${rec.useCase}: ${rec.currentModel} -> ${rec.recommendedModel}, ${rec.projection.savingsPct}% cheaper`);
      await client.learner.reviewRecommendation(rec.id, {
        decision: 'accepted',
        reviewedBy: 'finops@acme.com',
        comment: 'Quality holds on the eval set',
      });
    }

    // Routing evidence as Sentinel-schema records (needs evidence:read), from the last saved cursor
    const feed = client.siem.callsFeed({ cursor: savedCursor, since: '2026-11-01T00:00:00Z' });
    for await (const record of feed) {
      await forwardToSiem(record);
    }
    savedCursor = feed.cursor;
    // ---- end docs ----

    expect(logs).toEqual([
      'github/create_issue: 42 calls, owner platform@acme.com',
      'github/list_repos: 5 calls, owner platform@acme.com',
      '6 blocked calls',
      'support-triage: openai:gpt-4o -> mistral:mistral-small, 75% cheaper',
    ]);
    expect(posted).toEqual([{ decision: 'accepted', reviewed_by: 'finops@acme.com', comment: 'Quality holds on the eval set' }]);
    expect(forwarded).toEqual(['e1', 'e2', 'e3']);
    expect(savedCursor).toBe('c3');
    expect(siemQueries).toEqual(['?cursor=saved', '?cursor=c2']);
  });

  it('Provider Credentials', async () => {
    const cred = { provider: 'openai', key_hint: '****WXYZ', key_id: 'k1', created_at: null, rotated_at: null };
    const calls: string[] = [];
    routeFetch((url) => {
      calls.push(url.pathname);
      if (url.pathname === '/partner/v1/provider-credentials') {
        return json({ data: [cred], meta: { providers: ['openai', 'anthropic'], env_fallback: ['anthropic'] } });
      }
      if (url.pathname.startsWith('/partner/v1/provider-key-fingerprints/')) {
        return json({ data: { provider: 'openai', api_key_id: 'key_abc123', api_key_fingerprint: '3f9a1c0b7d2e4f60', created_at: null }, meta: { corroborated_assets: 3 } });
      }
      return json(cred);
    });
    const saved = process.env.OPENAI_ADMIN_KEY;
    process.env.OPENAI_ADMIN_KEY = 'sk-admin-0123456789abcdefWXYZ';
    const client = new PartnerClient({ clientId: 'ps-partner-ardoq-abc123', clientSecret: 'sk-partner-secret-xyz789' });
    try {
      // ---- begin docs ----
      // Store a provider admin key; a 409 means one is stored already, so rotate it
      const stored = await client.providerCredentials.create('openai', { adminKey: process.env.OPENAI_ADMIN_KEY! });
      console.log(`Stored ${stored.provider} key ${stored.keyHint}`);
      await client.providerCredentials.rotate('openai', { adminKey: process.env.OPENAI_ADMIN_KEY! });

      const { data: keys, meta } = await client.providerCredentials.list();
      console.log(`${keys.length} stored, env fallback for: ${meta.envFallback.join(', ') || 'none'}`);

      // Link the provider's key id to the SDK's api_key_fingerprint (sha256 of the key, first 16 hex characters)
      const link = await client.providerKeyFingerprints.put('openai', 'key_abc123', { apiKeyFingerprint: '3f9a1c0b7d2e4f60' });
      console.log(`Corroborated ${link.meta.corroboratedAssets} assets`);
      // ---- end docs ----
    } finally {
      if (saved === undefined) delete process.env.OPENAI_ADMIN_KEY;
      else process.env.OPENAI_ADMIN_KEY = saved;
    }

    expect(logs).toEqual(['Stored openai key ****WXYZ', '1 stored, env fallback for: anthropic', 'Corroborated 3 assets']);
    expect(calls).toEqual([
      '/partner/v1/provider-credentials/openai',
      '/partner/v1/provider-credentials/openai/rotate',
      '/partner/v1/provider-credentials',
      '/partner/v1/provider-key-fingerprints/openai/key_abc123',
    ]);
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
    const t: [
      AIAsset?,
      DataFlow?,
      RiskMapping?,
      ChangeEvent?,
      UsageSummary?,
      Attestation?,
      WebhookSubscription?,
      SpendAlert?,
      FocusCostRow?,
      McpTool?,
      LearnerRecommendation?,
      SiemRecord?,
    ] = [];
    expect(t).toEqual([]);
  });
});
