import { ProviderAPIConfig } from '../types';

// Regolo.ai by Seeweb, Italy (OpenAI-compatible). The base URL is a fixed vendor host.
export const REGOLO_BASE_URL = 'https://api.regolo.ai/v1';

const RegoloAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => REGOLO_BASE_URL,
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

export default RegoloAPIConfig;
