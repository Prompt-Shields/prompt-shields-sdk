/**
 * Regression tests: the proxy routes never let the request path choose the
 * upstream host (security).
 *
 * Upstream's Azure branch of `ProviderContext.getProxyPath` built the
 * upstream URL as `https:/${reqPath}` for any path containing
 * `.openai.azure.com`, so `/v1/proxy/evil.com/.openai.azure.com/...` sent the
 * Azure api-key to evil.com. The full-URL form now needs the configured host
 * exactly, and every proxied URL must keep the configured base URL's origin.
 */
jest.mock('jose', () => ({}));

import { json, post, resetHarness, upstream, upstreamCalls } from './harness';
import app from '../../index';
import Providers from '../../providers';
import { assertProxyURLOrigin } from '../../handlers/services/providerContext';

beforeEach(() => {
  resetHarness();
  upstream(/.*/, () => json({ ok: true }));
});

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

const chatBody = { messages: [{ role: 'user', content: 'hi' }] };

const origin = (url: string) => new URL(url).origin;

describe('Azure full-URL proxy paths', () => {
  test('a foreign host in the path is refused before any upstream call', async () => {
    const res = await post(
      app,
      '/v1/proxy/evil.com/.openai.azure.com/openai/deployments/x/chat/completions',
      chatBody,
      AZURE
    );
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(0);
  });

  test.each([
    '/v1/proxy/evil.openai.azure.com/openai/deployments/x/chat/completions',
    '/v1/proxy/myres.openai.azure.com.evil.com/openai/deployments/x/chat/completions',
    '/v1/proxy/myres.openai.azure.com@evil.com/openai/deployments/x/chat/completions',
    '/v1/proxy/myres.openai.azure.com:8443/openai/deployments/x/chat/completions',
    '/v1/proxy/evil.com%2F.openai.azure.com/openai/deployments/x/chat/completions',
    '/v1/proxy/evil.com\\.openai.azure.com/openai/deployments/x/chat/completions',
    '/v1/proxy//evil.com/myres.openai.azure.com/openai/deployments/x/chat/completions',
  ])('%s is refused', async (path) => {
    const res = await post(app, path, chatBody, AZURE);
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(0);
  });

  test('the configured resource host in the path still works', async () => {
    const res = await post(
      app,
      '/v1/proxy/myres.openai.azure.com/openai/deployments/x/chat/completions?api-version=2024-02-01',
      chatBody,
      AZURE
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toBe(
      'https://myres.openai.azure.com/openai/deployments/x/chat/completions?api-version=2024-02-01'
    );
    expect(upstreamCalls[0].headers['api-key']).toBe('azure-secret');
  });

  test('the host match is case-insensitive', async () => {
    const res = await post(
      app,
      '/v1/proxy/MyRes.OpenAI.Azure.com/openai/deployments/x/chat/completions',
      chatBody,
      AZURE
    );
    expect(res.status).toBe(200);
    expect(origin(upstreamCalls[0].url)).toBe('https://myres.openai.azure.com');
  });

  test('a relative proxy path goes to the configured resource', async () => {
    const res = await post(
      app,
      '/v1/proxy/deployments/x/chat/completions',
      chatBody,
      AZURE
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toBe(
      'https://myres.openai.azure.com/openai/deployments/x/chat/completions?api-version=2024-02-01'
    );
  });
});

const PROVIDERS: {
  name: string;
  headers: Record<string, string>;
  origin: string;
  legit: string;
  legitURL: string;
}[] = [
  {
    name: 'openai',
    headers: config({ provider: 'openai', api_key: 'sk-secret' }),
    origin: 'https://api.openai.com',
    legit: '/v1/proxy/chat/completions',
    legitURL: 'https://api.openai.com/v1/chat/completions',
  },
  {
    name: 'azure-openai',
    headers: AZURE,
    origin: 'https://myres.openai.azure.com',
    legit: '/v1/proxy/deployments/x/chat/completions',
    legitURL:
      'https://myres.openai.azure.com/openai/deployments/x/chat/completions?api-version=2024-02-01',
  },
  {
    name: 'anthropic',
    headers: config({ provider: 'anthropic', api_key: 'sk-ant-secret' }),
    origin: 'https://api.anthropic.com',
    legit: '/v1/proxy/messages',
    legitURL: 'https://api.anthropic.com/v1/messages',
  },
  {
    name: 'together-ai',
    headers: config({ provider: 'together-ai', api_key: 'tg-secret' }),
    origin: 'https://api.together.xyz',
    legit: '/v1/proxy/v1/chat/completions',
    legitURL: 'https://api.together.xyz/v1/chat/completions',
  },
  {
    name: 'openai (custom_host with a path)',
    headers: config({
      provider: 'openai',
      api_key: 'sk-secret',
      custom_host: 'https://llm-gw.example.com/openai/v1',
    }),
    origin: 'https://llm-gw.example.com',
    legit: '/v1/proxy/chat/completions',
    legitURL: 'https://llm-gw.example.com/openai/v1/chat/completions',
  },
  {
    name: 'ollama (custom_host)',
    headers: config({
      provider: 'ollama',
      custom_host: 'https://ollama.example.com',
    }),
    origin: 'https://ollama.example.com',
    legit: '/v1/proxy/api/chat',
    legitURL: 'https://ollama.example.com/api/chat',
  },
  {
    name: 'bedrock',
    headers: config({
      provider: 'bedrock',
      aws_access_key_id: 'AKIAEXAMPLE',
      aws_secret_access_key: 'secret',
      aws_region: 'us-east-1',
    }),
    origin: 'https://bedrock-runtime.us-east-1.amazonaws.com',
    legit: '/v1/proxy/model/m/converse',
    legitURL:
      'https://bedrock-runtime.us-east-1.amazonaws.com/model/m/converse',
  },
];

const CRAFTED = [
  '/v1/proxy.evil.com/chat/completions',
  '/v1/proxy@evil.com/chat/completions',
  '/v1/proxy:8443/chat/completions',
  '/v1/proxy//evil.com/chat/completions',
  '/v1/proxy/%2F%2Fevil.com/chat/completions',
  '/v1/proxy/%2f%2fevil.com%2fchat/completions',
  '/v1/proxy/\\\\evil.com/chat/completions',
  '/v1/proxy/@evil.com/chat/completions',
  '/v1/proxy/.evil.com/chat/completions',
  '/v1/proxy/:8443/chat/completions',
  '/v1/proxy/https://evil.com/chat/completions',
  '/v1/proxy/https:/evil.com/chat/completions',
  '/v1/proxy/https:%2F%2Fevil.com/chat/completions',
  '/v1/proxy/evil.com/.openai.azure.com/chat/completions',
  '/v1/proxy/evil.com/.openai.azure.com:8443/chat/completions',
  '/v1//evil.com/chat/completions',
  '/v1/proxy/chat/completions?x=@evil.com/',
];

describe.each(PROVIDERS)('$name proxy', (p) => {
  test('a legitimate proxy call reaches the configured upstream', async () => {
    const res = await post(app, p.legit, chatBody, p.headers);
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toBe(p.legitURL);
  });

  test.each(CRAFTED)('%s never leaves the configured origin', async (path) => {
    const res = await post(app, path, chatBody, p.headers);
    if (upstreamCalls.length === 0) {
      expect(res.status).toBe(400);
    }
    for (const call of upstreamCalls) {
      expect(origin(call.url)).toBe(p.origin);
      expect(call.url).not.toMatch(/^https?:\/\/[^/]*evil/);
    }
  });
});

describe('central origin check', () => {
  test('a URL off the base origin is refused', () => {
    const base = 'https://api.example.com/v1';
    expect(() =>
      assertProxyURLOrigin('https://api.example.com/v1/x', base)
    ).not.toThrow();
    for (const bad of [
      'https://api.example.com.evil.com/v1/x',
      'https://api.example.com@evil.com/v1/x',
      'https://user@api.example.com/v1/x',
      'https://api.example.com:8443/v1/x',
      'http://api.example.com/v1/x',
      '//evil.com/x',
      '/v1/x',
    ]) {
      expect(() => assertProxyURLOrigin(bad, base)).toThrow(
        'Invalid proxy path'
      );
    }
    expect(() => assertProxyURLOrigin('https://a.example.com/x', '')).toThrow(
      'Invalid proxy path'
    );
  });

  test('a buggy proxy builder fails closed with no upstream call', async () => {
    // together-ai's base URL is a bare host; a builder that dropped the
    // leading `/` would move the request to `api.together.xyz.evil.com`.
    const api = Providers['together-ai'].api;
    const builder = jest.fn(() => '.evil.com/v1/chat/completions');
    api.getProxyEndpoint = builder;
    try {
      const res = await post(
        app,
        '/v1/proxy/v1/chat/completions',
        chatBody,
        config({ provider: 'together-ai', api_key: 'tg-secret' })
      );
      expect(builder).toHaveBeenCalled();
      expect(res.status).toBe(400);
      expect(upstreamCalls).toHaveLength(0);
    } finally {
      delete api.getProxyEndpoint;
    }
  });
});

describe('non-proxy endpoints keep the base origin too', () => {
  test('request fields in the endpoint path stay on the configured host', async () => {
    const res = await post(
      app,
      '/v1/chat/completions',
      { ...chatBody, model: 'x@evil.com/' },
      AZURE
    );
    expect(res.status).toBe(200);
    expect(upstreamCalls).toHaveLength(1);
    expect(origin(upstreamCalls[0].url)).toBe('https://myres.openai.azure.com');
  });

  test('a buggy endpoint builder fails closed with no upstream call', async () => {
    const api = Providers['together-ai'].api;
    const original = api.getEndpoint;
    api.getEndpoint = jest.fn(() => '.evil.com/v1/chat/completions');
    try {
      const res = await post(
        app,
        '/v1/chat/completions',
        { ...chatBody, model: 'm' },
        config({ provider: 'together-ai', api_key: 'tg-secret' })
      );
      expect(api.getEndpoint).toHaveBeenCalled();
      expect(res.status).toBe(400);
      expect(upstreamCalls).toHaveLength(0);
    } finally {
      api.getEndpoint = original;
    }
  });
});
