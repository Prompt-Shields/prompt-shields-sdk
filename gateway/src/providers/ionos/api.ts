import { ProviderAPIConfig } from '../types';

// IONOS AI Model Hub, de-txl (OpenAI-compatible). The base URL is a fixed vendor host.
export const IONOS_BASE_URL = 'https://openai.inference.de-txl.ionos.com/v1';

const IONOSAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => IONOS_BASE_URL,
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

export default IONOSAPIConfig;
