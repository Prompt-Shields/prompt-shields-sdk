import { getAccessToken } from './utils';
import { AzureCredentials } from './types';

const fetchSpy = jest.spyOn(globalThis, 'fetch');

const managed = {
  resourceName: 'https://cognitiveservices.azure.com/',
  azureAuthMode: 'managed',
} as AzureCredentials;

beforeEach(() => {
  fetchSpy.mockReset();
  fetchSpy.mockRejectedValue(new Error('no network in tests'));
});

describe('azure guardrail host Azure identity', () => {
  test('refuses managed identity unless the operator opts in', async () => {
    const result = await getAccessToken(managed, 'contentSafety', {}, {});
    expect(result.token).toBe('');
    expect(result.error).toMatch(/disabled/);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('uses the host identity when enabled', async () => {
    fetchSpy.mockResolvedValue(
      new Response(JSON.stringify({ access_token: 'host-token' }))
    );
    const result = await getAccessToken(
      managed,
      'contentSafety',
      {},
      { AZURE_AMBIENT_CREDENTIALS_ENABLED: 'true' }
    );
    expect(result).toEqual({ token: 'host-token', error: null });
    expect(String(fetchSpy.mock.calls[0][0])).toContain('169.254.169.254');
  });
});
