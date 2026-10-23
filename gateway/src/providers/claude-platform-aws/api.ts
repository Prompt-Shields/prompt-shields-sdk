import { env } from 'hono/adapter';
import { GatewayError } from '../../errors/GatewayError';
import {
  generateAWSHeaders,
  getRegionFromEnv,
  isValidAwsRegion,
} from '../../utils/awsAuth';
import { providerAssumedRoleCredentials } from '../bedrock/utils';
import { ProviderAPIConfig } from '../types';
import { Options } from '../../types/requestBody';

/**
 * Claude Platform on AWS: Anthropic's own Claude API, operated by Anthropic
 * and billed through AWS (not Bedrock). Re-implemented from Anthropic's and
 * AWS's public docs (Portkey enterprise 2.10 lists it, the code is closed):
 * - host `aws-external-anthropic.<region>.api.aws`, region required, no default;
 * - the first-party API paths (`/v1/messages`, `/v1/models`, ...) and bodies;
 * - SigV4 with service `aws-external-anthropic`, or an API key generated in
 *   the AWS console, sent as `x-api-key`;
 * - `anthropic-version` and `anthropic-workspace-id` on every inference and
 *   resource request.
 */
export const CLAUDE_PLATFORM_AWS_SERVICE = 'aws-external-anthropic';
export const CLAUDE_PLATFORM_AWS_DOMAIN = 'api.aws';
const DEFAULT_ANTHROPIC_VERSION = '2023-06-01';

export const claudePlatformAwsHost = (region: string) =>
  `${CLAUDE_PLATFORM_AWS_SERVICE}.${region}.${CLAUDE_PLATFORM_AWS_DOMAIN}`;

/** SigV4 unless the caller chose (or only supplied) an API key. */
export function usesSigV4(providerOptions: Options): boolean {
  const { awsAuthType } = providerOptions;
  if (awsAuthType === 'apiKey') return false;
  if (
    awsAuthType === 'assumedRole' ||
    awsAuthType === 'serviceRole' ||
    awsAuthType === 'oauth'
  )
    return true;
  return !!providerOptions.awsAccessKeyId || !providerOptions.apiKey;
}

function regionOf(providerOptions: Options, c: any): string {
  if (providerOptions.awsAuthType === 'serviceRole') {
    providerOptions.awsRegion =
      providerOptions.awsRegion || getRegionFromEnv(c ? env(c) : undefined);
  }
  const region = providerOptions.awsRegion;
  if (!region) {
    throw new GatewayError(
      'claude-platform-aws requires an AWS region (aws_region)',
      400
    );
  }
  if (!isValidAwsRegion(region)) {
    throw new GatewayError('Invalid AWS region', 400);
  }
  return region;
}

const ClaudePlatformAwsAPIConfig: ProviderAPIConfig = {
  getBaseURL: ({ providerOptions, c }) =>
    `https://${claudePlatformAwsHost(regionOf(providerOptions, c))}/v1`,
  headers: async ({
    c,
    fn,
    providerOptions,
    transformedRequestBody,
    transformedRequestUrl,
    gatewayRequestBody,
  }) => {
    const workspaceId = providerOptions.anthropicAwsWorkspaceId;
    if (!workspaceId) {
      throw new GatewayError(
        'claude-platform-aws requires an Anthropic workspace id (anthropic_aws_workspace_id)',
        400
      );
    }
    const method = fn === 'proxy' ? c.req.method : 'POST';
    const headers: Record<string, string> = {
      'anthropic-version':
        providerOptions.anthropicVersion ??
        (gatewayRequestBody as any)?.anthropic_version ??
        DEFAULT_ANTHROPIC_VERSION,
      'anthropic-workspace-id': workspaceId,
    };
    const beta =
      providerOptions.anthropicBeta ??
      (gatewayRequestBody as any)?.anthropic_beta;
    if (beta) headers['anthropic-beta'] = beta;
    // The gateway drops content-type on GET; it must not be signed then.
    if (method !== 'GET') headers['content-type'] = 'application/json';

    if (!usesSigV4(providerOptions)) {
      if (!providerOptions.apiKey) {
        throw new GatewayError(
          'claude-platform-aws requires an API key or AWS credentials',
          400
        );
      }
      headers['x-api-key'] = providerOptions.apiKey;
      return headers;
    }

    const cEnv = env(c);
    await providerAssumedRoleCredentials(providerOptions, cEnv);
    if (
      !providerOptions.awsAccessKeyId ||
      !providerOptions.awsSecretAccessKey
    ) {
      throw new GatewayError(
        'claude-platform-aws requires an API key or AWS credentials',
        400
      );
    }
    // Only sign for the Claude Platform host of the configured region, so a
    // custom host can never receive the signed credentials.
    const region = regionOf(providerOptions, c);
    const url = new URL(transformedRequestUrl);
    if (
      url.protocol !== 'https:' ||
      url.port ||
      url.hostname !== claudePlatformAwsHost(region)
    ) {
      throw new GatewayError(
        'claude-platform-aws only signs requests to its regional endpoint',
        400
      );
    }
    return generateAWSHeaders(
      fn === 'proxy' ? gatewayRequestBody : transformedRequestBody,
      headers,
      transformedRequestUrl,
      method,
      CLAUDE_PLATFORM_AWS_SERVICE,
      region,
      providerOptions.awsAccessKeyId,
      providerOptions.awsSecretAccessKey,
      providerOptions.awsSessionToken || '',
      cEnv,
      CLAUDE_PLATFORM_AWS_DOMAIN
    );
  },
  getEndpoint: ({ fn }) => {
    switch (fn) {
      case 'chatComplete':
      case 'messages':
        return '/messages';
      case 'messagesCountTokens':
        return '/messages/count_tokens';
      default:
        return '';
    }
  },
};

export default ClaudePlatformAwsAPIConfig;
