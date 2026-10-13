import { ProviderAPIConfig } from '../types';

// BytePlus ModelArk, ap-southeast (OpenAI-compatible). The base URL is a fixed vendor host.
export const BYTEPLUS_BASE_URL =
  'https://ark.ap-southeast.bytepluses.com/api/v3';

const BytePlusAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => BYTEPLUS_BASE_URL,
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

export default BytePlusAPIConfig;
