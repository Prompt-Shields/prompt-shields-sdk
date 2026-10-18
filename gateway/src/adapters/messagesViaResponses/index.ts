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

/** Request header forcing (`true`) or disabling (`false`) the routing. */
export const MESSAGES_VIA_RESPONSES_HEADER = 'x-portkey-messages-via-responses';

/**
 * Whether a Messages request for this target must go via Responses.
 *
 * `override` is the explicit choice from the target config
 * (`messages_via_responses`) or the request header: `true` forces the route
 * (e.g. an Azure deployment of a codex model whose name does not say so),
 * `false` disables it. Left undefined, the model name decides. Only OpenAI
 * and Azure OpenAI targets are ever routed; an override elsewhere is ignored.
 */
export function routesMessagesViaResponses(
  provider: string,
  model: string,
  override?: boolean
): boolean {
  if (!PROVIDERS.has((provider || '').toLowerCase())) return false;
  if (typeof override === 'boolean') return override;
  const id = (model || '').toLowerCase().split('/').pop() || '';
  return RESPONSES_ONLY_MODEL.test(id);
}

/**
 * The explicit routing choice for one target: its config key wins over the
 * request header; anything but a boolean / `true` / `false` is no choice.
 *
 * The header (which the caller, not the operator, controls) cannot force
 * the route onto a target with hooks: `true` is then ignored. Hooks run on
 * the Responses route too, but only allowlisted checks may run there
 * (`responsesUnsupportedChecks`), so the caller must not be able to move a
 * hooked target onto it. `false` stays honoured.
 */
export function messagesViaResponsesOverride(
  targetSetting: unknown,
  headerValue: string | undefined,
  targetHasHooks: boolean = false
): boolean | undefined {
  if (typeof targetSetting === 'boolean') return targetSetting;
  const h = (headerValue || '').trim().toLowerCase();
  if (h === 'true') return targetHasHooks ? undefined : true;
  if (h === 'false') return false;
  return undefined;
}

export { stripIssuedReasoning } from './reasoningSignature';
export {
  transformMessagesToResponses,
  MessagesViaResponsesRequestError,
} from './requestTransform';
export { transformResponsesToMessages } from './responseTransform';
export {
  transformResponsesStreamChunk,
  createResponsesToMessagesStreamState,
} from './streamTransform';
