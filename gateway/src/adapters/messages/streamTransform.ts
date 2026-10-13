/**
 * Stream Transform for Messages API Adapter
 *
 * Transforms Chat Completions SSE chunks to Anthropic Messages API SSE format.
 *
 * PS fork (#73): content blocks are opened lazily and numbered in the order
 * they appear (thinking, text, tool_use), each block gets its own
 * content_block_stop, reasoning streams as `thinking` blocks, the final
 * message_delta carries cache token usage, and stop reasons share the
 * non-streaming mapping.
 */

import { randomUUID } from 'crypto';
import { normalizeUsage, plainReasoningText, toMessagesUsage } from '../shared';
import { toAnthropicStopReason } from './responseTransform';

type BlockType = 'thinking' | 'text' | 'tool_use';

interface StreamState {
  messageId: string;
  model: string;
  hasStarted: boolean;
  completed: boolean;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheCreationTokens: number;
  /** Index the next content block will get */
  contentBlockIndex: number;
  /** Currently open content block, if any */
  openBlock: { index: number; type: BlockType } | null;
  /** Chat tool_call index → Anthropic content block index */
  toolBlocks: Record<number, { index: number; id: string }>;
  hasToolUse: boolean;
  finishReason: string | null;
  stopReason: string | null;
}

function sse(event: string, data: Record<string, unknown>): string {
  return `event: ${event}\ndata: ${JSON.stringify({ type: event, ...data })}\n\n`;
}

function closeOpenBlock(state: StreamState, events: string[]) {
  if (!state.openBlock) return;
  events.push(sse('content_block_stop', { index: state.openBlock.index }));
  state.openBlock = null;
}

function openBlock(
  state: StreamState,
  events: string[],
  type: BlockType,
  contentBlock: Record<string, unknown>
): number {
  closeOpenBlock(state, events);
  const index = state.contentBlockIndex++;
  state.openBlock = { index, type };
  events.push(
    sse('content_block_start', { index, content_block: contentBlock })
  );
  return index;
}

/** Ensure an open block of the given type and return its index. */
function ensureBlock(
  state: StreamState,
  events: string[],
  type: 'thinking' | 'text'
): number {
  if (state.openBlock?.type === type) return state.openBlock.index;
  return openBlock(
    state,
    events,
    type,
    type === 'thinking'
      ? { type: 'thinking', thinking: '', signature: '' }
      : { type: 'text', text: '' }
  );
}

/**
 * Transform a single Chat Completions stream chunk to Messages API stream events
 */
export function transformStreamChunk(
  chunk: string,
  state: StreamState
): string | undefined {
  const trimmed = chunk.trim();

  // Handle [DONE]
  if (trimmed === 'data: [DONE]') {
    if (!state.hasStarted || state.completed) return undefined;
    state.completed = true;

    const events: string[] = [];
    closeOpenBlock(state, events);
    state.stopReason = toAnthropicStopReason(
      state.finishReason,
      state.hasToolUse
    );

    const usage: Record<string, number> = {
      input_tokens: state.inputTokens,
      output_tokens: state.outputTokens,
    };
    if (state.cacheReadTokens || state.cacheCreationTokens) {
      usage.cache_creation_input_tokens = state.cacheCreationTokens;
      usage.cache_read_input_tokens = state.cacheReadTokens;
    }

    events.push(
      sse('message_delta', {
        delta: { stop_reason: state.stopReason, stop_sequence: null },
        usage,
      }),
      sse('message_stop', {})
    );
    return events.join('');
  }

  // Skip non-data lines
  if (!trimmed || !trimmed.startsWith('data: ')) return undefined;

  // Parse the chunk
  let parsed: any;
  try {
    parsed = JSON.parse(trimmed.slice(6));
  } catch {
    return undefined;
  }

  const events: string[] = [];

  // PS fork (#73): a mid-stream provider error becomes an Anthropic `error`
  // event, and the stream ends without message_stop (as Anthropic does).
  if (parsed?.error) {
    if (state.completed) return undefined;
    state.completed = true;
    const err = parsed.error;
    return sse('error', {
      error: {
        type: err?.type || 'api_error',
        message:
          (typeof err === 'string' ? err : err?.message) || 'Stream error',
      },
    });
  }

  // Track usage (OpenAI sends it on a final chunk with empty choices)
  if (parsed.usage) {
    const u = normalizeUsage(parsed.usage);
    const mu = toMessagesUsage(u);
    if (u.promptTokens) {
      state.inputTokens = mu.input_tokens;
      state.cacheReadTokens = u.cacheReadTokens;
      state.cacheCreationTokens = u.cacheCreationTokens;
    }
    if (u.completionTokens) state.outputTokens = u.completionTokens;
  }

  // Initialize on first chunk
  if (!state.hasStarted) {
    state.hasStarted = true;
    state.messageId = `msg_${randomUUID()}`;
    state.model = parsed.model || '';

    events.push(
      sse('message_start', {
        message: {
          id: state.messageId,
          type: 'message',
          role: 'assistant',
          content: [],
          model: state.model,
          stop_reason: null,
          stop_sequence: null,
          usage: { input_tokens: state.inputTokens, output_tokens: 0 },
        },
      })
    );
  }

  const choice = parsed.choices?.[0];
  const delta = choice?.delta;

  // Thinking: content_blocks (Anthropic/Gemini/OpenRouter transforms) first,
  // otherwise OpenAI-compatible reasoning_content / reasoning strings.
  let sawBlockThinking = false;
  for (const block of delta?.content_blocks || []) {
    const d = block?.delta ?? {};
    if (typeof d.thinking === 'string' && d.thinking) {
      sawBlockThinking = true;
      const index = ensureBlock(state, events, 'thinking');
      events.push(
        sse('content_block_delta', {
          index,
          delta: { type: 'thinking_delta', thinking: d.thinking },
        })
      );
    }
    if (typeof d.signature === 'string' && d.signature) {
      sawBlockThinking = true;
      const index = ensureBlock(state, events, 'thinking');
      events.push(
        sse('content_block_delta', {
          index,
          delta: { type: 'signature_delta', signature: d.signature },
        })
      );
    }
  }
  if (!sawBlockThinking) {
    const reasoning = plainReasoningText(delta);
    if (reasoning) {
      const index = ensureBlock(state, events, 'thinking');
      events.push(
        sse('content_block_delta', {
          index,
          delta: { type: 'thinking_delta', thinking: reasoning },
        })
      );
    }
  }

  // Text
  if (typeof delta?.content === 'string' && delta.content) {
    const index = ensureBlock(state, events, 'text');
    events.push(
      sse('content_block_delta', {
        index,
        delta: { type: 'text_delta', text: delta.content },
      })
    );
  }

  // Tool calls
  if (Array.isArray(delta?.tool_calls)) {
    for (const tc of delta.tool_calls) {
      if (!tc) continue;
      const toolIdx = tc.index ?? 0;
      const known = state.toolBlocks[toolIdx];
      // A new id on a known index is a new call (Gemini restarts at 0)
      const isNew = !known || (tc.id && tc.id !== known.id);
      let index = known?.index;
      if (isNew) {
        const id = tc.id || `toolu_${randomUUID()}`;
        index = openBlock(state, events, 'tool_use', {
          type: 'tool_use',
          id,
          name: tc.function?.name || '',
          input: {},
        });
        state.toolBlocks[toolIdx] = { index, id };
        state.hasToolUse = true;
      }

      if (tc.function?.arguments) {
        events.push(
          sse('content_block_delta', {
            index,
            delta: {
              type: 'input_json_delta',
              partial_json: tc.function.arguments,
            },
          })
        );
      }
    }
  }

  // Track stop reason; close the open block once the choice finishes
  if (choice?.finish_reason) {
    state.finishReason = choice.finish_reason;
    state.stopReason = toAnthropicStopReason(
      state.finishReason,
      state.hasToolUse
    );
    closeOpenBlock(state, events);
  }

  return events.length > 0 ? events.join('') : undefined;
}

/**
 * Create initial stream state
 */
export function createStreamState(): StreamState {
  return {
    messageId: '',
    model: '',
    hasStarted: false,
    completed: false,
    inputTokens: 0,
    outputTokens: 0,
    cacheReadTokens: 0,
    cacheCreationTokens: 0,
    contentBlockIndex: 0,
    openBlock: null,
    toolBlocks: {},
    hasToolUse: false,
    finishReason: null,
    stopReason: null,
  };
}
