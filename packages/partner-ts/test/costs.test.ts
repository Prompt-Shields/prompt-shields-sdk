/**
 * Spend anomaly alerts, the FOCUS cost export (streamed) and risk mapping
 * reviews, against a mocked fetch.
 */
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import {
  APIConnectionError,
  BadRequestError,
  Download,
  FocusExport,
  InternalServerError,
  NotFoundError,
  PartnerClient,
  PartnerError,
} from '../src';
import type { FocusCostRow, RiskReviewResult, SpendAlert } from '../src';
import { json, mockFetch, noSleep, token } from './helpers';

const client = (fetch: ReturnType<typeof mockFetch>['fetch']) =>
  new PartnerClient({ apiKey: 'k', fetch, sleep: noSleep });

const ALERT_WIRE = {
  id: '019e1a2b-0000-7000-8000-00000000a1e7',
  group_by: 'use_case',
  group_key: 'interview-screening',
  day: '2026-11-02',
  observed_cost: 412.5,
  baseline_cost: 80.1,
  baseline_sigma: 21.4,
  z_score: 15.53,
  threshold_sigma: 3,
  history_days: 28,
  severity: 'critical',
  status: 'open',
  acknowledged_at: null,
  acknowledged_by: null,
  created_at: '2026-11-03T02:00:04Z',
};

const ALERT: SpendAlert = {
  id: '019e1a2b-0000-7000-8000-00000000a1e7',
  groupBy: 'use_case',
  groupKey: 'interview-screening',
  day: '2026-11-02',
  observedCost: 412.5,
  baselineCost: 80.1,
  baselineSigma: 21.4,
  zScore: 15.53,
  thresholdSigma: 3,
  historyDays: 28,
  severity: 'critical',
  status: 'open',
  acknowledgedAt: null,
  acknowledgedBy: null,
  createdAt: '2026-11-03T02:00:04Z',
};

describe('client.spendAlerts', () => {
  it('lists alerts with filters sent as snake_case query parameters', async () => {
    const m = mockFetch(json({ data: [ALERT_WIRE], meta: { total: 1 } }));
    const res = await client(m.fetch).spendAlerts.list({
      status: 'open',
      severity: 'critical',
      since: '2026-10-01',
      until: '2026-11-05',
      limit: 500,
    });
    expect(res).toEqual({ data: [ALERT], meta: { total: 1 } });
    const url = new URL(m.calls[0].url);
    expect(url.pathname).toBe('/partner/v1/spend-alerts');
    expect(Object.fromEntries(url.searchParams)).toEqual({
      status: 'open',
      severity: 'critical',
      since: '2026-10-01',
      until: '2026-11-05',
      limit: '500',
    });
    expect(m.calls[0].method).toBe('GET');
  });

  it('gets one alert, unwrapping `data`', async () => {
    const m = mockFetch(json({ data: ALERT_WIRE }));
    expect(await client(m.fetch).spendAlerts.get(ALERT.id)).toEqual(ALERT);
    expect(new URL(m.calls[0].url).pathname).toBe(`/partner/v1/spend-alerts/${ALERT.id}`);
  });

  it('throws NotFoundError for an unknown alert', async () => {
    const m = mockFetch(json({ title: 'Not Found', detail: 'Spend alert not found', status: 404 }, 404));
    await expect(client(m.fetch).spendAlerts.get('nope')).rejects.toBeInstanceOf(NotFoundError);
  });

  it('acknowledges an alert with a bodyless POST', async () => {
    const acked = {
      ...ALERT_WIRE,
      status: 'acknowledged',
      acknowledged_at: '2026-11-03T08:00:00Z',
      acknowledged_by: 'partner:p1',
    };
    const m = mockFetch(json({ data: acked }));
    const res = await client(m.fetch).spendAlerts.acknowledge(ALERT.id);
    expect(res).toMatchObject({ status: 'acknowledged', acknowledgedBy: 'partner:p1' });
    expect(m.calls[0].method).toBe('POST');
    expect(m.calls[0].body).toBeUndefined();
    expect(new URL(m.calls[0].url).pathname).toBe(`/partner/v1/spend-alerts/${ALERT.id}/acknowledge`);
  });

  it('retries acknowledge after a 503 and a network error, since it is idempotent', async () => {
    const m = mockFetch(
      json({ title: 'Unavailable' }, 503),
      new TypeError('fetch failed'),
      json({ data: { ...ALERT_WIRE, status: 'acknowledged' } }),
    );
    const c = new PartnerClient({ apiKey: 'k', fetch: m.fetch, sleep: noSleep, maxRetries: 2 });
    await expect(c.spendAlerts.acknowledge(ALERT.id)).resolves.toMatchObject({ status: 'acknowledged' });
    expect(m.calls).toHaveLength(3);
  });

  it('rejects an empty alert id before sending anything', async () => {
    const m = mockFetch();
    await expect(client(m.fetch).spendAlerts.get('')).rejects.toThrow(PartnerError);
    await expect(client(m.fetch).spendAlerts.acknowledge(' ')).rejects.toThrow(PartnerError);
    expect(m.calls).toHaveLength(0);
  });
});

/** A Response whose body arrives in the given chunks, recording how much was pulled. */
function streamed(
  chunks: Array<string | Uint8Array>,
  headers: Record<string, string> = {},
  opts: { failAfter?: number } = {},
) {
  const encoder = new TextEncoder();
  const state = { pulled: 0, cancelled: false };
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (opts.failAfter !== undefined && state.pulled >= opts.failAfter) {
        controller.error(new TypeError('terminated'));
        return;
      }
      if (state.pulled >= chunks.length) {
        controller.close();
        return;
      }
      const c = chunks[state.pulled++];
      controller.enqueue(typeof c === 'string' ? encoder.encode(c) : c);
    },
    cancel() {
      state.cancelled = true;
    },
  });
  const response = new Response(body, { status: 200, headers });
  return { response, state };
}

const FOCUS_HEADERS = {
  'content-type': 'application/x-ndjson',
  'content-disposition': 'attachment; filename="focus-cost-20261001T000000Z-20261101T000000Z.jsonl"',
  'x-focus-version': '1.4',
  'x-export-window-start': '2026-10-01T00:00:00Z',
  'x-export-window-end': '2026-11-01T00:00:00Z',
};

const ROW = (n: number): Partial<FocusCostRow> => ({
  BilledCost: 0.03 * n,
  BillingCurrency: 'USD',
  ChargePeriodStart: `2026-10-0${n}T00:00:00Z`,
  ServiceProviderName: 'OpenAI',
  x_EstimatedCost: 0.03 * n,
  x_RequestCount: n,
  x_UseCaseName: 'interview-screening',
  Tags: { 'ps:use_case': 'interview-screening' },
});

describe('client.usage.focusExport', () => {
  it('sends the query parameters, asks for JSON Lines and reads the export headers', async () => {
    const { response } = streamed([], FOCUS_HEADERS);
    const m = mockFetch(response);
    const exp = await client(m.fetch).usage.focusExport({
      start: new Date('2026-10-01T00:00:00Z'),
      end: '2026-11-01',
      format: 'jsonl',
      granularity: 'hour',
      costMode: 'merge_safe',
    });
    const url = new URL(m.calls[0].url);
    expect(url.pathname).toBe('/partner/v1/usage/focus-export');
    expect(Object.fromEntries(url.searchParams)).toEqual({
      start: '2026-10-01T00:00:00.000Z',
      end: '2026-11-01',
      format: 'jsonl',
      granularity: 'hour',
      cost_mode: 'merge_safe',
    });
    expect(m.calls[0].headers.Accept).toBe('application/x-ndjson');
    expect(m.calls[0].headers.Authorization).toBe('Bearer k');
    expect(exp).toBeInstanceOf(FocusExport);
    expect(exp).toMatchObject({
      format: 'jsonl',
      focusVersion: '1.4',
      windowStart: '2026-10-01T00:00:00Z',
      windowEnd: '2026-11-01T00:00:00Z',
      filename: 'focus-cost-20261001T000000Z-20261101T000000Z.jsonl',
      contentType: 'application/x-ndjson',
    });
  });

  it('defaults to CSV', async () => {
    const m = mockFetch(streamed(['a,b\n'], { 'content-type': 'text/csv' }).response);
    const exp = await client(m.fetch).usage.focusExport();
    expect(exp.format).toBe('csv');
    expect(m.calls[0].headers.Accept).toBe('text/csv');
    expect(new URL(m.calls[0].url).search).toBe('');
  });

  it('resolves before reading the body, then streams it chunk by chunk', async () => {
    const { response, state } = streamed(['one\n', 'two\n', 'three\n'], FOCUS_HEADERS);
    const exp = await client(mockFetch(response).fetch).usage.focusExport({ format: 'jsonl' });
    expect(state.pulled).toBeLessThanOrEqual(1); // at most the stream's first pre-pull
    const seen: string[] = [];
    for await (const chunk of exp) {
      seen.push(new TextDecoder().decode(chunk));
      // Never more than one chunk ahead of the consumer.
      expect(state.pulled - seen.length).toBeLessThanOrEqual(1);
    }
    expect(seen.join('')).toBe('one\ntwo\nthree\n');
  });

  it('parses JSON Lines rows split across chunks, keeping FOCUS column names', async () => {
    const lines = [ROW(1), ROW(2), ROW(3)].map((r) => JSON.stringify(r)).join('\r\n') + '\n';
    // Split mid-row and inside a multi-byte character boundary.
    const bytes = new TextEncoder().encode(lines);
    const parts = [bytes.slice(0, 7), bytes.slice(7, 120), bytes.slice(120)];
    const exp = await client(mockFetch(streamed(parts, FOCUS_HEADERS).response).fetch).usage.focusExport({
      format: 'jsonl',
    });
    const rows: FocusCostRow[] = [];
    for await (const row of exp.rows()) rows.push(row);
    expect(rows).toEqual([ROW(1), ROW(2), ROW(3)]);
    expect(rows[0].x_EstimatedCost).toBe(0.03);
    expect(rows[0].Tags).toEqual({ 'ps:use_case': 'interview-screening' });
  });

  it('decodes multi-byte UTF-8 split across chunks', async () => {
    const bytes = new TextEncoder().encode('{"x_UseCaseName":"Lönebesked"}\n');
    const cut = bytes.indexOf(0xc3) + 1; // between the two bytes of "ö"
    const exp = await client(
      mockFetch(streamed([bytes.slice(0, cut), bytes.slice(cut)], FOCUS_HEADERS).response).fetch,
    ).usage.focusExport({ format: 'jsonl' });
    const rows = [];
    for await (const row of exp.rows()) rows.push(row);
    expect(rows).toEqual([{ x_UseCaseName: 'Lönebesked' }]);
  });

  it('rows() refuses a CSV export; text() returns the whole body', async () => {
    const csv = 'BilledCost,BillingCurrency\n0.030000,USD\n';
    const m = mockFetch(streamed([csv], { 'content-type': 'text/csv' }).response);
    const exp = await client(m.fetch).usage.focusExport({ format: 'csv' });
    await expect(exp.rows().next()).rejects.toThrow(/format: 'jsonl'/);
    expect(await exp.text()).toBe(csv);
  });

  it('Download.lines() yields lines without endings and keeps a last line without a newline', async () => {
    const exp = new Download(streamed(['a,b\r\n1,', '2\n\n3,4'], {}).response);
    const lines: string[] = [];
    for await (const line of exp.lines()) lines.push(line);
    expect(lines).toEqual(['a,b', '1,2', '3,4']);
  });

  it('reports a malformed JSON line with its number', async () => {
    const exp = await client(mockFetch(streamed(['{"a":1}\n{oops\n'], FOCUS_HEADERS).response).fetch).usage.focusExport(
      { format: 'jsonl' },
    );
    const it = exp.rows();
    await expect(it.next()).resolves.toEqual({ value: { a: 1 }, done: false });
    await expect(it.next()).rejects.toThrow('FOCUS export line 2 is not JSON: {oops');
  });

  it('reads the body only once', async () => {
    const exp = await client(mockFetch(streamed(['x\n'], {}).response).fetch).usage.focusExport();
    expect(await exp.text()).toBe('x\n');
    expect(exp.bodyUsed).toBe(true);
    await expect(exp.text()).rejects.toThrow('already been read');
  });

  it('cancels the download when the consumer stops early', async () => {
    const { response, state } = streamed(['1\n', '2\n', '3\n', '4\n'], {});
    const exp = await client(mockFetch(response).fetch).usage.focusExport();
    for await (const line of exp.lines()) {
      expect(line).toBe('1');
      break;
    }
    expect(state.cancelled).toBe(true);
    expect(state.pulled).toBeLessThan(4);
  });

  it('cancel() stops an unread download', async () => {
    const { response, state } = streamed(['1\n'], {});
    const exp = await client(mockFetch(response).fetch).usage.focusExport();
    await exp.cancel();
    expect(state.cancelled).toBe(true);
    expect(exp.bodyUsed).toBe(true);
  });

  it('throws BadRequestError for a bad window, with the problem detail', async () => {
    const m = mockFetch(
      json({ title: 'Bad Request', detail: 'The window may span at most 366 days', status: 400 }, 400),
    );
    const err = await client(m.fetch)
      .usage.focusExport({ start: '2025-01-01', end: '2026-11-01' })
      .catch((e) => e);
    expect(err).toBeInstanceOf(BadRequestError);
    expect(err.detail).toBe('The window may span at most 366 days');
  });

  it('retries a 503 before the body starts', async () => {
    const m = mockFetch(json({ title: 'Unavailable' }, 503), streamed(['ok\n'], {}).response);
    const exp = await client(m.fetch).usage.focusExport();
    expect(await exp.text()).toBe('ok\n');
    expect(m.calls).toHaveLength(2);
  });

  it('gives up with the API error after maxRetries', async () => {
    const m = mockFetch(json({ title: 'Boom' }, 502), json({ title: 'Boom' }, 502));
    const c = new PartnerClient({ apiKey: 'k', fetch: m.fetch, sleep: noSleep, maxRetries: 1 });
    await expect(c.usage.focusExport()).rejects.toBeInstanceOf(InternalServerError);
  });

  it('fetches an OAuth token first and refreshes it on a 401', async () => {
    const m = mockFetch(
      token('t1'),
      json({ title: 'Unauthorized' }, 401),
      token('t2'),
      streamed(['ok\n'], {}).response,
    );
    const c = new PartnerClient({ clientId: 'id', clientSecret: 's', fetch: m.fetch, sleep: noSleep });
    const exp = await c.usage.focusExport();
    expect(await exp.text()).toBe('ok\n');
    expect(m.calls.map((x) => x.headers.Authorization)).toEqual([undefined, 'Bearer t1', undefined, 'Bearer t2']);
  });

  it('does not time out while the caller reads a slow body', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => (release = r));
    const body = new ReadableStream<Uint8Array>({
      async pull(controller) {
        await gate;
        controller.enqueue(new TextEncoder().encode('late\n'));
        controller.close();
      },
    });
    let signal: AbortSignal | undefined;
    const fetch = async (_url: string, init: RequestInit) => {
      signal = init.signal ?? undefined;
      return new Response(body, { status: 200 });
    };
    const c = new PartnerClient({ apiKey: 'k', fetch, timeoutMs: 20 });
    const exp = await c.usage.focusExport();
    const text = exp.text();
    await new Promise((r) => setTimeout(r, 60));
    expect(signal?.aborted).toBe(false);
    release();
    expect(await text).toBe('late\n');
  });

  it('times out while waiting for the response headers', async () => {
    const fetch = (_url: string, init: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new Error('aborted')));
      });
    const c = new PartnerClient({ apiKey: 'k', fetch, timeoutMs: 10, maxRetries: 0 });
    await expect(c.usage.focusExport()).rejects.toThrow(/timed out after 10ms/);
  });

  it("passes the caller's signal through and does not retry an abort", async () => {
    const controller = new AbortController();
    let calls = 0;
    let started: () => void = () => undefined;
    const inFlight = new Promise<void>((r) => (started = r));
    const fetch = (_url: string, init: RequestInit) => {
      calls++;
      started();
      return new Promise<Response>((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new Error('aborted')));
      });
    };
    const c = new PartnerClient({ apiKey: 'k', fetch, sleep: noSleep });
    const pending = c.usage.focusExport({}, { signal: controller.signal });
    await inFlight;
    controller.abort();
    const err = await pending.catch((e) => e);
    expect(err).toBeInstanceOf(APIConnectionError);
    expect(err.message).toMatch(/was aborted/);
    expect(calls).toBe(1);
  });

  it('does not send a request whose signal is already aborted', async () => {
    const m = mockFetch();
    const controller = new AbortController();
    controller.abort();
    await expect(client(m.fetch).usage.focusExport({}, { signal: controller.signal })).rejects.toThrow(
      /was aborted/,
    );
    expect(m.calls).toHaveLength(0);
  });

  it("the caller's signal also aborts the request once the body is streaming", async () => {
    const controller = new AbortController();
    let signal: AbortSignal | undefined;
    const fetch = async (_url: string, init: RequestInit) => {
      signal = init.signal ?? undefined;
      return streamed(['1\n'], {}).response;
    };
    const c = new PartnerClient({ apiKey: 'k', fetch });
    await c.usage.focusExport({}, { signal: controller.signal });
    expect(signal?.aborted).toBe(false);
    controller.abort();
    expect(signal?.aborted).toBe(true);
  });

  describe('saveTo()', () => {
    let dir: string;
    beforeEach(() => {
      dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ps-focus-'));
    });
    afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

    it('streams the body into a file and returns the bytes written', async () => {
      const chunks = Array.from({ length: 50 }, (_, i) => `row-${i},0.030000,USD\n`);
      const exp = await client(mockFetch(streamed(chunks, {}).response).fetch).usage.focusExport();
      const file = path.join(dir, 'focus.csv');
      const bytes = await exp.saveTo(file);
      expect(fs.readFileSync(file, 'utf8')).toBe(chunks.join(''));
      expect(bytes).toBe(Buffer.byteLength(chunks.join('')));
    });

    it('removes the partial file and throws when the download breaks off', async () => {
      const exp = await client(
        mockFetch(streamed(['a\n', 'b\n', 'c\n'], {}, { failAfter: 2 }).response).fetch,
      ).usage.focusExport();
      const file = path.join(dir, 'focus.csv');
      const err = await exp.saveTo(file).catch((e) => e);
      expect(err).toBeInstanceOf(APIConnectionError);
      expect(err.message).toMatch(/terminated/);
      expect(fs.existsSync(file)).toBe(false);
      expect(fs.readdirSync(dir)).toEqual([]);
    });

    it('removes the partial file when the body fails before the file is open', async () => {
      const exp = await client(
        mockFetch(streamed(['a\n'], {}, { failAfter: 0 }).response).fetch,
      ).usage.focusExport();
      const file = path.join(dir, 'focus.csv');
      const err = await exp.saveTo(file).catch((e) => e);
      expect(err).toBeInstanceOf(APIConnectionError);
      expect(fs.readdirSync(dir)).toEqual([]);
    });

    it('leaves an existing file untouched when the download fails', async () => {
      const file = path.join(dir, 'focus.csv');
      fs.writeFileSync(file, 'previous\n');
      const exp = await client(
        mockFetch(streamed(['a\n', 'b\n', 'c\n'], {}, { failAfter: 2 }).response).fetch,
      ).usage.focusExport();
      await expect(exp.saveTo(file)).rejects.toThrow(APIConnectionError);
      expect(fs.readFileSync(file, 'utf8')).toBe('previous\n');
      expect(fs.readdirSync(dir)).toEqual(['focus.csv']);
    });

    it('replaces a symlink at the path instead of writing through it', async () => {
      const target = path.join(dir, 'target.txt');
      fs.writeFileSync(target, 'keep\n');
      const file = path.join(dir, 'focus.csv');
      fs.symlinkSync(target, file);
      const exp = await client(mockFetch(streamed(['a\n'], {}).response).fetch).usage.focusExport();
      await exp.saveTo(file);
      expect(fs.readFileSync(target, 'utf8')).toBe('keep\n');
      expect(fs.lstatSync(file).isSymbolicLink()).toBe(false);
      expect(fs.readFileSync(file, 'utf8')).toBe('a\n');
    });
  });

  it.each([
    ['../../etc/passwd', 'passwd'],
    ['..\\..\\evil.csv', 'evil.csv'],
    ['/abs/x.jsonl', 'x.jsonl'],
    ['..', null],
    ['dir/', null],
  ])('reduces a server file name %j to a bare name', (raw, expected) => {
    const d = new Download(new Response('', { headers: { 'content-disposition': `attachment; filename="${raw}"` } }));
    expect(d.filename).toBe(expected);
  });

  describe('truncation and aborts while streaming', () => {
    it('throws APIConnectionError when the body ends mid-line', async () => {
      const exp = await client(
        mockFetch(streamed(['{"BilledCost":1}\n', '{"BilledC'], {}).response).fetch,
      ).usage.focusExport({ format: 'jsonl' });
      const rows: unknown[] = [];
      const err = await (async () => {
        for await (const row of exp.rows()) rows.push(row);
      })().catch((e) => e);
      expect(rows).toHaveLength(1);
      expect(err).toBeInstanceOf(APIConnectionError);
      expect(err.message).toMatch(/truncated/);
    });

    it('accepts an empty body', async () => {
      const exp = await client(mockFetch(streamed([], {}).response).fetch).usage.focusExport();
      expect(await exp.text()).toBe('');
    });

    it("maps the caller's abort during the body read to APIConnectionError", async () => {
      const controller = new AbortController();
      const fetch = async (_url: string, init: RequestInit) => {
        const body = new ReadableStream<Uint8Array>({
          start(c) {
            c.enqueue(new TextEncoder().encode('a\n'));
            init.signal?.addEventListener('abort', () =>
              c.error(Object.assign(new Error('This operation was aborted'), { name: 'AbortError' })),
            );
          },
        });
        return new Response(body, { status: 200 });
      };
      const exp = await new PartnerClient({ apiKey: 'k', fetch }).usage.focusExport(
        {},
        { signal: controller.signal },
      );
      const it = exp.chunks();
      await it.next();
      controller.abort();
      const err = await it.next().catch((e) => e);
      expect(err).toBeInstanceOf(APIConnectionError);
      expect(err.message).toMatch(/aborted/);
    });
  });
});

const MAPPING_WIRE = {
  id: 'r1',
  risk_category: 'compliance',
  risk_level: 'high',
  framework: 'EU_AI_ACT',
  status: 'confirmed',
  source: 'rules',
  rule_id: 'eu-ai-act-annex-iii-4a',
  ruleset_version: '2026.10',
  framework_ref: 'Annex III, point 4(a)',
  reviewed_at: '2026-11-04T10:00:00Z',
  reviewed_by: 'dpo@acme.com',
  review_comment: 'Recruitment screening',
};

const REVIEW_WIRE = {
  id: 'rev1',
  risk_mapping_id: 'r1',
  asset_id: 'id1',
  status: 'confirmed',
  previous_status: 'suggested',
  comment: 'Recruitment screening',
  reviewed_by: 'dpo@acme.com',
  reviewed_at: '2026-11-04T10:00:00Z',
  actor_type: 'partner',
  partner_id: 'p1',
  mapping: { risk_category: 'compliance', risk_level: 'high', framework_ref: 'Annex III, point 4(a)' },
};

describe('risk mapping reviews', () => {
  it('assets.reviewRisk posts the review and camelCases the review and the current mapping', async () => {
    const m = mockFetch(json({ data: REVIEW_WIRE, meta: { current: MAPPING_WIRE } }, 201));
    const res: RiskReviewResult = await client(m.fetch).assets.reviewRisk('id1', 'r1', {
      status: 'confirmed',
      reviewedBy: 'dpo@acme.com',
      comment: 'Recruitment screening',
    });
    expect(m.calls[0].method).toBe('POST');
    expect(new URL(m.calls[0].url).pathname).toBe('/partner/v1/assets/id1/risks/r1/review');
    expect(JSON.parse(m.calls[0].body as string)).toEqual({
      status: 'confirmed',
      reviewed_by: 'dpo@acme.com',
      comment: 'Recruitment screening',
    });
    expect(res.data).toMatchObject({
      riskMappingId: 'r1',
      previousStatus: 'suggested',
      actorType: 'partner',
      mapping: { riskCategory: 'compliance', riskLevel: 'high', frameworkRef: 'Annex III, point 4(a)' },
    });
    expect(res.meta.current).toMatchObject({
      riskCategory: 'compliance',
      ruleId: 'eu-ai-act-annex-iii-4a',
      reviewComment: 'Recruitment screening',
    });
    expect(res.meta.current).not.toHaveProperty('risk_category');
  });

  it('assets.reviewRisk is not retried after a 5xx', async () => {
    const m = mockFetch(json({ title: 'Boom' }, 503));
    await expect(
      client(m.fetch).assets.reviewRisk('id1', 'r1', { status: 'rejected', reviewedBy: 'dpo@acme.com' }),
    ).rejects.toBeInstanceOf(InternalServerError);
    expect(m.calls).toHaveLength(1);
  });

  it('assets.riskReviews returns the history with meta.current camelCased, or null', async () => {
    const m = mockFetch(
      json({ data: [REVIEW_WIRE], meta: { total: 1, current: MAPPING_WIRE } }),
      json({ data: [], meta: { total: 0, current: null } }),
    );
    const c = client(m.fetch);
    const history = await c.assets.riskReviews('id1', 'r1', { limit: 10 });
    expect(new URL(m.calls[0].url).pathname).toBe('/partner/v1/assets/id1/risks/r1/reviews');
    expect(new URL(m.calls[0].url).searchParams.get('limit')).toBe('10');
    expect(history.meta.total).toBe(1);
    expect(history.meta.current).toMatchObject({ riskLevel: 'high', frameworkRef: 'Annex III, point 4(a)' });
    expect(history.data[0].reviewedBy).toBe('dpo@acme.com');
    const empty = await c.assets.riskReviews('id1', 'gone');
    expect(empty.meta.current).toBeNull();
  });

  it('requires both ids', async () => {
    const c = client(mockFetch().fetch);
    await expect(c.assets.riskReviews('id1', '')).rejects.toThrow(PartnerError);
    await expect(c.assets.reviewRisk('', 'r1', { status: 'confirmed', reviewedBy: 'x' })).rejects.toThrow(PartnerError);
  });
});

describe('client.teams and client.projects', () => {
  const TEAM_WIRE = {
    id: '019e1a2b-0000-7000-8000-0000000000t1',
    name: 'Payments',
    description: null,
    created_at: '2026-11-06T10:00:00Z',
    updated_at: null,
  };

  it('lists teams, unwrapping `data`', async () => {
    const m = mockFetch(json({ data: [TEAM_WIRE] }));
    expect(await client(m.fetch).teams.list()).toEqual([
      {
        id: TEAM_WIRE.id,
        name: 'Payments',
        description: null,
        createdAt: '2026-11-06T10:00:00Z',
        updatedAt: null,
      },
    ]);
    expect(new URL(m.calls[0].url).pathname).toBe('/partner/v1/teams');
  });

  it('lists projects filtered by team as a snake_case query parameter', async () => {
    const wire = { ...TEAM_WIRE, id: 'p1', name: 'checkout', team_id: TEAM_WIRE.id };
    const m = mockFetch(json({ data: [wire] }));
    const res = await client(m.fetch).projects.list({ teamId: TEAM_WIRE.id });
    expect(res[0].teamId).toBe(TEAM_WIRE.id);
    const url = new URL(m.calls[0].url);
    expect(url.pathname).toBe('/partner/v1/projects');
    expect(Object.fromEntries(url.searchParams)).toEqual({ team_id: TEAM_WIRE.id });
  });
});
