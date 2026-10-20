/**
 * Security events (schema v1): JSON lines behind PS_SECURITY_EVENTS_LOG and
 * the guardrail / PII / rate-limit / telemetry-drop metrics on /metrics.
 *
 * Drives the real gateway app, so guardrail verdicts from unified routes and
 * proxied calls (#206) are covered through the hooks manager's single
 * reporting point.
 */
jest.mock('jose', () => ({}));

import { readFileSync } from 'fs';
import { join } from 'path';
import { Hono } from 'hono';
import { json, post, resetHarness, upstream } from './harness';
import app from '../../index';
import {
  _resetSecurityEventsForTests,
  checkLabel,
  flushSecurityEvents,
  hookLabel,
  MAX_HOOK_LABELS,
  piiCategories,
  recordHookResults,
} from '../../middlewares/psSecurityEvents';
import { HookType } from '../../middlewares/hooks/types';
import { OtlpJsonExporter } from '../../middlewares/psOtel';
import {
  _setSecurityLogsExporterForTests,
  OtlpLogsExporter,
} from '../../middlewares/psSecurityEvents/otlp';
import { adminAuthMiddleware } from '../../middlewares/adminAuth';

const CHAT_URL = /api\.openai\.com\/v1\/chat\/completions$/;
const SECRET_PROMPT = 'tell me the SECRET launch code 4111-1111-1111-1111';
const PII_VALUE = 'jane.doe@example.com';

const chatOk = (text = 'ok') =>
  json({
    id: 'chatcmpl-s',
    object: 'chat.completion',
    model: 'gpt-4o-mini',
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content: text },
        finish_reason: 'stop',
      },
    ],
    usage: { prompt_tokens: 3, completion_tokens: 1, total_tokens: 4 },
  });

const openai = (extra: Record<string, any> = {}) => ({
  'x-portkey-config': JSON.stringify({
    provider: 'openai',
    api_key: 'sk-regression',
    ...extra,
  }),
});
const blockWord = (word: string, deny: boolean, id?: string) => [
  {
    ...(id ? { id } : {}),
    'default.contains': { words: [word], operator: 'none' },
    deny,
  },
];
const chatBody = (text: string) => ({
  model: 'gpt-4o-mini',
  messages: [{ role: 'user', content: text }],
});

let written: string[] = [];
let writeSpy: jest.SpyInstance;

beforeEach(() => {
  resetHarness();
  _resetSecurityEventsForTests();
  written = [];
  writeSpy = jest
    .spyOn(process.stdout, 'write')
    .mockImplementation((chunk: any) => {
      written.push(String(chunk));
      return true;
    });
  process.env.PS_SECURITY_EVENTS_LOG = 'true';
});

afterEach(() => {
  writeSpy.mockRestore();
  delete process.env.PS_SECURITY_EVENTS_LOG;
  delete process.env.PS_SECURITY_EVENTS_TENANT;
  delete process.env.PS_METRICS_TOKEN;
  delete process.env.MAX_JSON_PAYLOAD_SIZE_IN_MB;
});

// docs/security-events.schema.json: every emitted event must conform
// (required fields, no undeclared fields, enum and const values).
const SCHEMA = JSON.parse(
  readFileSync(
    join(__dirname, '../../../../docs/security-events.schema.json'),
    'utf8'
  )
);
function expectConforms(event: any) {
  const def = SCHEMA.$defs[String(event['event.name']).replace('.', '_')];
  expect(def).toBeDefined();
  for (const key of def.required) expect(event).toHaveProperty([key]);
  for (const [key, value] of Object.entries(event)) {
    const prop = def.properties[key];
    if (!prop) throw new Error(`field ${key} is not in the schema`);
    if ('const' in prop) expect(value).toBe(prop.const);
    if (prop.enum) expect(prop.enum).toContain(value);
    if (prop.items?.enum)
      for (const v of value as any[]) expect(prop.items.enum).toContain(v);
    if (prop.type === 'string') expect(typeof value).toBe('string');
    if (prop.type === 'integer') expect(Number.isInteger(value)).toBe(true);
    if (prop.type === 'boolean') expect(typeof value).toBe('boolean');
    if (prop.maxLength)
      expect((value as string).length).toBeLessThanOrEqual(prop.maxLength);
    if (prop.pattern) expect(value).toMatch(new RegExp(prop.pattern));
  }
}

/** Security events written so far (flushes the queue first). */
async function securityEvents(): Promise<any[]> {
  await new Promise((r) => setImmediate(r));
  flushSecurityEvents();
  return written
    .join('')
    .split('\n')
    .filter(Boolean)
    .map((l) => {
      try {
        return JSON.parse(l);
      } catch {
        return undefined;
      }
    })
    .filter((e) => e?.['log.logger'] === 'ps.security')
    .map((e) => {
      expectConforms(e);
      return e;
    });
}

async function metrics(): Promise<string> {
  const res = await app.request('/metrics');
  return res.text();
}

const counter = (text: string, line: string): number => {
  const hit = text.split('\n').find((l) => l.startsWith(line + ' '));
  return hit ? Number(hit.slice(line.length + 1)) : 0;
};

describe('guardrail verdict events', () => {
  test('a denied input guardrail emits a deny event and metric', async () => {
    process.env.PS_SECURITY_EVENTS_TENANT = 'bank-a';
    const metric =
      'ps_gateway_guardrail_verdicts_total{hook="input_guardrail",check="default.contains",verdict="deny",stage="input"}';
    const before = counter(await metrics(), metric);
    upstream(CHAT_URL, () => chatOk());
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody(SECRET_PROMPT),
      {
        ...openai({ input_guardrails: blockWord('SECRET', true) }),
        'x-portkey-trace-id': 'trace-abc-123',
        'x-ps-business-unit': 'Retail',
        'x-ps-environment': 'production',
        'x-ps-owner': 'owner@bank.example',
      }
    );
    expect(res.status).toBe(446);

    const events = await securityEvents();
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      'ps.schema_version': '1.0.0',
      'event.name': 'guardrail.verdict',
      'event.kind': 'alert',
      'event.outcome': 'failure',
      action: 'blocked',
      severity: 'high',
      signature: 'default.contains',
      vendor_product: 'Prompt Shields Gateway',
      'ps.request_id': 'trace-abc-123',
      'http.route': '/v1/chat/completions',
      'http.request.method': 'POST',
      'gen_ai.provider.name': 'openai',
      'gen_ai.request.model': 'gpt-4o-mini',
      'ps.tenant': 'bank-a',
      'ps.business_unit': 'Retail',
      'ps.environment': 'production',
      'ps.guardrail.check_id': 'default.contains',
      'ps.guardrail.hook_type': 'guardrail',
      'ps.guardrail.stage': 'input',
      'ps.guardrail.verdict': 'deny',
      'ps.guardrail.deny': true,
    });
    expect(events[0]['ps.guardrail.hook_id']).toMatch(/^input_guardrail_/);
    const raw = written.join('');
    expect(raw).not.toContain('SECRET');
    expect(raw).not.toContain('4111');
    expect(raw).not.toContain('owner@bank.example');
    expect(raw).not.toContain('sk-regression');

    expect(counter(await metrics(), metric)).toBe(before + 1);
  });

  test('a failed output guardrail without deny is a flag', async () => {
    upstream(CHAT_URL, () => chatOk('a LEAK of data'));
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody('hi'),
      openai({ output_guardrails: blockWord('LEAK', false, 'leak-check') })
    );
    expect(res.status).toBe(246);
    const [event] = await securityEvents();
    expect(event).toMatchObject({
      'event.name': 'guardrail.verdict',
      'ps.guardrail.hook_id': 'leak-check',
      'ps.guardrail.stage': 'output',
      'ps.guardrail.verdict': 'flag',
      'ps.guardrail.deny': false,
      action: 'allowed',
      severity: 'medium',
    });
    expect(written.join('')).not.toContain('LEAK');
    expect(await metrics()).toContain(
      'ps_gateway_guardrail_verdicts_total{hook="leak-check",check="default.contains",verdict="flag",stage="output"}'
    );
  });

  test('a passing check is an informational allow event', async () => {
    upstream(CHAT_URL, () => chatOk());
    await post(
      app,
      '/v1/chat/completions',
      chatBody('hello'),
      openai({ input_guardrails: blockWord('SECRET', true) })
    );
    const [event] = await securityEvents();
    expect(event).toMatchObject({
      'ps.guardrail.verdict': 'allow',
      'event.kind': 'event',
      'event.outcome': 'success',
      severity: 'informational',
    });
  });

  test('a proxied model call (#206) reports through the same path', async () => {
    upstream(CHAT_URL, () => chatOk());
    const res = await post(
      app,
      '/v1/proxy/chat/completions',
      chatBody(SECRET_PROMPT),
      openai({ input_guardrails: blockWord('SECRET', true) })
    );
    expect(res.status).toBe(446);
    const [event] = await securityEvents();
    expect(event).toMatchObject({
      'ps.guardrail.verdict': 'deny',
      'http.route': '/v1/proxy/*',
    });
    expect(written.join('')).not.toContain('SECRET');
  });

  test('nothing is written when PS_SECURITY_EVENTS_LOG is unset', async () => {
    delete process.env.PS_SECURITY_EVENTS_LOG;
    upstream(CHAT_URL, () => chatOk());
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody(SECRET_PROMPT),
      openai({ input_guardrails: blockWord('SECRET', true) })
    );
    expect(res.status).toBe(446);
    expect(await securityEvents()).toEqual([]);
    expect(written.join('')).toBe('');
  });

  test('PS_SECURITY_EVENTS_LOG=stderr writes to stderr only', async () => {
    process.env.PS_SECURITY_EVENTS_LOG = 'stderr';
    const errLines: string[] = [];
    const errSpy = jest
      .spyOn(process.stderr, 'write')
      .mockImplementation((chunk: any) => {
        errLines.push(String(chunk));
        return true;
      });
    try {
      upstream(CHAT_URL, () => chatOk());
      await post(
        app,
        '/v1/chat/completions',
        chatBody('x'),
        openai({ input_guardrails: blockWord('SECRET', true) })
      );
      expect(await securityEvents()).toEqual([]);
      expect(errLines.join('')).toContain('"log.logger":"ps.security"');
    } finally {
      errSpy.mockRestore();
    }
  });
});

describe('PII and injection detections', () => {
  // A minimal Hono context for recordHookResults.
  async function record(results: any[], hooks: any[], extra = {}) {
    const mini = new Hono();
    mini.post('/v1/chat/completions', (c) => {
      recordHookResults(
        c,
        {
          provider: 'openai',
          requestType: 'chatComplete',
          request: {
            json: { model: 'gpt-4o', messages: [] },
            text: PII_VALUE,
            isStreamingRequest: false,
            isTransformed: false,
            headers: {},
          },
          response: {
            json: {},
            text: '',
            statusCode: null,
            isTransformed: false,
          },
          ...extra,
        },
        hooks,
        results,
        { portkey: { pii: () => {} }, azure: { shieldPrompt: () => {} } }
      );
      return c.text('ok');
    });
    await mini.request('/v1/chat/completions', { method: 'POST' });
  }

  const hook = (id: string, checkId: string, deny = true) => ({
    id,
    type: HookType.GUARDRAIL,
    eventType: 'beforeRequestHook' as const,
    deny,
    checks: [{ id: checkId, parameters: {} }],
  });
  const result = (checkId: string, verdict: boolean, data: any) => ({
    id: 'h',
    verdict,
    checks: [
      { id: checkId, verdict, data, execution_time: 3, created_at: new Date() },
    ],
  });

  test('categories and counts only, never values', async () => {
    const before = await metrics();
    const portkeyData = {
      explanation: `Found restricted PII in the text: EMAIL_ADDRESS`,
      detectedCategories: ['EMAIL_ADDRESS', 'PHONE_NUMBER'],
      textExcerpt: `contact ${PII_VALUE} or +46 70 123 45 67`,
    };
    await record(
      [result('portkey.pii', false, portkeyData)],
      [hook('pii-hook', 'portkey.pii')]
    );
    const azureData = [
      {
        redactedText: '***',
        entities: [
          { text: PII_VALUE, category: 'Email', offset: 0 },
          { text: 'other@example.com', category: 'Email', offset: 30 },
          { text: '19121212-1212', category: 'SE Personal Number' },
        ],
      },
    ];
    await record(
      [result('azure.pii', false, azureData)],
      [hook('azure-hook', 'azure.pii', false)]
    );

    const events = await securityEvents();
    const pii = events.filter((e) => e['event.name'] === 'pii.detected');
    expect(
      pii.map((e) => [e['ps.pii.category'], e['ps.pii.count']]).sort()
    ).toEqual([
      ['EMAIL_ADDRESS', 1],
      ['Email', 2],
      ['PHONE_NUMBER', 1],
      ['other', 1],
    ]);
    expect(pii[0]).toMatchObject({
      'ps.guardrail.check_id': 'portkey.pii',
      action: 'blocked',
      severity: 'high',
    });

    const raw = written.join('');
    for (const leak of [
      PII_VALUE,
      'other@example.com',
      '19121212',
      '+46 70',
      'textExcerpt',
      'redactedText',
      'Found restricted',
    ]) {
      expect(raw).not.toContain(leak);
    }

    const after = await metrics();
    for (const [category, n] of [
      ['EMAIL_ADDRESS', 1],
      ['Email', 2],
    ] as const) {
      const line = `ps_gateway_pii_detections_total{category="${category}"}`;
      expect(counter(after, line)).toBe(counter(before, line) + n);
    }
    expect(after).not.toContain(PII_VALUE);
  });

  test('an errored check blocks only with failOnError in a deny hook', async () => {
    const errored = (failOnError: boolean) => ({
      id: 'h',
      verdict: !failOnError,
      checks: [
        {
          id: 'default.webhook',
          verdict: false,
          data: null,
          error: { name: 'Error', message: 'boom' },
          fail_on_error: failOnError,
          execution_time: 3,
          created_at: new Date(),
        },
      ],
    });
    await record([errored(true)], [hook('fail-closed', 'default.webhook')]);
    await record([errored(false)], [hook('fail-open', 'default.webhook')]);

    const events = await securityEvents();
    const byHook = (id: string) =>
      events.find((e) => e['ps.guardrail.hook_id'] === id);
    expect(byHook('fail-closed')).toMatchObject({
      'ps.guardrail.verdict': 'error',
      'ps.guardrail.deny': true,
      action: 'blocked',
      severity: 'high',
    });
    expect(byHook('fail-open')).toMatchObject({
      'ps.guardrail.verdict': 'error',
      'ps.guardrail.deny': false,
      action: 'allowed',
      severity: 'low',
    });
    expect(written.join('')).not.toContain('boom');
  });

  test('a failed PII check without categories counts as unspecified', async () => {
    await record(
      [result('portkey.pii', false, null)],
      [hook('p', 'portkey.pii')]
    );
    const [, pii] = await securityEvents();
    expect(pii).toMatchObject({
      'event.name': 'pii.detected',
      'ps.pii.category': 'unspecified',
      'ps.pii.count': 1,
    });
  });

  test('prompt injection detections are their own event', async () => {
    await record(
      [result('azure.shieldPrompt', false, { attackDetected: true })],
      [hook('inj', 'azure.shieldPrompt')]
    );
    const events = await securityEvents();
    expect(events.map((e) => e['event.name'])).toEqual([
      'guardrail.verdict',
      'injection.detected',
    ]);
    expect(events[1]).toMatchObject({
      'ps.guardrail.check_id': 'azure.shieldPrompt',
      severity: 'high',
      action: 'blocked',
      'gen_ai.request.model': 'gpt-4o',
    });
  });

  test('passing checks emit no detection', async () => {
    await record(
      [result('azure.shieldPrompt', true, null)],
      [hook('inj', 'azure.shieldPrompt')]
    );
    const events = await securityEvents();
    expect(events.map((e) => e['event.name'])).toEqual(['guardrail.verdict']);
  });

  test('piiCategories ignores values and odd category strings', () => {
    const counts = piiCategories({
      payload: {
        pii: [
          { entity_type: 'email', pii: PII_VALUE },
          { entity_type: `bad value ${PII_VALUE}` },
        ],
      },
    });
    expect([...counts]).toEqual([
      ['email', 1],
      ['other', 1],
    ]);
  });
});

describe('gateway refusals', () => {
  test('a /metrics auth failure is an auth.failure event', async () => {
    process.env.PS_METRICS_TOKEN = 'metrics-token-value';
    const res = await app.request('/metrics', {
      headers: { authorization: 'Bearer wrong-token-value' },
    });
    expect(res.status).toBe(401);
    const missing = await app.request('/metrics');
    expect(missing.status).toBe(401);
    const events = await securityEvents();
    expect(events).toHaveLength(2);
    expect(events[0]).toMatchObject({
      'event.name': 'auth.failure',
      'event.category': ['authentication'],
      action: 'failure',
      'ps.auth.realm': 'metrics',
      'ps.auth.reason': 'invalid_credentials',
      'http.response.status_code': 401,
    });
    expect(events[1]['ps.auth.reason']).toBe('missing_credentials');
    const raw = written.join('');
    expect(raw).not.toContain('wrong-token-value');
    expect(raw).not.toContain('metrics-token-value');
  });

  test('an admin auth failure is an auth.failure event', async () => {
    process.env.PS_GATEWAY_ADMIN_TOKEN = 'admin-token-0123456789';
    try {
      const admin = new Hono();
      admin.get('/log/stream', adminAuthMiddleware, (c) => c.text('ok'));
      const res = await admin.request('/log/stream', {
        headers: { authorization: 'Bearer not-the-admin-token' },
      });
      expect(res.status).toBe(401);
    } finally {
      delete process.env.PS_GATEWAY_ADMIN_TOKEN;
    }
    const [event] = await securityEvents();
    expect(event).toMatchObject({
      'event.name': 'auth.failure',
      'ps.auth.realm': 'admin',
      'ps.auth.reason': 'invalid_credentials',
      'http.route': '/log/stream',
    });
    expect(written.join('')).not.toContain('not-the-admin-token');
  });

  test('an upstream 429 is a rate_limit.hit event and metric', async () => {
    const line = 'ps_gateway_rate_limited_total{source="upstream"}';
    const before = counter(await metrics(), line);
    upstream(CHAT_URL, () =>
      json({ error: { message: 'Rate limit reached' } }, 429)
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody('hi'),
      openai()
    );
    expect(res.status).toBe(429);
    const [event] = await securityEvents();
    expect(event).toMatchObject({
      'event.name': 'rate_limit.hit',
      'ps.rate_limit.source': 'upstream',
      'gen_ai.provider.name': 'openai',
      'http.response.status_code': 429,
    });
    expect(counter(await metrics(), line)).toBe(before + 1);
  });

  test('an oversized body is a payload.rejected event', async () => {
    process.env.MAX_JSON_PAYLOAD_SIZE_IN_MB = '0.0001';
    const body = chatBody('x'.repeat(500));
    const res = await post(app, '/v1/chat/completions', body, {
      ...openai(),
      'content-length': String(JSON.stringify(body).length),
    });
    expect(res.status).toBe(413);
    const [event] = await securityEvents();
    expect(event).toMatchObject({
      'event.name': 'payload.rejected',
      'ps.payload.reason': 'payload_too_large',
      'http.response.status_code': 413,
    });
    expect(event['http.request.body.size']).toBeGreaterThan(500);
    expect(written.join('')).not.toContain('xxxx');
  });

  test('a proxied request guardrails cannot inspect is payload.rejected', async () => {
    const res = await post(
      app,
      '/v1/proxy/audio/speech',
      { model: 'tts-1', input: SECRET_PROMPT },
      openai({ input_guardrails: blockWord('SECRET', true) })
    );
    expect(res.status).toBe(400);
    const [event] = await securityEvents();
    expect(event).toMatchObject({
      'event.name': 'payload.rejected',
      'ps.payload.reason': 'unsupported_guardrail',
      'http.response.status_code': 400,
    });
    expect(written.join('')).not.toContain('SECRET');
  });
});

describe('metric label bounds and telemetry drops', () => {
  test('unknown check ids and hook ids past the cap are "other"', () => {
    const registry = { default: { contains: () => {} } };
    expect(checkLabel('default.contains', registry)).toBe('default.contains');
    expect(checkLabel('default.toString', registry)).toBe('other');
    expect(checkLabel('constructor.name', registry)).toBe('other');
    expect(checkLabel('custom.check.x', registry)).toBe('other');
    expect(hookLabel('input_guardrail_k3x')).toBe('input_guardrail');
    expect(hookLabel('bad id with spaces')).toBe('other');
    for (let i = 0; i < MAX_HOOK_LABELS; i++)
      expect(hookLabel(`hook-${i}`)).toBe(`hook-${i}`);
    expect(hookLabel('one-too-many')).toBe('other');
    expect(hookLabel('hook-0')).toBe('hook-0');
  });

  test('failed and overflowing OTel batches are counted', async () => {
    const line = (reason: string) =>
      `ps_gateway_telemetry_dropped_total{sink="otel",reason="${reason}"}`;
    const before = await metrics();
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
    const exporter = new OtlpJsonExporter(
      {
        endpoint: 'http://otel.invalid/v1/traces',
        headers: {},
        serviceName: 'test',
        captureContent: false,
        maxBatch: 10,
        maxQueue: 2,
        flushMs: 60_000,
      } as any,
      (async () => new Response('', { status: 503 })) as any
    );
    const span = { span: {}, environment: undefined } as any;
    exporter.enqueue(span);
    exporter.enqueue(span);
    exporter.enqueue(span); // overflow: oldest dropped
    await exporter.flush(); // 503: two dropped
    const failing = new OtlpJsonExporter(
      { ...(exporter as any).config },
      (async () => {
        throw new Error('ECONNREFUSED');
      }) as any
    );
    failing.enqueue(span);
    await failing.flush();
    warn.mockRestore();
    const after = await metrics();
    expect(counter(after, line('queue_full'))).toBe(
      counter(before, line('queue_full')) + 1
    );
    expect(counter(after, line('http_error'))).toBe(
      counter(before, line('http_error')) + 2
    );
    expect(counter(after, line('send_failed'))).toBe(
      counter(before, line('send_failed')) + 1
    );
  });
});

describe('OTLP log records (PS_SECURITY_EVENTS_OTLP)', () => {
  test('events reach /v1/logs without the stdout sink, content-free', async () => {
    delete process.env.PS_SECURITY_EVENTS_LOG;
    const sent: any[] = [];
    const exporter = new OtlpLogsExporter(
      {
        endpoint: 'http://otel-logs.test/v1/logs',
        headers: {},
        serviceName: 'gw',
        maxBatch: 100,
        flushMs: 60_000,
        maxQueue: 100,
      },
      async (_url, init) => {
        sent.push(JSON.parse(String(init.body)));
        return new Response('{}', { status: 200 });
      }
    );
    _setSecurityLogsExporterForTests(exporter);

    upstream(CHAT_URL, () => chatOk());
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody(SECRET_PROMPT),
      {
        ...openai({ input_guardrails: blockWord('SECRET', true) }),
        'x-ps-owner': 'owner@bank.example',
      }
    );
    expect(res.status).toBe(446);
    expect(await securityEvents()).toHaveLength(0);

    await exporter.flush();
    expect(sent).toHaveLength(1);
    const records = sent[0].resourceLogs[0].scopeLogs[0].logRecords;
    expect(records).toHaveLength(1);
    expect(records[0]).toMatchObject({
      eventName: 'guardrail.verdict',
      severityText: 'high',
      severityNumber: 17,
    });
    expectConforms(JSON.parse(records[0].body.stringValue));
    const raw = JSON.stringify(sent);
    expect(raw).not.toContain('SECRET');
    expect(raw).not.toContain('owner@bank.example');
    expect(raw).not.toContain('sk-regression');
  });
});
