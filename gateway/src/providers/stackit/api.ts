import { ProviderAPIConfig } from '../types';

// STACKIT AI Model Serving, eu01 (OpenAI-compatible). The base URL is a fixed vendor host.
export const STACKIT_BASE_URL =
  'https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1';

const STACKITAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => STACKIT_BASE_URL,
  headers: ({ providerOptions }) => {
    return { Authorization: `Bearer ${providerOptions.apiKey}` };
  },
  getEndpoint: ({ fn }) => {
    switch (fn) {
      case 'chatComplete':
        return '/chat/completions';
      case 'embed':
        return '/embeddings';
      default:
        return '';
    }
  },
};

export default STACKITAPIConfig;
