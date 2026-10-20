/**
 * PS fork (#63, MCP Phase 1): filter every SSE stream from an upstream MCP
 * server without buffering the stream.
 *
 * Events are forwarded as they complete. A client matches a JSON-RPC response
 * to its request by `id`, whatever stream it arrives on, so a response is
 * released only where the gateway checked it:
 * - on the stream of a POST, only the response to that request (same `id`),
 *   after `onResponse` has checked it; responses with any other `id` are
 *   dropped (the spec requires a stream's messages to relate to its request);
 * - on a GET stream (2025-11-25), no response at all. Resuming a stream with
 *   `Last-Event-ID` would replay a `tools/call` or `tools/list` response the
 *   gateway never saw, so responses there are dropped.
 *
 * Every forwarded message is the JSON the gateway parsed, re-serialised, so a
 * client cannot read a different message (duplicate keys, `NaN`, batches).
 * Data the gateway cannot parse as one JSON-RPC message is dropped. Events
 * without data (comments, `retry`, the 2025-11-25 priming event with an `id`
 * and empty data) pass through.
 *
 * Fail closed: an event larger than `maxEventBytes` ends the stream (with
 * `tooLarge()`'s message, when given), and an incomplete event at the end of
 * the stream is still checked.
 */

export interface SseFilterOptions {
  /** The POST's request id; undefined for a GET stream (no response allowed). */
  requestId?: string | number;
  maxEventBytes: number;
  /** Checks the response; returns a replacement message, or undefined to keep it. */
  onResponse?: (message: any) => Promise<any | undefined>;
  /** The error message sent when an event is too large. */
  tooLarge?: () => any;
  /** Called when the upstream stream has ended (not when the client cancels). */
  onEnd?: () => void;
}

const encoder = new TextEncoder();

const sseEvent = (message: unknown) =>
  encoder.encode(`event: message\ndata: ${JSON.stringify(message)}\n\n`);

/** `data` of an SSE event (fields per the HTML SSE parsing rules). */
function eventData(lines: string[]): string | undefined {
  const data: string[] = [];
  for (const line of lines) {
    if (line === 'data') data.push('');
    else if (line.startsWith('data:'))
      data.push(line.slice(line[5] === ' ' ? 6 : 5));
  }
  return data.length ? data.join('\n') : undefined;
}

const isDataLine = (line: string) =>
  line === 'data' || line.startsWith('data:');

const isMessageObject = (v: unknown): v is Record<string, any> =>
  !!v && typeof v === 'object' && !Array.isArray(v);

/** A JSON-RPC response (has `result` or `error`). */
export const isResponse = (message: any): boolean =>
  isMessageObject(message) && ('result' in message || 'error' in message);

export function isResponseTo(message: any, id: string | number): boolean {
  return isResponse(message) && message.id === id;
}

/** One JSON-RPC message, or undefined when `data` is not one. */
function parseMessage(data: string): Record<string, any> | undefined {
  let message: unknown;
  try {
    message = JSON.parse(data);
  } catch {
    return undefined;
  }
  if (!isMessageObject(message) || message.jsonrpc !== '2.0') return undefined;
  if (!isResponse(message) && typeof message.method !== 'string')
    return undefined;
  return message;
}

export function filterSseStream(
  body: ReadableStream<Uint8Array>,
  opts: SseFilterOptions
): ReadableStream<Uint8Array> {
  const decoder = new TextDecoder();
  let buffer = ''; // text not yet split into lines
  let raw = ''; // the current event, as received
  let lines: string[] = [];
  let failed = false;

  const fail = (controller: TransformStreamDefaultController<Uint8Array>) => {
    failed = true;
    if (opts.tooLarge) controller.enqueue(sseEvent(opts.tooLarge()));
    controller.terminate();
  };

  /** The event with its data replaced by `message`, other fields kept. */
  const rebuilt = (fields: string[], message: unknown) =>
    encoder.encode(
      [...fields, `data: ${JSON.stringify(message)}`, '', ''].join('\n')
    );

  const dispatch = async (
    controller: TransformStreamDefaultController<Uint8Array>
  ) => {
    const text = raw;
    const eventLines = lines;
    raw = '';
    lines = [];
    const data = eventData(eventLines);
    if (data === undefined || data === '') {
      if (text) controller.enqueue(encoder.encode(text));
      return;
    }
    const message = parseMessage(data);
    if (!message) return; // not one JSON-RPC message: dropped
    const fields = eventLines.filter((l) => !isDataLine(l));
    if (!isResponse(message)) {
      controller.enqueue(rebuilt(fields, message));
      return;
    }
    if (opts.requestId === undefined || message.id !== opts.requestId) return;
    const replacement = opts.onResponse
      ? await opts.onResponse(message)
      : undefined;
    controller.enqueue(rebuilt(fields, replacement ?? message));
  };

  const consume = async (
    controller: TransformStreamDefaultController<Uint8Array>,
    final: boolean
  ) => {
    for (;;) {
      const m = /\r\n|\r|\n/.exec(buffer);
      // A trailing \r may be the first half of \r\n: wait for more input.
      if (!m || (!final && m[0] === '\r' && m.index === buffer.length - 1)) {
        break;
      }
      const line = buffer.slice(0, m.index);
      raw += buffer.slice(0, m.index + m[0].length);
      buffer = buffer.slice(m.index + m[0].length);
      if (line === '') await dispatch(controller);
      else lines.push(line);
    }
    if (raw.length + buffer.length > opts.maxEventBytes) fail(controller);
  };

  return body.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      async transform(chunk, controller) {
        if (failed) return;
        buffer += decoder.decode(chunk, { stream: true });
        await consume(controller, false);
      },
      async flush(controller) {
        if (failed) return;
        buffer += decoder.decode();
        await consume(controller, true);
        if (failed) return;
        if (buffer) {
          raw += buffer;
          lines.push(buffer);
          buffer = '';
        }
        if (raw) await dispatch(controller);
        opts.onEnd?.();
      },
    })
  );
}
