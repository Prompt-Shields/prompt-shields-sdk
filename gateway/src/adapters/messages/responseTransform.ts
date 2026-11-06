/**
 * Chat Completions → Anthropic Messages API Response Transform
 *
 * Converts Chat Completions responses to Anthropic Messages API format.
 */

import {
  ChatCompletionResponse,
  ErrorResponse,
  FINISH_REASON,
} from '../../providers/types';
import {
  MessagesResponse,
  ContentBlock,
  TextBlock,
  ToolUseBlock,
  ThinkingBlock,
  RedactedThinkingBlock,
  Usage,
  ANTHROPIC_STOP_REASON,
} from '../../types/messagesResponse';
import { randomUUID } from 'crypto';
import { logger } from '../../apm';
import {
  normalizeFinishReason,
  normalizeUsage,
  plainReasoningText,
  toMessagesError,
  toMessagesUsage,
} from '../shared';

// Anthropic stop reasons a non-strict provider may already return as-is
const NATIVE_STOP_REASONS = new Set<string>(
  Object.values(ANTHROPIC_STOP_REASON)
);

/**
 * Map a Chat Completions finish_reason to an Anthropic stop_reason.
 * PS fork (#73): provider-native reasons (e.g. Gemini `STOP`) are normalised
 * first, content filtering maps to `refusal`, and a turn that produced tool
 * calls reports `tool_use` even when the provider says `stop` (Gemini does).
 */
export function toAnthropicStopReason(
  finishReason: string | null | undefined,
  hasToolUse: boolean
): ANTHROPIC_STOP_REASON {
  let reason: ANTHROPIC_STOP_REASON;
  if (finishReason && NATIVE_STOP_REASONS.has(finishReason)) {
    reason = finishReason as ANTHROPIC_STOP_REASON;
  } else {
    switch (normalizeFinishReason(finishReason)) {
      case FINISH_REASON.length:
        reason = ANTHROPIC_STOP_REASON.max_tokens;
        break;
      case FINISH_REASON.tool_calls:
      case FINISH_REASON.function_call:
        reason = ANTHROPIC_STOP_REASON.tool_use;
        break;
      case FINISH_REASON.content_filter:
        reason = ANTHROPIC_STOP_REASON.refusal;
        break;
      default:
        reason = ANTHROPIC_STOP_REASON.end_turn;
    }
  }
  if (hasToolUse && reason === ANTHROPIC_STOP_REASON.end_turn) {
    return ANTHROPIC_STOP_REASON.tool_use;
  }
  return reason;
}

/**
 * Transform Chat Completions response to Anthropic Messages API format
 */
export function transformChatCompletionsToMessages(
  response: ChatCompletionResponse | ErrorResponse,
  responseStatus: number,
  provider: string
): MessagesResponse | ErrorResponse {
  // PS fork (#73): errors use the Anthropic error envelope; a body that is
  // not an object is a failed request (api_error), not a 500
  if (!isRecord(response)) {
    return toMessagesError(
      null,
      responseStatus === 200 ? 502 : responseStatus,
      provider
    ) as any;
  }
  if ('error' in response || responseStatus !== 200) {
    return toMessagesError(response, responseStatus, provider) as any;
  }
  const body: Record<string, any> = response;

  const thinkingBlocks: ContentBlock[] = [];
  const textBlocks: ContentBlock[] = [];
  const toolBlocks: ContentBlock[] = [];
  let finishReason: string | null = null;

  // Process choices. PS fork (#73): the provider's arrays may hold null or
  // badly typed entries; those are skipped, never dereferenced.
  const choices: unknown[] = Array.isArray(body.choices) ? body.choices : [];
  for (const choice of choices) {
    if (!isRecord(choice)) continue;
    const msg: Record<string, any> = isRecord(choice.message)
      ? choice.message
      : {};
    if (typeof choice.finish_reason === 'string') {
      finishReason = choice.finish_reason;
    }

    // Handle thinking/reasoning from content_blocks (non-standard extension)
    const blocks: Record<string, any>[] = Array.isArray(msg.content_blocks)
      ? msg.content_blocks.filter(isRecord)
      : [];
    for (const block of blocks) {
      if (block.type === 'redacted_thinking' && stringOr(block.data)) {
        thinkingBlocks.push({
          type: 'redacted_thinking',
          data: block.data,
        } as RedactedThinkingBlock);
      } else if (stringOr(block.thinking) || block.type === 'thinking') {
        thinkingBlocks.push({
          type: 'thinking',
          thinking: stringOr(block.thinking) || stringOr(block.text),
          signature: stringOr(block.signature),
        } as ThinkingBlock);
      }
    }
    // PS fork (#73): OpenAI-compatible reasoning fields (reasoning_content)
    if (!thinkingBlocks.length) {
      const reasoning = plainReasoningText(msg);
      if (reasoning) {
        thinkingBlocks.push({
          type: 'thinking',
          thinking: reasoning,
          signature: '',
        } as ThinkingBlock);
      }
    }

    // Handle tool calls
    if (Array.isArray(msg.tool_calls)) {
      for (const tc of msg.tool_calls) {
        if (!isRecord(tc) || !isRecord(tc.function)) continue;
        if (tc.type && tc.type !== 'function') continue;
        const name = stringOr(tc.function.name);
        toolBlocks.push({
          type: 'tool_use',
          id: stringOr(tc.id) || `toolu_${randomUUID()}`,
          name,
          input: toolInput(tc.function.arguments, provider, name),
        } as ToolUseBlock);
      }
    }

    // Handle text content
    const textContent = msg.content;
    const text =
      typeof textContent === 'string'
        ? textContent
        : Array.isArray(textContent)
          ? textContent
              .map((c: unknown) => (isRecord(c) ? stringOr(c.text) : ''))
              .join('')
          : typeof textContent === 'number' || typeof textContent === 'boolean'
            ? String(textContent)
            : '';
    if (text) {
      textBlocks.push({
        type: 'text',
        text,
      } as TextBlock);
    }
  }

  // PS fork (#73): Anthropic order is thinking, text, then tool_use
  const content: ContentBlock[] = [
    ...thinkingBlocks,
    ...textBlocks,
    ...toolBlocks,
  ];

  // PS fork (#73): cache reads/writes reported separately from input_tokens
  const usage: Usage = toMessagesUsage(normalizeUsage(body.usage));

  return {
    id: stringOr(body.id) || `msg_${randomUUID()}`,
    type: 'message',
    role: 'assistant',
    content,
    model: typeof body.model === 'string' ? body.model : (undefined as any),
    stop_reason: toAnthropicStopReason(finishReason, toolBlocks.length > 0),
    stop_sequence: null,
    usage,
  };
}

/** PS fork (#73): a plain (non-null, non-array) object. */
function isRecord(value: unknown): value is Record<string, any> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** PS fork (#73): `value` if it is a string, else ''. */
function stringOr(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

/**
 * PS fork (#73): tool_use `input` from Chat Completions arguments: JSON text
 * (or an already parsed object) that holds an object, else `{}`.
 */
function toolInput(
  args: unknown,
  provider: string,
  toolName: string
): Record<string, unknown> {
  if (isRecord(args)) return args;
  if (typeof args !== 'string') return {};
  try {
    const parsed = JSON.parse(args || '{}');
    return parsed !== null && typeof parsed === 'object' ? parsed : {};
  } catch (err) {
    logger.warn({
      message: 'Failed to parse tool arguments as JSON',
      provider,
      toolName,
      error: err instanceof Error ? err.message : String(err),
    });
    return {};
  }
}

export default transformChatCompletionsToMessages;
