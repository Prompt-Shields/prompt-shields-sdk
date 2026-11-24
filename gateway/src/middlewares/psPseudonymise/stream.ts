/**
 * PS fork (#350): restoring placeholders in streamed answers, including
 * placeholders split across chunks. A port of the SDK's `_ChunkRestorer`
 * (packages/sdk/prompt_shields/pseudonymise.py) over parsed SSE events,
 * plus OpenAI Responses events, and the SSE byte-stream transform the
 * middleware puts on the response.
 */
import { Pseudonymisation, StreamRestorer } from './core';

type Json = any;
const isObj = (v: unknown): v is Record<string, any> =>
  !!v && typeof v === 'object' && !Array.isArray(v);
const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v));

/*
 * Restorer keys (one restorer per streamed field):
 *   c|r <choice>           chat delta content / refusal
 *   t <choice> <tool>      chat tool-call arguments (JSON)
 *   f <choice>             chat legacy function_call arguments (JSON)
 *   a|j <block>            Anthropic text / partial_json (JSON)
 *   o|R <out> <part>       Responses output_text / refusal delta
 *   J <out>                Responses function_call_arguments delta (JSON)
 *   C <out>                Responses custom_tool_call_input delta
 */
const JSON_CODES = new Set(['t', 'f', 'j', 'J']);
const CHAT_CODES = new Set(['c', 'r', 't', 'f']);
const RESPONSES_CODES = new Set(['o', 'R', 'J', 'C']);

// Responses delta event -> [code, its done event, the done event's field].
const RESPONSES_DELTAS: Record<string, [string, string, string]> = {
  'response.output_text.delta': ['o', 'response.output_text.done', 'text'],
  'response.refusal.delta': ['R', 'response.refusal.done', 'refusal'],
  'response.function_call_arguments.delta': [
    'J',
    'response.function_call_arguments.done',
    'arguments',
  ],
  'response.custom_tool_call_input.delta': [
    'C',
    'response.custom_tool_call_input.done',
    'input',
  ],
};
const RESPONSES_DONE: Record<string, [string, string]> = Object.fromEntries(
  Object.values(RESPONSES_DELTAS).map(([code, done, field]) => [
    done,
    [code, field],
  ])
);

const responsesKey = (code: string, event: Json) =>
  code === 'J' || code === 'C'
    ? `${code}|${event.output_index}`
    : `${code}|${event.output_index}|${event.content_index}`;

/**
 * Per-field restorers over a stream of parsed events. `process` returns
 * the events to emit for one event, restored; text still held back when a
 * field ends comes out in an extra event, a copy of the field's last event.
 */
export class ChunkRestorer {
  #restorers = new Map<string, StreamRestorer>();
  #last = new Map<string, Json>();
  constructor(private readonly owner: Pseudonymisation) {}

  #feed(key: string, text: unknown, chunk: Json): unknown {
    if (typeof text !== 'string') return text;
    let restorer = this.#restorers.get(key);
    if (!restorer) {
      restorer = this.owner.streamRestorer(JSON_CODES.has(key.split('|')[0]));
      this.#restorers.set(key, restorer);
    }
    this.#last.set(key, chunk);
    return restorer.feed(text);
  }

  #flush(key: string): Json | undefined {
    const restorer = this.#restorers.get(key);
    const last = this.#last.get(key);
    this.#restorers.delete(key);
    this.#last.delete(key);
    if (!restorer || !restorer.pending || last === undefined) return undefined;
    return synthetic(key, last, restorer.flush());
  }

  process(chunk: Json): Json[] {
    if (!isObj(chunk)) return [chunk];
    const before: Json[] = [];
    const kind = chunk.type;

    // Anthropic Messages events.
    if (kind === 'content_block_delta') {
      const delta = chunk.delta;
      if (isObj(delta)) {
        if (typeof delta.text === 'string')
          delta.text = this.#feed(`a|${chunk.index}`, delta.text, chunk);
        else if (typeof delta.partial_json === 'string')
          delta.partial_json = this.#feed(
            `j|${chunk.index}`,
            delta.partial_json,
            chunk
          );
      }
      return [chunk];
    }
    if (kind === 'content_block_stop') {
      for (const key of [`a|${chunk.index}`, `j|${chunk.index}`]) {
        const extra = this.#flush(key);
        if (extra !== undefined) before.push(extra);
      }
      return [...before, chunk];
    }

    // OpenAI Responses events.
    if (typeof kind === 'string' && kind.startsWith('response.')) {
      const deltaSpec = RESPONSES_DELTAS[kind];
      if (deltaSpec) {
        chunk.delta = this.#feed(
          responsesKey(deltaSpec[0], chunk),
          chunk.delta,
          chunk
        );
        return [chunk];
      }
      const doneSpec = RESPONSES_DONE[kind];
      if (doneSpec) {
        const [code, field] = doneSpec;
        const extra = this.#flush(responsesKey(code, chunk));
        if (extra !== undefined) before.push(extra);
        if (typeof chunk[field] === 'string')
          chunk[field] = this.owner.restore(chunk[field], JSON_CODES.has(code));
        return [...before, chunk];
      }
      if (isObj(chunk.part)) this.owner.restoreResponsesItem(chunk.part);
      if (isObj(chunk.item)) this.owner.restoreResponsesItem(chunk.item);
      if (isObj(chunk.response))
        this.owner.restoreResponsesObject(chunk.response);
      return [chunk];
    }

    // Chat Completions chunks.
    if (!Array.isArray(chunk.choices)) return [chunk];
    for (const choice of chunk.choices) {
      if (!isObj(choice)) continue;
      const index = choice.index;
      const delta = choice.delta;
      // Where each field of this choice was written in this chunk.
      const fed = new Map<string, [Json, string]>();
      if (isObj(delta)) {
        for (const [code, name] of [
          ['c', 'content'],
          ['r', 'refusal'],
        ]) {
          if (typeof delta[name] === 'string') {
            const key = `${code}|${index}`;
            delta[name] = this.#feed(key, delta[name], chunk);
            fed.set(key, [delta, name]);
          }
        }
        const calls = Array.isArray(delta.tool_calls) ? delta.tool_calls : [];
        for (const tc of calls) {
          const fn = isObj(tc) ? tc.function : undefined;
          if (isObj(fn) && typeof fn.arguments === 'string') {
            const key = `t|${index}|${tc.index}`;
            fn.arguments = this.#feed(key, fn.arguments, chunk);
            fed.set(key, [fn, 'arguments']);
          }
        }
        const fc = delta.function_call;
        if (isObj(fc) && typeof fc.arguments === 'string') {
          const key = `f|${index}`;
          fc.arguments = this.#feed(key, fc.arguments, chunk);
          fed.set(key, [fc, 'arguments']);
        }
      }
      if (choice.finish_reason === null || choice.finish_reason === undefined)
        continue;
      for (const key of [...this.#restorers.keys()]) {
        const [code, choiceIndex] = key.split('|');
        if (!CHAT_CODES.has(code) || choiceIndex !== String(index)) continue;
        const written = fed.get(key);
        if (written) {
          // Written in this chunk: append the rest in place.
          const [obj, name] = written;
          const restorer = this.#restorers.get(key) as StreamRestorer;
          this.#restorers.delete(key);
          this.#last.delete(key);
          obj[name] = (obj[name] ?? '') + restorer.flush();
          continue;
        }
        const extra = this.#flush(key);
        if (extra !== undefined) before.push(extra);
      }
    }
    return [...before, chunk];
  }

  /** Extra events for text still held back when the stream ends. */
  finish(): Json[] {
    const out: Json[] = [];
    for (const key of [...this.#restorers.keys()]) {
      const extra = this.#flush(key);
      if (extra !== undefined) out.push(extra);
    }
    return out;
  }
}

function choiceOf(chunk: Json, index: string): Json {
  return chunk.choices.find((c: Json) => String(c?.index) === index);
}

/** A copy of `last` (the field's last event) carrying only `text`. */
function synthetic(key: string, last: Json, text: string): Json {
  const chunk = clone(last);
  const [code, a, b] = key.split('|');
  if (code === 'a' || code === 'j') {
    chunk.delta[code === 'j' ? 'partial_json' : 'text'] = text;
    return chunk;
  }
  if (RESPONSES_CODES.has(code)) {
    chunk.delta = text;
    return chunk;
  }
  const choice = choiceOf(chunk, a);
  chunk.choices = [choice];
  choice.finish_reason = null;
  const delta = choice.delta;
  for (const name of ['content', 'refusal', 'tool_calls', 'function_call'])
    if (delta[name] !== undefined && delta[name] !== null) delta[name] = null;
  if (code === 'f') {
    const fc = clone(choiceOf(last, a).delta.function_call);
    if (fc.name !== undefined && fc.name !== null) fc.name = null;
    fc.arguments = text;
    delta.function_call = fc;
  } else if (code === 't') {
    const tc = clone(
      choiceOf(last, a).delta.tool_calls.find(
        (t: Json) => String(t?.index) === b
      )
    );
    for (const name of ['id', 'type'])
      if (tc[name] !== undefined && tc[name] !== null) tc[name] = null;
    const fn = tc.function;
    if (fn.name !== undefined && fn.name !== null) fn.name = null;
    fn.arguments = text;
    delta.tool_calls = [tc];
  } else {
    delta[code === 'c' ? 'content' : 'refusal'] = text;
  }
  return chunk;
}

// --- SSE -------------------------------------------------------------------

// An event larger than this without a boundary is passed through as it is
// (unrestored: the client sees placeholders, never another value).
const MAX_EVENT_CHARS = 4 << 20;
const BOUNDARY = /\r\n\r\n|\n\n|\r\r/g;

// An extra event: Anthropic and Responses streams name every event after
// its `type`; Chat Completions streams have data lines only.
function serialize(event: Json, named: boolean): string {
  const data = `data: ${JSON.stringify(event)}`;
  return named && isObj(event) && typeof event.type === 'string'
    ? `event: ${event.type}\n${data}`
    : data;
}

/**
 * The SSE event `block` (without its terminating blank line) restored,
 * as zero or more blocks. Events whose data is not a JSON object (`[DONE]`)
 * first release whatever text is still held back. An event the restorer
 * leaves unchanged is returned byte for byte.
 */
function restoreBlock(
  block: string,
  restorer: ChunkRestorer,
  state: { named: boolean }
): string[] {
  const data: string[] = [];
  const other: string[] = [];
  for (const line of block.split(/\r\n|\n|\r/)) {
    if (line.startsWith('data:'))
      data.push(line.slice(line.startsWith('data: ') ? 6 : 5));
    else {
      if (line.startsWith('event:')) state.named = true;
      other.push(line);
    }
  }
  if (!data.length) return [block];
  let parsed: Json;
  try {
    parsed = JSON.parse(data.join('\n'));
  } catch {
    parsed = undefined;
  }
  if (!isObj(parsed))
    return [...restorer.finish().map((e) => serialize(e, state.named)), block];
  const before = JSON.stringify(parsed);
  const out = restorer.process(parsed);
  if (
    out.length === 1 &&
    out[0] === parsed &&
    JSON.stringify(parsed) === before
  )
    return [block];
  return out.map((e) =>
    e === parsed
      ? [...other, `data: ${JSON.stringify(e)}`].join('\n')
      : serialize(e, state.named)
  );
}

/**
 * An SSE byte stream with this request's placeholders restored. `onDone`
 * runs once: when the stream ends, fails or is cancelled.
 */
export function restoreSSE(
  body: ReadableStream<Uint8Array>,
  owner: Pseudonymisation,
  onDone: () => void
): ReadableStream<Uint8Array> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  const restorer = new ChunkRestorer(owner);
  const state = { named: false };
  // Text after the last complete event, kept in pieces: a long event that
  // arrives in small chunks is neither copied nor searched again per chunk.
  let pieces: string[] = [];
  let pendingChars = 0;
  let tail = ''; // the last 3 characters of the pieces
  let finished = false;
  const done = () => {
    if (finished) return;
    finished = true;
    try {
      onDone();
    } catch {
      /* never break the stream */
    }
  };

  // Complete events in `buffer`, restored; returns them and the rest.
  const drainAll = (buffer: string): [string, string] => {
    let out = '';
    while (true) {
      BOUNDARY.lastIndex = 0;
      const m = BOUNDARY.exec(buffer);
      if (!m) break;
      const block = buffer.slice(0, m.index);
      buffer = buffer.slice(m.index + m[0].length);
      out += restoreBlock(block, restorer, state)
        .map((b) => b + m[0])
        .join('');
    }
    return [out, buffer];
  };

  // `text` appended; complete events restored and returned.
  const push = (text: string): string => {
    if (!text) return '';
    // Only the new text, plus the 3 characters before it (a boundary is at
    // most 4), can hold a boundary not seen yet.
    BOUNDARY.lastIndex = 0;
    if (!BOUNDARY.test(tail + text)) {
      pieces.push(text);
      pendingChars += text.length;
      tail = (tail + text).slice(-3);
      // Too long without a boundary: passed through as it is.
      return pendingChars <= MAX_EVENT_CHARS ? '' : rest();
    }
    const [out, left] = drainAll(pieces.join('') + text);
    pieces = left ? [left] : [];
    pendingChars = left.length;
    tail = left.slice(-3);
    return out;
  };

  const rest = (): string => {
    const raw = pieces.join('');
    pieces = [];
    pendingChars = 0;
    tail = '';
    return raw;
  };

  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      while (true) {
        let result: ReadableStreamReadResult<Uint8Array>;
        try {
          result = await reader.read();
        } catch (err) {
          done();
          controller.error(err);
          return;
        }
        if (result.done) {
          let out = push(decoder.decode());
          // Held-back text, then whatever trailing bytes had no boundary.
          out += restorer
            .finish()
            .map((e) => serialize(e, state.named) + '\n\n')
            .join('');
          out += rest();
          if (out) controller.enqueue(encoder.encode(out));
          controller.close();
          done();
          return;
        }
        const out = push(decoder.decode(result.value, { stream: true }));
        if (out) {
          controller.enqueue(encoder.encode(out));
          return;
        }
      }
    },
    cancel(reason) {
      done();
      return reader.cancel(reason);
    },
  });
}
