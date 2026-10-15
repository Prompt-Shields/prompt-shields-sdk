/**
 * Golden route-decision vectors (#11) replayed through the whole gateway app
 * (#69). `router/vectors.test.ts` checks the decision function; this checks
 * that, with every ported middleware mounted, the decision still reaches the
 * provider request and the ps-telemetry event (requested_model,
 * served_model, route_group, route_reason, route_est_cost).
 *
 * The app's router uses `defaultPolicy`; it is swapped for each vector file's
 * example policy. Pending vectors are covered (as skipped) by vectors.test.ts.
 */
jest.mock('jose', () => ({}));
jest.mock('../../middlewares/router/policy', () => {
  const actual = jest.requireActual('../../middlewares/router/policy');
  return { ...actual, defaultPolicy: { ...actual.defaultPolicy } };
});

import * as fs from 'fs';
import * as path from 'path';
import { json, post, resetHarness, telemetryEvent, upstream } from './harness';
import app from '../../index';
import { defaultPolicy } from '../../middlewares/router/policy';
import { RoutePolicy } from '../../middlewares/router/types';

const SCHEMA_DIR = path.resolve(__dirname, '../../../../schemas/route-policy');
const readJson = (p: string) => JSON.parse(fs.readFileSync(p, 'utf8'));

// Same adapter and hint mapping as router/vectors.test.ts.
function toRoutePolicy(doc: any): RoutePolicy {
  const groups: RoutePolicy['groups'] = {};
  for (const [name, tiers] of Object.entries<any[]>(doc.groups)) {
    groups[name] = tiers.map((t) => ({
      model: t.model,
      group: name,
      inputCostPer1k: t.input_cost_per_1k,
      outputCostPer1k: t.output_cost_per_1k,
    }));
  }
  return {
    defaultGroup: doc.default_group,
    qualityToGroup: doc.quality_to_group,
    groups,
  };
}

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
  Object.assign(defaultPolicy, toRoutePolicy(policyDoc));

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
  // approx_input_tokens: the router estimates ceil(chars / 4) tokens.
  const messages =
    v.request.approx_input_tokens != null
      ? [
          {
            role: 'user',
            content: 'x'.repeat(4 * v.request.approx_input_tokens),
          },
        ]
      : v.request.messages;
  const requested = v.request.model ?? 'auto';

  const res = await post(
    app,
    '/v1/chat/completions',
    { model: requested, messages, max_tokens: v.request.max_tokens },
    headers
  );
  expect(res.status).toBe(200);

  const ev = await telemetryEvent();
  expect(ev).toMatchObject({
    requested_model: requested,
    served_model: v.expected.model,
    route_group: v.expected.group,
    route_reason: v.expected.reason,
  });
  if (v.expected.est_cost != null) {
    expect(
      Math.abs(ev.route_est_cost - v.expected.est_cost)
    ).toBeLessThanOrEqual(1e-9 * Math.abs(v.expected.est_cost) + 1e-15);
  }
});
