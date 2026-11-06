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
import {
  normalizeUsage,
  plainReasoningText,
  toMessagesUsage,
  toolCallKey,
} from '../shared';
import { toAnthropicStopReason } from './responseTransform';

type BlockType = 'thinking' | 'text' | 'tool_use';

interface ToolCall {
  id: string;
  name: string;
  /** Content block index once the block is open (streamed live) */
  index?: number;
  /** Arguments not yet sent (buffered calls) */
  args: string;
}

interface StreamState {
  messageId: string;
  model: string;
  /** Model the client asked for; used when the provider's chunks name none */
  requestedModel: string;
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
  /** Chat tool_call index → its tool call. PS fork (#73): a Map (toolCallKey) */
  toolBlocks: Map<string, ToolCall>;
  /**
   * PS fork (#73): tool calls that started while another tool_use block was
   * open. Anthropic blocks never interleave, but OpenAI-compatible streams
   * may send parallel calls' arguments interleaved, so these are buffered
   * and sent, in order, once the model finishes.
   */
  bufferedTools: ToolCall[];
  hasToolUse: boolean;
  finishReason: string | null;
  stopReason: string | null;
}

/** PS fork (#73): a plain (non-null, non-array) object. */
function isRecord(value: unknown): value is Record<string, any> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
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

/** PS fork (#73): send buffered tool calls, one complete block each. */
function flushBufferedTools(state: StreamState, events: string[]) {
  for (const call of state.bufferedTools) {
    call.index = openBlock(state, events, 'tool_use', {
      type: 'tool_use',
      id: call.id,
      name: call.name,
      input: {},
    });
    if (call.args) {
      events.push(
        sse('content_block_delta', {
          index: call.index,
          delta: { type: 'input_json_delta', partial_json: call.args },
        })
      );
    }
    closeOpenBlock(state, events);
  }
  state.bufferedTools = [];
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
    flushBufferedTools(state, events);
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
  // PS fork (#73): a chunk that is not an object carries nothing
  if (!isRecord(parsed)) return undefined;

  const events: string[] = [];

  // PS fork (#73): a mid-stream provider error becomes an Anthropic `error`
  // event, and the stream ends without message_stop (as Anthropic does).
  if (parsed.error) {
    if (state.completed) return undefined;
    state.completed = true;
    const err = parsed.error;
    const message = typeof err === 'string' ? err : err?.message;
    return sse('error', {
      error: {
        type: (typeof err?.type === 'string' && err.type) || 'api_error',
        message: (typeof message === 'string' && message) || 'Stream error',
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
    // PS fork (#208, from the Portkey enterprise changelog 2.31): providers
    // whose chunks carry no model (e.g. Vertex Gemini) used to get an empty
    // message_start.model, which breaks clients that group by model.
    state.model =
      (typeof parsed.model === 'string' && parsed.model) ||
      state.requestedModel;

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

  // PS fork (#73): null or badly typed entries in the provider's arrays are
  // skipped, never dereferenced
  const first = Array.isArray(parsed.choices) ? parsed.choices[0] : undefined;
  const choice: Record<string, any> | undefined = isRecord(first)
    ? first
    : undefined;
  const delta: Record<string, any> | undefined = isRecord(choice?.delta)
    ? choice.delta
    : undefined;

  // Thinking: content_blocks (Anthropic/Gemini/OpenRouter transforms) first,
  // otherwise OpenAI-compatible reasoning_content / reasoning strings.
  let sawBlockThinking = false;
  const blocks: unknown[] = Array.isArray(delta?.content_blocks)
    ? delta.content_blocks
    : [];
  for (const block of blocks) {
    if (!isRecord(block)) continue;
    const d = isRecord(block.delta) ? block.delta : {};
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
      if (!isRecord(tc)) continue;
      const fn = isRecord(tc.function) ? tc.function : {};
      const id = typeof tc.id === 'string' ? tc.id : '';
      const toolIdx = toolCallKey(tc.index);
      let call: ToolCall | undefined = state.toolBlocks.get(toolIdx);
      // A new id on a known index is a new call (Gemini restarts at 0)
      if (!call || (id && id !== call.id)) {
        call = {
          id: id || `toolu_${randomUUID()}`,
          name: typeof fn.name === 'string' ? fn.name : '',
          args: '',
        };
        state.toolBlocks.set(toolIdx, call);
        state.hasToolUse = true;
        // PS fork (#73): stream it live unless another tool_use block is
        // open (its arguments may still be coming); buffer it otherwise.
        if (
          state.openBlock?.type === 'tool_use' ||
          state.bufferedTools.length
        ) {
          state.bufferedTools.push(call);
        } else {
          call.index = openBlock(state, events, 'tool_use', {
            type: 'tool_use',
            id: call.id,
            name: call.name,
            input: {},
          });
        }
      }

      const args = typeof fn.arguments === 'string' ? fn.arguments : '';
      if (args) {
        if (call.index === undefined) {
          call.args += args;
        } else if (state.openBlock?.index === call.index) {
          events.push(
            sse('content_block_delta', {
              index: call.index,
              delta: { type: 'input_json_delta', partial_json: args },
            })
          );
        }
      }
    }
  }

  // Track stop reason; close the open block once the choice finishes
  if (typeof choice?.finish_reason === 'string' && choice.finish_reason) {
    state.finishReason = choice.finish_reason;
    state.stopReason = toAnthropicStopReason(
      state.finishReason,
      state.hasToolUse
    );
    closeOpenBlock(state, events);
    flushBufferedTools(state, events);
  }

  return events.length > 0 ? events.join('') : undefined;
}

/**
 * Create initial stream state
 */
export function createStreamState(requestedModel?: unknown): StreamState {
  return {
    messageId: '',
    model: '',
    requestedModel: typeof requestedModel === 'string' ? requestedModel : '',
    hasStarted: false,
    completed: false,
    inputTokens: 0,
    outputTokens: 0,
    cacheReadTokens: 0,
    cacheCreationTokens: 0,
    contentBlockIndex: 0,
    openBlock: null,
    toolBlocks: new Map(),
    bufferedTools: [],
    hasToolUse: false,
    finishReason: null,
    stopReason: null,
  };
}
