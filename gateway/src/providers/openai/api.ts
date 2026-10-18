import { ProviderAPIConfig } from '../types';
import {
  getOAuthClientCredentialsToken,
  hasOAuthClientCredentials,
} from '../../utils/oauthClientCredentials';

const OpenAIAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => 'https://api.openai.com/v1',
  headers: async ({ providerOptions, fn, c }) => {
    // PS fork (#76): keyless auth via OAuth client credentials.
    const bearer = hasOAuthClientCredentials(providerOptions)
      ? await getOAuthClientCredentialsToken(providerOptions, c)
      : providerOptions.apiKey;
    const headersObj: Record<string, string> = {
      Authorization: `Bearer ${bearer}`,
    };
    if (providerOptions.openaiOrganization) {
      headersObj['OpenAI-Organization'] = providerOptions.openaiOrganization;
    }

    if (providerOptions.openaiProject) {
      headersObj['OpenAI-Project'] = providerOptions.openaiProject;
    }

    if (
      fn === 'createTranscription' ||
      fn === 'createTranslation' ||
      fn === 'uploadFile' ||
      fn === 'imageEdit'
    )
      headersObj['Content-Type'] = 'multipart/form-data';

    if (providerOptions.openaiBeta) {
      headersObj['OpenAI-Beta'] = providerOptions.openaiBeta;
    }

    return headersObj;
  },
  getEndpoint: ({ fn, gatewayRequestURL }) => {
    // PS fork (#73): everything after the first `/v1`. Splitting and taking
    // [1] cut the path at any later `/v1` (e.g. an id starting `v1`), which
    // turned `POST /v1/responses/v1x/cancel` into `POST /v1/responses`.
    const basePath = gatewayRequestURL.split('/v1').slice(1).join('/v1');
    switch (fn) {
      case 'complete':
        return '/completions';
      case 'chatComplete':
        return '/chat/completions';
      case 'embed':
        return '/embeddings';
      case 'imageGenerate':
        return '/images/generations';
      case 'imageEdit':
        return '/images/edits';
      case 'createSpeech':
        return '/audio/speech';
      case 'createTranscription':
        return '/audio/transcriptions';
      case 'createTranslation':
        return '/audio/translations';
      case 'realtime':
        return basePath;
      case 'uploadFile':
        return basePath;
      case 'retrieveFile':
        return basePath;
      case 'listFiles':
        return basePath;
      case 'deleteFile':
        return basePath;
      case 'retrieveFileContent':
        return basePath;
      case 'createBatch':
        return basePath;
      case 'createFinetune':
        return basePath;
      case 'retrieveFinetune':
        return basePath;
      case 'listFinetunes':
        return basePath;
      case 'cancelFinetune':
        return basePath;
      case 'retrieveBatch':
        return basePath;
      case 'cancelBatch':
        return basePath;
      case 'listBatches':
        return basePath;
      case 'createModelResponse':
        // PS fork (#73): /v1/messages routed via Responses (codex models)
        return basePath.startsWith('/responses') ? basePath : '/responses';
      case 'getModelResponse':
        return basePath;
      case 'deleteModelResponse':
        return basePath;
      case 'listResponseInputItems':
        return basePath;
      case 'cancelModelResponse':
        return basePath;
      case 'countResponseInputTokens':
        return basePath;
      default:
        return '';
    }
  },
};

export default OpenAIAPIConfig;
