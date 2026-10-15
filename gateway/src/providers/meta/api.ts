import { ProviderAPIConfig } from '../types';

// Meta Llama API, OpenAI compatibility endpoint. The base URL is a fixed vendor host.
export const META_BASE_URL = 'https://api.llama.com/compat/v1';

const MetaAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => META_BASE_URL,
  headers: ({ providerOptions }) => {
    return { Authorization: `Bearer ${providerOptions.apiKey}` };
  },
  getEndpoint: ({ fn }) => {
    switch (fn) {
      case 'chatComplete':
        return '/chat/completions';
      default:
        return '';
    }
  },
};

export default MetaAPIConfig;
