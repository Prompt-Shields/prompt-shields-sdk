import { ProviderAPIConfig } from '../types';

// MiniMax international platform (OpenAI-compatible). The base URL is a fixed vendor host.
export const MINIMAX_BASE_URL = 'https://api.minimax.io/v1';

const MiniMaxAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => MINIMAX_BASE_URL,
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

export default MiniMaxAPIConfig;
