import { ProviderAPIConfig } from '../types';

// SCX.ai sovereign inference, Sydney AU (OpenAI-compatible). The base URL is a fixed vendor host.
export const SCX_AI_BASE_URL = 'https://api.scx.ai/v1';

const SCXAIAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => SCX_AI_BASE_URL,
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

export default SCXAIAPIConfig;
