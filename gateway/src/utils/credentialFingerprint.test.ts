import { createHash } from 'crypto';
import {
  CREDENTIAL_FINGERPRINT_KEY,
  credentialFingerprint,
  recordCredentialFingerprint,
} from './credentialFingerprint';

const sha16 = (s: string) =>
  createHash('sha256').update(s).digest('hex').slice(0, 16);

describe('credentialFingerprint', () => {
  test.each([
    [
      'OAuth client (secret and scope ignored)',
      {
        oauthTokenUrl: 'https://idp.example.com/t',
        oauthClientId: 'c',
        oauthClientSecret: 's',
        oauthScope: 'x',
        apiKey: 'sk-ignored',
      },
      sha16('oauth-client\nhttps://idp.example.com/t\nc'),
    ],
    [
      'AWS assumedRole (not the minted session keys)',
      {
        awsAuthType: 'assumedRole',
        awsRoleArn: 'arn:aws:iam::123456789012:role/r',
        awsAccessKeyId: 'ASIATEMP',
      },
      sha16('aws-role\narn:aws:iam::123456789012:role/r'),
    ],
    [
      'static AWS keys',
      { awsAccessKeyId: 'AKIASTATIC', awsSecretAccessKey: 's' },
      sha16('aws-key\nAKIASTATIC'),
    ],
    [
      'Azure Entra client',
      {
        azureAuthMode: 'entra',
        azureEntraTenantId: 't',
        azureEntraClientId: 'c',
        azureEntraClientSecret: 's',
      },
      sha16('azure-entra\nt\nc'),
    ],
    [
      'Azure workload identity with a client id',
      { azureAuthMode: 'workload', azureWorkloadClientId: 'w' },
      sha16('azure-workload\nw'),
    ],
    [
      'Vertex service account',
      {
        vertexServiceAccountJson: {
          client_email: 'sa@p.iam.gserviceaccount.com',
          private_key: 'k',
        },
      },
      sha16('gcp-sa\nsa@p.iam.gserviceaccount.com'),
    ],
    ['static API key, as the SDK', { apiKey: 'sk-abc' }, sha16('sk-abc')],
  ])('%s', async (_name, options, expected) => {
    expect(await credentialFingerprint(options as any)).toBe(expected);
  });

  test.each([
    ['AWS serviceRole (host identity)', { awsAuthType: 'serviceRole' }],
    ['Vertex workload (host identity)', { vertexAuthType: 'workload' }],
    ['Azure managed identity, no client id', { azureAuthMode: 'managed' }],
    ['Azure AD token', { azureAdToken: 'eyJx', apiKey: 'k' }],
    ['JWT as api key', { apiKey: 'eyJhbGciOiJIUzI1NiJ9.eyJhIjoxfQ.c2ln' }],
    ['partial OAuth', { oauthClientSecret: 's', apiKey: 'sk-ignored' }],
    ['nothing', {}],
  ])('none for %s', async (_name, options) => {
    expect(await credentialFingerprint(options as any)).toBeUndefined();
  });

  test('recorded only when PS_TELEMETRY_CREDENTIAL_FINGERPRINT=true', async () => {
    const set = jest.fn();
    await recordCredentialFingerprint({ set }, { apiKey: 'k' }, {});
    await recordCredentialFingerprint(
      { set },
      { apiKey: 'k' },
      { PS_TELEMETRY_CREDENTIAL_FINGERPRINT: '1' }
    );
    expect(set).not.toHaveBeenCalled();
    await recordCredentialFingerprint(
      { set },
      { apiKey: 'k' },
      { PS_TELEMETRY_CREDENTIAL_FINGERPRINT: 'TRUE' }
    );
    expect(set).toHaveBeenCalledWith(CREDENTIAL_FINGERPRINT_KEY, sha16('k'));
  });
});

// The same literal vectors are pinned in the SDK suite
// (packages/sdk/tests/test_credentials.py), so `prompt_shields.credentials`
// and the gateway report one fingerprint per identity.
describe('SDK parity', () => {
  test.each([
    [
      {
        oauthTokenUrl: 'https://idp.example.com/oauth2/token',
        oauthClientId: 'client-a',
        oauthClientSecret: 's',
      },
      {},
      '276226128299b028',
    ],
    [
      {
        awsAuthType: 'assumedRole',
        awsRoleArn: 'arn:aws:iam::123456789012:role/r',
      },
      {},
      '36d1362714d3ea4e',
    ],
    [{ awsAccessKeyId: 'AKIASTATIC' }, {}, 'f6e7cc0275129b17'],
    [
      {
        azureAuthMode: 'entra',
        azureEntraTenantId: 'tenant-1',
        azureEntraClientId: 'client-1',
      },
      {},
      'd90880d979ddcaca',
    ],
    [
      { azureAuthMode: 'managed', azureManagedClientId: 'mi-client' },
      {},
      'fbcf0f5d67bfb730',
    ],
    [
      { azureAuthMode: 'workload', azureWorkloadClientId: 'wi-client' },
      {},
      '082f4758ea8eebff',
    ],
    [
      {
        vertexServiceAccountJson: {
          client_email: 'sa@proj.iam.gserviceaccount.com',
        },
      },
      {},
      '721bf1a8e95f0e39',
    ],
    [
      { openaiAuthType: 'workload' },
      { OPENAI_SERVICE_ACCOUNT_ID: 'svc_acct_456' },
      'a2488441e9dcd04f',
    ],
    [
      { anthropicAuthType: 'workload' },
      {
        ANTHROPIC_ORGANIZATION_ID: '00000000-0000-0000-0000-000000000001',
        ANTHROPIC_SERVICE_ACCOUNT_ID: 'svac_def',
      },
      '8375b737f5b41df5',
    ],
    [{ apiKey: 'sk-static' }, {}, '679bb5c90f55bc80'],
  ])('%j', async (options, env, expected) => {
    expect(await credentialFingerprint(options as any, env)).toBe(expected);
  });
});
