/**
 * PS fork (security): guardrail plugin egress is SSRF-safe. The webhook, log
 * and vendor base-URL overrides come from caller-controlled guardrail config,
 * so internal and metadata hosts are refused, redirects are never followed,
 * and a refused request is an ordinary check error (failOnError unchanged).
 */
// HooksManager loads every plugin; `jose` (default.jwt) ships ESM only.
jest.mock('jose', () => ({}));

import http from 'http';
import { AddressInfo } from 'net';
import { Environment } from '../src/utils/env';
import {
  isSafePluginEgressURL,
  isSafeUpstreamURL,
} from '../src/middlewares/requestValidator';
import { HooksManager } from '../src/middlewares/hooks';
import { HookType } from '../src/middlewares/hooks/types';
import { pluginRequest, PluginEgressError } from './egress';
import { handler as webhookHandler } from './default/webhook';
import { handler as logHandler } from './default/log';
import { handler as validUrlsHandler } from './default/validUrls';
import { handler as aporiaHandler } from './aporia/validateProject';
import { handler as contentSafetyHandler } from './azure/contentSafety';
import { handler as shieldPromptHandler } from './azure/shieldPrompt';
import { pluginHandler as bedrockHandler } from './bedrock/index';
import { handler as javelinHandler } from './javelin/guardrails';

const env = Environment() as Record<string, any>;
const originalTrusted = env.TRUSTED_CUSTOM_HOSTS;

const context = {
  request: {
    text: 'hello',
    json: { messages: [{ role: 'user', content: 'hello' }] },
  },
  response: { text: '', json: {} },
  requestType: 'chatComplete' as const,
};

function listen(
  onRequest: http.RequestListener
): Promise<{ server: http.Server; port: number }> {
  return new Promise((resolve) => {
    const server = http.createServer(onRequest);
    server.listen(0, '127.0.0.1', () =>
      resolve({ server, port: (server.address() as AddressInfo).port })
    );
  });
}

afterEach(() => {
  env.TRUSTED_CUSTOM_HOSTS = originalTrusted;
  jest.restoreAllMocks();
});

describe('isSafePluginEgressURL', () => {
  test('refuses metadata, loopback, private and internal-TLD hosts', () => {
    env.TRUSTED_CUSTOM_HOSTS = undefined;
    for (const url of [
      'http://169.254.169.254/latest/meta-data/',
      'http://metadata.google.internal/computeMetadata/v1/',
      'http://localhost:8787/v1/health',
      'http://127.0.0.1:8080/',
      'http://10.0.0.5/',
      'http://192.168.1.1/',
      'http://[::1]/',
      'http://0x7f000001/',
      'http://redis.cache.svc.local/',
      'http://kube.internal/',
      'http://redis:6379/',
      'http://prompt-shields-collector:8000/partner/v1/health',
      'http://kubernetes.default.svc/',
      'http://user:pw@example.com/',
      'file:///etc/passwd',
    ]) {
      expect(isSafePluginEgressURL(url)).toBe(false);
    }
    expect(isSafePluginEgressURL('https://hooks.example.com/check')).toBe(true);
  });

  test('localhost is trusted only when listed in TRUSTED_CUSTOM_HOSTS', () => {
    env.TRUSTED_CUSTOM_HOSTS = undefined;
    // Provider custom hosts keep the development defaults (#209) ...
    expect(isSafeUpstreamURL('http://localhost:11434/v1')).toBe(true);
    // ... guardrail egress does not.
    expect(isSafePluginEgressURL('http://localhost:11434/v1')).toBe(false);

    env.TRUSTED_CUSTOM_HOSTS = 'localhost,guard.internal';
    expect(isSafePluginEgressURL('http://localhost:11434/v1')).toBe(true);
    expect(isSafePluginEgressURL('http://guard.internal/check')).toBe(true);
    expect(isSafePluginEgressURL('http://169.254.169.254/')).toBe(false);

    env.TRUSTED_CUSTOM_HOSTS = 'guardrails,::1';
    expect(isSafePluginEgressURL('http://guardrails:8080/check')).toBe(true);
    expect(isSafePluginEgressURL('http://[::1]:8080/check')).toBe(true);
    expect(isSafePluginEgressURL('http://redis:6379/')).toBe(false);
  });

  test('TRUSTED_CUSTOM_HOSTS=none trusts no host at all', () => {
    env.TRUSTED_CUSTOM_HOSTS = 'none';
    expect(isSafeUpstreamURL('http://localhost:11434/v1')).toBe(false);
    expect(isSafePluginEgressURL('http://localhost:11434/v1')).toBe(false);
    expect(isSafeUpstreamURL('https://api.openai.com/v1')).toBe(true);
  });
});

describe('webhook plugin', () => {
  test('a webhook to 169.254.169.254 is refused without a request', async () => {
    const fetchSpy = jest.spyOn(globalThis, 'fetch');
    const result = await webhookHandler(
      context,
      {
        webhookURL: 'http://169.254.169.254/latest/meta-data/iam/',
        headers: { 'Metadata-Flavor': 'Google' },
      } as any,
      'beforeRequestHook'
    );
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(result.verdict).toBe(false);
    expect(result.error).toBeInstanceOf(PluginEgressError);
    expect(result.data.explanation).toMatch(/refused/);
  });

  test('a 307 from an allowed host to an internal host is not followed', async () => {
    let internalHits = 0;
    const internal = await listen((_req, res) => {
      internalHits++;
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ verdict: true }));
    });
    const allowed = await listen((_req, res) => {
      res.writeHead(307, {
        location: `http://localhost:${internal.port}/latest/meta-data/`,
      });
      res.end();
    });
    // The allowed hook host is listed; the redirect target is not.
    env.TRUSTED_CUSTOM_HOSTS = '127.0.0.1';
    try {
      const result = await webhookHandler(
        context,
        { webhookURL: `http://127.0.0.1:${allowed.port}/hook` } as any,
        'beforeRequestHook'
      );
      expect(result.verdict).toBe(false);
      expect(result.error).toBeInstanceOf(PluginEgressError);
      expect(result.error.message).toMatch(/redirect \(307\)/);
      expect(internalHits).toBe(0);
    } finally {
      allowed.server.close();
      internal.server.close();
    }
  });

  test('a normal webhook still works', async () => {
    let received: any = null;
    const hook = await listen((req, res) => {
      let body = '';
      req.on('data', (c) => (body += c));
      req.on('end', () => {
        received = { body: JSON.parse(body), auth: req.headers['x-hook'] };
        res.writeHead(200, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ verdict: true, data: { ok: 1 } }));
      });
    });
    env.TRUSTED_CUSTOM_HOSTS = '127.0.0.1';
    try {
      const result = await webhookHandler(
        context,
        {
          webhookURL: `http://127.0.0.1:${hook.port}/hook`,
          headers: { 'x-hook': 'secret' },
        } as any,
        'beforeRequestHook'
      );
      expect(result.error).toBeNull();
      expect(result.verdict).toBe(true);
      expect(result.data.responseData).toEqual({ ok: 1 });
      expect(received.auth).toBe('secret');
      expect(received.body.eventType).toBe('beforeRequestHook');
    } finally {
      hook.server.close();
    }
  });
});

test('log plugin: refuses an internal log URL', async () => {
  const fetchSpy = jest.spyOn(globalThis, 'fetch');
  const result = await logHandler(
    context,
    { logURL: 'http://10.1.2.3:9200/_bulk' } as any,
    'beforeRequestHook'
  );
  expect(fetchSpy).not.toHaveBeenCalled();
  expect(result.verdict).toBe(false);
  expect(result.error).toBeInstanceOf(PluginEgressError);
});

test('validUrls: URLs in the text pointing at internal hosts are not fetched', async () => {
  const fetchSpy = jest.spyOn(globalThis, 'fetch');
  const result = await validUrlsHandler(
    {
      ...context,
      request: {
        ...context.request,
        text: 'see http://169.254.169.254/latest/meta-data/',
      },
    },
    { onlyDNS: false } as any,
    'beforeRequestHook'
  );
  expect(fetchSpy).not.toHaveBeenCalled();
  expect(result.verdict).toBe(false);
});

describe('vendor plugins: base-URL overrides and built hosts', () => {
  test('aporia apiEndpoint pointing at an internal host is refused', async () => {
    const fetchSpy = jest.spyOn(globalThis, 'fetch');
    const result = await aporiaHandler(
      context,
      {
        projectID: 'p1',
        credentials: { apiKey: 'k', apiEndpoint: 'http://169.254.169.254' },
      } as any,
      'beforeRequestHook'
    );
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(result.error).toBeInstanceOf(PluginEgressError);
  });

  test('azure contentSafety customHost on a private address is refused', async () => {
    const fetchSpy = jest.spyOn(globalThis, 'fetch');
    const result = await contentSafetyHandler(
      context,
      {
        credentials: {
          contentSafety: {
            resourceName: 'r',
            apiKey: 'k',
            customHost: 'http://10.0.0.5',
          },
        },
      } as any,
      'beforeRequestHook'
    );
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(result.error).toBeInstanceOf(PluginEgressError);
  });

  test('azure resourceName cannot swap the vendor host', async () => {
    const fetchSpy = jest.spyOn(globalThis, 'fetch');
    const result = await shieldPromptHandler(
      context,
      {
        credentials: {
          contentSafety: { resourceName: 'evil.example.com/x?', apiKey: 'k' },
        },
      } as any,
      'beforeRequestHook'
    );
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(result.error).toBeInstanceOf(PluginEgressError);
  });

  test('bedrock awsRegion cannot swap the vendor host', async () => {
    const fetchSpy = jest.spyOn(globalThis, 'fetch');
    const result = await bedrockHandler(
      context,
      {
        guardrailId: 'g',
        guardrailVersion: '1',
        credentials: {
          awsAccessKeyId: 'a',
          awsSecretAccessKey: 's',
          awsRegion: 'x@169.254.169.254/#',
        },
      } as any,
      'beforeRequestHook',
      { env: {} } as any
    );
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(result.error?.message).toMatch(/Invalid AWS region/);
  });

  test('javelin domain pointing at localhost is refused', async () => {
    env.TRUSTED_CUSTOM_HOSTS = undefined;
    const fetchSpy = jest.spyOn(globalThis, 'fetch');
    jest.spyOn(console, 'log').mockImplementation(() => {});
    jest.spyOn(console, 'error').mockImplementation(() => {});
    const result = await javelinHandler(
      context,
      { credentials: { apiKey: 'k', domain: 'localhost:8787' } } as any,
      'beforeRequestHook'
    );
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(result.error).toBeTruthy();
  });
});

describe('pluginRequest', () => {
  test('caps the response body', async () => {
    const big = await listen((_req, res) => {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end('x'.repeat(1000));
    });
    env.TRUSTED_CUSTOM_HOSTS = '127.0.0.1';
    try {
      await expect(
        pluginRequest(
          `http://127.0.0.1:${big.port}/`,
          {},
          { maxResponseBytes: 100 }
        )
      ).rejects.toThrow(/too large/);
    } finally {
      big.server.close();
    }
  });

  test('times out', async () => {
    const slow = await listen(() => {
      /* never answers */
    });
    env.TRUSTED_CUSTOM_HOSTS = '127.0.0.1';
    try {
      await expect(
        pluginRequest(`http://127.0.0.1:${slow.port}/`, {}, { timeout: 50 })
      ).rejects.toMatchObject({ name: 'AbortError' });
    } finally {
      slow.server.closeAllConnections();
      slow.server.close();
    }
  });
});

describe('failOnError semantics are unchanged for a refused egress', () => {
  async function runWebhookHook(failOnError: boolean) {
    const manager = new HooksManager();
    const span = manager.createSpan(
      { messages: [{ role: 'user', content: 'hello' }] },
      {},
      'openai',
      false,
      [
        {
          id: 'guard-1',
          type: HookType.GUARDRAIL,
          eventType: 'beforeRequestHook',
          deny: true,
          checks: [
            {
              id: 'default.webhook',
              parameters: {
                webhookURL: 'http://169.254.169.254/latest/meta-data/',
                failOnError,
              },
            },
          ],
        },
      ],
      [],
      null,
      'chatComplete',
      {}
    );
    return manager.executeHooks(span.id, ['syncBeforeRequestHook'], {
      env: {},
    } as any);
  }

  test('without failOnError the errored check does not deny (fail open)', async () => {
    const { results, shouldDeny } = await runWebhookHook(false);
    expect(shouldDeny).toBe(false);
    expect(results[0].verdict).toBe(true);
    expect(results[0].checks[0].error?.name).toBe('PluginEgressError');
  });

  test('with failOnError the errored check denies', async () => {
    const { results, shouldDeny } = await runWebhookHook(true);
    expect(shouldDeny).toBe(true);
    expect(results[0].verdict).toBe(false);
    expect(results[0].checks[0].fail_on_error).toBe(true);
  });
});
