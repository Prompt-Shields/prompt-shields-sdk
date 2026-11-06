import { ProviderAPIConfig } from '../types';
import {
  assertWorkloadUpstreamIsVendor,
  getWorkloadIdentityToken,
  isWorkloadAuth,
  withAnthropicOAuthBeta,
} from '../../utils/vendorWorkloadIdentity';

const AnthropicAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => 'https://api.anthropic.com/v1',
  headers: async ({ providerOptions, gatewayRequestBody, c }) => {
    // PS fork (#76): the operator's workload identity federation sends a
    // minted bearer token instead of X-API-Key.
    const workload = isWorkloadAuth(
      'anthropic',
      providerOptions.anthropicAuthType,
      c
    );
    const headers: Record<string, string | string[]> = {};
    if (workload) {
      assertWorkloadUpstreamIsVendor('anthropic', providerOptions, c);
      const token = await getWorkloadIdentityToken('anthropic', c);
      headers['Authorization'] = `Bearer ${token}`;
    } else {
      headers['X-API-Key'] =
        providerOptions.apiKey || providerOptions.anthropicApiKey || '';
    }

    // Accept anthropic_beta and anthropic_version in body to support enviroments which cannot send it in headers.
    const betaHeader =
      providerOptions?.['anthropicBeta'] ??
      (gatewayRequestBody as Params)?.['anthropic_beta'] ??
      'messages-2023-12-15';
    const version =
      providerOptions?.['anthropicVersion'] ??
      (gatewayRequestBody as Params)?.['anthropic_version'] ??
      '2023-06-01';

    headers['anthropic-beta'] = workload
      ? withAnthropicOAuthBeta(betaHeader)
      : betaHeader;
    headers['anthropic-version'] = version;
    return headers;
  },
  getEndpoint: ({ fn }) => {
    switch (fn) {
      case 'complete':
        return '/complete';
      case 'chatComplete':
        return '/messages';
      case 'messages':
        return '/messages';
      case 'messagesCountTokens':
        return '/messages/count_tokens';
      default:
        return '';
    }
  },
};

export default AnthropicAPIConfig;
