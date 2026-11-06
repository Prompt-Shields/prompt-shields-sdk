// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import { Hono } from 'hono';
import { requestValidator } from '.';
import { VALID_PROVIDERS } from '../../globals';
import { configErrorMap, configSchema } from './schema/config';

// Pins the 400 body for an invalid x-portkey-config. Every expected string
// below was captured from the gateway running zod 3, before the zod 4
// upgrade (#208 F1), so these tests fail if the upgrade changes what clients
// see.

const post = (config: unknown) => {
  const app = new Hono();
  app.post('/v1/chat/completions', requestValidator, (c) => c.text('ok'));
  return app.request('/v1/chat/completions', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-portkey-config': JSON.stringify(config),
    },
    body: JSON.stringify({ model: 'gpt-4o', messages: [] }),
  });
};

const NO_CREDENTIALS =
  "Invalid configuration. It must have either 'provider' and 'api_key', or 'strategy' and 'targets', or 'cache', or 'retry', or 'request_timeout'";
const VERTEX_FIELDS =
  "Invalid configuration. ('vertex_project_id' and 'vertex_region') or ('vertex_service_account_json' and 'vertex_region') are required for 'vertex-ai' provider. Example: { 'provider': 'vertex-ai', 'vertex_project_id': 'my-project-id', 'vertex_region': 'us-central1', api_key: 'ya29...' }";
const BAD_MODE =
  "Invalid 'mode' value. Must be one of: single, loadbalance, fallback, conditional";
const BAD_CACHE_MODE =
  "Invalid 'cache.mode' value. Must be one of: simple, semantic";

const k = 'k';
const cases: [string, unknown, string[]][] = [
  // zod built-in type errors: worded as zod 3 did, through configErrorMap
  [
    'wrong type',
    { provider: 'openai', api_key: 123 },
    ['path: api_key, message: Expected string, received number'],
  ],
  [
    'missing required field',
    { provider: 'openai', api_key: k, retry: {} },
    ['path: retry,attempts, message: Required'],
  ],
  [
    'array where an object is expected',
    { provider: 'openai', api_key: k, retry: [] },
    ['path: retry, message: Expected object, received array'],
  ],
  [
    'null where an object is expected',
    { provider: 'openai', api_key: k, strategy: null },
    ['path: strategy, message: Expected object, received null'],
  ],
  [
    'nested target field',
    {
      strategy: { mode: 'fallback' },
      targets: [{ provider: 'openai', api_key: 1 }],
    },
    ['path: targets,0,api_key, message: Expected string, received number'],
  ],
  [
    'catchall value',
    {
      provider: 'vertex-ai',
      vertex_region: 'us',
      vertex_service_account_json: { a: 1 },
    },
    [
      'path: vertex_service_account_json,a, message: Expected string, received number',
    ],
  ],
  [
    'array element',
    { provider: 'openai', api_key: k, on_status_codes: ['500'] },
    ['path: on_status_codes,0, message: Expected number, received string'],
  ],
  [
    'boolean field',
    { provider: 'openai', api_key: k, strict_open_ai_compliance: 'yes' },
    [
      'path: strict_open_ai_compliance, message: Expected boolean, received string',
    ],
  ],
  [
    'number field given a string',
    { provider: 'openai', api_key: k, retry: { attempts: '3' } },
    ['path: retry,attempts, message: Expected number, received string'],
  ],
  [
    'hook that is not an object',
    { provider: 'openai', api_key: k, before_request_hooks: ['x'] },
    ['path: before_request_hooks,0, message: Expected object, received string'],
  ],
  [
    'two issues in one condition',
    {
      strategy: { mode: 'conditional', conditions: [{ query: 'x', then: 1 }] },
      targets: [{ provider: 'openai', api_key: k }],
    },
    [
      'path: strategy,conditions,0,query, message: Expected object, received string',
      'path: strategy,conditions,0,then, message: Expected string, received number',
    ],
  ],
  // unions
  [
    'guardrails array of the wrong type',
    { provider: 'openai', api_key: k, input_guardrails: [1] },
    ['path: input_guardrails, message: Invalid input'],
  ],
  [
    'guardrails not an array',
    { provider: 'openai', api_key: k, input_guardrails: 'x' },
    ['path: input_guardrails, message: Invalid input'],
  ],
  // refinements: our own messages
  [
    'invalid strategy mode',
    {
      strategy: { mode: 'bogus' },
      targets: [{ provider: 'openai', api_key: k }],
    },
    [`path: strategy,mode, message: ${BAD_MODE}`],
  ],
  [
    'no credentials',
    { provider: 'openai' },
    [`path: , message: ${NO_CREDENTIALS}`],
  ],
  [
    'nested target without credentials',
    { strategy: { mode: 'fallback' }, targets: [{ provider: 'openai' }] },
    [`path: targets,0, message: ${NO_CREDENTIALS}`],
  ],
  [
    'vertex without project or region',
    { provider: 'vertex-ai', api_key: k },
    [`path: , message: ${VERTEX_FIELDS}`],
  ],
  [
    'invalid cache mode',
    { provider: 'openai', api_key: k, cache: { mode: 'x' } },
    [`path: cache,mode, message: ${BAD_CACHE_MODE}`],
  ],
  [
    'refinements at several levels, in order',
    {
      provider: 'vertex-ai',
      api_key: k,
      cache: { mode: 'x' },
      strategy: { mode: 'y' },
    },
    [
      `path: strategy,mode, message: ${BAD_MODE}`,
      `path: cache,mode, message: ${BAD_CACHE_MODE}`,
      `path: , message: ${VERTEX_FIELDS}`,
    ],
  ],
  [
    'unsafe custom host',
    { provider: 'openai', api_key: k, custom_host: 'http://169.254.169.254' },
    ['path: , message: Invalid custom host'],
  ],
  [
    'forward_headers loop',
    {
      provider: 'openai',
      api_key: k,
      forward_headers: ['x-portkey-forward-headers'],
    },
    [
      "path: forward_headers, message: forward_headers must not contain the 'x-portkey-forward-headers' header",
    ],
  ],
];

describe('invalid x-portkey-config: 400 response body', () => {
  test.each(cases)('%s', async (_name, config, errors) => {
    const res = await post(config);
    expect(res.status).toBe(400);
    expect(res.headers.get('content-type')).toBe('application/json');
    expect(await res.json()).toEqual({
      status: 'failure',
      message: 'Invalid config passed',
      errors,
    });
  });

  test('invalid provider lists the valid ones', async () => {
    const res = await post({ provider: 'nope', api_key: k });
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({
      status: 'failure',
      message: 'Invalid config passed',
      errors: [
        `path: provider, message: Invalid 'provider' value. Must be one of: ${VALID_PROVIDERS.join(', ')}`,
      ],
    });
  });
});

describe('valid x-portkey-config', () => {
  test.each([
    [
      'provider with retry and cache',
      {
        provider: 'openai',
        api_key: k,
        retry: { attempts: 2 },
        cache: { mode: 'simple' },
      },
    ],
    [
      'load-balanced targets',
      {
        strategy: { mode: 'loadbalance' },
        targets: [
          { provider: 'openai', api_key: k, weight: 1 },
          { provider: 'anthropic', api_key: k },
        ],
      },
    ],
    [
      'guardrails and hooks as objects',
      {
        provider: 'openai',
        api_key: k,
        input_guardrails: [{ id: 'g1', deny: true }],
        after_request_hooks: [{ id: 'h1', type: 'guardrail' }],
      },
    ],
  ])('%s passes', async (_name, config) => {
    const res = await post(config);
    expect(res.status).toBe(200);
    expect(await res.text()).toBe('ok');
  });
});

describe('configErrorMap', () => {
  test('only rewords built-in type errors', () => {
    const res = configSchema.safeParse(
      { provider: 'openai', api_key: k, weight: Number.NaN },
      { error: configErrorMap }
    );
    expect(res.success).toBe(false);
    expect(res.error.issues.map((i: any) => i.message)).toEqual([
      'Expected number, received nan',
    ]);
  });
});
