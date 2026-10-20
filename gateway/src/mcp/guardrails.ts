/**
 * PS fork (#63, MCP Phase 1): the gateway's guardrails on MCP tool calls.
 *
 * Tool arguments (before the call) and tool results (after it) go through
 * the request's HooksManager, so every installed check runs on them and the
 * security events and metrics of #211 are recorded as for model calls. The
 * content is shown to the checks in the Completions shape every text check
 * supports: arguments as `request.json.prompt`, the result as
 * `response.json.choices[0].text`. Checks only read it; transformations
 * (mutators, regexReplace) are not applied to MCP traffic.
 *
 * Fail closed: the call is blocked when a deny guardrail fails, when one of
 * its checks errors, when a hook is skipped, or when the hooks cannot run.
 * Arguments and results are held in memory for the checks only: they are
 * never logged, stored or sent as telemetry.
 */
import { Context } from 'hono';
import { env } from 'hono/adapter';
import type { HooksManager } from '../middlewares/hooks';
import { HOOKS_EVENT_TYPE_PRESETS } from '../middlewares/hooks/globals';
import type { HookObject, HookResult } from '../middlewares/hooks/types';

export type GuardrailStage = 'input' | 'output';

const isTextMimeType = (mime: unknown): boolean =>
  typeof mime === 'string' &&
  /^(text\/|application\/([\w.+-]*\+)?(json|xml|yaml|javascript|x-sh)\b)/i.test(
    mime.trim()
  );

function decodeBase64Text(blob: string): string {
  try {
    const bytes = Uint8Array.from(atob(blob.replace(/\s+/g, '')), (ch) =>
      ch.charCodeAt(0)
    );
    return new TextDecoder().decode(bytes);
  } catch {
    // Not base64: checked as it is.
    return blob;
  }
}

/** Text of a tools/call response for the output checks. */
export function toolResultText(message: any): string {
  if (message?.error) {
    const { message: text, data } = message.error;
    return [
      typeof text === 'string' ? text : '',
      data === undefined ? '' : JSON.stringify(data),
    ]
      .filter(Boolean)
      .join('\n');
  }
  const result = message?.result;
  if (!result || typeof result !== 'object') return '';
  const parts: string[] = [];
  if (Array.isArray(result.content)) {
    for (const item of result.content) {
      if (!item || typeof item !== 'object') continue;
      if (typeof item.text === 'string') parts.push(item.text);
      const resource = item.resource;
      if (resource && typeof resource === 'object') {
        if (typeof resource.text === 'string') parts.push(resource.text);
        // A text resource sent as base64 is still text to the client.
        else if (
          typeof resource.blob === 'string' &&
          isTextMimeType(resource.mimeType)
        )
          parts.push(decodeBase64Text(resource.blob));
      }
      // Anything else a client may show the model (resource_link names,
      // descriptions and URIs, unknown item types), without binary data.
      if (
        item.type !== 'text' &&
        item.type !== 'image' &&
        item.type !== 'audio'
      )
        parts.push(
          JSON.stringify({
            ...item,
            text: undefined,
            data: undefined,
            resource:
              resource && typeof resource === 'object'
                ? { ...resource, text: undefined, blob: undefined }
                : resource,
          })
        );
    }
  }
  if (result.structuredContent !== undefined)
    parts.push(JSON.stringify(result.structuredContent));
  // 2026-07-28 multi round-trip: the server asks the client for input.
  if (result.inputRequests !== undefined)
    parts.push(JSON.stringify(result.inputRequests));
  return parts.join('\n');
}

export function isBlocked(hooks: HookObject[], results: HookResult[]) {
  if (results.length !== hooks.length) return true;
  return results.some((result, i) => {
    if (!result || result.skipped) return true;
    if (!hooks[i].deny) return false;
    return (
      !result.verdict ||
      (Array.isArray(result.checks) && result.checks.some((ch) => ch.error))
    );
  });
}

/** True when the configured guardrails block this content. */
export async function mcpGuardrailsBlock(
  c: Context,
  hooks: HookObject[],
  stage: GuardrailStage,
  text: string
): Promise<boolean> {
  if (!hooks.length) return false;
  try {
    const manager = c.get('hooksManager') as HooksManager | undefined;
    if (!manager) return true;
    const input = stage === 'input';
    const span = manager.createSpan(
      { prompt: input ? text : '' },
      {},
      'mcp',
      false,
      input ? hooks : [],
      input ? [] : hooks,
      null,
      'complete',
      {}
    );
    if (!input) {
      manager.setSpanContextResponse(
        span.id,
        { choices: [{ text, index: 0 }] },
        200
      );
    }
    const { results } = await manager.executeHooks(
      span.id,
      [
        input
          ? HOOKS_EVENT_TYPE_PRESETS.SYNC_BEFORE_REQUEST_HOOK
          : HOOKS_EVENT_TYPE_PRESETS.SYNC_AFTER_REQUEST_HOOK,
      ],
      { env: env(c) }
    );
    return isBlocked(hooks, results);
  } catch {
    return true;
  }
}
