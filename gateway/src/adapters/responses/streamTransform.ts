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
 * and the native API do. Web searches (Anthropic server tool blocks, Google
 * grounding) stream as `web_search_call` items with
 * `response.web_search_call.*` events, and citations as
 * `response.output_text.annotation.added` (webSearchOutput.ts).
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
import { carriesThinking, encodeThinkingCarry } from './thinkingCarry';
import {
  UrlCitation,
  WebSearchCallStatus,
  anthropicBlockAnnotations,
  anthropicSearchQuery,
  anthropicSearchResult,
  codePointLength,
  googleGroundingOutput,
  mergeGroundingMetadata,
  wantsWebSearchSources,
  webSearchCallItem,
} from './webSearchOutput';

type OutputKind = 'message' | 'reasoning' | 'function_call' | 'web_search_call';

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
  /** PS fork (#73): provider thinking blocks, by stream content block */
  thinking?: { index: unknown; block: any }[];
  /** PS fork (#73): the provider, to tag the carried thinking */
  provider?: string;
  /** PS fork (#73): message url_citation annotations */
  annotations?: UrlCitation[];
  /** PS fork (#73): message text length in code points */
  cpLength?: number;
  /** PS fork (#73): web search status once its result arrived */
  searchStatus?: WebSearchCallStatus;
  /** PS fork (#73): web search query (else parsed from `text`) */
  query?: string;
  /** PS fork (#73): result URLs, when the request includes sources */
  sources?: string[];
}

/** PS fork (#73): the Anthropic content block text is appended for. */
interface CitedBlock {
  index: unknown;
  /** The message item the block's text went to, and where it started */
  item: OutputItem | null;
  start: number;
  citations: any[];
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
  /** PS fork (#73): the provider; signed thinking is carried for some */
  provider: string;
  /** PS fork (#73): `include` asks for web_search_call.action.sources */
  includeSources: boolean;
  /** PS fork (#73): the current Anthropic content block and its citations */
  citedBlock: CitedBlock | null;
  /** PS fork (#73): web search items by server_tool_use id / block index */
  searchByToolId: Record<string, OutputItem>;
  searchByBlockIndex: Map<unknown, OutputItem>;
  /** PS fork (#73): Google grounding metadata seen in the stream */
  grounding: any[];
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
                annotations: item.annotations ?? [],
                logprobs: [],
              },
            ]
          : [],
      };
    case 'reasoning': {
      // PS fork (#73): signed thinking travels in encrypted_content
      const encrypted =
        final && item.thinking?.length
          ? encodeThinkingCarry(
              item.provider || '',
              item.thinking.map((t) => t.block)
            )
          : undefined;
      return {
        id: item.id,
        type: 'reasoning',
        summary:
          final && item.text ? [{ type: 'summary_text', text: item.text }] : [],
        ...(encrypted ? { encrypted_content: encrypted } : {}),
      };
    }
    case 'function_call':
      return {
        id: item.id,
        type: 'function_call',
        call_id: item.call_id,
        name: item.name,
        arguments: final ? item.text : '',
        status,
      };
    case 'web_search_call':
      // PS fork (#73): a search without a result did not complete
      return webSearchCallItem(
        item.id,
        final
          ? item.searchStatus === 'completed'
            ? 'completed'
            : 'failed'
          : item.searchStatus === 'searching'
            ? 'searching'
            : 'in_progress',
        item.query ?? anthropicSearchQuery(item.text),
        item.sources
      );
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
  web_search_call: 'ws',
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
  } else if (kind === 'web_search_call') {
    events.push(event(state, 'response.web_search_call.in_progress', base));
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
    if (state.citedBlock?.item === item) flushCitations(state, events);
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
          annotations: item.annotations ?? [],
          logprobs: [],
        },
      })
    );
  } else if (item.kind === 'web_search_call') {
    // completion events were sent when the result arrived
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

/** The open message or reasoning item of this kind, opening one. */
function ensureOpen(
  state: StreamState,
  events: string[],
  kind: 'message' | 'reasoning'
): OutputItem {
  let item = state.openItem;
  if (item?.kind !== kind) {
    if (item) closeItem(state, events, item);
    item = openItem(state, events, kind, {
      ...(kind === 'reasoning' && { provider: state.provider }),
    });
    state.openItem = item;
  }
  return item;
}

/**
 * PS fork (#73): the thinking block of stream content block `index` in the
 * open reasoning item, starting one when the index or type changes.
 */
function carriedBlock(
  state: StreamState,
  events: string[],
  index: unknown,
  type: 'thinking' | 'redacted_thinking'
): any {
  const item = ensureOpen(state, events, 'reasoning');
  item.thinking ??= [];
  const last = item.thinking[item.thinking.length - 1];
  if (last && last.index === index && last.block.type === type) {
    return last.block;
  }
  const block =
    type === 'thinking'
      ? { type, thinking: '', signature: '' }
      : { type, data: '' };
  item.thinking.push({ index, block });
  return block;
}

/** Append text to the open message or reasoning item, opening one. */
function appendText(
  state: StreamState,
  events: string[],
  kind: 'message' | 'reasoning',
  text: string
) {
  const item = ensureOpen(state, events, kind);
  const base = { item_id: item.id, output_index: item.outputIndex };
  if (kind === 'message') {
    // PS fork (#73): where the current Anthropic text block starts
    const block = state.citedBlock;
    if (block && !block.item) {
      block.item = item;
      block.start = item.cpLength ?? 0;
    }
    item.cpLength = (item.cpLength ?? 0) + codePointLength(text);
  }
  item.text += text;
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

/** PS fork (#73): add a url_citation to a message item. */
function addAnnotation(
  state: StreamState,
  events: string[],
  item: OutputItem,
  annotation: UrlCitation
) {
  item.annotations ??= [];
  events.push(
    event(state, 'response.output_text.annotation.added', {
      item_id: item.id,
      output_index: item.outputIndex,
      content_index: 0,
      annotation_index: item.annotations.length,
      annotation,
    })
  );
  item.annotations.push(annotation);
}

/**
 * PS fork (#73): annotate the current Anthropic text block once its text is
 * complete (its citations arrive before the text they cover).
 */
function flushCitations(state: StreamState, events: string[]) {
  const block = state.citedBlock;
  if (!block?.citations.length) return;
  const item = block.item;
  const citations = block.citations;
  block.citations = [];
  if (!item || item.done) return;
  for (const a of anthropicBlockAnnotations(
    citations,
    block.start,
    item.cpLength ?? 0
  )) {
    addAnnotation(state, events, item, a);
  }
}

/** PS fork (#73): a web search ran; `completed` unless it failed. */
function searchResult(
  state: StreamState,
  events: string[],
  item: OutputItem,
  status: 'completed' | 'failed',
  urls: string[]
) {
  if (item.done || item.searchStatus) return;
  const base = { item_id: item.id, output_index: item.outputIndex };
  if (state.includeSources) item.sources = urls;
  item.searchStatus = 'searching';
  events.push(event(state, 'response.web_search_call.searching', base));
  item.searchStatus = status;
  if (status === 'completed') {
    events.push(event(state, 'response.web_search_call.completed', base));
  }
}

/**
 * PS fork (#73): Anthropic web search blocks and citations in a chunk's
 * content_blocks. The stream transform drops each block's `type`, so blocks
 * are told apart by shape: a server_tool_use start has `id` and `name`
 * (client tool_use starts are tool_calls, not content_blocks), a
 * web_search_tool_result start has `tool_use_id` and `content`.
 */
function anthropicWebSearchBlock(
  state: StreamState,
  events: string[],
  block: any
) {
  const index = block?.index;
  const d = block?.delta || {};
  if (index !== undefined && state.citedBlock?.index !== index) {
    flushCitations(state, events);
    state.citedBlock = { index, item: null, start: 0, citations: [] };
  }
  if (
    typeof d.id === 'string' &&
    d.name === 'web_search' &&
    (d.type === undefined || d.type === 'server_tool_use')
  ) {
    for (const item of state.items) {
      if (item.kind !== 'web_search_call') closeItem(state, events, item);
    }
    const item = openItem(state, events, 'web_search_call', {
      call_id: d.id,
      ...(d.input && Object.keys(d.input).length
        ? { query: anthropicSearchQuery(d.input) }
        : {}),
    });
    state.searchByToolId[d.id] = item;
    state.searchByBlockIndex.set(index, item);
    return;
  }
  if (typeof d.partial_json === 'string') {
    const item = state.searchByBlockIndex.get(index);
    if (item && !item.done && item.query === undefined)
      item.text += d.partial_json;
    return;
  }
  if (typeof d.tool_use_id === 'string' && 'content' in d) {
    const item = state.searchByToolId[d.tool_use_id];
    if (!item) return;
    const { status, urls } = anthropicSearchResult(d.content);
    searchResult(state, events, item, status, urls);
    closeItem(state, events, item);
    return;
  }
  if (d.citation && state.citedBlock) {
    state.citedBlock.citations.push(d.citation);
  } else if (Array.isArray(d.citations) && state.citedBlock) {
    state.citedBlock.citations.push(...d.citations);
  }
}

/**
 * PS fork (#73): Google grounding, applied when the text is complete: the
 * annotations go on the (single) message, then each search query is a
 * completed web_search_call after the items streamed so far.
 */
function applyGrounding(state: StreamState, events: string[]) {
  if (!state.grounding.length) return;
  const messages = state.items.filter((i) => i.kind === 'message');
  const message =
    messages.length === 1 && !messages[0].done ? messages[0] : null;
  const { calls, annotations } = googleGroundingOutput(
    mergeGroundingMetadata(state.grounding),
    message?.text ?? '',
    state.includeSources
  );
  if (message) {
    for (const a of annotations) addAnnotation(state, events, message, a);
  }
  if (!calls.length) return;
  for (const item of state.items) closeItem(state, events, item);
  for (const call of calls) {
    const item = openItem(state, events, 'web_search_call', {
      query: call.action.query,
    });
    searchResult(
      state,
      events,
      item,
      'completed',
      call.action.sources?.map((s) => s.url) ?? []
    );
    closeItem(state, events, item);
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
  // PS fork (#73): grounding annotations before the message closes
  if (!state.error) applyGrounding(state, events);
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
  // and, for providers that sign thinking, the signature and redacted
  // thinking to carry to the next turn (thinkingCarry.ts)
  let sawBlockThinking = false;
  const carry = carriesThinking(state.provider);
  for (const block of delta?.content_blocks || []) {
    anthropicWebSearchBlock(state, events, block);
    const d = block?.delta || {};
    if (typeof d.thinking === 'string' && d.thinking) {
      sawBlockThinking = true;
      appendText(state, events, 'reasoning', d.thinking);
      if (carry) {
        carriedBlock(state, events, block.index, 'thinking').thinking +=
          d.thinking;
      }
    }
    if (carry && typeof d.signature === 'string' && d.signature) {
      sawBlockThinking = true;
      carriedBlock(state, events, block.index, 'thinking').signature +=
        d.signature;
    }
    if (carry && typeof d.data === 'string' && d.data) {
      sawBlockThinking = true;
      carriedBlock(state, events, block.index, 'redacted_thinking').data +=
        d.data;
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

  // PS fork (#73): Google Search grounding, applied at the end
  const grounding = parsed.choices?.[0]?.groundingMetadata;
  if (grounding && typeof grounding === 'object') {
    state.grounding.push(grounding);
  }

  // Track finish reason
  if (parsed.choices?.[0]?.finish_reason) {
    state.finishReason = parsed.choices[0].finish_reason;
  }

  return events.length > 0 ? events.join('') : undefined;
}

/**
 * Create initial stream state. PS fork (#73): `originalRequest` is the
 * client's Responses request, echoed in every response snapshot; `provider`
 * is the target, whose signed thinking is carried in encrypted_content.
 */
export function createStreamState(
  originalRequest?: any,
  provider?: string
): StreamState {
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
    provider: provider || '',
    includeSources: wantsWebSearchSources(originalRequest),
    citedBlock: null,
    searchByToolId: {},
    searchByBlockIndex: new Map(),
    grounding: [],
  };
}
