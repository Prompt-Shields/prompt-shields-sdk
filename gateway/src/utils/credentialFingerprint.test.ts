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
