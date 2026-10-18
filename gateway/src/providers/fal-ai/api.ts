import { GatewayError } from '../../errors/GatewayError';
import { ProviderAPIConfig } from '../types';

/**
 * fal.ai model APIs (Portkey enterprise 2.9 lists `fal-ai` for image, video
 * and audio generation; the code is closed, so this follows fal's public
 * docs): synchronous calls are `POST https://fal.run/<model id>` with a JSON
 * body and `Authorization: Key <FAL_KEY>`.
 */
export const FAL_AI_BASE_URL = 'https://fal.run';

// A fal model id is `<owner>/<app>[/<path>...]`, e.g. `fal-ai/flux/schnell`.
const FAL_MODEL_ID_RE = /^[A-Za-z0-9][\w.-]*(\/[A-Za-z0-9][\w.-]*)+$/;

/** The upstream path for a model id; anything else is rejected. */
export function falModelPath(model: unknown): string {
  if (
    typeof model !== 'string' ||
    !FAL_MODEL_ID_RE.test(model) ||
    model.split('/').some((s) => s === '.' || s === '..')
  ) {
    throw new GatewayError(
      'fal-ai requires a fal model id as model, e.g. fal-ai/flux/schnell',
      400
    );
  }
  return `/${model}`;
}

const FalAIAPIConfig: ProviderAPIConfig = {
  getBaseURL: () => FAL_AI_BASE_URL,
  headers: ({ providerOptions }) => {
    // Header mode strips only `Bearer `, so `Authorization: Key ...` arrives
    // with its scheme still attached.
    const key = (providerOptions.apiKey || '').replace(/^Key\s+/i, '');
    if (!key) throw new GatewayError('fal-ai requires an API key', 400);
    return { Authorization: `Key ${key}` };
  },
  getEndpoint: ({ fn, gatewayRequestBodyJSON }) => {
    switch (fn) {
      case 'imageGenerate':
        return falModelPath(gatewayRequestBodyJSON?.model);
      default:
        return '';
    }
  },
};

export default FalAIAPIConfig;
