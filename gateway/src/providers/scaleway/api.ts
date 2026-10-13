import { ProviderAPIConfig } from '../types';

// Scaleway Generative APIs, fr-par (OpenAI-compatible). The base URL is a fixed vendor host.
export const SCALEWAY_BASE_URL = 'https://api.scaleway.ai/v1';

const ScalewayAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => SCALEWAY_BASE_URL,
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

export default ScalewayAPIConfig;
