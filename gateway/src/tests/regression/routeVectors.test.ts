/**
 * Golden route-decision vectors (#11) replayed through the whole gateway app
 * (#69). `router/vectors.test.ts` checks the decision function; this checks
 * that, with every ported middleware mounted, the decision still reaches the
 * provider request and the ps-telemetry event (requested_model,
 * served_model, route_group, route_reason, route_est_cost,
 * route_sensitivity_level, route_sensitivity_source), and that a
 * PolicyViolation is a 403 before any upstream call (#19).
 *
 * The app's router uses `defaultPolicy`; it is swapped for each vector file's
 * example policy. Declared classification and use case travel as
 * X-PS-Data-Classification / X-PS-Use-Case. A vector's pii_detected becomes
 * text in the prompt that the gateway's own detector finds (#18). Pending
 * vectors are covered (as skipped) by vectors.test.ts.
 */
jest.mock('jose', () => ({}));
jest.mock('../../middlewares/router/policy', () => {
  const actual = jest.requireActual('../../middlewares/router/policy');
  return { ...actual, defaultPolicy: { ...actual.defaultPolicy } };
});

import * as fs from 'fs';
import * as path from 'path';
import {
  json,
  post,
  resetHarness,
  telemetryEvent,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';
import { defaultPolicy } from '../../middlewares/router/policy';
import { fromRoutePolicyDoc } from '../../middlewares/router/constraints';

const SCHEMA_DIR = path.resolve(__dirname, '../../../../schemas/route-policy');
const readJson = (p: string) => JSON.parse(fs.readFileSync(p, 'utf8'));

// Text the detector reports as exactly this mapped PII category (plus only
// categories no example policy maps, such as phone and financial_data).
const PII_TEXT: Record<string, string> = {
  email: 'jane@acme.com',
  iban: 'NO9386011117947',
  national_id: '19121212-1212',
  health_data: 'diagnosis',
  phone: '415-555-0199',
};

// Same hint mapping as router/vectors.test.ts.
const HINT_HEADERS: Record<string, string> = {
  quality: 'X-PS-Quality',
  max_cost: 'X-PS-Max-Cost',
  model_group: 'X-PS-Route',
};

const vectorFiles = fs
  .readdirSync(path.join(SCHEMA_DIR, 'vectors'))
  .filter((f) => f.endsWith('.json'))
  .sort();

const cases: [string, any, any][] = [];
for (const f of vectorFiles) {
  const doc = readJson(path.join(SCHEMA_DIR, 'vectors', f));
  for (const v of doc.vectors) {
    if (v.status === 'pending') continue;
    cases.push([`${path.basename(f, '.json')}: ${v.id}`, doc.policy, v]);
  }
}

beforeEach(() => resetHarness());

test('there are active vectors to replay', () => {
  expect(cases.length).toBeGreaterThan(10);
});

test.each(cases)('%s', async (_name, policyName, v) => {
  const policyDoc = readJson(
    path.join(SCHEMA_DIR, 'examples', `${policyName}.json`)
  );
  for (const k of Object.keys(defaultPolicy)) delete (defaultPolicy as any)[k];
  Object.assign(defaultPolicy, fromRoutePolicyDoc(policyDoc));

  // The provider echoes the model it was asked for, like a real one.
  upstream(/api\.openai\.com\/v1\/chat\/completions$/, (call) =>
    json({
      id: 'chatcmpl-v',
      object: 'chat.completion',
      created: 1,
      model: call.body.model,
      choices: [
        {
          index: 0,
          message: { role: 'assistant', content: 'ok' },
          finish_reason: 'stop',
        },
      ],
      usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
    })
  );

  const headers: Record<string, string> = {
    'x-portkey-provider': 'openai',
    authorization: 'Bearer sk-regression',
  };
  for (const [k, val] of Object.entries(v.request.hints || {})) {
    headers[HINT_HEADERS[k]] = String(val);
  }
  if (v.request.data_classification != null)
    headers['x-ps-data-classification'] = v.request.data_classification;
  if (v.request.use_case != null) headers['x-ps-use-case'] = v.request.use_case;
  // approx_input_tokens: the router estimates ceil(chars / 4) tokens.
  const messages =
    v.request.approx_input_tokens != null
      ? [
          {
            role: 'user',
            content: 'x'.repeat(4 * v.request.approx_input_tokens),
          },
        ]
      : v.request.messages.map((m: any, i: number) =>
          i === 0 && v.request.pii_detected
            ? {
                ...m,
                content: [
                  m.content,
                  ...v.request.pii_detected.map((p: string) => PII_TEXT[p]),
                ].join(' '),
              }
            : m
        );
  const requested = v.request.model ?? 'auto';

  const res = await post(
    app,
    '/v1/chat/completions',
    { model: requested, messages, max_tokens: v.request.max_tokens },
    headers
  );
  if (v.expected.error === 'PolicyViolation') {
    expect(res.status).toBe(403);
    expect(((await res.json()) as any).error.code).toBe('policy_violation');
    expect(upstreamCalls).toHaveLength(0);
    return;
  }
  expect(res.status).toBe(200);

  const ev = await telemetryEvent();
  const want: Record<string, unknown> = { requested_model: requested };
  const fields: Record<string, string> = {
    model: 'served_model',
    group: 'route_group',
    reason: 'route_reason',
    sensitivity_level: 'route_sensitivity_level',
    sensitivity_source: 'route_sensitivity_source',
  };
  for (const [k, field] of Object.entries(fields)) {
    if (v.expected[k] != null) want[field] = v.expected[k];
  }
  expect(ev).toMatchObject(want);
  if (v.expected.est_cost != null) {
    expect(
      Math.abs(ev.route_est_cost - v.expected.est_cost)
    ).toBeLessThanOrEqual(1e-9 * Math.abs(v.expected.est_cost) + 1e-15);
  }
});
