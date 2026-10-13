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
  // PS fork (#73): errors use the Anthropic error envelope
  if ('error' in response || responseStatus !== 200) {
    return toMessagesError(response, responseStatus, provider) as any;
  }

  const thinkingBlocks: ContentBlock[] = [];
  const textBlocks: ContentBlock[] = [];
  const toolBlocks: ContentBlock[] = [];
  let finishReason: string | null = null;

  // Process choices
  for (const choice of response.choices || []) {
    const msg = (choice.message as any) || {};
    finishReason = choice.finish_reason ?? finishReason;

    // Handle thinking/reasoning from content_blocks (non-standard extension)
    if (msg.content_blocks?.length) {
      for (const block of msg.content_blocks) {
        if (block.type === 'redacted_thinking' && block.data) {
          thinkingBlocks.push({
            type: 'redacted_thinking',
            data: block.data,
          } as RedactedThinkingBlock);
        } else if (block.thinking || block.type === 'thinking') {
          thinkingBlocks.push({
            type: 'thinking',
            thinking: block.thinking || block.text || '',
            signature: block.signature || '',
          } as ThinkingBlock);
        }
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
    if (msg.tool_calls?.length) {
      for (const tc of msg.tool_calls) {
        if (tc.type === 'function' || (!tc.type && tc.function)) {
          let input: Record<string, unknown> = {};
          try {
            const parsed = JSON.parse(tc.function.arguments || '{}');
            input = parsed !== null && typeof parsed === 'object' ? parsed : {};
          } catch (err) {
            logger.warn({
              message: 'Failed to parse tool arguments as JSON',
              provider,
              toolName: tc.function?.name,
              error: err instanceof Error ? err.message : String(err),
            });
          }
          toolBlocks.push({
            type: 'tool_use',
            id: tc.id || `toolu_${randomUUID()}`,
            name: tc.function.name,
            input,
          } as ToolUseBlock);
        }
      }
    }

    // Handle text content
    const textContent = msg.content;
    if (textContent != null) {
      const text =
        typeof textContent === 'string'
          ? textContent
          : Array.isArray(textContent)
            ? textContent.map((c: any) => c.text || '').join('')
            : String(textContent);

      if (text) {
        textBlocks.push({
          type: 'text',
          text,
        } as TextBlock);
      }
    }
  }

  // PS fork (#73): Anthropic order is thinking, text, then tool_use
  const content: ContentBlock[] = [
    ...thinkingBlocks,
    ...textBlocks,
    ...toolBlocks,
  ];

  // PS fork (#73): cache reads/writes reported separately from input_tokens
  const usage: Usage = toMessagesUsage(normalizeUsage(response.usage));

  return {
    id: response.id || `msg_${randomUUID()}`,
    type: 'message',
    role: 'assistant',
    content,
    model: response.model,
    stop_reason: toAnthropicStopReason(finishReason, toolBlocks.length > 0),
    stop_sequence: null,
    usage,
  };
}

export default transformChatCompletionsToMessages;
