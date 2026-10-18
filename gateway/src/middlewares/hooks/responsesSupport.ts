/**
 * PS fork (#73): hook checks that cannot run on Responses API requests.
 *
 * Guardrails and mutators run on `createModelResponse` (the native
 * `/v1/responses` route and `/v1/messages` routed via Responses). Most
 * checks read the request and response text through `plugins/utils.ts`,
 * which maps the Responses shapes. The checks below read Chat Completions
 * fields (`messages`, `choices`) directly, or only act on chat requests,
 * so on a Responses request they would pass without inspecting anything.
 * A target that has one of them is not called via the Responses API: the
 * request is rejected instead of the check being skipped.
 */
import { HookObject } from './types';

export const RESPONSES_UNSUPPORTED_CHECKS = new Set([
  'default.addPrefix',
  'exa.online',
  'lasso.classify',
  'aporia.validateProject',
  'crowdstrike-aidr.guardChatCompletion',
  'qualifire.toolUseQuality',
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
          RESPONSES_UNSUPPORTED_CHECKS.has(check.id)
        ) {
          ids.add(check.id);
        }
      }
    }
  }
  return [...ids];
}
