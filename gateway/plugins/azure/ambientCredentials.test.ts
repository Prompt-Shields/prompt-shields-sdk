import { getAccessToken } from './utils';
import { AzureCredentials } from './types';
import { handler as contentSafetyHandler } from './contentSafety';
import { handler as shieldPromptHandler } from './shieldPrompt';
import { handler as protectedMaterialHandler } from './protectedMaterial';
import { handler as piiHandler } from './pii';

const fetchSpy = jest.spyOn(globalThis, 'fetch');

const managed = {
  resourceName: 'my-resource',
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
    expect(String(fetchSpy.mock.calls[0][0])).toContain(
      `resource=${encodeURIComponent('https://cognitiveservices.azure.com/')}`
    );
  });
});

describe('azure guardrails fail closed on host Azure identity', () => {
  const context = {
    request: {
      text: 'hello',
      json: { messages: [{ role: 'user', content: 'hello' }] },
    },
    response: {
      text: 'hello',
      json: { choices: [{ message: { role: 'assistant', content: 'hello' } }] },
    },
    requestType: 'chatComplete',
  } as any;

  const cases = [
    [
      'contentSafety',
      contentSafetyHandler,
      'contentSafety',
      'beforeRequestHook',
    ],
    ['shieldPrompt', shieldPromptHandler, 'contentSafety', 'beforeRequestHook'],
    [
      'protectedMaterial',
      protectedMaterialHandler,
      'contentSafety',
      'afterRequestHook',
    ],
    ['pii', piiHandler, 'pii', 'beforeRequestHook'],
  ] as const;

  test.each(cases)(
    '%s denies when managed identity is disabled',
    async (_name, handler, credKey, eventType) => {
      const result = await (handler as any)(
        context,
        { credentials: { [credKey]: managed } },
        eventType,
        { env: {} }
      );
      expect(result.verdict).toBe(false);
      expect(String(result.error?.message ?? result.error)).toMatch(/disabled/);
      expect(fetchSpy).not.toHaveBeenCalled();
    }
  );

  test.each(cases)(
    '%s reaches IMDS when enabled',
    async (_name, handler, credKey, eventType) => {
      await (handler as any)(
        context,
        { credentials: { [credKey]: managed } },
        eventType,
        { env: { AZURE_AMBIENT_CREDENTIALS_ENABLED: 'true' } }
      );
      expect(String(fetchSpy.mock.calls[0][0])).toContain('169.254.169.254');
    }
  );
});

describe('azure guardrails keep the host token on Azure', () => {
  const enabled = { AZURE_AMBIENT_CREDENTIALS_ENABLED: 'true' };
  const context = {
    request: {
      text: 'hello',
      json: { messages: [{ role: 'user', content: 'hello' }] },
    },
    requestType: 'chatComplete',
  } as any;

  test.each([
    [{ customHost: 'https://evil.example' }, /custom host/],
    [{ resourceName: 'evil.example#' }, /resource name/],
    [{ resourceName: 'evil.example/' }, /resource name/],
    [{ resourceName: 'https://cognitiveservices.azure.com/' }, /resource name/],
    [{ resourceName: '' }, /resource name/],
  ])('refuses %p before any token request', async (override, message) => {
    const result = await getAccessToken(
      { ...managed, ...override } as AzureCredentials,
      'contentSafety',
      {},
      enabled
    );
    expect(result).toMatchObject({ token: '', denied: true });
    expect(result.error).toMatch(message);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test.each([
    ['contentSafety', contentSafetyHandler, 'contentSafety'],
    ['pii', piiHandler, 'pii'],
  ] as const)(
    '%s fails closed on a custom host with managed identity',
    async (_name, handler, credKey) => {
      const result = await (handler as any)(
        context,
        {
          credentials: {
            [credKey]: { ...managed, customHost: 'https://evil.example' },
          },
        },
        'beforeRequestHook',
        { env: enabled }
      );
      expect(result.verdict).toBe(false);
      expect(fetchSpy).not.toHaveBeenCalled();
    }
  );

  test('api key with a custom host is unaffected', async () => {
    const result = await getAccessToken(
      {
        resourceName: 'x',
        azureAuthMode: 'apiKey',
        apiKey: 'caller-key',
        customHost: 'https://my-private-link.example',
      } as AzureCredentials,
      'contentSafety',
      {},
      enabled
    );
    expect(result).toEqual({ token: 'caller-key', error: null });
  });
});
