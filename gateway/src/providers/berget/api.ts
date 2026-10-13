import { ProviderAPIConfig } from '../types';

// Berget AI, Sweden (OpenAI-compatible). The base URL is a fixed vendor host.
export const BERGET_BASE_URL = 'https://api.berget.ai/v1';

const BergetAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => BERGET_BASE_URL,
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

export default BergetAPIConfig;
