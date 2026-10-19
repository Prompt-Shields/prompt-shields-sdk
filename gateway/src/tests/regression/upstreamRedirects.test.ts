/**
 * Regression tests: upstream redirects are never followed, and realtime
 * WebSocket upstreams get the HTTP upstream URL checks (security).
 *
 * fetch follows a 3xx by default. On Node 22 a cross-origin hop drops only
 * `Authorization`, so a 307 from a custom host (or an open redirect at a
 * provider) re-sent the body and the `api-key` / `x-api-key` /
 * `x-goog-api-key` headers to the `Location` host, including internal and
 * metadata hosts that isSafeUpstreamURL refuses. The harness's fake fetch
 * follows redirects the way Node does unless `redirect: 'manual'` is set.
 *
 * The realtime handlers built `getBaseURL() + getEndpoint()` with no
 * isSafeUpstreamURL or base-origin check (and Node did not await
 * getBaseURL), so a crafted Azure resource name or Foundry URL opened a
 * WebSocket to any host with the provider credentials attached.
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

import { json, post, resetHarness, upstream, upstreamCalls } from './harness';
import app from '../../index';
import { externalServiceFetch } from '../../utils/fetch';
import { realTimeHandler } from '../../handlers/realtimeHandler';
import { realTimeHandlerNode } from '../../handlers/realtimeHandlerNode';

beforeEach(() => {
  resetHarness();
  wsInstances.length = 0;
});

const METADATA = 'http://169.254.169.254/latest/meta-data/iam/';
const EVIL = 'https://evil.example/steal';

const redirectTo =
  (location: string, status = 307) =>
  () =>
    new Response(null, { status, headers: { location } });

const completion = {
  id: 'c1',
  object: 'chat.completion',
  created: 1,
  model: 'gpt-4o',
  choices: [
    {
      index: 0,
      message: { role: 'assistant', content: 'ok' },
      finish_reason: 'stop',
    },
  ],
  usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
};

const config = (cfg: Record<string, any>) => ({
  'x-portkey-config': JSON.stringify(cfg),
});

const AZURE = config({
  provider: 'azure-openai',
  api_key: 'azure-secret',
  resource_name: 'myres',
  deployment_id: 'x',
  api_version: '2024-02-01',
});

const ANTHROPIC_CUSTOM_HOST = config({
  provider: 'anthropic',
  api_key: 'anthropic-secret',
  custom_host: 'https://llm.example.com/v1',
});

const GOOGLE = config({ provider: 'google', api_key: 'goog-secret' });

const chatBody = (model = 'gpt-4o') => ({
  model,
  max_tokens: 10,
  messages: [{ role: 'user', content: 'hi' }],
});

const secretsIn = (call: { headers: Record<string, string> }) =>
  Object.values(call.headers).filter((v) => /secret/.test(v));

async function expectBlocked(res: Response, status = 307) {
  expect(res.status).toBe(502);
  expect(res.headers.get('location')).toBeNull();
  const text = await res.text();
  expect(text).toContain('does not follow');
  expect(text).toContain(String(status));
  expect(text).not.toContain('169.254');
  expect(text).not.toContain('evil.example');
}

describe('upstream redirects are not followed', () => {
  test.each([
    ['the metadata host', METADATA],
    ['a foreign host', EVIL],
  ])('a custom host 307 to %s makes no second request', async (_, location) => {
    upstream(/llm\.example\.com/, redirectTo(location));
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody('claude-x'),
      ANTHROPIC_CUSTOM_HOST
    );
    await expectBlocked(res);
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toBe('https://llm.example.com/v1/messages');
    expect(upstreamCalls[0].headers['x-api-key']).toBe('anthropic-secret');
  });

  test.each([
    ['the metadata host', METADATA],
    ['a foreign host', EVIL],
  ])(
    'an Azure 307 to %s does not leak the api-key or body',
    async (_, location) => {
      upstream(/myres\.openai\.azure\.com/, redirectTo(location));
      const res = await post(app, '/v1/chat/completions', chatBody(), AZURE);
      await expectBlocked(res);
      expect(upstreamCalls).toHaveLength(1);
      expect(upstreamCalls[0].headers['api-key']).toBe('azure-secret');
    }
  );

  test.each([301, 302, 303, 308])(
    'a Google %i is not followed (x-goog-api-key stays put)',
    async (status) => {
      upstream(/generativelanguage\.googleapis\.com/, redirectTo(EVIL, status));
      const res = await post(
        app,
        '/v1/chat/completions',
        chatBody('gemini-2.0-flash'),
        GOOGLE
      );
      await expectBlocked(res, status);
      expect(upstreamCalls).toHaveLength(1);
      expect(upstreamCalls.every((c) => !c.url.includes('evil'))).toBe(true);
    }
  );

  test('a same-origin redirect is not followed either', async () => {
    upstream(
      /myres\.openai\.azure\.com/,
      redirectTo('https://myres.openai.azure.com/openai/other')
    );
    const res = await post(app, '/v1/chat/completions', chatBody(), AZURE);
    await expectBlocked(res);
    expect(upstreamCalls).toHaveLength(1);
  });

  test('a streaming request is answered 502, not followed', async () => {
    upstream(/myres\.openai\.azure\.com/, redirectTo(METADATA));
    const res = await post(
      app,
      '/v1/chat/completions',
      { ...chatBody(), stream: true },
      AZURE
    );
    expect(res.status).toBe(502);
    expect(upstreamCalls).toHaveLength(1);
  });

  test('a blocked redirect is not retried even when 502 is retryable', async () => {
    upstream(/myres\.openai\.azure\.com/, redirectTo(EVIL));
    const res = await post(
      app,
      '/v1/chat/completions',
      chatBody(),
      config({
        provider: 'azure-openai',
        api_key: 'azure-secret',
        resource_name: 'myres',
        deployment_id: 'x',
        api_version: '2024-02-01',
        retry: { attempts: 3, on_status_codes: [502] },
      })
    );
    expect(res.status).toBe(502);
    expect(upstreamCalls).toHaveLength(1);
  });

  test('a proxied request 302 is not followed', async () => {
    upstream(/myres\.openai\.azure\.com/, redirectTo(METADATA, 302));
    const res = await post(
      app,
      '/v1/proxy/myres.openai.azure.com/openai/deployments/x/chat/completions?api-version=2024-02-01',
      chatBody(),
      AZURE
    );
    expect(res.status).toBe(502);
    expect(upstreamCalls).toHaveLength(1);
  });

  test('provider helper fetches (files, batches, tokens) do not follow', async () => {
    upstream(/api\.example\.com/, redirectTo(METADATA));
    const res = await externalServiceFetch('https://api.example.com/token', {
      method: 'POST',
      headers: { 'x-api-key': 'helper-secret' },
      body: 'grant_type=client_credentials',
    });
    await expectBlocked(res);
    expect(upstreamCalls).toHaveLength(1);
  });

  test('a normal response still passes through', async () => {
    upstream(/myres\.openai\.azure\.com/, () => json(completion));
    const res = await post(app, '/v1/chat/completions', chatBody(), AZURE);
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
  });

  test('no recorded call ever carries a key to a redirect target', async () => {
    upstream(/myres\.openai\.azure\.com/, redirectTo(EVIL));
    await post(app, '/v1/chat/completions', chatBody(), AZURE);
    const leaked = upstreamCalls.filter(
      (c) => !c.url.startsWith('https://myres.openai.azure.com/')
    );
    expect(leaked.flatMap(secretsIn)).toEqual([]);
    expect(leaked).toEqual([]);
  });
});

// --- realtime ---------------------------------------------------------------

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
    store,
  } as any;
};

const azureRealtime = (extra: Record<string, string> = {}) => ({
  'x-portkey-provider': 'azure-openai',
  authorization: 'Bearer azure-secret',
  'x-portkey-azure-resource-name': 'myres',
  'x-portkey-azure-deployment-id': 'gpt-4o-realtime',
  'x-portkey-azure-api-version': '2024-10-01-preview',
  ...extra,
});

const badRealtime: [string, Record<string, string>][] = [
  [
    'an Azure resource name pointing at the metadata host',
    azureRealtime({
      'x-portkey-azure-resource-name': '169.254.169.254/latest/meta-data#',
    }),
  ],
  [
    'an Azure resource name pointing at a private address',
    azureRealtime({ 'x-portkey-azure-resource-name': '10.0.0.5:6379/x?' }),
  ],
  [
    'a Foundry URL on the metadata host',
    {
      'x-portkey-provider': 'azure-ai',
      authorization: 'Bearer azure-secret',
      'x-portkey-azure-foundry-url': 'http://169.254.169.254/latest',
    },
  ],
  [
    'a Foundry URL with userinfo',
    {
      'x-portkey-provider': 'azure-ai',
      authorization: 'Bearer azure-secret',
      'x-portkey-azure-foundry-url': 'https://user:pw@evil.example',
    },
  ],
];

describe('realtime (Node) upstream URL', () => {
  test.each(badRealtime)('%s opens no WebSocket', async (_, headers) => {
    const c = realtimeContext(headers, '/v1/realtime');
    await realTimeHandlerNode(c);
    expect(wsInstances).toHaveLength(0);
    expect(c.get('websocketError')).toBe(true);
  });

  test('a crafted deployment cannot leave the resource host', async () => {
    const c = realtimeContext(
      azureRealtime({
        'x-portkey-azure-deployment-id': 'x@evil.example/#',
      }),
      '/v1/realtime'
    );
    await realTimeHandlerNode(c);
    expect(wsInstances).toHaveLength(1);
    expect(new URL(wsInstances[0].url).host).toBe('myres.openai.azure.com');
  });

  test('a crafted model on OpenAI stays on api.openai.com', async () => {
    const c = realtimeContext(
      { 'x-portkey-provider': 'openai', authorization: 'Bearer sk-secret' },
      '/v1/realtime?model=x@evil.example/v1'
    );
    await realTimeHandlerNode(c);
    expect(wsInstances).toHaveLength(1);
    const url = new URL(wsInstances[0].url);
    expect(url.protocol).toBe('wss:');
    expect(url.host).toBe('api.openai.com');
  });

  test('a valid Azure realtime connection uses wss and no redirects', async () => {
    const c = realtimeContext(azureRealtime(), '/v1/realtime');
    await realTimeHandlerNode(c);
    expect(wsInstances).toHaveLength(1);
    expect(wsInstances[0].url).toMatch(
      /^wss:\/\/myres\.openai\.azure\.com\/openai\/realtime\?/
    );
    expect(wsInstances[0].options.followRedirects).toBe(false);
    expect(wsInstances[0].options.headers['api-key']).toBe('azure-secret');
  });
});

describe('realtime (Workers) upstream URL', () => {
  test.each(badRealtime)('%s is refused with 400', async (_, headers) => {
    const res = await realTimeHandler(realtimeContext(headers, '/v1/realtime'));
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('a valid connection does not follow an upstream redirect', async () => {
    upstream(/myres\.openai\.azure\.com/, redirectTo(METADATA));
    const end = { accept: jest.fn() };
    (globalThis as any).WebSocketPair = function () {
      return [end, end];
    };
    let res: Response;
    try {
      res = await realTimeHandler(
        realtimeContext(azureRealtime(), '/v1/realtime')
      );
    } finally {
      delete (globalThis as any).WebSocketPair;
    }
    // No webSocket on a 3xx: the handler fails without a second request.
    expect(res.status).toBe(500);
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toMatch(
      /^https:\/\/myres\.openai\.azure\.com\/openai\/realtime\?/
    );
  });
});
