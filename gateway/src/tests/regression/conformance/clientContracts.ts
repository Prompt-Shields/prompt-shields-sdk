/**
 * Client contracts for the Messages and Responses adapters (#73).
 *
 * The conformance suite replays every adapted SSE stream through these
 * checkers, which enforce what the real clients rely on, and compares JSON
 * bodies against the golden shapes below. They are written from:
 *
 * - Anthropic Messages: the "Streaming Messages" event-flow docs
 *   (docs.anthropic.com/en/docs/build-with-claude/streaming, recorded
 *   examples "Basic streaming request", "Streaming request with tool use",
 *   "Streaming request with extended thinking") and the accumulator in
 *   `@anthropic-ai/sdk` `src/lib/MessageStream.ts` (`#accumulateMessage`),
 *   which Claude Code uses: a delta addresses `content[index]`, so block
 *   indexes must be dense and in order, and each delta type belongs to one
 *   block type; `message_delta` carries the stop reason and usage
 *   (`input_tokens` and the cache counts are copied when present).
 * - OpenAI Responses: the streaming event reference
 *   (platform.openai.com/docs/api-reference/responses-streaming) and the
 *   accumulator in `openai` 4.104.0 `lib/responses/ResponseStream.js`
 *   (`_ResponseStream_accumulateResponse`): the first event must be
 *   `response.created`, `output_item.added` appends to `output`, and
 *   content / argument deltas address `output[output_index]`. The Codex CLI
 *   reads finished items from `response.output_item.done` and needs a
 *   terminal `response.completed` (or `response.incomplete` / `failed`).
 *   The `sequence_number` field is checked to count up from 0 by one.
 *
 * Golden key sets come from the `Message` type of `@anthropic-ai/sdk` and the
 * `Response` / `ResponseStreamEvent` types of `openai` 4.104.0
 * (`resources/responses/responses.d.ts`).
 */

export interface SSEEvent {
  event: string;
  data: any;
}

/** Split an SSE body into events; `event:` must match `data.type`. */
export function parseSSE(text: string): SSEEvent[] {
  const out: SSEEvent[] = [];
  for (const raw of text.split(/\r?\n\r?\n/)) {
    if (!raw.trim()) continue;
    let event = '';
    const dataLines: string[] = [];
    for (const line of raw.split(/\r?\n/)) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) continue;
    const data = JSON.parse(dataLines.join('\n'));
    if (event && data?.type !== event) {
      throw new Error(`event "${event}" carries data.type "${data?.type}"`);
    }
    out.push({ event: event || data?.type, data });
  }
  return out;
}

function fail(msg: string, ev?: SSEEvent): never {
  throw new Error(ev ? `${msg} (at ${JSON.stringify(ev.data)})` : msg);
}

// ── Anthropic Messages ──────────────────────────────────────────────────────

export const ANTHROPIC_STOP_REASONS = [
  'end_turn',
  'max_tokens',
  'stop_sequence',
  'tool_use',
  'pause_turn',
  'refusal',
];

/** Golden key set of a non-streaming `Message`. */
export const ANTHROPIC_MESSAGE_KEYS = [
  'content',
  'id',
  'model',
  'role',
  'stop_reason',
  'stop_sequence',
  'type',
  'usage',
];

const DELTA_BLOCK: Record<string, string[]> = {
  text_delta: ['text'],
  citations_delta: ['text'],
  input_json_delta: ['tool_use', 'server_tool_use'],
  thinking_delta: ['thinking'],
  signature_delta: ['thinking'],
};

/** Check a non-streaming Messages body against the golden shape. */
export function checkAnthropicMessage(msg: any) {
  for (const k of ANTHROPIC_MESSAGE_KEYS) {
    if (!(k in msg)) fail(`Message lacks "${k}"`);
  }
  if (msg.type !== 'message' || msg.role !== 'assistant') {
    fail('Message type/role');
  }
  if (typeof msg.id !== 'string' || !msg.id) fail('Message id');
  if (!ANTHROPIC_STOP_REASONS.includes(msg.stop_reason)) {
    fail(`stop_reason "${msg.stop_reason}"`);
  }
  checkAnthropicUsage(msg.usage);
  for (const b of msg.content) checkAnthropicBlock(b);
}

function checkAnthropicUsage(u: any) {
  if (typeof u?.input_tokens !== 'number') fail('usage.input_tokens');
  if (typeof u?.output_tokens !== 'number') fail('usage.output_tokens');
  for (const k of ['cache_read_input_tokens', 'cache_creation_input_tokens']) {
    if (k in u && typeof u[k] !== 'number' && u[k] !== null) fail(k);
  }
}

function checkAnthropicBlock(b: any) {
  switch (b.type) {
    case 'text':
      if (typeof b.text !== 'string') fail('text block');
      break;
    case 'thinking':
      if (typeof b.thinking !== 'string' || typeof b.signature !== 'string') {
        fail('thinking block needs thinking and signature strings');
      }
      break;
    case 'redacted_thinking':
      if (typeof b.data !== 'string') fail('redacted_thinking block');
      break;
    case 'tool_use':
      if (!/^\S+$/.test(b.id ?? '') || typeof b.name !== 'string') {
        fail('tool_use id/name');
      }
      if (!b.input || typeof b.input !== 'object' || Array.isArray(b.input)) {
        fail('tool_use input must be an object');
      }
      break;
    default:
      fail(`unexpected content block type ${b.type}`);
  }
}

/**
 * Replay an Anthropic Messages stream as the SDK accumulator does and return
 * the final message. Throws on anything the SDK or Claude Code would reject.
 */
export function replayAnthropicStream(events: SSEEvent[]): any {
  const seq = events.filter((e) => e.event !== 'ping');
  if (!seq.length) fail('empty stream');
  if (seq[0].event !== 'message_start') fail('first event', seq[0]);
  const start = seq[0].data.message;
  for (const k of ANTHROPIC_MESSAGE_KEYS) {
    if (!(k in start)) fail(`message_start.message lacks "${k}"`);
  }
  if (start.content.length !== 0 || start.stop_reason !== null) {
    fail('message_start must have empty content and null stop_reason');
  }
  checkAnthropicUsage(start.usage);
  const msg = { ...start, content: [] as any[], usage: { ...start.usage } };
  const partialJson: Record<number, string> = {};
  let open: number | null = null;
  let sawDelta = false;
  let stopped = false;

  for (const ev of seq.slice(1)) {
    if (stopped) fail('event after message_stop', ev);
    const d = ev.data;
    switch (ev.event) {
      case 'content_block_start': {
        if (sawDelta) fail('block after message_delta', ev);
        if (open !== null) fail('block started before previous stopped', ev);
        if (d.index !== msg.content.length) fail('non-dense block index', ev);
        const block = { ...d.content_block };
        if (block.type === 'text' && block.text !== '') fail('text start', ev);
        if (block.type === 'thinking' && block.thinking !== '') {
          fail('thinking start', ev);
        }
        msg.content.push(block);
        if (block.type === 'tool_use') partialJson[d.index] = '';
        open = d.index;
        break;
      }
      case 'content_block_delta': {
        if (d.index !== open) fail('delta for a block that is not open', ev);
        const block = msg.content[d.index];
        const allowed = DELTA_BLOCK[d.delta?.type];
        if (!allowed?.includes(block.type)) {
          fail(`${d.delta?.type} on a ${block.type} block`, ev);
        }
        if (d.delta.type === 'text_delta') block.text += d.delta.text;
        if (d.delta.type === 'thinking_delta') {
          block.thinking += d.delta.thinking;
        }
        if (d.delta.type === 'signature_delta') {
          block.signature = d.delta.signature;
        }
        if (d.delta.type === 'input_json_delta') {
          partialJson[d.index] += d.delta.partial_json;
        }
        break;
      }
      case 'content_block_stop': {
        if (d.index !== open) fail('stop for a block that is not open', ev);
        const block = msg.content[d.index];
        if (block.type === 'tool_use') {
          const raw = partialJson[d.index];
          try {
            block.input = raw ? JSON.parse(raw) : {};
          } catch {
            fail(`tool_use input is not JSON: ${raw}`, ev);
          }
        }
        open = null;
        break;
      }
      case 'message_delta': {
        if (open !== null) fail('message_delta with a block open', ev);
        if (sawDelta) fail('second message_delta', ev);
        sawDelta = true;
        if (!ANTHROPIC_STOP_REASONS.includes(d.delta?.stop_reason)) {
          fail(`stop_reason "${d.delta?.stop_reason}"`, ev);
        }
        if (typeof d.usage?.output_tokens !== 'number') {
          fail('message_delta.usage.output_tokens', ev);
        }
        msg.stop_reason = d.delta.stop_reason;
        msg.stop_sequence = d.delta.stop_sequence ?? null;
        // Same copy rule as the SDK accumulator.
        for (const k of [
          'output_tokens',
          'input_tokens',
          'cache_read_input_tokens',
          'cache_creation_input_tokens',
        ]) {
          if (d.usage[k] != null) msg.usage[k] = d.usage[k];
        }
        break;
      }
      case 'message_stop':
        if (!sawDelta) fail('message_stop before message_delta', ev);
        stopped = true;
        break;
      case 'error':
        fail(`stream error ${JSON.stringify(d.error)}`, ev);
        break;
      default:
        fail(`unknown event ${ev.event}`, ev);
    }
  }
  if (!stopped) fail('stream ended without message_stop');
  checkAnthropicMessage(msg);
  return msg;
}

// ── OpenAI Responses ────────────────────────────────────────────────────────

/** Golden key set of a `Response` object (openai 4.104.0, required keys). */
export const RESPONSE_KEYS = [
  'created_at',
  'error',
  'id',
  'incomplete_details',
  'instructions',
  'metadata',
  'model',
  'object',
  'output',
  'parallel_tool_calls',
  'status',
  'temperature',
  'tool_choice',
  'tools',
  'top_p',
  'usage',
];

/** Check a Response body (final or streamed snapshot). */
export function checkResponse(r: any) {
  for (const k of RESPONSE_KEYS) {
    if (!(k in r)) fail(`Response lacks "${k}"`);
  }
  if (r.object !== 'response') fail('object');
  if (!['completed', 'incomplete', 'failed', 'in_progress'].includes(r.status))
    fail(`status ${r.status}`);
  if (r.status === 'incomplete' && !r.incomplete_details?.reason) {
    fail('incomplete without incomplete_details.reason');
  }
  const ids = new Set<string>();
  for (const item of r.output) {
    checkResponseItem(item);
    if (ids.has(item.id)) fail(`duplicate output id ${item.id}`);
    ids.add(item.id);
  }
  if (r.status !== 'in_progress') checkResponsesUsage(r.usage);
}

function checkResponsesUsage(u: any) {
  if (!u) fail('usage missing');
  for (const k of ['input_tokens', 'output_tokens', 'total_tokens']) {
    if (typeof u[k] !== 'number') fail(`usage.${k}`);
  }
  if (typeof u.input_tokens_details?.cached_tokens !== 'number') {
    fail('usage.input_tokens_details.cached_tokens');
  }
  if (typeof u.output_tokens_details?.reasoning_tokens !== 'number') {
    fail('usage.output_tokens_details.reasoning_tokens');
  }
  if (u.total_tokens !== u.input_tokens + u.output_tokens) {
    fail('usage.total_tokens != input + output');
  }
}

function checkResponseItem(item: any) {
  if (typeof item.id !== 'string' || !item.id) fail('output item id');
  switch (item.type) {
    case 'message':
      if (item.role !== 'assistant' || !Array.isArray(item.content)) {
        fail('message item');
      }
      for (const p of item.content) {
        if (p.type !== 'output_text' || typeof p.text !== 'string') {
          fail('message content part');
        }
        if (!Array.isArray(p.annotations)) fail('output_text.annotations');
      }
      break;
    case 'function_call':
      for (const k of ['call_id', 'name', 'arguments']) {
        if (typeof item[k] !== 'string') fail(`function_call.${k}`);
      }
      break;
    case 'reasoning':
      if (!Array.isArray(item.summary)) fail('reasoning.summary');
      for (const s of item.summary) {
        if (s.type !== 'summary_text' || typeof s.text !== 'string') {
          fail('reasoning summary part');
        }
      }
      break;
    default:
      fail(`unexpected output item type ${item.type}`);
  }
}

const TERMINAL = [
  'response.completed',
  'response.incomplete',
  'response.failed',
];

/**
 * Replay a Responses stream as the openai SDK accumulator does (plus the
 * documented `.done` events Codex reads) and return the terminal response.
 */
export function replayResponsesStream(events: SSEEvent[]): any {
  if (!events.length) fail('empty stream');
  events.forEach((ev, i) => {
    if (ev.data.sequence_number !== i) fail(`sequence_number != ${i}`, ev);
  });
  if (events[0].event !== 'response.created') fail('first event', events[0]);
  checkResponse(events[0].data.response);
  const output: any[] = [];
  const done = new Set<number>();
  let terminal: any = null;

  const itemAt = (ev: SSEEvent, type: string) => {
    const item = output[ev.data.output_index];
    if (!item) fail(`missing output at index ${ev.data.output_index}`, ev);
    if (item.type !== type) fail(`expected ${type} item`, ev);
    if (done.has(ev.data.output_index)) fail('event for a done item', ev);
    if ('item_id' in ev.data && ev.data.item_id !== item.id) {
      fail('item_id does not match the item at output_index', ev);
    }
    return item;
  };

  for (const ev of events.slice(1)) {
    if (terminal) fail('event after the terminal event', ev);
    const d = ev.data;
    switch (ev.event) {
      case 'response.in_progress':
        break;
      case 'response.output_item.added':
        if (d.output_index !== output.length) {
          fail('output_index is not the next position', ev);
        }
        if (d.item.status && d.item.status !== 'in_progress') {
          fail('added item not in_progress', ev);
        }
        output.push(JSON.parse(JSON.stringify(d.item)));
        if (d.item.type === 'function_call') {
          output[output.length - 1].arguments = d.item.arguments ?? '';
        }
        break;
      case 'response.content_part.added': {
        const item = itemAt(ev, 'message');
        if (d.content_index !== item.content.length) {
          fail('content_index is not the next part', ev);
        }
        item.content.push({ ...d.part });
        break;
      }
      case 'response.output_text.delta': {
        const part = itemAt(ev, 'message').content[d.content_index];
        if (part?.type !== 'output_text') fail('missing output_text', ev);
        part.text += d.delta;
        break;
      }
      case 'response.output_text.done': {
        const part = itemAt(ev, 'message').content[d.content_index];
        if (part?.text !== d.text) fail('output_text.done text', ev);
        break;
      }
      case 'response.content_part.done': {
        const part = itemAt(ev, 'message').content[d.content_index];
        if (part?.text !== d.part?.text) fail('content_part.done text', ev);
        break;
      }
      case 'response.function_call_arguments.delta':
        itemAt(ev, 'function_call').arguments += d.delta;
        break;
      case 'response.function_call_arguments.done': {
        const item = itemAt(ev, 'function_call');
        if (item.arguments !== d.arguments) fail('arguments.done', ev);
        break;
      }
      case 'response.reasoning_summary_part.added': {
        const item = itemAt(ev, 'reasoning');
        if (d.summary_index !== item.summary.length) {
          fail('summary_index is not the next part', ev);
        }
        item.summary.push({ ...d.part });
        break;
      }
      case 'response.reasoning_summary_text.delta': {
        const part = itemAt(ev, 'reasoning').summary[d.summary_index];
        if (part?.type !== 'summary_text') fail('missing summary part', ev);
        part.text += d.delta;
        break;
      }
      case 'response.reasoning_summary_text.done':
      case 'response.reasoning_summary_part.done': {
        const part = itemAt(ev, 'reasoning').summary[d.summary_index];
        const text = d.text ?? d.part?.text;
        if (part?.text !== text) fail(`${ev.event} text`, ev);
        break;
      }
      case 'response.output_item.done': {
        const item = output[d.output_index];
        if (!item || item.id !== d.item.id || item.type !== d.item.type) {
          fail('output_item.done does not match the added item', ev);
        }
        expectSameItem(item, d.item, ev);
        done.add(d.output_index);
        output[d.output_index] = d.item;
        break;
      }
      case 'error':
        fail(`stream error ${JSON.stringify(d)}`, ev);
        break;
      default:
        if (TERMINAL.includes(ev.event)) {
          terminal = d.response;
          break;
        }
        fail(`unknown event ${ev.event}`, ev);
    }
  }
  if (!terminal) fail('stream ended without a terminal response event');
  if (done.size !== output.length) fail('an output item was never done');
  checkResponse(terminal);
  if (terminal.output.length !== output.length) {
    fail('terminal output differs from the streamed items');
  }
  terminal.output.forEach((item: any, i: number) => {
    if (item.id !== output[i].id) fail(`terminal output[${i}] id`);
    expectSameItem(output[i], item);
  });
  return terminal;
}

function expectSameItem(streamed: any, final: any, ev?: SSEEvent) {
  if (final.type === 'message') {
    const a = streamed.content.map((p: any) => p.text).join('|');
    const b = final.content.map((p: any) => p.text).join('|');
    if (a !== b) fail(`message text "${a}" vs "${b}"`, ev);
  } else if (final.type === 'function_call') {
    if (streamed.arguments !== final.arguments) fail('arguments', ev);
    if (streamed.call_id !== final.call_id || streamed.name !== final.name) {
      fail('function_call call_id/name', ev);
    }
  } else if (final.type === 'reasoning') {
    const a = streamed.summary.map((p: any) => p.text).join('|');
    const b = final.summary.map((p: any) => p.text).join('|');
    if (a !== b) fail(`reasoning summary "${a}" vs "${b}"`, ev);
  }
}
