import { ProviderAPIConfig } from '../types';
import { version } from '../../../package.json';

const BytezInferenceAPI: ProviderAPIConfig = {
  getBaseURL: () => 'https://api.bytez.com',
  headers: async ({ providerOptions }) => {
    const { apiKey } = providerOptions;

    const headers: Record<string, string> = {};

    headers['Authorization'] = `Key ${apiKey}`;
    headers['user-agent'] = `portkey/${version}`;

    return headers;
  },
  getEndpoint: ({ gatewayRequestBodyJSON }) => {
    // `version` is a Bytez-specific body field, not part of Params.
    const { version = 2, model } = gatewayRequestBodyJSON as {
      version?: number;
      model?: string;
    };
    return `/models/v${version}/${model}`;
  },
};

export default BytezInferenceAPI;
