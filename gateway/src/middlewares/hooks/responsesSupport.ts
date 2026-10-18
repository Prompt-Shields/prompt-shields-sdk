/**
 * PS fork (#73): hook checks that can run on Responses API requests.
 *
 * Guardrails and mutators run on `createModelResponse` (the native
 * `/v1/responses` route and `/v1/messages` routed via Responses). A check
 * written for Chat Completions that reads `messages` / `choices` directly
 * would pass a Responses request without inspecting anything, so this is an
 * allowlist, not a deny list: only checks verified to read the request and
 * response through the shared getters in `plugins/utils.ts` (or the span's
 * `request.text` / `response.text`), or that read no message text at all,
 * may run. A target with any other enabled check (a new plugin, a custom
 * one, or one of those below) is not called via the Responses API: the
 * request is rejected instead of the check being skipped.
 *
 * Deliberately left out:
 * - `default.webhook`: the endpoint receives the raw request body and may
 *   only understand Chat Completions.
 * - `default.addPrefix`, `exa.online`: rewrite `messages` only.
 * - `lasso.classify`, `aporia.validateProject`,
 *   `qualifire.toolUseQuality`: read `messages` / `choices` directly.
 * - `crowdstrike-aidr.guardChatCompletion`: sends the body to a Chat
 *   Completions guard API.
 *
 * Add a check here only after verifying it on a Responses request.
 */
import { HookObject } from './types';

export const RESPONSES_SUPPORTED_CHECKS = new Set([
  // Read text through plugins/utils.ts getText / getCurrentContentPart.
  'default.regexMatch',
  'default.sentenceCount',
  'default.wordCount',
  'default.characterCount',
  'default.jsonSchema',
  'default.jsonKeys',
  'default.contains',
  'default.validUrls',
  'default.containsCode',
  'default.alluppercase',
  'default.alllowercase',
  'default.endsWith',
  'default.regexReplace',
  'default.notNull',
  // Read no message text.
  'default.log',
  'default.modelWhitelist',
  'default.modelRules',
  'default.jwt',
  'default.requiredMetadataKeys',
  'default.allowedRequestTypes',
  // Partner checks using the shared getters or request.text / response.text.
  'qualifire.contentModeration',
  'qualifire.grounding',
  'qualifire.policy',
  'qualifire.hallucinations',
  'qualifire.pii',
  'qualifire.promptInjections',
  'portkey.moderateContent',
  'portkey.language',
  'portkey.pii',
  'portkey.gibberish',
  'sydelabs.sydeguard',
  'pillar.scanPrompt',
  'pillar.scanResponse',
  'patronus.phi',
  'patronus.pii',
  'patronus.isConcise',
  'patronus.isHelpful',
  'patronus.isPolite',
  'patronus.noApologies',
  'patronus.noGenderBias',
  'patronus.noRacialBias',
  'patronus.retrievalAnswerRelevance',
  'patronus.toxicity',
  'patronus.custom',
  'mistral.moderateContent',
  'pangea.textGuard',
  'pangea.pii',
  'promptfoo.pii',
  'promptfoo.harm',
  'promptfoo.guard',
  'bedrock.guard',
  'acuvity.scan',
  'azure.pii',
  'azure.contentSafety',
  'azure.protectedMaterial',
  'azure.shieldPrompt',
  'promptsecurity.protectPrompt',
  'promptsecurity.protectResponse',
  'panw-prisma-airs.intercept',
  'walledai.walledprotect',
  'javelin.guardrails',
  'f5-guardrails.scan',
]);

/** Enabled check ids in `hooks` that cannot run on a Responses request. */
export function responsesUnsupportedChecks(
  hooks: (HookObject[] | undefined)[]
): string[] {
  const ids = new Set<string>();
  for (const list of hooks) {
    for (const hook of Array.isArray(list) ? list : []) {
      for (const check of hook?.checks || []) {
        if (
          check?.is_enabled !== false &&
          !RESPONSES_SUPPORTED_CHECKS.has(check?.id)
        ) {
          ids.add(String(check?.id));
        }
      }
    }
  }
  return [...ids];
}
