/**
 * PS fork (#63, MCP Phase 1): inspect one JSON-RPC response inside an SSE
 * response stream without buffering the stream.
 *
 * Events are forwarded as they complete. Only the event that carries the
 * response to the proxied request (same `id`, with `result` or `error`) is
 * held while `onResponse` runs, and replaced when it returns a new message.
 * Notifications, server requests and comments pass through byte for byte.
 *
 * Fail closed: an event larger than `maxEventBytes` ends the stream with a
 * JSON-RPC error for the request instead of being forwarded unread, and an
 * incomplete event at the end of the stream is still inspected.
 */

export interface SseInspectOptions {
  requestId: string | number;
  maxEventBytes: number;
  /** Returns a replacement message, or undefined to forward the event unchanged. */
  onResponse: (message: any) => Promise<any | undefined>;
  /** The error message sent when an event is too large. */
  tooLarge: () => any;
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

export function isResponseTo(message: any, id: string | number): boolean {
  return (
    !!message &&
    typeof message === 'object' &&
    !Array.isArray(message) &&
    message.id === id &&
    ('result' in message || 'error' in message)
  );
}

export function inspectSseResponse(
  body: ReadableStream<Uint8Array>,
  opts: SseInspectOptions
): ReadableStream<Uint8Array> {
  const decoder = new TextDecoder();
  let buffer = ''; // text not yet split into lines
  let raw = ''; // the current event, as received
  let lines: string[] = [];
  let failed = false;

  const fail = (controller: TransformStreamDefaultController<Uint8Array>) => {
    failed = true;
    controller.enqueue(sseEvent(opts.tooLarge()));
    controller.terminate();
  };

  const dispatch = async (
    controller: TransformStreamDefaultController<Uint8Array>
  ) => {
    const text = raw;
    const data = eventData(lines);
    raw = '';
    lines = [];
    if (data !== undefined) {
      let message: any;
      try {
        message = JSON.parse(data);
      } catch {
        message = undefined;
      }
      if (isResponseTo(message, opts.requestId)) {
        const replacement = await opts.onResponse(message);
        if (replacement !== undefined) {
          controller.enqueue(sseEvent(replacement));
          return;
        }
      }
    }
    if (text) controller.enqueue(encoder.encode(text));
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
