import { ProviderAPIConfig } from '../types';

// Lightning AI model APIs (OpenAI-compatible). The base URL is a fixed vendor host.
export const LIGHTNING_AI_BASE_URL = 'https://lightning.ai/api/v1';

const LightningAIAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => LIGHTNING_AI_BASE_URL,
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

export default LightningAIAPIConfig;
