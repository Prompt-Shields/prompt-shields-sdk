// utils/env uses top-level await, which ts-jest cannot compile here.
const mockEnv: Record<string, any> = {};
jest.mock('../../utils/env', () => ({ Environment: () => mockEnv }));

import fs from 'fs';
import { GatewayError } from '../../errors/GatewayError';
import { Options } from '../../types/requestBody';
import AzureOpenAIAPIConfig from './api';
import AzureAIInferenceAPI from '../azure-ai-inference/api';

const fetchSpy = jest.spyOn(globalThis, 'fetch');
const readFileSpy = jest.spyOn(fs, 'readFileSync');

const configs = [
  ['azure-openai', AzureOpenAIAPIConfig],
  ['azure-ai-inference', AzureAIInferenceAPI],
] as const;

const AZURE_URL =
  'https://my-resource.openai.azure.com/openai/deployments/d/chat/completions';

function callHeaders(
  config: any,
  providerOptions: Partial<Options>,
  transformedRequestUrl: string = AZURE_URL,
  requestHeaders: Record<string, string> = {},
  fn: string = 'chatComplete'
) {
  return config.headers({
    providerOptions: {
      provider: 'azure-openai',
      apiKey: 'caller-key',
      ...providerOptions,
    } as Options,
    fn,
    transformedRequestUrl,
    c: { req: { header: (name: string) => requestHeaders[name] } },
  });
}

beforeEach(() => {
  for (const k of Object.keys(mockEnv)) delete mockEnv[k];
  fetchSpy.mockReset();
  fetchSpy.mockRejectedValue(new Error('no network in tests'));
  readFileSpy.mockClear();
});

afterAll(() => {
  readFileSpy.mockRestore();
});

describe.each(configs)('%s host Azure identity', (_name, config) => {
  test.each(['managed', 'workload'])(
    'refuses azureAuthMode %s unless the operator opts in',
    async (azureAuthMode) => {
      mockEnv.AZURE_AUTHORITY_HOST = 'https://login.microsoftonline.com';
      mockEnv.AZURE_TENANT_ID = 'tenant';
      mockEnv.AZURE_CLIENT_ID = 'host-client';
      mockEnv.AZURE_FEDERATED_TOKEN_FILE = '/var/run/secrets/azure/token';
      const err = await callHeaders(config, { azureAuthMode }).catch(
        (e: unknown) => e
      );
      expect(err).toBeInstanceOf(GatewayError);
      expect(err.status).toBe(403);
      // Neither IMDS nor Entra was called, and the federated token was not read.
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(readFileSpy).not.toHaveBeenCalled();
    }
  );

  test('only "true" enables it', async () => {
    mockEnv.AZURE_AMBIENT_CREDENTIALS_ENABLED = '1';
    await expect(
      callHeaders(config, { azureAuthMode: 'managed' })
    ).rejects.toThrow(GatewayError);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('managed uses the host identity when enabled', async () => {
    mockEnv.AZURE_AMBIENT_CREDENTIALS_ENABLED = 'TRUE';
    fetchSpy.mockResolvedValue(
      new Response(JSON.stringify({ access_token: 'host-token' }))
    );
    const headers = await callHeaders(config, { azureAuthMode: 'managed' });
    expect(headers.Authorization).toBe('Bearer host-token');
    expect(String(fetchSpy.mock.calls[0][0])).toContain(
      'http://169.254.169.254/metadata/identity/oauth2/token'
    );
  });

  test('caller-supplied credentials are unaffected', async () => {
    const headers = await callHeaders(config, {});
    expect(headers['api-key'] ?? headers.Authorization).toContain('caller-key');
    const withToken = await callHeaders(config, {
      azureAuthMode: 'managed',
      azureAdToken: 'Bearer caller-ad-token',
    });
    expect(withToken.Authorization).toBe('Bearer caller-ad-token');
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe.each(configs)(
  '%s host Azure token stays on Azure',
  (_name, config) => {
    beforeEach(() => {
      mockEnv.AZURE_AMBIENT_CREDENTIALS_ENABLED = 'true';
      mockEnv.AZURE_AUTHORITY_HOST = 'https://login.microsoftonline.com';
      mockEnv.AZURE_TENANT_ID = 'tenant';
      mockEnv.AZURE_CLIENT_ID = 'host-client';
      mockEnv.AZURE_FEDERATED_TOKEN_FILE = '/var/run/secrets/azure/token';
    });

    async function expectRefused(
      azureAuthMode: string,
      providerOptions: Partial<Options>,
      url?: string,
      requestHeaders?: Record<string, string>
    ) {
      const err = await callHeaders(
        config,
        { azureAuthMode, ...providerOptions },
        url,
        requestHeaders
      ).catch((e: unknown) => e);
      expect(err).toBeInstanceOf(GatewayError);
      expect(err.status).toBe(400);
      // No token was minted: IMDS and Entra untouched, federated token unread.
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(readFileSpy).not.toHaveBeenCalled();
    }

    describe.each(['managed', 'workload'])('azureAuthMode %s', (mode) => {
      test('refuses a custom host from config', async () => {
        await expectRefused(mode, { customHost: 'https://evil.example' });
      });

      test('refuses a custom host from the x-portkey-custom-host header', async () => {
        await expectRefused(mode, {}, AZURE_URL, {
          'x-portkey-custom-host': 'https://evil.example',
        });
      });

      test.each([
        'evil.example/',
        'evil.example#',
        'evil.example?',
        'evil.example:443/x',
        'a.b',
        'user@evil',
        '-leading',
        'a'.repeat(64),
      ])('refuses resource name %p', async (resourceName) => {
        await expectRefused(mode, { resourceName });
      });

      test.each([
        'https://evil.example/models',
        'http://my-hub.services.ai.azure.com/models',
        'https://my-hub.services.ai.azure.com.evil.example/models',
        'https://user@my-hub.services.ai.azure.com/models',
        'https://my-hub.services.ai.azure.com:8443/models',
        'https://evil.eastus.cloudapp.azure.com/models',
        'https://services.ai.azure.com/models',
        'not a url',
      ])('refuses Foundry URL %p', async (azureFoundryUrl) => {
        await expectRefused(mode, { azureFoundryUrl });
      });

      test.each([
        // The azure-openai proxy path trick: /v1/proxy/evil.example/x.openai.azure.com/...
        'https://evil.example/x.openai.azure.com/openai/deployments/d',
        'https://evil.example/openai',
        'https://my-resource.openai.azure.com.evil.example/openai',
        'wss://evil.example/openai/realtime',
        '',
      ])('refuses final upstream URL %p', async (url) => {
        await expectRefused(mode, {}, url);
      });
    });

    test('missing upstream URL is refused', async () => {
      const err = await config
        .headers({
          providerOptions: {
            provider: 'azure-openai',
            azureAuthMode: 'managed',
          } as Options,
          fn: 'chatComplete',
          c: {},
        } as any)
        .catch((e: unknown) => e);
      expect(err).toBeInstanceOf(GatewayError);
      expect(err.status).toBe(400);
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    test.each([
      [{ resourceName: 'my-resource' }, AZURE_URL],
      [
        { azureFoundryUrl: 'https://my-hub.services.ai.azure.com/models' },
        'https://my-hub.services.ai.azure.com/models/chat/completions',
      ],
      [
        { azureFoundryUrl: 'https://my-ep.eastus2.models.ai.azure.com' },
        'https://my-ep.eastus2.models.ai.azure.com/chat/completions',
      ],
      [{}, 'https://models.inference.ai.azure.com/chat/completions'],
      [{}, 'https://my-res.cognitiveservices.azure.com/openai/x'],
      [{}, 'wss://my-resource.openai.azure.com/openai/realtime?deployment=d'],
      [
        { azureEntraScope: 'https://cognitiveservices.azure.com/.default' },
        AZURE_URL,
      ],
      [{ azureEntraScope: 'https://cognitiveservices.azure.com/' }, AZURE_URL],
    ])('allows Azure upstream %p %s', async (providerOptions, url) => {
      fetchSpy.mockResolvedValue(
        new Response(JSON.stringify({ access_token: 'host-token' }))
      );
      const headers = await callHeaders(
        config,
        { azureAuthMode: 'managed', ...providerOptions },
        url
      );
      expect(headers.Authorization).toBe('Bearer host-token');
    });

    describe.each(['managed', 'workload'])('azureAuthMode %s scope', (mode) => {
      test.each([
        'https://management.azure.com/',
        'https://management.azure.com/.default',
        'https://storage.azure.com/.default',
        'https://vault.azure.net/.default',
        'https://graph.microsoft.com/.default',
        'api://some-app/.default',
      ])('refuses caller scope %p', async (azureEntraScope) => {
        await expectRefused(mode, { azureEntraScope });
      });

      test('refuses a storage scope outside the batch-output download', async () => {
        await expectRefused(
          mode,
          { azureEntraScope: 'https://storage.azure.com/.default' },
          'https://acct.blob.core.windows.net/c/x.openai.azure.com'
        );
      });

      test('refuses Blob Storage for caller requests (proxy path)', async () => {
        await expectRefused(
          mode,
          {},
          'https://acct.blob.core.windows.net/c/x.openai.azure.com'
        );
      });

      test('refuses the storage scope on a non-Blob host for file content', async () => {
        const err = await callHeaders(
          config,
          {
            azureAuthMode: mode,
            azureEntraScope: 'https://storage.azure.com/.default',
          },
          AZURE_URL,
          {},
          'retrieveFileContent'
        ).catch((e: unknown) => e);
        expect(err).toBeInstanceOf(GatewayError);
        expect(err.status).toBe(400);
        expect(fetchSpy).not.toHaveBeenCalled();
      });
    });

    test('batch output blob download may use a storage token', async () => {
      fetchSpy.mockResolvedValue(
        new Response(JSON.stringify({ access_token: 'host-token' }))
      );
      const headers = await callHeaders(
        config,
        {
          azureAuthMode: 'managed',
          azureEntraScope: 'https://storage.azure.com/.default',
        },
        'https://acct.blob.core.windows.net/c/output.jsonl',
        {},
        'retrieveFileContent'
      );
      expect(headers.Authorization).toBe('Bearer host-token');
    });

    test('caller-supplied credentials may still use a custom host', async () => {
      const headers = await callHeaders(
        config,
        { customHost: 'https://my-proxy.example', resourceName: 'x/evil#' },
        'https://my-proxy.example/openai/chat/completions',
        { 'x-portkey-custom-host': 'https://my-proxy.example' }
      );
      expect(headers['api-key'] ?? headers.Authorization).toContain(
        'caller-key'
      );
      const withToken = await callHeaders(
        config,
        {
          azureAuthMode: 'managed',
          azureAdToken: 'caller-ad-token',
          customHost: 'https://my-proxy.example',
        },
        'https://my-proxy.example/openai'
      );
      expect(withToken.Authorization).toBe('Bearer caller-ad-token');
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    test('entra with caller client secret may still use a custom host', async () => {
      fetchSpy.mockResolvedValue(
        new Response(JSON.stringify({ access_token: 'caller-entra-token' }))
      );
      const headers = await callHeaders(
        config,
        {
          azureAuthMode: 'entra',
          azureEntraTenantId: 't',
          azureEntraClientId: 'c',
          azureEntraClientSecret: 's',
          customHost: 'https://my-proxy.example',
        },
        'https://my-proxy.example/openai'
      );
      expect(headers.Authorization).toBe('Bearer caller-entra-token');
    });
  }
);
