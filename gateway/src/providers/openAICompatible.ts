/**
 * PS fork (#20): one adapter for OpenAI-compatible providers on a fixed
 * vendor base URL, configured per provider (base URL, bearer auth, whether
 * the vendor offers embeddings). Used by the EU sovereign providers
 * (`berget`, `scaleway`, `regolo`, `ionos`, `stackit`, `infercom`). Their
 * base URLs must match `providers.<id>.api_base` in the model catalog
 * (schemas/catalog/source.v1.json); a test checks it.
 *
 * The base URL is fixed: caller-supplied hosts and regions are ignored by
 * `getBaseURL`. A caller that sets `custom_host` goes through the gateway's
 * safe-upstream checks (requestValidator), and the routing data policy does
 * not vouch for such a target with this provider's facts (dataPolicyGate).
 */
import {
  chatCompleteParams,
  embedParams,
  responseTransformers,
} from './open-ai-base';
import { ProviderAPIConfig, ProviderConfigs } from './types';

export interface OpenAICompatibleOptions {
  /** Gateway provider id, e.g. `scaleway`. */
  provider: string;
  /** The vendor's OpenAI-compatible base URL, ending in the version path. */
  baseURL: string;
  /** The vendor serves `/embeddings` too. */
  embed?: boolean;
}

export function openAICompatibleAPIConfig(
  baseURL: string,
  embed = false
): ProviderAPIConfig {
  return {
    getBaseURL: () => baseURL,
    headers: ({ providerOptions }) => {
      return { Authorization: `Bearer ${providerOptions.apiKey}` };
    },
    getEndpoint: ({ fn }) => {
      switch (fn) {
        case 'chatComplete':
          return '/chat/completions';
        case 'embed':
          return embed ? '/embeddings' : '';
        default:
          return '';
      }
    },
  };
}

export function openAICompatibleProvider(
  options: OpenAICompatibleOptions
): ProviderConfigs {
  const embed = !!options.embed;
  const config: ProviderConfigs = {
    chatComplete: chatCompleteParams([]),
    api: openAICompatibleAPIConfig(options.baseURL, embed),
    responseTransforms: responseTransformers(options.provider, {
      chatComplete: true,
      embed,
    }),
  };
  if (embed) config.embed = embedParams([]);
  return config;
}
