/**
 * MCP observability, the routing learner and the SIEM pull, against a
 * mocked fetch.
 */
import {
  APIConnectionError,
  APIError,
  InternalServerError,
  NotFoundError,
  PartnerClient,
  PartnerError,
  SiemBatch,
} from '../src';
import type { LearnerRecommendation, McpTool, SiemRecord } from '../src';
import { json, mockFetch, noSleep } from './helpers';

const client = (fetch: ReturnType<typeof mockFetch>['fetch']) =>
  new PartnerClient({ apiKey: 'k', fetch, sleep: noSleep });

const query = (url: string) => Object.fromEntries(new URL(url).searchParams);

// ── MCP ─────────────────────────────────────────────────────────────────

const TOOL_WIRE = {
  server_name: 'github',
  tool_name: 'create_issue',
  source: 'gateway',
  calling_service: 'support-bot',
  environment: 'production',
  transport: 'streamable-http',
  call_count: 42,
  error_count: 1,
  blocked_count: 2,
  user_hash: null,
  user_group: null,
  avg_latency_ms: 180,
  first_seen: '2026-10-01T09:00:00Z',
  last_seen: '2026-11-05T17:12:00Z',
  asset_id: '019e1a2b-0000-7000-8000-0000000000c1',
  owner_email: 'platform@acme.com',
};

const TOOL: McpTool = {
  serverName: 'github',
  toolName: 'create_issue',
  source: 'gateway',
  callingService: 'support-bot',
  environment: 'production',
  transport: 'streamable-http',
  callCount: 42,
  errorCount: 1,
  blockedCount: 2,
  userHash: null,
  userGroup: null,
  avgLatencyMs: 180,
  firstSeen: '2026-10-01T09:00:00Z',
  lastSeen: '2026-11-05T17:12:00Z',
  assetId: '019e1a2b-0000-7000-8000-0000000000c1',
  ownerEmail: 'platform@acme.com',
};

const USAGE_WIRE = {
  day: '2026-11-05',
  source: 'gateway',
  server_name: 'github',
  tool_name: 'create_issue',
  call_count: 10,
  error_count: 0,
  blocked_count: 1,
  latency_ms_total: 1800,
  latency_count: 10,
  avg_latency_ms: 180,
  request_bytes: 5120,
  response_bytes: 20480,
  bytes_count: 10,
  asset_id: null,
  owner_email: null,
};

describe('client.mcp', () => {
  it('listToolsPage sends the filters as snake_case and camelCases the page', async () => {
    const m = mockFetch(json({ data: [TOOL_WIRE], meta: { has_more: false, next_cursor: null } }));
    const page = await client(m.fetch).mcp.listToolsPage({ serverName: 'github', source: 'gateway', limit: 1000 });
    expect(page).toEqual({ data: [TOOL], meta: { hasMore: false, nextCursor: null } });
    expect(new URL(m.calls[0].url).pathname).toBe('/partner/v1/mcp/tools');
    expect(query(m.calls[0].url)).toEqual({ server_name: 'github', source: 'gateway', limit: '1000' });
  });

  it('listTools follows meta.next_cursor as `cursor`', async () => {
    const m = mockFetch(
      json({ data: [TOOL_WIRE], meta: { has_more: true, next_cursor: 'c1' } }),
      json({ data: [{ ...TOOL_WIRE, tool_name: 'list_repos' }], meta: { has_more: false, next_cursor: null } }),
    );
    const iter = client(m.fetch).mcp.listTools({ limit: 1 });
    const tools = await iter;
    expect(tools.map((t) => t.toolName)).toEqual(['create_issue', 'list_repos']);
    expect(query(m.calls[0].url)).toEqual({ limit: '1' });
    expect(query(m.calls[1].url)).toEqual({ limit: '1', cursor: 'c1' });
    expect(iter.lastPage?.meta).toEqual({ hasMore: false, nextCursor: null });
  });

  it('listTools starts from a given cursor', async () => {
    const m = mockFetch(json({ data: [], meta: { has_more: false, next_cursor: null } }));
    await client(m.fetch).mcp.listTools({ cursor: 'saved' });
    expect(query(m.calls[0].url)).toEqual({ cursor: 'saved' });
  });

  it('listUsage pages daily rows and exposes the window in meta', async () => {
    const meta = { since: '2026-10-07', until: null };
    const m = mockFetch(
      json({ data: [USAGE_WIRE], meta: { ...meta, has_more: true, next_cursor: 'u1' } }),
      json({ data: [{ ...USAGE_WIRE, day: '2026-11-04' }], meta: { ...meta, has_more: false, next_cursor: null } }),
    );
    const iter = client(m.fetch).mcp.listUsage({ since: '2026-10-07', toolName: 'create_issue', serverName: 'github' });
    const days = [];
    for await (const day of iter) days.push(day);
    expect(days.map((d) => [d.day, d.requestBytes, d.latencyMsTotal])).toEqual([
      ['2026-11-05', 5120, 1800],
      ['2026-11-04', 5120, 1800],
    ]);
    expect(query(m.calls[0].url)).toEqual({ since: '2026-10-07', tool_name: 'create_issue', server_name: 'github' });
    expect(query(m.calls[1].url)).toEqual({
      since: '2026-10-07',
      tool_name: 'create_issue',
      server_name: 'github',
      cursor: 'u1',
    });
    expect(iter.lastPage?.meta).toEqual({ since: '2026-10-07', until: null, hasMore: false, nextCursor: null });
  });

  it('stops on a repeated cursor', async () => {
    const page = json({ data: [], meta: { has_more: true, next_cursor: 'same' } });
    const m = mockFetch(page, json({ data: [], meta: { has_more: true, next_cursor: 'same' } }));
    await expect(client(m.fetch).mcp.listUsage().toArray()).rejects.toThrow(/cursor repeated/);
  });
});

// ── Learner ─────────────────────────────────────────────────────────────

const SNAPSHOT_SUMMARY_WIRE = {
  id: '019e1a2b-0000-7000-8000-0000000000a1',
  learner_version: 'lv1-0123456789abcdef',
  parent_version: null,
  mode: 'recommend',
  algorithm: 'linear-thompson.v1',
  feature_schema: 'ps-prompt-features.v1',
  policy_version: 'pv-7',
  data_fingerprint: 'ab'.repeat(32),
  training_rows: 5000,
  scored_rows: 1200,
  recommendations: 1,
  created_at: '2026-11-05T08:00:00Z',
};

const REC_WIRE = {
  id: '019e1a2b-0000-7000-8000-0000000000b1',
  snapshot_id: SNAPSHOT_SUMMARY_WIRE.id,
  learner_version: 'lv1-0123456789abcdef',
  use_case: 'support-triage',
  sensitivity_level: 'internal',
  current_model: 'openai:gpt-4o',
  recommended_model: 'mistral:mistral-small_2501',
  candidates: ['openai:gpt-4o', 'mistral:mistral-small_2501'],
  projection: {
    calls: 900,
    current: { model: 'openai:gpt-4o', quality: 0.91, cost_per_call: 0.004, latency_ms: 900, reward: 0.8, quality_method: 'dr', cost_method: 'priced' },
    recommended: { model: 'mistral:mistral-small_2501', quality: 0.9, cost_per_call: 0.001, latency_ms: 600, reward: 0.85, quality_method: 'dr', cost_method: 'priced' },
    quality_delta: -0.01,
    reward_delta: 0.05,
    savings_per_call: 0.003,
    savings_total: 2.7,
    savings_pct: 75,
    prob_best: 0.93,
    arm_samples: { 'openai:gpt-4o': 70, 'mistral:mistral-small_2501': 930 },
  },
  status: 'pending',
  reviewed_by: null,
  reviewed_at: null,
  review_comment: null,
  reviewer_actor_type: null,
  created_at: '2026-11-05T08:00:00Z',
  realised: null,
};

describe('client.learner', () => {
  it('lists snapshots', async () => {
    const m = mockFetch(json({ data: [SNAPSHOT_SUMMARY_WIRE], meta: { total: 1 } }));
    const res = await client(m.fetch).learner.listSnapshots({ limit: 10 });
    expect(res.meta).toEqual({ total: 1 });
    expect(res.data[0]).toMatchObject({ learnerVersion: 'lv1-0123456789abcdef', parentVersion: null, trainingRows: 5000 });
    expect(new URL(m.calls[0].url).pathname).toBe('/partner/v1/learner/snapshots');
    expect(query(m.calls[0].url)).toEqual({ limit: '10' });
  });

  it('gets a snapshot, keeping the uploaded document exactly as sent', async () => {
    const doc = { schema: 'ps-learner-snapshot.v1', reward_weights: { cost_lambda: 1 }, groups: [{ use_case: 'x', arm_stats: {} }] };
    const m = mockFetch(json({ data: { ...SNAPSHOT_SUMMARY_WIRE, snapshot: doc } }));
    const snap = await client(m.fetch).learner.getSnapshot('lv1-0123456789abcdef');
    expect(snap.snapshot).toEqual(doc);
    expect(snap.dataFingerprint).toBe('ab'.repeat(32));
    expect(new URL(m.calls[0].url).pathname).toBe('/partner/v1/learner/snapshots/lv1-0123456789abcdef');
  });

  it('throws NotFoundError for an unknown snapshot, and refuses an empty version', async () => {
    const m = mockFetch(json({ detail: 'not found' }, 404));
    await expect(client(m.fetch).learner.getSnapshot('lv1-ffffffffffffffff')).rejects.toBeInstanceOf(NotFoundError);
    await expect(client(mockFetch().fetch).learner.getSnapshot('')).rejects.toThrow(PartnerError);
  });

  it('lists recommendations with filters, keeping arm_samples keyed by model', async () => {
    const m = mockFetch(json({ data: [REC_WIRE], meta: { total: 1 } }));
    const res = await client(m.fetch).learner.listRecommendations({
      status: 'pending',
      useCase: 'support-triage',
      learnerVersion: 'lv1-0123456789abcdef',
      limit: 200,
    });
    expect(query(m.calls[0].url)).toEqual({
      status: 'pending',
      use_case: 'support-triage',
      learner_version: 'lv1-0123456789abcdef',
      limit: '200',
    });
    const rec: LearnerRecommendation = res.data[0];
    expect(rec.recommendedModel).toBe('mistral:mistral-small_2501');
    expect(rec.projection.armSamples).toEqual({ 'openai:gpt-4o': 70, 'mistral:mistral-small_2501': 930 });
    expect(rec.projection.recommended).toMatchObject({ costPerCall: 0.001, qualityMethod: 'dr' });
    expect(rec.projection.probBest).toBe(0.93);
  });

  it('gets one recommendation, with realised savings once accepted', async () => {
    const realised = {
      since: '2026-11-05T09:00:00Z',
      calls: 100,
      calls_on_recommended: 80,
      adoption: 0.8,
      cost_per_call: 0.0016,
      savings_per_call: 0.0024,
      savings_pct: 60,
      projected_savings_pct: 75,
      delta_points: -15,
      feedback_count: 3,
      feedback_quality: 0.9,
      projected_quality: 0.9,
    };
    const m = mockFetch(json({ data: { ...REC_WIRE, status: 'accepted', realised } }));
    const rec = await client(m.fetch).learner.getRecommendation(REC_WIRE.id);
    expect(rec.realised).toMatchObject({ callsOnRecommended: 80, projectedSavingsPct: 75, deltaPoints: -15 });
    expect(new URL(m.calls[0].url).pathname).toBe(`/partner/v1/learner/recommendations/${REC_WIRE.id}`);
  });

  it('reviews a recommendation with a snake_case body and unwraps `data`', async () => {
    const decided = { ...REC_WIRE, status: 'accepted', reviewed_by: 'finops@acme.com', reviewer_actor_type: 'partner' };
    const m = mockFetch(json({ data: decided }));
    const rec = await client(m.fetch).learner.reviewRecommendation(REC_WIRE.id, {
      decision: 'accepted',
      reviewedBy: 'finops@acme.com',
      comment: 'Quality holds on the eval set',
    });
    expect(rec).toMatchObject({ status: 'accepted', reviewedBy: 'finops@acme.com', reviewerActorType: 'partner' });
    expect(m.calls[0].method).toBe('POST');
    expect(new URL(m.calls[0].url).pathname).toBe(`/partner/v1/learner/recommendations/${REC_WIRE.id}/review`);
    expect(JSON.parse(m.calls[0].body as string)).toEqual({
      decision: 'accepted',
      reviewed_by: 'finops@acme.com',
      comment: 'Quality holds on the eval set',
    });
  });

  it('does not retry a review after a 5xx or a network error (a resend would be a 409)', async () => {
    const m = mockFetch(json({ detail: 'unavailable' }, 503), json({ data: REC_WIRE }));
    await expect(
      client(m.fetch).learner.reviewRecommendation(REC_WIRE.id, { decision: 'rejected', reviewedBy: 'a@b.c' }),
    ).rejects.toBeInstanceOf(InternalServerError);
    expect(m.calls).toHaveLength(1);

    const n = mockFetch(new TypeError('socket hang up'), json({ data: REC_WIRE }));
    await expect(
      client(n.fetch).learner.reviewRecommendation(REC_WIRE.id, { decision: 'rejected', reviewedBy: 'a@b.c' }),
    ).rejects.toBeInstanceOf(APIConnectionError);
    expect(n.calls).toHaveLength(1);
  });

  it('retries a review after a 429, and surfaces a 409 for a decided recommendation', async () => {
    const m = mockFetch(json({ detail: 'slow down' }, 429, { 'Retry-After': '1' }), json({ data: REC_WIRE }));
    await client(m.fetch).learner.reviewRecommendation(REC_WIRE.id, { decision: 'rejected', reviewedBy: 'a@b.c' });
    expect(m.calls).toHaveLength(2);

    const conflict = mockFetch(json({ detail: 'Recommendation is accepted; only pending ones can be reviewed' }, 409));
    const err = await client(conflict.fetch)
      .learner.reviewRecommendation(REC_WIRE.id, { decision: 'rejected', reviewedBy: 'a@b.c' })
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(APIError);
    expect((err as APIError).status).toBe(409);
  });
});

// ── SIEM ────────────────────────────────────────────────────────────────

const CALL = (n: number): SiemRecord => ({
  TimeGenerated: `2026-11-05T10:00:0${n}Z`,
  EventUid: `e${n}`,
  EventType: 'AICall',
  PsTenantId: 't1',
  PsServedModel: 'gpt-4o',
});

function ndjson(records: unknown[], headers: Record<string, string> = {}, tail = '\n'): Response {
  const body = records.map((r) => JSON.stringify(r)).join('\n') + (records.length ? tail : '');
  return new Response(body, { headers: { 'content-type': 'application/x-ndjson', ...headers } });
}

describe('client.siem', () => {
  it('calls() asks for NDJSON, sends the query and reads the cursor headers', async () => {
    const m = mockFetch(ndjson([CALL(1), CALL(2)], { 'X-PS-Cursor': 'cur2', 'X-PS-Has-More': 'true' }));
    const batch = await client(m.fetch).siem.calls({ since: new Date('2026-11-05T00:00:00Z'), limit: 2, settleSeconds: 60 });
    expect(batch).toBeInstanceOf(SiemBatch);
    expect(batch.cursor).toBe('cur2');
    expect(batch.hasMore).toBe(true);
    const records = [];
    for await (const r of batch.records()) records.push(r);
    expect(records).toEqual([CALL(1), CALL(2)]);
    expect(new URL(m.calls[0].url).pathname).toBe('/partner/v1/siem/calls');
    expect(query(m.calls[0].url)).toEqual({ since: '2026-11-05T00:00:00.000Z', limit: '2', settle_seconds: '60' });
    expect(m.calls[0].headers.Accept).toBe('application/x-ndjson');
  });

  it('audit() sends a numeric cursor; an empty batch has hasMore false', async () => {
    const m = mockFetch(ndjson([], { 'X-PS-Cursor': '41', 'X-PS-Has-More': 'false' }));
    const batch = await client(m.fetch).siem.audit({ cursor: 41 });
    expect(query(m.calls[0].url)).toEqual({ cursor: '41' });
    expect(new URL(m.calls[0].url).pathname).toBe('/partner/v1/siem/audit');
    expect([batch.cursor, batch.hasMore]).toEqual(['41', false]);
    expect(await batch.text()).toBe('');
  });

  it('reports a truncated batch and a malformed line', async () => {
    const cut = mockFetch(ndjson([CALL(1), CALL(2)], {}, ''));
    const batch = await client(cut.fetch).siem.calls();
    await expect(async () => {
      for await (const _ of batch.records()) void _;
    }).rejects.toBeInstanceOf(APIConnectionError);

    const bad = mockFetch(new Response('{"TimeGenerated":"x"}\nnot json\n'));
    const b2 = await client(bad.fetch).siem.audit();
    const it = b2.records();
    await expect(it.next()).resolves.toEqual({ done: false, value: { TimeGenerated: 'x' } });
    await expect(it.next()).rejects.toThrow(/SIEM record line 2 is not JSON/);
  });

  it('callsFeed follows the cursor until a batch is not full, then holds the next start', async () => {
    const m = mockFetch(
      ndjson([CALL(1), CALL(2)], { 'X-PS-Cursor': 'c2', 'X-PS-Has-More': 'true' }),
      ndjson([CALL(3)], { 'X-PS-Cursor': 'c3', 'X-PS-Has-More': 'false' }),
    );
    const feed = client(m.fetch).siem.callsFeed({ since: '2026-11-05T00:00:00Z', limit: 2 });
    expect(feed.cursor).toBeNull();
    const ids = (await feed.toArray()).map((r) => r.EventUid);
    expect(ids).toEqual(['e1', 'e2', 'e3']);
    expect(feed.cursor).toBe('c3');
    // `since` only without a cursor; afterwards the cursor alone.
    expect(query(m.calls[0].url)).toEqual({ since: '2026-11-05T00:00:00Z', limit: '2' });
    expect(query(m.calls[1].url)).toEqual({ cursor: 'c2', limit: '2' });
  });

  it('a feed resumed from a saved cursor keeps it when the server sends none', async () => {
    const m = mockFetch(ndjson([], { 'X-PS-Has-More': 'false' }));
    const feed = client(m.fetch).siem.auditFeed({ cursor: 7, since: '2026-01-01T00:00:00Z' });
    expect(await feed.toArray()).toEqual([]);
    expect(feed.cursor).toBe('7');
    expect(query(m.calls[0].url)).toEqual({ cursor: '7' });
  });

  it('stopping part way through a batch leaves the cursor at the start of that batch', async () => {
    const m = mockFetch(
      ndjson([CALL(1), CALL(2)], { 'X-PS-Cursor': 'c2', 'X-PS-Has-More': 'true' }),
      ndjson([CALL(3), CALL(4)], { 'X-PS-Cursor': 'c4', 'X-PS-Has-More': 'true' }),
    );
    const feed = client(m.fetch).siem.callsFeed({ cursor: 'c0' });
    for await (const r of feed) {
      if (r.EventUid === 'e3') break;
    }
    expect(feed.cursor).toBe('c2');
    expect(m.remaining()).toBe(0);
  });

  it('a failed batch leaves the cursor where it was', async () => {
    const m = mockFetch(
      ndjson([CALL(1)], { 'X-PS-Cursor': 'c1', 'X-PS-Has-More': 'true' }),
      ndjson([CALL(2)], { 'X-PS-Cursor': 'c2', 'X-PS-Has-More': 'true' }, ''),
    );
    const feed = client(m.fetch).siem.callsFeed();
    await expect(feed.toArray()).rejects.toBeInstanceOf(APIConnectionError);
    expect(feed.cursor).toBe('c1');
  });

  it('refuses to loop on a cursor that does not advance', async () => {
    const m = mockFetch(
      ndjson([CALL(1)], { 'X-PS-Cursor': 'c1', 'X-PS-Has-More': 'true' }),
      ndjson([CALL(1)], { 'X-PS-Cursor': 'c1', 'X-PS-Has-More': 'true' }),
    );
    await expect(client(m.fetch).siem.callsFeed().toArray()).rejects.toThrow(/did not advance/);
  });

  it('retries a 503 before the body starts', async () => {
    const m = mockFetch(json({ detail: 'busy' }, 503), ndjson([CALL(1)], { 'X-PS-Cursor': 'c1', 'X-PS-Has-More': 'false' }));
    const records = await client(m.fetch).siem.callsFeed().toArray();
    expect(records).toHaveLength(1);
    expect(m.calls).toHaveLength(2);
  });
});

describe('SiemFeed.batches() cursor', () => {
  const page = (ids: number[], cursor: string, more: boolean, tail = '\n') =>
    ndjson(ids.map(CALL), { 'X-PS-Cursor': cursor, 'X-PS-Has-More': String(more) }, tail);
  const read = async (batch: SiemBatch) => {
    const out: string[] = [];
    for await (const r of batch.records()) out.push(String(r.EventUid));
    return out;
  };

  it('moves the cursor past each batch read to the end', async () => {
    const m = mockFetch(page([1, 2], 'c2', true), page([3, 4], 'c4', true), page([5], 'c5', false));
    const feed = client(m.fetch).siem.callsFeed({ cursor: 'c0' });
    const ids: string[] = [];
    const cursors: Array<string | null> = [];
    let n = 0;
    for await (const batch of feed.batches()) {
      cursors.push(feed.cursor);
      // records() and text() both count as reading the batch to the end.
      if (n++ === 1) {
        const lines = (await batch.text()).trim().split('\n');
        ids.push(...lines.map((l) => String((JSON.parse(l) as SiemRecord).EventUid)));
      } else {
        ids.push(...(await read(batch)));
      }
      expect(batch.fullyRead).toBe(true);
    }
    expect(ids).toEqual(['e1', 'e2', 'e3', 'e4', 'e5']);
    expect(cursors).toEqual(['c0', 'c2', 'c4']);
    expect(feed.cursor).toBe('c5');
    expect(query(m.calls[1].url)).toEqual({ cursor: 'c2' });
  });

  it('asking for the next batch early throws and skips no record', async () => {
    const m = mockFetch(
      page([1, 2], 'c2', true),
      page([1, 2], 'c2', true), // the same cursor, asked for again
      page([3], 'c3', false),
    );
    const feed = client(m.fetch).siem.callsFeed({ cursor: 'c0' });
    let partRead: SiemBatch | undefined;
    // Part read: one record, then on to the next batch.
    await expect(async () => {
      for await (const batch of feed.batches()) {
        partRead = batch;
        const it = batch.records();
        await it.next();
        await it.return(undefined);
      }
    }).rejects.toThrow(/not read to the end.*no record was skipped/);
    expect(partRead?.fullyRead).toBe(false);
    expect(feed.cursor).toBe('c0');
    expect(m.calls).toHaveLength(1);

    // Starting again re-requests the same cursor and gets every record.
    const ids = (await feed.toArray()).map((r) => r.EventUid);
    expect(ids).toEqual(['e1', 'e2', 'e3']);
    expect(query(m.calls[1].url)).toEqual({ cursor: 'c0' });
    expect(feed.cursor).toBe('c3');
  });

  it('a batch never read is cancelled and the cursor stays put', async () => {
    const m = mockFetch(page([1], 'c1', true));
    const feed = client(m.fetch).siem.auditFeed({ cursor: 'c0' });
    let seen: SiemBatch | undefined;
    await expect(async () => {
      for await (const batch of feed.batches()) seen = batch;
    }).rejects.toBeInstanceOf(PartnerError);
    expect(seen?.bodyUsed).toBe(true); // cancelled
    expect(seen?.consumed).toBe(false);
    expect(feed.cursor).toBe('c0');
    expect(m.remaining()).toBe(0);
  });

  it('markConsumed() lets the cursor move past a batch read from body or skipped', async () => {
    const m = mockFetch(page([1], 'c1', true), page([2], 'c2', false));
    const feed = client(m.fetch).siem.callsFeed();
    for await (const batch of feed.batches()) {
      if (batch.cursor === 'c1') {
        await new Response(batch.body).text();
        expect(batch.fullyRead).toBe(false);
      }
      batch.markConsumed();
      expect(batch.consumed).toBe(true);
    }
    expect(feed.cursor).toBe('c2');
  });

  it('breaking out of the loop leaves the cursor unchanged', async () => {
    const m = mockFetch(page([1, 2], 'c2', true), page([3, 4], 'c4', true));
    const feed = client(m.fetch).siem.callsFeed({ cursor: 'c0' });
    for await (const batch of feed.batches()) {
      await read(batch);
      // Break with the second batch read in full: its cursor applies only
      // once the next batch is asked for, so a failure after reading (while
      // forwarding, say) still replays it.
      if (batch.cursor === 'c4') break;
    }
    expect(feed.cursor).toBe('c2');

    const m2 = mockFetch(page([1, 2], 'c2', true));
    const feed2 = client(m2.fetch).siem.callsFeed({ cursor: 'c0' });
    for await (const batch of feed2.batches()) {
      expect(batch.cursor).toBe('c2');
      break;
    }
    expect(feed2.cursor).toBe('c0');
  });

  it('an error part way through a batch leaves the cursor unchanged', async () => {
    // A truncated body, thrown out of the loop.
    const m = mockFetch(page([1], 'c1', true), page([2, 3], 'c3', true, ''));
    const feed = client(m.fetch).siem.callsFeed();
    await expect(async () => {
      for await (const batch of feed.batches()) await read(batch);
    }).rejects.toBeInstanceOf(APIConnectionError);
    expect(feed.cursor).toBe('c1');

    // A malformed line caught inside the loop: the batch was not read to the
    // end, so asking for the next one throws instead of moving past it.
    const bad = mockFetch(
      new Response(`${JSON.stringify(CALL(1))}\nnot json\n${JSON.stringify(CALL(2))}\n`, {
        headers: { 'X-PS-Cursor': 'c2', 'X-PS-Has-More': 'true' },
      }),
    );
    const feed2 = client(bad.fetch).siem.callsFeed({ cursor: 'c0' });
    await expect(async () => {
      for await (const batch of feed2.batches()) {
        await read(batch).catch(() => undefined);
      }
    }).rejects.toThrow(/not read to the end/);
    expect(feed2.cursor).toBe('c0');
  });
});
