/**
 * PS fork (#73): OpenAI Responses SSE → Anthropic Messages SSE.
 *
 * `response.created` opens the message; reasoning summary, output text and
 * function-call items each become one content block (thinking, text,
 * tool_use), opened on their first content and closed when the item is
 * done. `response.completed` / `response.incomplete` close the message with
 * usage and the stop reason. A failed response or an `error` event becomes
 * an Anthropic `error` event, and so does an upstream that ends without a
 * terminal event, so the truncation stays visible to the client and to
 * telemetry (no message_stop is invented).
 */

import { randomUUID } from 'crypto';
import { toMessagesUsage } from '../shared';
import {
  normalizeResponsesUsage,
  responsesStopReason,
} from './responseTransform';

type BlockType = 'thinking' | 'text' | 'tool_use';

export interface ResponsesToMessagesStreamState {
  started: boolean;
  completed: boolean;
  messageId: string;
  model: string;
  nextIndex: number;
  /** The open content block and the Responses item it belongs to */
  open: {
    index: number;
    type: BlockType;
    itemId: string;
    summaryIndex?: number;
    sentArgs?: boolean;
  } | null;
  hasToolUse: boolean;
}

export function createResponsesToMessagesStreamState(): ResponsesToMessagesStreamState {
  return {
    started: false,
    completed: false,
    messageId: '',
    model: '',
    nextIndex: 0,
    open: null,
    hasToolUse: false,
  };
}

function sse(event: string, data: Record<string, unknown>): string {
  return `event: ${event}\ndata: ${JSON.stringify({ type: event, ...data })}\n\n`;
}

/** The JSON payload of one SSE message (its `data:` lines). */
function dataOf(chunk: string): any {
  const data = chunk
    .split(/\r?\n/)
    .filter((l) => l.startsWith('data:'))
    .map((l) => l.slice(5).trimStart())
    .join('\n');
  if (!data || data === '[DONE]') return undefined;
  try {
    return JSON.parse(data);
  } catch {
    return undefined;
  }
}

function start(
  state: ResponsesToMessagesStreamState,
  events: string[],
  response?: any
) {
  if (state.started) return;
  state.started = true;
  state.messageId = response?.id || `msg_${randomUUID()}`;
  state.model = response?.model || '';
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
        usage: { input_tokens: 0, output_tokens: 0 },
      },
    })
  );
}

function close(state: ResponsesToMessagesStreamState, events: string[]) {
  if (!state.open) return;
  events.push(sse('content_block_stop', { index: state.open.index }));
  state.open = null;
}

function open(
  state: ResponsesToMessagesStreamState,
  events: string[],
  type: BlockType,
  itemId: string,
  contentBlock: Record<string, unknown>
) {
  close(state, events);
  const index = state.nextIndex++;
  state.open = { index, type, itemId };
  events.push(
    sse('content_block_start', { index, content_block: contentBlock })
  );
  return state.open;
}

/** The open block for this item and type, opening one if needed. */
function blockFor(
  state: ResponsesToMessagesStreamState,
  events: string[],
  type: 'thinking' | 'text',
  itemId: string
) {
  if (state.open?.type === type && state.open.itemId === itemId) {
    return state.open;
  }
  return open(
    state,
    events,
    type,
    itemId,
    type === 'thinking'
      ? { type: 'thinking', thinking: '', signature: '' }
      : { type: 'text', text: '' }
  );
}

function errorEvent(
  state: ResponsesToMessagesStreamState,
  type: string,
  message: string
): string {
  state.completed = true;
  return sse('error', { error: { type, message } });
}

/**
 * Transform one Responses SSE message. Pass `null` once the upstream has
 * ended to flush a truncation error if no terminal event was seen.
 */
export function transformResponsesStreamChunk(
  chunk: string | null,
  state: ResponsesToMessagesStreamState
): string | undefined {
  if (state.completed) return undefined;
  if (chunk === null) {
    // Also when nothing arrived at all: an empty 200 stream would otherwise
    // reach the client with no events and no error.
    return errorEvent(
      state,
      'api_error',
      'Upstream stream ended before the response completed'
    );
  }

  const ev = dataOf(chunk);
  if (!ev || typeof ev !== 'object') return undefined;
  const events: string[] = [];

  switch (ev.type) {
    case 'response.created':
    case 'response.in_progress':
      start(state, events, ev.response);
      break;

    case 'response.output_item.added': {
      start(state, events);
      const item = ev.item;
      if (item?.type === 'function_call') {
        state.hasToolUse = true;
        open(state, events, 'tool_use', item.id ?? '', {
          type: 'tool_use',
          id: item.call_id || item.id || `toolu_${randomUUID()}`,
          name: item.name || '',
          input: {},
        });
      }
      break;
    }

    case 'response.function_call_arguments.delta':
      if (
        state.open?.type === 'tool_use' &&
        state.open.itemId === (ev.item_id ?? state.open.itemId) &&
        ev.delta
      ) {
        state.open.sentArgs = true;
        events.push(
          sse('content_block_delta', {
            index: state.open.index,
            delta: { type: 'input_json_delta', partial_json: ev.delta },
          })
        );
      }
      break;

    case 'response.reasoning_summary_text.delta': {
      if (!ev.delta) break;
      start(state, events);
      const block = blockFor(state, events, 'thinking', ev.item_id ?? '');
      // Summary parts of one reasoning item share a block, separated.
      const sep =
        block.summaryIndex !== undefined &&
        ev.summary_index !== undefined &&
        ev.summary_index !== block.summaryIndex
          ? '\n\n'
          : '';
      block.summaryIndex = ev.summary_index;
      events.push(
        sse('content_block_delta', {
          index: block.index,
          delta: { type: 'thinking_delta', thinking: sep + ev.delta },
        })
      );
      break;
    }

    case 'response.output_text.delta':
    case 'response.refusal.delta': {
      if (!ev.delta) break;
      start(state, events);
      const block = blockFor(state, events, 'text', ev.item_id ?? '');
      events.push(
        sse('content_block_delta', {
          index: block.index,
          delta: { type: 'text_delta', text: ev.delta },
        })
      );
      break;
    }

    case 'response.output_item.done': {
      const item = ev.item;
      if (
        state.open?.type === 'tool_use' &&
        !state.open.sentArgs &&
        item?.type === 'function_call' &&
        item.arguments
      ) {
        // Arguments sent only on the finished item
        events.push(
          sse('content_block_delta', {
            index: state.open.index,
            delta: { type: 'input_json_delta', partial_json: item.arguments },
          })
        );
      }
      if (!item?.id || state.open?.itemId === item.id) close(state, events);
      break;
    }

    case 'response.completed':
    case 'response.incomplete': {
      start(state, events, ev.response);
      close(state, events);
      state.completed = true;
      const usage = toMessagesUsage(
        normalizeResponsesUsage(ev.response?.usage)
      );
      events.push(
        sse('message_delta', {
          delta: {
            stop_reason: responsesStopReason(ev.response, state.hasToolUse),
            stop_sequence: null,
          },
          usage,
        }),
        sse('message_stop', {})
      );
      break;
    }

    case 'response.failed': {
      const err = ev.response?.error;
      events.push(
        errorEvent(state, 'api_error', err?.message || 'Response failed')
      );
      break;
    }

    case 'error':
      events.push(
        errorEvent(
          state,
          'api_error',
          ev.message || ev.error?.message || 'Stream error'
        )
      );
      break;
  }

  return events.length ? events.join('') : undefined;
}
