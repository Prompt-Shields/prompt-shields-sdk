// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../../utils/env', () => ({ Environment: () => ({}) }));

import Providers from '..';
import { CLAUDE_PLATFORM_AWS, VALID_PROVIDERS } from '../../globals';
import { isSafeUpstreamURL } from '../../middlewares/requestValidator';
import { transformUsingProviderConfig } from '../../services/transformToProviderRequest';

const config: any = Providers[CLAUDE_PLATFORM_AWS];
const c: any = { req: { method: 'POST' }, env: {}, get: () => undefined };
const URL_ = 'https://aws-external-anthropic.eu-west-1.api.aws/v1/messages';

const baseURL = (providerOptions: object) =>
  config.api.getBaseURL({ providerOptions, fn: 'chatComplete', c });

const headers = (providerOptions: object, extra: object = {}) =>
  config.api.headers({
    c,
    fn: 'chatComplete',
    providerOptions,
    transformedRequestBody: { model: 'claude-haiku-4-5', max_tokens: 8 },
    transformedRequestUrl: URL_,
    gatewayRequestBody: {},
    ...extra,
  });

const sigv4 = {
  awsAccessKeyId: 'AKIATEST',
  awsSecretAccessKey: 'secret',
  awsRegion: 'eu-west-1',
  anthropicAwsWorkspaceId: 'wrkspc_1',
};

test('is registered as a valid provider', () => {
  expect(VALID_PROVIDERS).toContain('claude-platform-aws');
  expect(config).toBeDefined();
});

describe('base URL', () => {
  test('is the regional api.aws host', () => {
    const url = baseURL({ awsRegion: 'eu-west-1' });
    expect(url).toBe('https://aws-external-anthropic.eu-west-1.api.aws/v1');
    expect(isSafeUpstreamURL(url)).toBe(true);
  });

  test('needs a valid region and has no default', () => {
    expect(() => baseURL({})).toThrow(/region/);
    expect(() => baseURL({ awsRegion: 'evil.example/#' })).toThrow(
      /Invalid AWS region/
    );
  });

  test('routes chat and messages to /messages', () => {
    expect(config.api.getEndpoint({ fn: 'chatComplete' })).toBe('/messages');
    expect(config.api.getEndpoint({ fn: 'messages' })).toBe('/messages');
    expect(config.api.getEndpoint({ fn: 'messagesCountTokens' })).toBe(
      '/messages/count_tokens'
    );
    expect(config.api.getEndpoint({ fn: 'embed' })).toBe('');
  });
});

describe('headers', () => {
  test('API key goes in x-api-key with the workspace and version', async () => {
    expect(
      await headers({
        apiKey: 'k',
        awsRegion: 'eu-west-1',
        anthropicAwsWorkspaceId: 'wrkspc_1',
        anthropicBeta: 'files-api-2025-04-14',
      })
    ).toEqual({
      'x-api-key': 'k',
      'anthropic-version': '2023-06-01',
      'anthropic-workspace-id': 'wrkspc_1',
      'anthropic-beta': 'files-api-2025-04-14',
      'content-type': 'application/json',
    });
  });

  test('SigV4 uses the aws-external-anthropic service', async () => {
    const h = await headers(sigv4);
    expect(h.authorization).toMatch(
      /^AWS4-HMAC-SHA256 Credential=AKIATEST\/\d{8}\/eu-west-1\/aws-external-anthropic\/aws4_request, SignedHeaders=anthropic-version;anthropic-workspace-id;content-type;host;x-amz-content-sha256;x-amz-date,/
    );
    expect(h['x-api-key']).toBeUndefined();
    expect(h.host).toBe('aws-external-anthropic.eu-west-1.api.aws');
  });

  test('AWS keys win over an API key unless awsAuthType is apiKey', async () => {
    expect((await headers({ ...sigv4, apiKey: 'k' })).authorization).toMatch(
      /^AWS4-HMAC-SHA256/
    );
    expect(
      (await headers({ ...sigv4, apiKey: 'k', awsAuthType: 'apiKey' }))[
        'x-api-key'
      ]
    ).toBe('k');
  });

  test('never signs for another host', async () => {
    for (const url of [
      'https://claude.example.com/v1/messages',
      'https://aws-external-anthropic.us-east-1.api.aws/v1/messages',
      'http://aws-external-anthropic.eu-west-1.api.aws/v1/messages',
    ]) {
      await expect(
        headers(sigv4, { transformedRequestUrl: url })
      ).rejects.toThrow(/regional endpoint/);
    }
  });

  test('rejects look-alike, userinfo, port and path-smuggled URLs', async () => {
    for (const url of [
      'https://aws-external-anthropic.eu-west-1.api.aws.evil.com/v1/messages',
      'https://evil.com/aws-external-anthropic.eu-west-1.api.aws/v1/messages',
      'https://evil.com?x=aws-external-anthropic.eu-west-1.api.aws',
      'https://x-aws-external-anthropic.eu-west-1.api.aws/v1/messages',
      'https://aws-external-anthropic.eu-west-1.api.aws:8443/v1/messages',
      'https://aws-external-anthropic.eu-west-1.api.aws@evil.com/v1/messages',
      'https://u:p@aws-external-anthropic.eu-west-1.api.aws/v1/messages',
      'https://evil.api.aws/v1/messages',
    ]) {
      await expect(
        headers(sigv4, { transformedRequestUrl: url })
      ).rejects.toThrow(/regional endpoint|Invalid AWS/);
    }
  });

  test('rejects regions that would change the host', () => {
    for (const awsRegion of [
      'eu-west-1.evil.com',
      'eu-west-1/',
      'eu-west-1@evil.com',
      'eu-west-1:443',
      'eu-west-1\r\nx: y',
      'EU-WEST-1',
      ' eu-west-1',
      'eu-west-1 ',
    ]) {
      expect(() => baseURL({ awsRegion })).toThrow(/Invalid AWS region/);
    }
  });

  test('serviceRole needs AWS_AMBIENT_CREDENTIALS_ENABLED, like Bedrock', async () => {
    await expect(
      headers({
        awsAuthType: 'serviceRole',
        awsRegion: 'eu-west-1',
        anthropicAwsWorkspaceId: 'wrkspc_1',
      })
    ).rejects.toThrow(/serviceRole is disabled/);
    await expect(
      headers({
        awsAuthType: 'assumedRole',
        awsRegion: 'eu-west-1',
        anthropicAwsWorkspaceId: 'wrkspc_1',
      })
    ).rejects.toThrow(/role ARN/);
  });

  test('needs a workspace id and some credential', async () => {
    await expect(
      headers({ ...sigv4, anthropicAwsWorkspaceId: undefined })
    ).rejects.toThrow(/workspace/);
    await expect(
      headers({ awsRegion: 'eu-west-1', anthropicAwsWorkspaceId: 'w' })
    ).rejects.toThrow(/API key or AWS credentials/);
  });
});

describe('transforms', () => {
  test('chat request becomes a Messages body', () => {
    const body: any = transformUsingProviderConfig(config.chatComplete, {
      model: 'claude-haiku-4-5',
      messages: [
        { role: 'system', content: 'be brief' },
        { role: 'user', content: 'hi' },
      ],
      max_tokens: 16,
    } as any);
    expect(body).toMatchObject({
      model: 'claude-haiku-4-5',
      max_tokens: 16,
      messages: [{ role: 'user', content: 'hi' }],
    });
    expect(JSON.stringify(body.system)).toContain('be brief');
  });

  test('chat response is OpenAI-shaped and tagged with the provider', () => {
    const out = config.responseTransforms.chatComplete(
      {
        id: 'msg_1',
        type: 'message',
        role: 'assistant',
        model: 'claude-haiku-4-5',
        content: [{ type: 'text', text: 'Hi' }],
        stop_reason: 'end_turn',
        usage: { input_tokens: 3, output_tokens: 1 },
      },
      200,
      new Headers(),
      true,
      'https://x',
      {},
      {}
    );
    expect(out).toMatchObject({
      provider: 'claude-platform-aws',
      model: 'claude-haiku-4-5',
      choices: [{ message: { content: 'Hi' }, finish_reason: 'stop' }],
      usage: { prompt_tokens: 3, completion_tokens: 1, total_tokens: 4 },
    });
  });
});

describe('assertAwsUrl with a fixed domain', () => {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const { assertAwsUrl } = require('../../utils/awsAuth');
  test('accepts only https subdomains of the given domain', () => {
    expect(assertAwsUrl(URL_, {}, 'api.aws')).toBe(URL_);
    for (const url of [
      'https://aws-external-anthropic.eu-west-1.api.aws.evil.com/',
      'https://evilapi.aws/',
      'https://api.aws/',
      'http://aws-external-anthropic.eu-west-1.api.aws/',
      'https://aws-external-anthropic.eu-west-1.api.aws:444/',
      'https://a@aws-external-anthropic.eu-west-1.api.aws/',
    ]) {
      expect(() => assertAwsUrl(url, {}, 'api.aws')).toThrow();
    }
  });
  test('without a domain, existing callers still require amazonaws.com', () => {
    expect(() => assertAwsUrl(URL_, {})).toThrow();
    expect(
      assertAwsUrl('https://bedrock-runtime.us-east-1.amazonaws.com/x', {})
    ).toBeTruthy();
  });
});
