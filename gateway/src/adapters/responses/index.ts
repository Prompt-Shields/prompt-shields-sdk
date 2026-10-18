/**
 * Responses API Adapter
 *
 * Enables all providers to support the OpenAI Responses API by translating
 * to/from the Chat Completions format at the handler level.
 */

export { transformResponsesToChatCompletions } from './requestTransform';
export {
  transformChatCompletionsToResponses,
  createResponsesAdapterTransformer,
} from './responseTransform';
export { transformStreamChunk, createStreamState } from './streamTransform';

// Providers that natively support the Responses API (no adapter needed).
// PS fork: matches the providers in this tree that define a
// createModelResponse config. Upstream 2.0.0 also lists x-ai and openrouter,
// whose native Responses support this fork does not have, so they use the
// adapter here.
const NATIVE_PROVIDERS = new Set([
  'openai',
  'azure-openai',
  'groq',
  'iointelligence',
  // PS fork (#74): Bedrock Mantle serves the OpenAI Responses API.
  'bedrock-mantle',
]);

export function supportsResponsesApiNatively(provider: string): boolean {
  return NATIVE_PROVIDERS.has(provider?.toLowerCase() || '');
}
