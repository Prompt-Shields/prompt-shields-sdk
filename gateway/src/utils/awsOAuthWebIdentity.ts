/**
 * PS fork: OAuth 2.0 client credentials for AWS providers (#76), so Bedrock,
 * SageMaker, Bedrock Mantle and Claude Platform on AWS callers need no
 * static AWS keys. Re-implemented from Portkey's enterprise changelog (2.28).
 *
 * With `awsAuthType: oauth` the caller supplies the same OAuth client
 * credentials the `openai` provider accepts plus an IAM role ARN. The gateway
 * gets a client-credentials access token from the caller's IdP (through
 * getOAuthClientCredentialsToken, so the token URL passes the same SSRF
 * checks and redirects are not followed), and exchanges it with STS
 * AssumeRoleWithWebIdentity for short-lived AWS credentials. The role's trust
 * policy must name the IdP as an IAM OIDC provider; the access token must be
 * a JWT whose `aud` the trust policy accepts (`oauthAudience`).
 *
 * No gateway identity is used: STS is called unsigned, so no operator opt-in
 * is needed. Credentials are cached per (OAuth client identity incl. secret
 * hash, role ARN, region, endpoint domain) until shortly before they expire,
 * in process memory only. Neither the access token nor the AWS credentials
 * reach error messages or logs; STS error bodies are dropped.
 */
import { GatewayError } from '../errors/GatewayError';
import { Options } from '../types/requestBody';
import {
  AWSCredentials,
  assertAwsUrl,
  getAwsEndpointDomain,
  isValidAwsRegion,
  parseSTSXmlResponse,
} from './awsAuth';
import { externalServiceFetch } from './fetch';
import {
  assertSafeTokenUrl,
  getOAuthClientCredentialsToken,
  oauthCacheKey,
} from './oauthClientCredentials';

export const AWS_OAUTH_AUTH_TYPE = 'oauth';

const ROLE_ARN_RE = /^arn:aws[a-z-]*:iam::\d{12}:role\/[\w+=,.@/-]{1,512}$/;
const MAX_CACHE_ENTRIES = 1000;
const MAX_REFRESH_SKEW_MS = 5 * 60 * 1000;
const DEFAULT_LIFETIME_MS = 15 * 60 * 1000;

const credentialCache = new Map<
  string,
  { credentials: AWSCredentials; expiresAt: number }
>();
const inflight = new Map<string, Promise<AWSCredentials>>();

export function isValidRoleArn(roleArn?: string): boolean {
  return !!roleArn && ROLE_ARN_RE.test(roleArn);
}

async function assumeRoleWithWebIdentity(
  stsUrl: string,
  roleArn: string,
  token: string
): Promise<{ credentials: AWSCredentials; lifetimeMs: number }> {
  const body = new URLSearchParams({
    Version: '2011-06-15',
    Action: 'AssumeRoleWithWebIdentity',
    RoleArn: roleArn,
    RoleSessionName: `ps-gateway-${Date.now()}`,
    WebIdentityToken: token,
  });
  let response: Response;
  try {
    response = await externalServiceFetch(stsUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        Accept: 'application/xml',
      },
      body,
      redirect: 'manual',
    });
  } catch {
    throw new GatewayError('AWS STS web identity request failed', 502);
  }
  if (!response.ok) {
    // Deliberately drop the body.
    throw new GatewayError(
      `AWS STS web identity request failed with status ${response.status}`,
      502
    );
  }
  let credentials: AWSCredentials | null = null;
  try {
    credentials = parseSTSXmlResponse(await response.text());
  } catch {
    credentials = null;
  }
  if (!credentials?.accessKeyId || !credentials.secretAccessKey) {
    throw new GatewayError('AWS STS web identity response was invalid', 502);
  }
  const expiresAt = credentials.expiration
    ? Date.parse(credentials.expiration)
    : NaN;
  const lifetimeMs =
    Number.isFinite(expiresAt) && expiresAt > Date.now()
      ? expiresAt - Date.now()
      : DEFAULT_LIFETIME_MS;
  return { credentials, lifetimeMs };
}

/**
 * Exchanges the caller's OAuth client credentials for short-lived AWS
 * credentials for `awsRoleArn`. Validates the role ARN, region and both URLs
 * before any network call.
 */
export async function getAwsOAuthWebIdentityCredentials(
  providerOptions: Options,
  env?: Record<string, any>
): Promise<AWSCredentials> {
  const { awsRoleArn } = providerOptions;
  if (!isValidRoleArn(awsRoleArn)) {
    throw new GatewayError('awsAuthType oauth requires an IAM role ARN', 400);
  }
  const region = providerOptions.awsRegion || 'us-east-1';
  if (!isValidAwsRegion(region)) {
    throw new GatewayError('Invalid AWS region', 400);
  }
  const domain = getAwsEndpointDomain(env);
  const stsUrl = assertAwsUrl(`https://sts.${region}.${domain}/`, env);
  const { oauthTokenUrl, oauthClientId, oauthClientSecret } = providerOptions;
  if (!oauthTokenUrl || !oauthClientId || !oauthClientSecret) {
    throw new GatewayError(
      'OAuth client credentials require a token URL, client id and client secret',
      400
    );
  }
  assertSafeTokenUrl(oauthTokenUrl, env);

  const key = JSON.stringify([
    await oauthCacheKey(
      oauthTokenUrl,
      oauthClientId,
      oauthClientSecret,
      providerOptions.oauthScope,
      providerOptions.oauthAudience
    ),
    awsRoleArn,
    region,
    domain,
  ]);
  const cached = credentialCache.get(key);
  if (cached && cached.expiresAt > Date.now()) return cached.credentials;
  if (cached) credentialCache.delete(key);

  let pending = inflight.get(key);
  if (!pending) {
    pending = (async () => {
      // Validates the token URL (https, SSRF checks, allowlist) first.
      const token = await getOAuthClientCredentialsToken(providerOptions, env);
      const { credentials, lifetimeMs } = await assumeRoleWithWebIdentity(
        stsUrl,
        awsRoleArn as string,
        token
      );
      const skew = Math.min(MAX_REFRESH_SKEW_MS, lifetimeMs / 2);
      if (credentialCache.size >= MAX_CACHE_ENTRIES) {
        const oldest = credentialCache.keys().next().value;
        if (oldest !== undefined) credentialCache.delete(oldest);
      }
      const result = { ...credentials, awsRoleArn, awsRegion: region };
      credentialCache.set(key, {
        credentials: result,
        expiresAt: Date.now() + lifetimeMs - skew,
      });
      return result;
    })().finally(() => inflight.delete(key));
    inflight.set(key, pending);
  }
  return pending;
}

/** Test hook. */
export function _resetAwsOAuthCredentialCache() {
  credentialCache.clear();
  inflight.clear();
}
