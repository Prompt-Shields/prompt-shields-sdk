/**
 * Stream Transform for Responses API Adapter
 *
 * Transforms Chat Completions SSE chunks to Responses API SSE chunks in real-time.
 * This enables streaming support for providers that only support Chat Completions.
 *
 * PS fork (#73): output items are opened lazily in the order their content
 * arrives (reasoning, message, function calls), so `output_index` matches
 * the non-streaming output and an item exists only when it has content, as
 * with the native Responses API. Reasoning streams as a `reasoning` item
 * with `reasoning_summary_*` events, each item is closed with its `.done`
 * events and `response.output_item.done`, and a mid-stream provider error
 * ends the stream with `response.failed`. Every response snapshot echoes
 * the client's request fields (`echoRequestFields`), as the JSON response
 * and the native API do.
 */

import { randomUUID } from 'crypto';
import {
  normalizeUsage,
  plainReasoningText,
  toResponsesUsage,
} from '../shared';
import {
  echoRequestFields,
  responseModel,
  toResponsesStatus,
} from './responseTransform';

type OutputKind = 'message' | 'reasoning' | 'function_call';

/** PS fork (#73): one output item of the streamed response. */
interface OutputItem {
  kind: OutputKind;
  id: string;
  outputIndex: number;
  /** Message text, reasoning summary text, or function call arguments */
  text: string;
  done: boolean;
  call_id?: string;
  name?: string;
}

interface StreamState {
  responseId: string;
  /** Id of the (last) message output item; '' until text arrives */
  outputItemId: string;
  contentPartIndex: number;
  hasStarted: boolean;
  completed: boolean;
  model: string;
  inputTokens: number;
  outputTokens: number;
  finishReason: string | null;
  sequenceNumber: number;
  /** All message text so far */
  accumulatedText: string;
  createdAt: number;
  /** PS fork (#73): cached input tokens (input_tokens_details) */
  cachedTokens: number;
  cacheCreationTokens: number;
  /** PS fork (#73): reasoning output tokens (output_tokens_details) */
  reasoningTokens: number;
  totalTokens: number;
  /** PS fork (#73): output items in output_index order */
  items: OutputItem[];
  /** The message or reasoning item text is currently appended to */
  openItem: OutputItem | null;
  /** Chat tool_call index → function call item */
  toolCallByIndex: Record<number, OutputItem>;
  /** PS fork (#73): a mid-stream provider error */
  error: { code: string; message: string } | null;
  /** PS fork (#73): model named in the request, for chunks without one */
  requestedModel: string;
  /** PS fork (#73): request fields every snapshot echoes */
  requestEcho: ReturnType<typeof echoRequestFields>;
}

function itemJson(item: OutputItem, status: 'in_progress' | 'completed') {
  const final = status !== 'in_progress';
  switch (item.kind) {
    case 'message':
      return {
        id: item.id,
        type: 'message',
        role: 'assistant',
        status,
        content: final
          ? [
              {
                type: 'output_text',
                text: item.text,
                annotations: [],
                logprobs: [],
              },
            ]
          : [],
      };
    case 'reasoning':
      return {
        id: item.id,
        type: 'reasoning',
        summary: final ? [{ type: 'summary_text', text: item.text }] : [],
      };
    case 'function_call':
      return {
        id: item.id,
        type: 'function_call',
        call_id: item.call_id,
        name: item.name,
        arguments: final ? item.text : '',
        status,
      };
  }
}

function event(state: StreamState, type: string, data: object): string {
  return `event: ${type}\ndata: ${JSON.stringify({
    type,
    sequence_number: state.sequenceNumber++,
    ...data,
  })}\n\n`;
}

const ID_PREFIX: Record<OutputKind, string> = {
  message: 'msg',
  reasoning: 'rs',
  function_call: 'fc',
};

function openItem(
  state: StreamState,
  events: string[],
  kind: OutputKind,
  extra: Partial<OutputItem> = {}
): OutputItem {
  const item: OutputItem = {
    kind,
    id: `${ID_PREFIX[kind]}_${randomUUID()}`,
    outputIndex: state.items.length,
    text: '',
    done: false,
    ...extra,
  };
  state.items.push(item);
  events.push(
    event(state, 'response.output_item.added', {
      output_index: item.outputIndex,
      item: itemJson(item, 'in_progress'),
    })
  );
  const base = { item_id: item.id, output_index: item.outputIndex };
  if (kind === 'message') {
    state.outputItemId = item.id;
    events.push(
      event(state, 'response.content_part.added', {
        ...base,
        content_index: 0,
        part: { type: 'output_text', text: '', annotations: [], logprobs: [] },
      })
    );
  } else if (kind === 'reasoning') {
    events.push(
      event(state, 'response.reasoning_summary_part.added', {
        ...base,
        summary_index: 0,
        part: { type: 'summary_text', text: '' },
      })
    );
  }
  return item;
}

function closeItem(state: StreamState, events: string[], item: OutputItem) {
  if (item.done) return;
  item.done = true;
  if (state.openItem === item) state.openItem = null;
  const base = { item_id: item.id, output_index: item.outputIndex };
  if (item.kind === 'message') {
    events.push(
      event(state, 'response.output_text.done', {
        ...base,
        content_index: 0,
        text: item.text,
        logprobs: [],
      }),
      event(state, 'response.content_part.done', {
        ...base,
        content_index: 0,
        part: {
          type: 'output_text',
          text: item.text,
          annotations: [],
          logprobs: [],
        },
      })
    );
  } else if (item.kind === 'reasoning') {
    events.push(
      event(state, 'response.reasoning_summary_text.done', {
        ...base,
        summary_index: 0,
        text: item.text,
      }),
      event(state, 'response.reasoning_summary_part.done', {
        ...base,
        summary_index: 0,
        part: { type: 'summary_text', text: item.text },
      })
    );
  } else {
    events.push(
      event(state, 'response.function_call_arguments.done', {
        ...base,
        arguments: item.text,
      })
    );
  }
  events.push(
    event(state, 'response.output_item.done', {
      output_index: item.outputIndex,
      item: itemJson(item, 'completed'),
    })
  );
}

/** Append text to the open message or reasoning item, opening one. */
function appendText(
  state: StreamState,
  events: string[],
  kind: 'message' | 'reasoning',
  text: string
) {
  let item = state.openItem;
  if (item?.kind !== kind) {
    if (item) closeItem(state, events, item);
    item = openItem(state, events, kind);
    state.openItem = item;
  }
  item.text += text;
  const base = { item_id: item.id, output_index: item.outputIndex };
  if (kind === 'message') {
    state.accumulatedText += text;
    events.push(
      event(state, 'response.output_text.delta', {
        ...base,
        content_index: 0,
        delta: text,
        logprobs: [],
      })
    );
  } else {
    events.push(
      event(state, 'response.reasoning_summary_text.delta', {
        ...base,
        summary_index: 0,
        delta: text,
      })
    );
  }
}

/**
 * Build a complete response snapshot with all required fields
 */
function buildResponseSnapshot(
  state: StreamState,
  status: 'in_progress' | 'completed' | 'incomplete' | 'failed',
  includeOutput: boolean = false
): any {
  const output = includeOutput
    ? state.items.map((item) =>
        itemJson(item, status === 'in_progress' ? 'in_progress' : 'completed')
      )
    : [];

  return {
    id: state.responseId,
    object: 'response',
    created_at: state.createdAt,
    completed_at: status === 'completed' ? Math.floor(Date.now() / 1000) : null,
    status,
    incomplete_details:
      status === 'incomplete'
        ? {
            reason:
              toResponsesStatus(state.finishReason).reason ??
              'max_output_tokens',
          }
        : null,
    model: state.model,
    output,
    error: status === 'failed' ? state.error : null,
    usage: {
      input_tokens: state.inputTokens,
      input_tokens_details: {
        cached_tokens: state.cachedTokens,
        // PS fork (#9): cache writes, only when there are any
        ...(state.cacheCreationTokens
          ? { cache_creation_tokens: state.cacheCreationTokens }
          : {}),
      },
      output_tokens: state.outputTokens,
      output_tokens_details: { reasoning_tokens: state.reasoningTokens },
      // PS fork (#73): always input + output, as the Responses API reports
      total_tokens: state.inputTokens + state.outputTokens,
    },
    // PS fork (#73): echo the request (tools, instructions, sampling,
    // text.format, reasoning, metadata, ...) as the JSON response does
    ...state.requestEcho,
  };
}

/** Close every open item and emit the terminal response event. */
function finish(state: StreamState): string {
  state.completed = true;
  const events: string[] = [];
  for (const item of state.items) closeItem(state, events, item);
  const status = state.error
    ? 'failed'
    : toResponsesStatus(state.finishReason).status;
  const type =
    status === 'failed'
      ? 'response.failed'
      : status === 'incomplete'
        ? 'response.incomplete'
        : 'response.completed';
  events.push(
    event(state, type, {
      response: buildResponseSnapshot(state, status as any, true),
    })
  );
  return events.join('');
}

/**
 * Transform a single Chat Completions stream chunk to Responses API stream events
 */
export function transformStreamChunk(
  chunk: string,
  state: StreamState
): string | undefined {
  // Skip empty chunks and [DONE]
  const trimmed = chunk.trim();
  if (!trimmed || trimmed === 'data: [DONE]') {
    if (trimmed === 'data: [DONE]' && state.hasStarted && !state.completed) {
      return finish(state);
    }
    return undefined;
  }

  // Parse the chunk
  const dataPrefix = 'data: ';
  if (!trimmed.startsWith(dataPrefix)) return undefined;

  let parsed: any;
  try {
    parsed = JSON.parse(trimmed.slice(dataPrefix.length));
  } catch {
    return undefined;
  }
  if (state.completed) return undefined;

  const events: string[] = [];

  // Initialize on first chunk
  if (!state.hasStarted) {
    state.hasStarted = true;
    state.responseId = `resp_${randomUUID()}`;
    state.contentPartIndex = 0;
    // PS fork (#73): chunks without a model (e.g. Gemini) fall back to the
    // requested one, as the JSON response does
    state.model = parsed.model || state.requestedModel;
    state.createdAt = Math.floor(Date.now() / 1000);

    events.push(
      event(state, 'response.created', {
        response: buildResponseSnapshot(state, 'in_progress'),
      }),
      event(state, 'response.in_progress', {
        response: buildResponseSnapshot(state, 'in_progress'),
      })
    );
  }

  // PS fork (#73): a mid-stream provider error ends the response as failed
  if (parsed?.error) {
    const err = parsed.error;
    state.error = {
      code:
        (typeof err === 'object' && (err.code || err.type)) || 'server_error',
      message: (typeof err === 'string' ? err : err?.message) || 'Stream error',
    };
    return events.join('') + finish(state);
  }

  const delta = parsed.choices?.[0]?.delta;

  // PS fork (#73): reasoning, from content_blocks (Anthropic, Gemini,
  // OpenRouter transforms) or reasoning_content / reasoning strings
  let sawBlockThinking = false;
  for (const block of delta?.content_blocks || []) {
    const thinking = block?.delta?.thinking;
    if (typeof thinking === 'string' && thinking) {
      sawBlockThinking = true;
      appendText(state, events, 'reasoning', thinking);
    }
  }
  if (!sawBlockThinking) {
    const reasoning = plainReasoningText(delta);
    if (reasoning) appendText(state, events, 'reasoning', reasoning);
  }

  // Handle content delta
  if (typeof delta?.content === 'string' && delta.content) {
    appendText(state, events, 'message', delta.content);
  }

  // Handle tool calls. PS fork (#73): each call is its own output item with
  // a stable item id, and argument deltas reference that item.
  if (Array.isArray(delta?.tool_calls)) {
    for (const tc of delta.tool_calls) {
      if (!tc) continue;
      const toolIdx = tc.index ?? 0;
      let fc: OutputItem | undefined = state.toolCallByIndex[toolIdx];
      // A new id on a known index is a new call (Gemini restarts at 0)
      if (!fc || (tc.id && tc.id !== fc.call_id)) {
        if (state.openItem) closeItem(state, events, state.openItem);
        fc = openItem(state, events, 'function_call', {
          call_id: tc.id || `call_${randomUUID()}`,
          name: tc.function?.name || '',
        });
        state.toolCallByIndex[toolIdx] = fc;
      }
      if (tc.function?.arguments) {
        fc.text += tc.function.arguments;
        events.push(
          event(state, 'response.function_call_arguments.delta', {
            item_id: fc.id,
            output_index: fc.outputIndex,
            delta: tc.function.arguments,
          })
        );
      }
    }
  }

  // Track usage if provided
  if (parsed.usage) {
    // PS fork (#73): keep cached and reasoning token detail
    const u = toResponsesUsage(normalizeUsage(parsed.usage));
    state.inputTokens = u.input_tokens;
    state.outputTokens = u.output_tokens;
    state.cachedTokens = u.input_tokens_details.cached_tokens;
    state.cacheCreationTokens =
      u.input_tokens_details.cache_creation_tokens ?? 0;
    state.reasoningTokens = u.output_tokens_details.reasoning_tokens;
    state.totalTokens = u.total_tokens;
  }

  // Track finish reason
  if (parsed.choices?.[0]?.finish_reason) {
    state.finishReason = parsed.choices[0].finish_reason;
  }

  return events.length > 0 ? events.join('') : undefined;
}

/**
 * Create initial stream state. PS fork (#73): `originalRequest` is the
 * client's Responses request, echoed in every response snapshot.
 */
export function createStreamState(originalRequest?: any): StreamState {
  return {
    responseId: '',
    outputItemId: '',
    contentPartIndex: 0,
    hasStarted: false,
    completed: false,
    model: '',
    inputTokens: 0,
    outputTokens: 0,
    finishReason: null,
    sequenceNumber: 0,
    accumulatedText: '',
    createdAt: 0,
    cachedTokens: 0,
    cacheCreationTokens: 0,
    reasoningTokens: 0,
    totalTokens: 0,
    items: [],
    openItem: null,
    toolCallByIndex: {},
    error: null,
    requestedModel: responseModel(originalRequest),
    requestEcho: echoRequestFields(originalRequest),
  };
}
