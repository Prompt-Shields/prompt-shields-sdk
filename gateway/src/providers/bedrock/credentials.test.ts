// utils/env uses top-level await, which ts-jest cannot compile here.
const mockEnv: Record<string, any> = {};
jest.mock('../../utils/env', () => ({ Environment: () => mockEnv }));

import { GatewayError } from '../../errors/GatewayError';
import { Options } from '../../types/requestBody';
import { providerAssumedRoleCredentials } from './utils';

const fetchSpy = jest.spyOn(globalThis, 'fetch');

beforeEach(() => {
  for (const k of Object.keys(mockEnv)) delete mockEnv[k];
  fetchSpy.mockReset();
  fetchSpy.mockRejectedValue(new Error('no network in tests'));
});

describe('providerAssumedRoleCredentials', () => {
  test('ignores other auth types', async () => {
    const opts = { provider: 'bedrock', awsAuthType: 'accessKey' } as Options;
    await providerAssumedRoleCredentials(opts, {});
    expect(opts.awsAccessKeyId).toBeUndefined();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('refuses serviceRole unless the operator opts in', async () => {
    mockEnv.AWS_ACCESS_KEY_ID = 'AKIAHOST';
    mockEnv.AWS_SECRET_ACCESS_KEY = 'host-secret';
    const opts = { provider: 'bedrock', awsAuthType: 'serviceRole' } as Options;
    const err = await providerAssumedRoleCredentials(opts, {}).catch((e) => e);
    expect(err).toBeInstanceOf(GatewayError);
    expect(err.status).toBe(403);
    expect(opts.awsAccessKeyId).toBeUndefined();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('assumedRole never falls back to host credentials by default', async () => {
    mockEnv.AWS_ACCESS_KEY_ID = 'AKIAHOST';
    mockEnv.AWS_SECRET_ACCESS_KEY = 'host-secret';
    const opts = {
      provider: 'bedrock',
      awsAuthType: 'assumedRole',
      awsRoleArn: 'arn:aws:iam::111122223333:role/victim',
      awsRegion: 'us-east-1',
    } as Options;
    await expect(providerAssumedRoleCredentials(opts, {})).rejects.toThrow(
      GatewayError
    );
    expect(opts.awsAccessKeyId).toBeUndefined();
    // No STS, IMDS or ECS call was attempted with the host identity.
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('serviceRole uses host credentials when enabled', async () => {
    mockEnv.AWS_AMBIENT_CREDENTIALS_ENABLED = 'true';
    mockEnv.AWS_ACCESS_KEY_ID = 'AKIAHOST';
    mockEnv.AWS_SECRET_ACCESS_KEY = 'host-secret';
    mockEnv.AWS_REGION = 'eu-north-1';
    const opts = { provider: 'bedrock', awsAuthType: 'serviceRole' } as Options;
    await providerAssumedRoleCredentials(opts, {});
    expect(opts.awsAccessKeyId).toBe('AKIAHOST');
    expect(opts.awsSecretAccessKey).toBe('host-secret');
  });

  test('a caller-set region wins over the credentials region', async () => {
    mockEnv.AWS_AMBIENT_CREDENTIALS_ENABLED = 'true';
    mockEnv.AWS_ACCESS_KEY_ID = 'AKIAHOST';
    mockEnv.AWS_SECRET_ACCESS_KEY = 'host-secret';
    const opts = {
      provider: 'bedrock',
      awsAuthType: 'serviceRole',
      awsRegion: 'eu-west-1',
    } as Options;
    await providerAssumedRoleCredentials(opts, {});
    expect(opts.awsRegion).toBe('eu-west-1');
  });
});

describe('AWS host guard', () => {
  // Imported lazily so the env mock above applies.
  const { assertAwsUrl, isValidAwsRegion, fetchSTSAssumeRoleCredentials } =
    jest.requireActual('../../utils/awsAuth');
  const BedrockAPIConfig = jest.requireActual('./api').default;

  test.each(['us-east-1', 'eu-north-1', 'us-gov-west-1', 'ap-southeast-4'])(
    'accepts region %s',
    (r) => expect(isValidAwsRegion(r)).toBe(true)
  );

  test.each([
    'x@evil.example/',
    'evil.example#',
    'us-east-1.evil',
    'us-east-1/',
    '',
    undefined,
  ])('rejects region %p', (r) => expect(isValidAwsRegion(r)).toBe(false));

  test.each([
    'https://evil.example/.s3.us-east-1.amazonaws.com',
    'https://bedrock-runtime.x@evil.example/.amazonaws.com',
    'http://bedrock-runtime.us-east-1.amazonaws.com',
    'https://bedrock-runtime.us-east-1.amazonaws.com:8443',
    'https://evil.example#.s3.us-east-1.amazonaws.com',
  ])('refuses to sign for %s', (url) => {
    expect(() => assertAwsUrl(url)).toThrow(GatewayError);
  });

  test('allows AWS hosts', () => {
    const url = 'https://my-bucket.s3.eu-north-1.amazonaws.com';
    expect(assertAwsUrl(url)).toBe(url);
  });

  test('getBaseURL rejects a crafted region before any network call', async () => {
    await expect(
      BedrockAPIConfig.getBaseURL({
        providerOptions: {
          provider: 'bedrock',
          awsRegion: 'x@evil.example/',
        },
        fn: 'chatComplete',
        gatewayRequestURL: 'http://localhost/v1/chat/completions',
        params: { model: 'anthropic.claude-3-haiku' },
        c: { get: () => undefined, env: {}, req: {} },
      })
    ).rejects.toThrow(GatewayError);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('getBaseURL rejects a crafted S3 bucket', async () => {
    await expect(
      BedrockAPIConfig.getBaseURL({
        providerOptions: {
          provider: 'bedrock',
          awsRegion: 'us-east-1',
          awsS3Bucket: 'evil.example/',
        },
        fn: 'uploadFile',
        gatewayRequestURL: 'http://localhost/v1/files',
        params: {},
        c: { get: () => undefined, env: {}, req: {} },
      })
    ).rejects.toThrow(GatewayError);
  });

  test('STS query parameters are encoded and the region is checked', async () => {
    fetchSpy.mockResolvedValue(
      new Response(
        '<AssumeRoleResponse><Credentials><AccessKeyId>ASIA1</AccessKeyId><SecretAccessKey>s</SecretAccessKey><SessionToken>t</SessionToken></Credentials></AssumeRoleResponse>'
      )
    );
    const creds = await fetchSTSAssumeRoleCredentials(
      'arn:aws:iam::111122223333:role/r&DurationSeconds=43200',
      'us-east-1',
      'AKIA',
      'secret',
      undefined,
      'ext&x=1'
    );
    expect(creds?.accessKeyId).toBe('ASIA1');
    const url = new URL(String(fetchSpy.mock.calls[0][0]));
    expect(url.hostname).toBe('sts.us-east-1.amazonaws.com');
    expect(url.searchParams.get('RoleArn')).toBe(
      'arn:aws:iam::111122223333:role/r&DurationSeconds=43200'
    );
    expect(url.searchParams.get('DurationSeconds')).toBeNull();
    expect(url.searchParams.get('ExternalId')).toBe('ext&x=1');

    fetchSpy.mockClear();
    expect(
      await fetchSTSAssumeRoleCredentials(
        'arn:aws:iam::111122223333:role/r',
        'x@evil.example/',
        'AKIA',
        'secret'
      )
    ).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
