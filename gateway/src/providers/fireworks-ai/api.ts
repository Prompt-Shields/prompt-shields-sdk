import { GatewayError } from '../../errors/GatewayError';
import { ProviderAPIConfig } from '../types';

/**
 * PS fork (#208): re-implemented from the Portkey enterprise changelog 2.31
 * ("Hardened upstream URL construction for Fireworks AI requests"; closed
 * source, so written from the description). The account id (header
 * `x-portkey-fireworks-account-id`) and the image model were pasted into the
 * upstream path as is, so a `/`, `?`, `#` or `..` in them sent the request,
 * with the caller's Fireworks key, to another Fireworks API path. Account ids
 * are slugs; image models may be `accounts/<account>/models/<model>` paths
 * but never contain empty, `.` or `..` segments or URL metacharacters.
 */
const FIREWORKS_ACCOUNT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const FIREWORKS_MODEL_SEGMENT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._@:-]{0,255}$/;

export const assertFireworksAccountId = (accountId: unknown) => {
  if (
    typeof accountId !== 'string' ||
    !FIREWORKS_ACCOUNT_ID_PATTERN.test(accountId)
  ) {
    throw new GatewayError('Invalid fireworksAccountId', 400);
  }
};

export const assertFireworksModelPath = (model: unknown) => {
  if (
    typeof model !== 'string' ||
    !model
      .split('/')
      .every((segment) => FIREWORKS_MODEL_SEGMENT_PATTERN.test(segment))
  ) {
    throw new GatewayError('Invalid model for fireworks-ai', 400);
  }
};

const inferenceFunctions = [
  'complete',
  'chatComplete',
  'embed',
  'imageGenerate',
];

const FireworksAIAPIConfig: ProviderAPIConfig = {
  getBaseURL: ({ fn, providerOptions }) => {
    if (inferenceFunctions.includes(fn ?? '')) {
      return 'https://api.fireworks.ai/inference/v1';
    }
    const accountId = providerOptions.fireworksAccountId;
    assertFireworksAccountId(accountId);
    return `https://api.fireworks.ai/v1/accounts/${accountId}`;
  },
  headers: ({ providerOptions }) => {
    return {
      Authorization: `Bearer ${providerOptions.apiKey}`,
      Accept: 'application/json',
    };
  },
  getEndpoint: ({
    fn,
    gatewayRequestBodyJSON: gatewayRequestBody,
    gatewayRequestURL,
  }) => {
    const model = gatewayRequestBody?.model;

    const jobIdIndex = ['cancelFinetune'].includes(fn ?? '') ? -2 : -1;
    const jobId = gatewayRequestURL.split('/').at(jobIdIndex);

    const url = new URL(gatewayRequestURL);
    const params = url.searchParams;

    const size = encodeURIComponent(params.get('limit') ?? 50);
    const page = encodeURIComponent(params.get('after') ?? '1');

    switch (fn) {
      case 'complete':
        return '/completions';
      case 'chatComplete':
        return '/chat/completions';
      case 'embed':
        return '/embeddings';
      case 'imageGenerate':
        assertFireworksModelPath(model);
        return `/image_generation/${model}`;
      case 'uploadFile':
        return '';
      case 'retrieveFile': {
        return `/datasets/${jobId}`;
      }
      case 'listFiles':
        return `/datasets?pageToken=${page}&pageSize=${size}`;
      case 'deleteFile': {
        return `/datasets/${jobId}`;
      }
      case 'createFinetune':
        return `/supervisedFineTuningJobs`;
      case 'retrieveFinetune':
        return `/supervisedFineTuningJobs/${jobId}`;
      case 'listFinetunes':
        return `/supervisedFineTuningJobs?pageToken=${page}&pageSize=${size}`;
      case 'cancelFinetune':
        return `/supervisedFineTuningJobs/${jobId}`;
      default:
        return '';
    }
  },
};

export default FireworksAIAPIConfig;
