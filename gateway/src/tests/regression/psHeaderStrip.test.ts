/**
 * X-PS-* business metadata never reaches the upstream provider (privacy).
 *
 * Drives the whole gateway app against a fake upstream that captures the
 * outgoing headers: the catch-all proxy, chat completions and Responses
 * send no `x-ps-*` header, the provider's own headers still go out, and the
 * telemetry event still carries the business unit and data classification
 * read from the incoming request.
 */
jest.mock('jose', () => ({}));

const wsInstances: { url: string; options: any }[] = [];
jest.mock('ws', () =>
  jest.fn().mockImplementation((url: string, options: any) => {
    wsInstances.push({ url, options });
    return {
      addEventListener: (type: string, fn: () => void) => {
        if (type === 'open') fn();
      },
      send: jest.fn(),
      close: jest.fn(),
    };
  })
);

import {
  json,
  post,
  resetHarness,
  telemetryEvent,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';
import Providers from '../../providers';
import { realTimeHandler } from '../../handlers/realtimeHandler';
import { realTimeHandlerNode } from '../../handlers/realtimeHandlerNode';
import {
  isPSInternalHeader,
  stripPSInternalHeaders,
} from '../../utils/psInternalHeaders';

const PS_HEADERS = {
  'x-ps-business-unit': 'payments',
  'x-ps-data-classification': 'confidential',
  'x-ps-use-case': 'refund-triage',
  'x-ps-owner': 'owner@example.com',
  'x-ps-environment': 'prod',
  'x-ps-route': 'cheap',
  'x-ps-max-cost': '0.5',
};

const OPENAI = {
  'x-portkey-provider': 'openai',
  authorization: 'Bearer sk-regression',
  'openai-beta': 'assistants=v2',
};

function psHeadersSent(headers: Record<string, string>): string[] {
  return Object.keys(headers).filter((k) =>
    k.toLowerCase().startsWith('x-ps-')
  );
}

async function expectStrippedWithTelemetry() {
  expect(upstreamCalls).toHaveLength(1);
  const sent = upstreamCalls[0].headers;
  expect(psHeadersSent(sent)).toEqual([]);
  // Headers the provider needs are still sent.
  expect(sent.authorization).toBe('Bearer sk-regression');

  const event = await telemetryEvent();
  expect(event.business_unit).toBe('payments');
  expect(event.data_classification).toBe('confidential');
  expect(event.use_case_name).toBe('refund-triage');
  expect(event.environment).toBe('prod');
}

const chatCompletion = {
  id: 'chatcmpl-1',
  object: 'chat.completion',
  model: 'gpt-4o-mini',
  choices: [
    {
      index: 0,
      message: { role: 'assistant', content: 'hi' },
      finish_reason: 'stop',
    },
  ],
  usage: { prompt_tokens: 3, completion_tokens: 1 },
};

beforeEach(() => {
  resetHarness();
  wsInstances.length = 0;
});

describe('X-PS-* headers are not forwarded upstream', () => {
  test('catch-all proxy route', async () => {
    upstream(/api\.openai\.com\/v1\/threads$/, () =>
      json({ id: 'thread_1', object: 'thread' })
    );
    const res = await post(
      app,
      '/v1/threads',
      {},
      { ...OPENAI, ...PS_HEADERS }
    );
    expect(res.status).toBe(200);
    await expectStrippedWithTelemetry();
    // Client headers the proxy passes through are still sent.
    expect(upstreamCalls[0].headers['openai-beta']).toBe('assistants=v2');
    // Gateway-only x-portkey-* headers are not passed through either.
    expect(
      Object.keys(upstreamCalls[0].headers).filter((k) =>
        k.startsWith('x-portkey-')
      )
    ).toEqual([]);
  });

  test('deprecated /v1/proxy route', async () => {
    upstream(/api\.openai\.com\/v1\/threads$/, () =>
      json({ id: 'thread_1', object: 'thread' })
    );
    await post(app, '/v1/proxy/threads', {}, { ...OPENAI, ...PS_HEADERS });
    expect(upstreamCalls).toHaveLength(1);
    expect(psHeadersSent(upstreamCalls[0].headers)).toEqual([]);
  });

  test('chat completions', async () => {
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      json(chatCompletion)
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      { model: 'gpt-4o', messages: [{ role: 'user', content: 'hi' }] },
      { ...OPENAI, ...PS_HEADERS }
    );
    expect(res.status).toBe(200);
    await expectStrippedWithTelemetry();
  });

  test('chat completions, even when forward_headers names them', async () => {
    upstream(/api\.openai\.com\/v1\/chat\/completions$/, () =>
      json(chatCompletion)
    );
    await post(
      app,
      '/v1/chat/completions',
      { model: 'gpt-4o', messages: [{ role: 'user', content: 'hi' }] },
      {
        ...OPENAI,
        ...PS_HEADERS,
        'x-custom-trace': 'abc',
        'x-portkey-forward-headers':
          'x-custom-trace,x-ps-business-unit,X-PS-Data-Classification',
      }
    );
    await expectStrippedWithTelemetry();
    // Other forwarded headers still go out.
    expect(upstreamCalls[0].headers['x-custom-trace']).toBe('abc');
  });

  test('Responses API', async () => {
    upstream(/api\.openai\.com\/v1\/responses$/, () =>
      json({
        id: 'resp_1',
        object: 'response',
        model: 'gpt-4o',
        status: 'completed',
        output: [],
        usage: { input_tokens: 3, output_tokens: 1, total_tokens: 4 },
      })
    );
    const res = await post(
      app,
      '/v1/responses',
      { model: 'gpt-4o', input: 'hi' },
      { ...OPENAI, ...PS_HEADERS }
    );
    expect(res.status).toBe(200);
    await expectStrippedWithTelemetry();
  });
});

describe('stripPSInternalHeaders', () => {
  test('drops X-PS-* in any case and keeps everything else', () => {
    const input = {
      'X-PS-Business-Unit': 'payments',
      'x-ps-data-classification': 'confidential',
      'X-Ps-Route': 'cheap',
      authorization: 'Bearer sk-1',
      'x-portkey-provider': 'openai',
      'x-psx-not-ours': 'kept',
    };
    expect(stripPSInternalHeaders(input)).toEqual({
      authorization: 'Bearer sk-1',
      'x-portkey-provider': 'openai',
      'x-psx-not-ours': 'kept',
    });
    // The input is not mutated: telemetry still reads it.
    expect(input['X-PS-Business-Unit']).toBe('payments');
    expect(isPSInternalHeader('X-PS-MAX-COST')).toBe(true);
    expect(isPSInternalHeader('x-portkey-ps-thing')).toBe(false);
  });
});

/**
 * Realtime upstream headers come from the provider config only, so no
 * built-in provider copies client headers today. These tests make a
 * provider's headers include X-PS-* to prove the realtime paths strip them
 * as well.
 */
describe('realtime upstream headers', () => {
  const realtimeContext = (headers: Record<string, string>, path: string) => {
    const store = new Map<string, unknown>();
    return {
      req: {
        url: `http://localhost:8787${path}`,
        raw: { headers: new Headers(headers) },
      },
      env: {},
      set: (k: string, v: unknown) => store.set(k, v),
      get: (k: string) => store.get(k),
    } as any;
  };
  const OPENAI_RT = {
    'x-portkey-provider': 'openai',
    authorization: 'Bearer sk-regression',
    ...PS_HEADERS,
  };

  let spy: jest.SpyInstance;
  beforeEach(() => {
    const api = Providers.openai.api;
    const original = api.headers;
    spy = jest.spyOn(api, 'headers').mockImplementation(async (args: any) => ({
      ...(await original(args)),
      'X-PS-Business-Unit': 'payments',
    }));
  });
  afterEach(() => spy.mockRestore());

  test('Node WebSocket', async () => {
    const c = realtimeContext(OPENAI_RT, '/v1/realtime?model=gpt-4o-realtime');
    await realTimeHandlerNode(c);
    expect(wsInstances).toHaveLength(1);
    const sent = wsInstances[0].options.headers;
    expect(psHeadersSent(sent)).toEqual([]);
    expect(sent.Authorization ?? sent.authorization).toBe(
      'Bearer sk-regression'
    );
  });

  test('Workers WebSocket', async () => {
    upstream(/api\.openai\.com\/v1\/realtime/, () => json({}));
    const end = { accept: jest.fn() };
    (globalThis as any).WebSocketPair = function () {
      return [end, end];
    };
    try {
      await realTimeHandler(
        realtimeContext(OPENAI_RT, '/v1/realtime?model=gpt-4o-realtime')
      );
    } finally {
      delete (globalThis as any).WebSocketPair;
    }
    expect(upstreamCalls).toHaveLength(1);
    const sent = upstreamCalls[0].headers;
    expect(psHeadersSent(sent)).toEqual([]);
    expect(sent.authorization).toBe('Bearer sk-regression');
    expect(sent.upgrade).toBe('websocket');
  });
});
