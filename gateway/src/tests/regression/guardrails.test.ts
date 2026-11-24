/**
 * Central-policy guardrails (#349) through the whole gateway app, offline:
 * a team policy redacts e-mail on its calls whatever guardrails the
 * caller sends, card numbers are redacted in streamed and JSON output,
 * deny refuses with 446, findings are security events carrying the
 * policy version, and without guardrails calls are untouched.
 */
jest.mock('jose', () => ({}));

import { readFileSync } from 'fs';
import { join } from 'path';
import {
  json,
  post,
  resetHarness,
  sse,
  telemetryEvent,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';
import {
  _resetSecurityEventsForTests,
  flushSecurityEvents,
} from '../../middlewares/psSecurityEvents';
import {
  parsePolicyBundle,
  setCentralPolicy,
} from '../../middlewares/policyPull/state';

const CHAT_URL = /api\.openai\.com\/v1\/chat\/completions$/;
const openai = (extra: Record<string, any> = {}) => ({
  'x-portkey-config': JSON.stringify({
    provider: 'openai',
    api_key: 'sk-regression',
    ...extra,
  }),
});
const chatBody = (text: string, stream = false) => ({
  model: 'gpt-4o-mini',
  messages: [{ role: 'user', content: text }],
  ...(stream ? { stream: true } : {}),
});
const chatOk = (text: string) =>
  json({
    id: 'chatcmpl-g',
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

const bundle = {
  schema: 'ps-policy-bundle.v1',
  tenant: {
    version: 'default@3',
    sections: {
      guardrails: {
        version: 1,
        rules: [
          {
            id: 'no-keys',
            check: 'secrets',
            stages: ['input'],
            action: 'deny',
          },
          {
            id: 'cards-out',
            check: 'pii',
            categories: ['credit_card'],
            stages: ['output'],
            action: 'redact',
          },
        ],
      },
    },
  },
  teams: {
    'team-1': {
      name: 'payments',
      version: 'default@3+payments@2',
      sections: {
        guardrails: {
          version: 1,
          rules: [
            {
              id: 'redact-email',
              check: 'pii',
              categories: ['email'],
              action: 'redact',
            },
          ],
        },
      },
    },
  },
  projects: {},
};

const SCHEMA = JSON.parse(
  readFileSync(
    join(__dirname, '../../../../docs/security-events.schema.json'),
    'utf8'
  )
);
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
  setCentralPolicy(parsePolicyBundle(bundle, '"etag"'));
});

afterEach(() => {
  writeSpy.mockRestore();
  delete process.env.PS_SECURITY_EVENTS_LOG;
  setCentralPolicy(undefined);
});

async function securityEvents(): Promise<any[]> {
  await new Promise((r) => setImmediate(r));
  flushSecurityEvents();
  return written
    .join('')
    .split('\n')
    .filter((l) => l.includes('"ps.security"'))
    .map((l) => JSON.parse(l))
    .map((e) => {
      const def = SCHEMA.$defs[String(e['event.name']).replace('.', '_')];
      for (const key of Object.keys(e))
        if (!def.properties[key])
          throw new Error(`${key} is not in the schema`);
      for (const key of def.required) expect(e).toHaveProperty([key]);
      return e;
    });
}

describe('central-policy guardrails', () => {
  it("redacts e-mail on the team's calls, whatever guardrails the caller sends", async () => {
    upstream(CHAT_URL, () => chatOk('done'));
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody('mail jane.doe@example.com now'),
      {
        ...openai({
          // A caller's own guardrail config cannot switch the policy off.
          input_guardrails: [],
          output_guardrails: [],
        }),
        'x-ps-team': 'payments',
      }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].body.messages[0].content).toBe('mail [EMAIL] now');
    const events = (await securityEvents()).filter(
      (e) => e['ps.guardrail.hook_id'] === 'ps.policy'
    );
    const verdict = events.find((e) => e['event.name'] === 'guardrail.verdict');
    expect(verdict).toMatchObject({
      'ps.guardrail.rule_id': 'redact-email',
      'ps.guardrail.action': 'redact',
      'ps.guardrail.stage': 'input',
      'ps.guardrail.category': 'email',
      'ps.guardrail.count': 1,
      'ps.policy_version': 'default@3+payments@2',
      action: 'allowed',
    });
    expect(events.some((e) => e['event.name'] === 'pii.detected')).toBe(true);
    expect(JSON.stringify(events)).not.toContain('jane.doe');
    const event = await telemetryEvent();
    expect(event.detected_pii_types).toEqual(['email']);
  });

  it('keeps the tenant rules for calls naming another team or none', async () => {
    upstream(CHAT_URL, () => chatOk('done'));
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody('use key sk-' + 'a'.repeat(32) + ' for jane@example.com'),
      { ...openai(), 'x-ps-team': 'someone-else' }
    );
    expect(res.status).toBe(446);
    expect(((await res.json()) as any).error.code).toBe('ps_guardrail_denied');
    expect(upstreamCalls).toHaveLength(0);
    const [denied] = (await securityEvents()).filter(
      (e) => e['event.name'] === 'guardrail.verdict'
    );
    expect(denied).toMatchObject({
      'ps.guardrail.rule_id': 'no-keys',
      'ps.guardrail.deny': true,
      'ps.policy_version': 'default@3',
      action: 'blocked',
    });
    // Without the team, e-mail is not redacted (only the team asks for it).
    upstream(CHAT_URL, () => chatOk('done'));
    await post(
      app,
      '/v1/chat/completions',
      chatBody('jane@example.com'),
      openai()
    );
    expect(upstreamCalls[0].body.messages[0].content).toBe('jane@example.com');
  });

  it('redacts a card number in a JSON response', async () => {
    upstream(CHAT_URL, () => chatOk('card 4111 1111 1111 1111 ok'));
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody('hi'),
      openai()
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.choices[0].message.content).toBe('card [CREDIT_CARD] ok');
  });

  it('redacts a card number in a streamed response', async () => {
    const chunk = (content: string) =>
      `data: ${JSON.stringify({
        id: 'c',
        object: 'chat.completion.chunk',
        model: 'gpt-4o-mini',
        choices: [{ index: 0, delta: { content }, finish_reason: null }],
      })}\n\n`;
    upstream(CHAT_URL, () =>
      sse(
        ['Card: 4111 1111', ' 1111 1111', ' — keep it safe.']
          .map(chunk)
          .join('') + 'data: [DONE]\n\n'
      )
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody('hi', true),
      openai()
    );
    expect(res.status).toBe(200);
    const text = await res.text();
    const content = text
      .split('\n')
      .filter((l) => l.startsWith('data: {'))
      .map((l) => JSON.parse(l.slice(6)).choices?.[0]?.delta?.content ?? '')
      .join('');
    expect(content).toBe('Card: [CREDIT_CARD] — keep it safe.');
    expect(text).not.toContain('1111');
    const verdict = (await securityEvents()).find(
      (e) => e['ps.guardrail.rule_id'] === 'cards-out'
    );
    expect(verdict).toMatchObject({
      'ps.guardrail.stage': 'output',
      'ps.guardrail.count': 1,
    });
  });

  it('leaves calls untouched without guardrails in the policy', async () => {
    setCentralPolicy(undefined);
    upstream(CHAT_URL, () => chatOk('card 4111 1111 1111 1111'));
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody('sk-' + 'a'.repeat(32) + ' jane@example.com'),
      { ...openai(), 'x-ps-team': 'payments' }
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].body.messages[0].content).toContain(
      'jane@example.com'
    );
    expect(((await res.json()) as any).choices[0].message.content).toBe(
      'card 4111 1111 1111 1111'
    );
    expect(
      (await securityEvents()).filter(
        (e) => e['ps.guardrail.hook_id'] === 'ps.policy'
      )
    ).toEqual([]);
  });
});
