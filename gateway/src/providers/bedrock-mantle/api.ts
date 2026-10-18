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
 * Amazon Bedrock Mantle: Bedrock's OpenAI-compatible inference endpoint
 * (Portkey enterprise 2.7; the code is closed, so this is written from AWS's
 * public docs and the enterprise changelog):
 * - host `bedrock-mantle.<region>.api.aws`, region required, no default;
 * - OpenAI Chat Completions and Responses under `/v1`, or `/openai/v1` for
 *   OpenAI's own GPT models (the GPT-5.x model cards), and the Anthropic
 *   Messages API for Claude models at `/anthropic/v1/messages`;
 * - SigV4 with service `bedrock-mantle`, or a Bedrock API key as a Bearer token.
 */
export const BEDROCK_MANTLE_SERVICE = 'bedrock-mantle';
export const BEDROCK_MANTLE_DOMAIN = 'api.aws';
const DEFAULT_ANTHROPIC_VERSION = '2023-06-01';
const OPENAI_API_PATHS = ['/v1', '/openai/v1'];

/**
 * Anthropic beta features Bedrock Mantle rejects; enterprise 2.27 drops them
 * from requests instead of failing them.
 */
const UNSUPPORTED_ANTHROPIC_BETAS = [
  'advanced-tool-use',
  'extended-cache-ttl',
  'prompt-caching-scope',
  'redact-thinking',
];

export const bedrockMantleHost = (region: string) =>
  `${BEDROCK_MANTLE_SERVICE}.${region}.${BEDROCK_MANTLE_DOMAIN}`;

/** SigV4 unless the caller chose (or only supplied) an API key. */
export function usesSigV4(providerOptions: Options): boolean {
  const { awsAuthType } = providerOptions;
  if (awsAuthType === 'apiKey') return false;
  if (awsAuthType === 'assumedRole' || awsAuthType === 'serviceRole')
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
      'bedrock-mantle requires an AWS region (aws_region)',
      400
    );
  }
  if (!isValidAwsRegion(region)) {
    throw new GatewayError('Invalid AWS region', 400);
  }
  return region;
}

/**
 * Base path of the OpenAI-compatible APIs. AWS documents `/openai/v1` for
 * OpenAI's GPT models (not gpt-oss) and `/v1` for the others; the caller
 * can pick one with `bedrock_mantle_api_path`.
 */
export function openAIApiPath(model: unknown, providerOptions: Options) {
  const override = providerOptions.bedrockMantleApiPath;
  if (override) {
    const path = `/${String(override).replace(/^\/+|\/+$/g, '')}`;
    if (!OPENAI_API_PATHS.includes(path)) {
      throw new GatewayError(
        'bedrock_mantle_api_path must be /v1 or /openai/v1',
        400
      );
    }
    return path;
  }
  const id = typeof model === 'string' ? model.toLowerCase() : '';
  return /^(openai\.)?gpt-(?!oss)/.test(id) ? '/openai/v1' : '/v1';
}

export function filterAnthropicBeta(beta?: string): string | undefined {
  if (!beta) return undefined;
  const kept = beta
    .split(',')
    .map((b) => b.trim())
    .filter(
      (b) => b && !UNSUPPORTED_ANTHROPIC_BETAS.some((u) => b.startsWith(u))
    );
  return kept.length ? kept.join(',') : undefined;
}

const BedrockMantleAPIConfig: ProviderAPIConfig = {
  getBaseURL: ({ providerOptions, c }) =>
    `https://${bedrockMantleHost(regionOf(providerOptions, c))}`,
  headers: async ({
    c,
    fn,
    providerOptions,
    transformedRequestBody,
    transformedRequestUrl,
    gatewayRequestBody,
  }) => {
    const method = fn === 'proxy' ? c.req.method : 'POST';
    const headers: Record<string, string> = {};
    // The gateway drops content-type on GET; it must not be signed then.
    if (method !== 'GET') headers['content-type'] = 'application/json';
    if (
      fn === 'messages' ||
      new URL(transformedRequestUrl).pathname.startsWith('/anthropic/')
    ) {
      headers['anthropic-version'] =
        providerOptions.anthropicVersion ??
        (gatewayRequestBody as any)?.anthropic_version ??
        DEFAULT_ANTHROPIC_VERSION;
      const beta = filterAnthropicBeta(
        providerOptions.anthropicBeta ??
          (gatewayRequestBody as any)?.anthropic_beta
      );
      if (beta) headers['anthropic-beta'] = beta;
    }

    if (!usesSigV4(providerOptions)) {
      if (!providerOptions.apiKey) {
        throw new GatewayError(
          'bedrock-mantle requires a Bedrock API key or AWS credentials',
          400
        );
      }
      headers['authorization'] = `Bearer ${providerOptions.apiKey}`;
      return headers;
    }

    const cEnv = env(c);
    await providerAssumedRoleCredentials(providerOptions, cEnv);
    if (
      !providerOptions.awsAccessKeyId ||
      !providerOptions.awsSecretAccessKey
    ) {
      throw new GatewayError(
        'bedrock-mantle requires a Bedrock API key or AWS credentials',
        400
      );
    }
    // Only sign for the Mantle host of the configured region, so a custom
    // host can never receive the signed credentials.
    const region = regionOf(providerOptions, c);
    const url = new URL(transformedRequestUrl);
    if (
      url.protocol !== 'https:' ||
      url.port ||
      url.username ||
      url.password ||
      url.hostname !== bedrockMantleHost(region)
    ) {
      throw new GatewayError(
        'bedrock-mantle only signs requests to its regional endpoint',
        400
      );
    }
    return generateAWSHeaders(
      fn === 'proxy' ? gatewayRequestBody : transformedRequestBody,
      headers,
      transformedRequestUrl,
      method,
      BEDROCK_MANTLE_SERVICE,
      region,
      providerOptions.awsAccessKeyId,
      providerOptions.awsSecretAccessKey,
      providerOptions.awsSessionToken || '',
      cEnv,
      BEDROCK_MANTLE_DOMAIN
    );
  },
  getEndpoint: ({ fn, gatewayRequestBodyJSON, providerOptions }) => {
    const model = gatewayRequestBodyJSON?.model;
    switch (fn) {
      case 'chatComplete':
        return `${openAIApiPath(model, providerOptions)}/chat/completions`;
      case 'createModelResponse':
        return `${openAIApiPath(model, providerOptions)}/responses`;
      case 'messages':
        return '/anthropic/v1/messages';
      default:
        return '';
    }
  },
  // `/v1/models` on the gateway is `/v1/models` on Mantle; an explicit
  // `/openai/v1/...` or `/anthropic/v1/...` path is kept as given.
  getProxyEndpoint: ({ reqPath, reqQuery }) =>
    /^\/(openai|anthropic)\/v1\//.test(reqPath)
      ? `${reqPath}${reqQuery}`
      : `/v1${reqPath}${reqQuery}`,
};

export default BedrockMantleAPIConfig;
