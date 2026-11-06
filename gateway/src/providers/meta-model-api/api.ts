import { GatewayError } from '../../errors/GatewayError';
import { ProviderAPIConfig } from '../types';

/**
 * Meta Model API (Portkey enterprise 2.25.1; the code is closed, so this is
 * written from Meta's public docs). It is a separate product from the Llama
 * API (`meta`), with its own host and key:
 * - host `api.meta.ai`, every API under `/v1`, `Authorization: Bearer`;
 * - OpenAI Chat Completions (`/v1/chat/completions`) and Responses
 *   (`/v1/responses`), Anthropic Messages (`/v1/messages`,
 *   `/v1/messages/count_tokens`), and a Files API (`POST`/`GET /v1/files`,
 *   `GET`/`DELETE /v1/files/{file_id}`, multipart upload).
 * The base URL is a fixed vendor host: no request field reaches it. As for
 * every provider, a custom host (validated by requestValidator) replaces it.
 * Anything else gets a 400 here instead of a request to the bare base URL.
 */
export const META_MODEL_API_BASE_URL = 'https://api.meta.ai/v1';
const DEFAULT_ANTHROPIC_VERSION = '2023-06-01';

const gatewayURL = (gatewayRequestURL?: string) =>
  new URL(gatewayRequestURL || '/', 'http://gateway.invalid');

/**
 * `/files/{id}` for a gateway `/v1/files/{id}` request, else ''. Only a
 * plain id segment is forwarded, so the path cannot leave `/files`.
 */
export function filePath(gatewayRequestURL?: string): string {
  const match = /^\/v1\/files\/([A-Za-z0-9_.-]+)$/.exec(
    gatewayURL(gatewayRequestURL).pathname
  );
  return match && !/^\.+$/.test(match[1]) ? `/files/${match[1]}` : '';
}

const MetaModelAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => META_MODEL_API_BASE_URL,
  headers: ({ fn, providerOptions, gatewayRequestBody }) => {
    const headers: Record<string, string> = {
      Authorization: `Bearer ${providerOptions.apiKey}`,
    };
    // The gateway swaps this for the caller's multipart content type, which
    // carries the boundary.
    if (fn === 'uploadFile') headers['Content-Type'] = 'multipart/form-data';
    if (fn === 'messages' || fn === 'messagesCountTokens') {
      headers['anthropic-version'] =
        providerOptions.anthropicVersion ??
        (gatewayRequestBody as any)?.anthropic_version ??
        DEFAULT_ANTHROPIC_VERSION;
    }
    return headers;
  },
  getEndpoint: ({ fn, gatewayRequestURL }) => {
    switch (fn) {
      case 'chatComplete':
        return '/chat/completions';
      case 'createModelResponse':
        return '/responses';
      case 'messages':
        return '/messages';
      case 'messagesCountTokens':
        return '/messages/count_tokens';
      case 'uploadFile':
        return '/files';
      case 'listFiles':
        return `/files${gatewayURL(gatewayRequestURL).search}`;
      case 'retrieveFile':
      case 'deleteFile': {
        const path = filePath(gatewayRequestURL);
        if (!path) throw new GatewayError('Invalid file id', 400);
        return path;
      }
      default:
        throw new GatewayError(`${fn} is not supported by meta-model-api`, 400);
    }
  },
};

export default MetaModelAPIConfig;
