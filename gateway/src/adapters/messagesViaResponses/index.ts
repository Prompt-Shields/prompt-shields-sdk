/**
 * PS fork (#73): Messages API → OpenAI Responses API routing.
 *
 * Some OpenAI models are served only by the Responses API (the codex
 * family, the `-pro` reasoning models, deep research), so the Messages →
 * Chat Completions adapter cannot reach them. For those models on providers
 * with a native Responses API, a `/v1/messages` request is translated to a
 * Responses request and the Responses output (JSON or SSE) back to Messages.
 *
 * Re-implemented from the enterprise changelog (2.16: "Messages API to
 * Responses API routing"); the code is not in the OSS gateway.
 */
import { AZURE_OPEN_AI, OPEN_AI } from '../../globals';

const PROVIDERS = new Set([OPEN_AI, AZURE_OPEN_AI]);

// Model ids (last path segment, lower-cased) that only the Responses API
// serves. `codex` covers codex-mini-latest and gpt-5.x-codex(-mini|-max).
const RESPONSES_ONLY_MODEL =
  /codex|^(o1-pro|o3-pro|o3-deep-research|o4-mini-deep-research|gpt-5(\.\d+)?-pro)(-|$)/;

/** Whether a Messages request for this target must go via Responses. */
export function routesMessagesViaResponses(
  provider: string,
  model: string
): boolean {
  if (!PROVIDERS.has((provider || '').toLowerCase())) return false;
  const id = (model || '').toLowerCase().split('/').pop() || '';
  return RESPONSES_ONLY_MODEL.test(id);
}

export { transformMessagesToResponses } from './requestTransform';
export { transformResponsesToMessages } from './responseTransform';
export {
  transformResponsesStreamChunk,
  createResponsesToMessagesStreamState,
} from './streamTransform';
