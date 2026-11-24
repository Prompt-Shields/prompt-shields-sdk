/**
 * Gateway spans for Langfuse and other OTLP backends (#386): cost from the
 * shared price table, session and user hash, the Langfuse preset, the
 * recorded-payload contract, and the /log/stream redaction of captured
 * content.
 */
import * as fs from 'fs';
import * as path from 'path';
import { Hono } from 'hono';
import {
  buildExportRequest,
  buildGenAISpan,
  OtelConfig,
  otelConfig,
  psOtel,
  sessionIdOf,
  userHashOf,
  userIdHash,
} from './psOtel';
import { costFields } from './psPricing';

const FIXTURE = path.resolve(
  __dirname,
  '../../../packages/sdk/tests/fixtures/otlp/langfuse_gateway_request.json'
);

const attrsOf = (span: any) =>
  Object.fromEntries(
    span.attributes.map((a: any) => [
      a.key,
      a.value.stringValue ??
        (a.value.intValue !== undefined
          ? Number(a.value.intValue)
          : undefined) ??
        a.value.doubleValue ??
        a.value.boolValue ??
        a.value.arrayValue?.values.map((v: any) => v.stringValue),
    ])
  );

const config = (over: Partial<OtelConfig> = {}): OtelConfig => ({
  endpoint: 'https://cloud.langfuse.com/api/public/otel/v1/traces',
  headers: {},
  serviceName: 'prompt-shields-gateway',
  captureContent: false,
  maxBatch: 2,
  flushMs: 60_000,
  maxQueue: 10,
  langfuse: true,
  ...over,
});

const chatReq = {
  model: 'gpt-4o',
  messages: [
    { role: 'system', content: 'You are terse.' },
    { role: 'user', content: 'Mail alice@example.com the Nightjar plan' },
  ],
};
const chatRes = {
  id: 'chatcmpl-1',
  model: 'gpt-4o-2024-08-06',
  choices: [
    {
      message: {
        role: 'assistant',
        content: 'Nightjar answer for bob@acme.io',
      },
      finish_reason: 'stop',
    },
  ],
  usage: { prompt_tokens: 120, completion_tokens: 30, total_tokens: 150 },
};
const headers = {
  'x-portkey-provider': 'openai',
  'x-ps-session-id': 'sess-42',
  'x-ps-environment': 'prod',
  traceparent: '00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01',
};

const USER_HASH = 'c160f8cc69a4f0bf2b0362752353d060' + '0'.repeat(32);

describe('otelConfig: Langfuse preset', () => {
  test('endpoint and Basic auth from the Langfuse keys', () => {
    const cfg = otelConfig({
      PS_OTEL_LANGFUSE: 'true',
      LANGFUSE_PUBLIC_KEY: 'pk-lf-1',
      LANGFUSE_SECRET_KEY: 'sk-lf-2',
      LANGFUSE_HOST: 'https://lf.example.com/',
    })!;
    expect(cfg.endpoint).toBe(
      'https://lf.example.com/api/public/otel/v1/traces'
    );
    expect(cfg.headers).toEqual({
      Authorization: `Basic ${Buffer.from('pk-lf-1:sk-lf-2').toString('base64')}`,
    });
    expect(cfg.langfuse).toBe(true);
  });

  test('an explicit endpoint wins and keeps its own headers', () => {
    const cfg = otelConfig({
      PS_OTEL_LANGFUSE: 'true',
      LANGFUSE_PUBLIC_KEY: 'pk',
      LANGFUSE_SECRET_KEY: 'sk',
      PS_OTEL_ENDPOINT: 'https://lf.example.com/api/public/otel/v1/traces',
      PS_OTEL_HEADERS: 'Authorization=Basic%20abc',
    })!;
    expect(cfg.headers).toEqual({ Authorization: 'Basic abc' });
  });

  test('off without PS_OTEL_LANGFUSE, and keys alone do not enable it', () => {
    expect(
      otelConfig({ LANGFUSE_PUBLIC_KEY: 'pk', LANGFUSE_SECRET_KEY: 'sk' })
    ).toBeNull();
    expect(otelConfig({ PS_OTEL_ENDPOINT: 'http://c' })!.langfuse).toBe(false);
  });
});

describe('cost, session and user on gateway spans', () => {
  test('cost is the collector cost from the shared price table', () => {
    const built = buildGenAISpan({
      path: '/v1/chat/completions',
      headers,
      status: 200,
      startMs: 1000,
      endMs: 1850,
      requestBody: chatReq,
      responseBody: chatRes,
    })!;
    const a = attrsOf(built.span);
    const { cost } = costFields('openai', 'gpt-4o', 'gpt-4o-2024-08-06', {
      tokens_in: 120,
      tokens_out: 30,
    });
    expect(cost).toBeGreaterThan(0);
    expect(a['gen_ai.usage.cost']).toBe(cost);
    expect(a['ps.cost.usd']).toBe(cost);
    expect(a['gen_ai.conversation.id']).toBe('sess-42');
    expect(a['session.id']).toBe('sess-42');
    expect(a['langfuse.session.id']).toBeUndefined(); // not in langfuse mode
  });

  test('unpriced models carry no cost', () => {
    const built = buildGenAISpan({
      path: '/v1/chat/completions',
      headers: { 'x-portkey-provider': 'openai' },
      status: 200,
      startMs: 0,
      endMs: 1,
      requestBody: { model: 'no-such-model' },
      responseBody: { usage: { prompt_tokens: 1, completion_tokens: 1 } },
    })!;
    expect(attrsOf(built.span)['gen_ai.usage.cost']).toBeUndefined();
  });

  test('session id must be a plausible id', () => {
    expect(sessionIdOf({ 'x-ps-session-id': ' s-1 ' })).toBe('s-1');
    expect(sessionIdOf({ 'x-ps-session-id': 'a b' })).toBeUndefined();
    expect(sessionIdOf({ 'x-ps-session-id': 'x'.repeat(257) })).toBeUndefined();
  });

  test('user hash: OIDC subject hash, else SHA-256 of X-PS-User-Id', async () => {
    expect(await userHashOf({ 'x-ps-user-id': 'u-1' })).toBe(
      await userIdHash('u-1')
    );
    expect(await userIdHash('abc')).toBe(
      'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    );
    expect(
      await userHashOf(
        { 'x-ps-user-id': 'u-1' },
        { subjectHash: 'f'.repeat(32), config: { telemetrySubject: 'hash' } }
      )
    ).toBe('f'.repeat(32));
    expect(
      await userHashOf(
        {},
        { subjectHash: 'f'.repeat(32), config: { telemetrySubject: 'off' } }
      )
    ).toBeUndefined();
    expect(await userHashOf({})).toBeUndefined();
  });
});

describe('Langfuse contract (recorded payload)', () => {
  const build = () => {
    const built = buildGenAISpan({
      path: '/v1/chat/completions',
      headers,
      status: 200,
      startMs: 1_767_225_599_150,
      endMs: 1_767_225_600_000,
      requestBody: chatReq,
      responseBody: chatRes,
      userHash: USER_HASH,
      langfuse: true,
    })!;
    built.span.spanId = '00f067aa0ba902b7'; // random per span: pinned
    return buildExportRequest([built], 'prompt-shields-gateway');
  };

  test('matches the recorded export request', () => {
    const got = build();
    if (process.env.UPDATE_OTLP_FIXTURES === '1') {
      fs.writeFileSync(
        FIXTURE,
        JSON.stringify(
          {
            _comment:
              'Recorded OTLP JSON export request from the gateway (psOtel.ts) with PS_OTEL_LANGFUSE=true, content capture off. Regenerate with UPDATE_OTLP_FIXTURES=1 only when the mapping changes on purpose; packages/sdk/tests/test_otlp.py checks the same Langfuse keys.',
            endpoint: config().endpoint,
            request: got,
          },
          null,
          2
        ) + '\n'
      );
    }
    const recorded = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
    expect(got).toEqual(recorded.request);
  });

  test('carries every attribute Langfuse maps, and no content', () => {
    const req = build();
    const a = attrsOf(req.resourceSpans[0].scopeSpans[0].spans[0]);
    expect(a['langfuse.observation.type']).toBe('generation');
    expect(a['langfuse.observation.model.name']).toBe('gpt-4o-2024-08-06');
    expect(JSON.parse(a['langfuse.observation.usage_details'])).toEqual({
      input: 120,
      output: 30,
    });
    expect(JSON.parse(a['langfuse.observation.cost_details']).total).toBe(
      a['gen_ai.usage.cost']
    );
    expect(a['langfuse.session.id']).toBe('sess-42');
    expect(a['langfuse.user.id']).toBe(USER_HASH);
    expect(a['user.id']).toBe(USER_HASH);
    expect(a['langfuse.environment']).toBe('prod');
    const raw = JSON.stringify(req);
    for (const needle of ['Nightjar', 'alice@example.com', 'bob@acme.io'])
      expect(raw).not.toContain(needle);
  });
});

describe('psOtel middleware', () => {
  const run = async (captureContent: boolean, extra = {}) => {
    const sent: any[] = [];
    const exporter = { enqueue: (s: any) => sent.push(s) } as any;
    const app = new Hono();
    app.use('*', psOtel(config({ captureContent }), exporter));
    app.post('/v1/chat/completions', (c) => c.json(chatRes));
    await app.request('/v1/chat/completions', {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...headers, ...extra },
      body: JSON.stringify(chatReq),
    });
    return sent;
  };

  test('hashes X-PS-User-Id; the raw id never leaves', async () => {
    const sent = await run(false, { 'x-ps-user-id': 'carol@corp.example' });
    const a = attrsOf(sent[0].span);
    expect(a['user.id']).toBe(await userIdHash('carol@corp.example'));
    expect(JSON.stringify(sent)).not.toContain('carol@corp.example');
  });

  test('captured content goes through the /log/stream PII redaction', async () => {
    const sent = await run(true);
    const a = attrsOf(sent[0].span);
    expect(a['gen_ai.input.messages']).toContain('Nightjar');
    expect(a['gen_ai.input.messages']).toContain('[EMAIL]');
    expect(a['gen_ai.output.messages']).toContain('[EMAIL]');
    expect(a['langfuse.observation.input']).toBe(a['gen_ai.input.messages']);
    const raw = JSON.stringify(sent);
    expect(raw).not.toContain('alice@example.com');
    expect(raw).not.toContain('bob@acme.io');
  });
});
