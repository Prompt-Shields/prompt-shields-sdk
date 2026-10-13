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

function callHeaders(config: any, providerOptions: Partial<Options>) {
  return config.headers({
    providerOptions: {
      provider: 'azure-openai',
      apiKey: 'caller-key',
      ...providerOptions,
    } as Options,
    fn: 'chatComplete',
    c: {},
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
