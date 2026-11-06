import { GatewayError } from '../../errors/GatewayError';
import { Options } from '../../types/requestBody';
import { endpointStrings, ProviderAPIConfig } from '../types';
import { getModelAndProvider, getAccessToken, getBucketAndFile } from './utils';
import {
  assertVertexAuthTypeAllowed,
  assertWorkloadUpstreamIsGoogle,
  getGcpHostAccessToken,
} from '../../utils/gcpAmbientAuth';

/**
 * PS fork (#208): re-implemented from the Portkey enterprise changelog 2.31
 * ("Hardened upstream URL construction for Vertex AI requests"; closed
 * source, so written from the description). The region, project id and
 * model come from the caller and were pasted into the upstream URL as is: a
 * region such as `evil.example/#` rewrote the `<region>-aiplatform` host, and
 * a `/`, `?`, `#` or `..` in the project or model moved the request to
 * another Vertex path. Each is now checked against the shape Google uses
 * (project ids may be legacy domain-scoped, `example.com:my-project`; models
 * may carry a version, `claude-sonnet-4@20250514`). Unset values are left to
 * fail upstream as before.
 */
const VERTEX_REGION_PATTERN = /^[a-z0-9-]{1,63}$/;
const VERTEX_PROJECT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9.:_-]{0,127}$/;
const VERTEX_MODEL_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._@-]{0,255}$/;

export const assertVertexUrlParts = (parts: {
  region?: unknown;
  projectId?: unknown;
  model?: unknown;
}) => {
  const { region, projectId, model } = parts;
  if (
    region !== undefined &&
    !(typeof region === 'string' && VERTEX_REGION_PATTERN.test(region))
  ) {
    throw new GatewayError('Invalid vertexRegion', 400);
  }
  if (
    projectId !== undefined &&
    !(
      typeof projectId === 'string' && VERTEX_PROJECT_ID_PATTERN.test(projectId)
    )
  ) {
    throw new GatewayError('Invalid vertexProjectId', 400);
  }
  if (
    model !== undefined &&
    !(typeof model === 'string' && VERTEX_MODEL_PATTERN.test(model))
  ) {
    throw new GatewayError('Invalid model for vertex-ai', 400);
  }
};

const getApiVersion = (provider: string) => {
  if (provider === 'meta') return 'v1beta1';
  return 'v1';
};

const getProjectRoute = (
  providerOptions: Options,
  inputModel: string
): string => {
  const {
    vertexProjectId: inputProjectId,
    vertexRegion,
    vertexServiceAccountJson,
  } = providerOptions;
  let projectId = inputProjectId;
  if (vertexServiceAccountJson && vertexServiceAccountJson.project_id) {
    projectId = vertexServiceAccountJson.project_id;
  }

  assertVertexUrlParts({ region: vertexRegion, projectId });
  const { provider } = getModelAndProvider(inputModel as string);
  const routeVersion = getApiVersion(provider);
  return `/${routeVersion}/projects/${projectId}/locations/${vertexRegion}`;
};

const FILE_ENDPOINTS = [
  'uploadFile',
  'retrieveFileContent',
  'deleteFile',
  'listFiles',
  'retrieveFile',
];

const BATCH_ENDPOINTS = [
  'createBatch',
  'retrieveBatch',
  'getBatchOutput',
  'listBatches',
  'cancelBatch',
  'createFinetune',
  'retrieveFinetune',
  'listFinetunes',
  'cancelFinetune',
];
const NON_INFERENCE_ENDPOINTS = [...FILE_ENDPOINTS, ...BATCH_ENDPOINTS];

// Good reference for using REST: https://cloud.google.com/vertex-ai/generative-ai/docs/start/quickstarts/quickstart-multimodal#gemini-beginner-samples-drest
// Difference versus Studio AI: https://cloud.google.com/vertex-ai/docs/start/ai-platform-users
export const GoogleApiConfig: ProviderAPIConfig = {
  getBaseURL: ({ providerOptions, fn }) => {
    const { vertexRegion } = providerOptions;

    if (FILE_ENDPOINTS.includes(fn as string)) {
      return `https://storage.googleapis.com`;
    }

    if (vertexRegion === 'global') {
      return 'https://aiplatform.googleapis.com';
    }

    assertVertexUrlParts({ region: vertexRegion });

    return `https://${vertexRegion}-aiplatform.googleapis.com`;
  },
  headers: async ({ c, providerOptions, gatewayRequestBody }) => {
    const { apiKey, vertexServiceAccountJson, vertexAuthType } =
      providerOptions;
    let authToken = apiKey;
    // PS fork (#76): host identity, gated by GCP_AMBIENT_CREDENTIALS_ENABLED.
    assertVertexAuthTypeAllowed(vertexAuthType, c);
    if (vertexAuthType === 'workload') {
      assertWorkloadUpstreamIsGoogle(providerOptions, c);
      authToken = await getGcpHostAccessToken(c);
    } else if (vertexServiceAccountJson) {
      authToken = await getAccessToken(c, vertexServiceAccountJson);
    }
    const anthropicBeta =
      providerOptions?.['anthropicBeta'] ??
      gatewayRequestBody?.['anthropic_beta'];

    return {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${authToken}`,
      ...(anthropicBeta && {
        'anthropic-beta': anthropicBeta,
      }),
    };
  },
  getEndpoint: ({
    fn,
    gatewayRequestBodyJSON: gatewayRequestBody,
    providerOptions,
    gatewayRequestURL,
  }) => {
    const { vertexProjectId, vertexRegion, vertexServiceAccountJson } =
      providerOptions;
    let mappedFn = fn;
    const { model: inputModel, stream } = gatewayRequestBody;
    if (stream) {
      mappedFn = `stream-${fn}` as endpointStrings;
    }

    const url = new URL(gatewayRequestURL);
    const searchParams = url.searchParams;

    if (NON_INFERENCE_ENDPOINTS.includes(fn)) {
      const jobIdIndex = [
        'cancelBatch',
        'retrieveFileContent',
        'cancelFinetune',
      ].includes(fn)
        ? -2
        : -1;
      const jobId = gatewayRequestURL.split('/').at(jobIdIndex);

      const url = new URL(gatewayRequestURL);
      const params = new URLSearchParams(url.search);
      const pageSize = encodeURIComponent(params.get('limit') ?? 20);
      const after = encodeURIComponent(params.get('after') ?? '');

      let projectId = vertexProjectId;
      if (!projectId || vertexServiceAccountJson) {
        projectId = vertexServiceAccountJson?.project_id;
      }
      if (!FILE_ENDPOINTS.includes(fn)) {
        assertVertexUrlParts({ region: vertexRegion, projectId });
      }
      switch (fn) {
        case 'retrieveBatch':
          return `/v1/projects/${projectId}/locations/${vertexRegion}/batchPredictionJobs/${jobId}`;
        case 'listBatches': {
          return `/v1/projects/${projectId}/locations/${vertexRegion}/batchPredictionJobs?pageSize=${pageSize}&pageToken=${after}`;
        }
        case 'cancelBatch': {
          return `/v1/projects/${projectId}/locations/${vertexRegion}/batchPredictionJobs/${jobId}:cancel`;
        }
        case 'uploadFile':
        case 'getBatchOutput':
          // We handle file upload in a separate request handler
          return '';
        case 'retrieveFile':
          return '';
        case 'retrieveFileContent': {
          const { bucket, file } = getBucketAndFile(jobId ?? '');
          return `/${bucket}/${file}`;
        }
        case 'createBatch':
          return `/v1/projects/${projectId}/locations/${vertexRegion}/batchPredictionJobs`;
        case 'createFinetune':
          return `/v1/projects/${projectId}/locations/${vertexRegion}/tuningJobs`;
        case 'listFinetunes': {
          const pageSize = encodeURIComponent(searchParams.get('limit') ?? 20);
          const after = encodeURIComponent(searchParams.get('after') ?? '');
          return `/v1/projects/${projectId}/locations/${vertexRegion}/tuningJobs?pageSize=${pageSize}&pageToken=${after}`;
        }
        case 'retrieveFinetune':
          return `/v1/projects/${projectId}/locations/${vertexRegion}/tuningJobs/${jobId}`;
        case 'cancelFinetune': {
          return `/v1/projects/${projectId}/locations/${vertexRegion}/tuningJobs/${jobId}:cancel`;
        }
        default:
          return '';
      }
    }

    if (!inputModel) {
      throw new GatewayError('Model is required', 400);
    }

    const { provider, model } = getModelAndProvider(inputModel as string);
    assertVertexUrlParts({ model });
    const projectRoute = getProjectRoute(providerOptions, inputModel as string);
    const googleUrlMap = new Map<string, string>([
      [
        'chatComplete',
        `${projectRoute}/publishers/${provider}/models/${model}:generateContent`,
      ],
      [
        'stream-chatComplete',
        `${projectRoute}/publishers/${provider}/models/${model}:streamGenerateContent?alt=sse`,
      ],
      [
        'embed',
        `${projectRoute}/publishers/${provider}/models/${model}:predict`,
      ],
      [
        'imageGenerate',
        `${projectRoute}/publishers/${provider}/models/${model}:predict`,
      ],
      // PS fork (#73 follow-up): Messages count_tokens for Gemini.
      [
        'messagesCountTokens',
        `${projectRoute}/publishers/${provider}/models/${model}:countTokens`,
      ],
    ]);

    switch (provider) {
      case 'google': {
        return googleUrlMap.get(mappedFn) || `${projectRoute}`;
      }

      case 'mistralai':
      case 'anthropic': {
        if (mappedFn === 'chatComplete' || mappedFn === 'messages') {
          return `${projectRoute}/publishers/${provider}/models/${model}:rawPredict`;
        } else if (
          mappedFn === 'stream-chatComplete' ||
          mappedFn === 'stream-messages'
        ) {
          return `${projectRoute}/publishers/${provider}/models/${model}:streamRawPredict`;
        } else if (mappedFn === 'messagesCountTokens') {
          return `${projectRoute}/publishers/${provider}/models/count-tokens:rawPredict`;
        }
        return `${projectRoute}/publishers/${provider}/models/${model}:rawPredict`;
      }

      case 'meta': {
        return `${projectRoute}/endpoints/openapi/chat/completions`;
      }

      case 'endpoints': {
        return `${projectRoute}/endpoints/${model}/chat/completions`;
      }

      default:
        return `${projectRoute}`;
    }
  },
};

export default GoogleApiConfig;
