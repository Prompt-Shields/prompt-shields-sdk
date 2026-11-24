/**
 * PS fork (#349): output guardrails on a streamed (SSE) response.
 *
 * A finding can be split across SSE events (a card number over three
 * deltas), so events are held back, not forwarded as they arrive. Each
 * piece of model text in an event (chat `delta.content`, Anthropic
 * `delta.text`, Responses `delta`, tool-call arguments, ...) belongs to a
 * slot: its path in the event plus the event's `index` / `output_index` /
 * `content_index`, so all deltas of one choice or content block share one.
 * An event is forwarded once every slot it adds text to has at least
 * `windowChars` more characters after it, or the stream ends. Findings
 * are looked for in each slot's text (the held-back text plus the last
 * `windowChars` characters already forwarded, for context), and redacted
 * in the events that carry them: the placeholder goes where the finding
 * starts, the rest of it is removed from the events that carry the rest.
 *
 * Bounds:
 *   - latency: text reaches the client about `windowChars` characters
 *     (default 256, about 60 tokens) after the provider sent it; the rest
 *     at the end of the stream. Events without model text (role, usage,
 *     stop) wait behind the text events before them, in order;
 *   - memory: about `windowChars` x 2 per slot, plus the held-back events;
 *     past `maxPendingChars` held back the oldest events are forwarded
 *     anyway (scanned, but a finding that continues past them can only be
 *     redacted from where it continues);
 *   - a finding longer than `windowChars` (a private key block, a very
 *     long e-mail local part) that started in text already forwarded is
 *     redacted only from where the window still holds it.
 * A `deny` finding ends the stream: nothing of the held-back text is
 * forwarded, an SSE error event (`ps_guardrail_denied`) is sent and the
 * upstream is cancelled. Text forwarded before it stays forwarded.
 */
import {
  evaluate,
  Hit,
  mapOutputText,
  placeholder,
  redactSpans,
  StageRules,
  Tally,
} from './scan';

export interface StreamGuardOptions {
  stage: StageRules;
  windowChars: number;
  tally: Tally;
  /** Called once when the stream has ended, whatever the reason. */
  onEnd?: () => void;
  /** Held-back characters (all slots) before the oldest events are forced out. */
  maxPendingChars?: number;
}

interface Slot {
  /** Retained text: up to windowChars forwarded characters, then the held-back ones. */
  text: string;
  /** Absolute offset of text[0] in the slot. */
  base: number;
  /** Absolute length of the slot's text so far. */
  total: number;
  /** Absolute offset up to which text has been forwarded. */
  emitted: number;
  /** Absolute offset up to which a placed redaction extends. */
  covered: number;
  /** The last scan's hits, in absolute offsets. */
  hits: Hit[];
  contextSeen: Set<string>;
}

interface Seg {
  slot: Slot;
  start: number;
  end: number;
}

interface QueuedEvent {
  lines: string[];
  obj?: any;
  segs: Seg[];
}

const enc = new TextEncoder();

/** Visits an SSE event's model text: a top-level text `delta`, then mapOutputText. */
function visitEventText(
  obj: any,
  visit: (path: string, text: string) => string
): void {
  if (
    typeof obj.delta === 'string' &&
    !(typeof obj.type === 'string' && obj.type.includes('audio'))
  ) {
    const next = visit('delta', obj.delta);
    if (next !== obj.delta) obj.delta = next;
  }
  mapOutputText(obj, visit);
}

const slotKey = (obj: any, path: string) =>
  `${obj.index ?? ''}|${obj.output_index ?? ''}|${obj.content_index ?? ''}|${path}`;

/** The SSE error event that ends a denied stream. */
export function errorEventLines(named: boolean, message: string): string[] {
  const data = JSON.stringify({
    type: 'error',
    error: {
      type: 'guardrail_violation',
      code: 'ps_guardrail_denied',
      message,
    },
  });
  return named ? ['event: error', `data: ${data}`] : [`data: ${data}`];
}

export function guardEventStream(
  source: ReadableStream<Uint8Array>,
  options: StreamGuardOptions
): ReadableStream<Uint8Array> {
  const { stage, tally } = options;
  const W = options.windowChars;
  const maxPending = options.maxPendingChars ?? Math.max(64 * W, 256 * 1024);
  const slots = new Map<string, Slot>();
  const queue: QueuedEvent[] = [];
  const decoder = new TextDecoder();
  let partial = '';
  let lines: string[] = [];
  let pending = 0;
  let named = false;
  let done = false;
  let ended = false;
  let forwarded = 0;
  const reader = source.getReader();

  const end = () => {
    if (ended) return;
    ended = true;
    options.onEnd?.();
  };

  const rescan = (slot: Slot) => {
    slot.hits = evaluate(slot.text, stage).map((h) =>
      h.start === undefined
        ? h
        : { ...h, start: h.start + slot.base, end: h.end! + slot.base }
    );
  };

  /** A deny hit not yet forwarded, if any. */
  const denyIn = (slot: Slot): Hit | undefined =>
    slot.hits.find(
      (h) =>
        h.action === 'deny' && (h.start === undefined || h.end! > slot.emitted)
    );

  const replacement = (seg: Seg): string => {
    const { slot, start: a, end: b } = seg;
    const at = (from: number, to: number) =>
      slot.text.slice(from - slot.base, to - slot.base);
    // Count each finding once, when the text where it starts is forwarded.
    for (const h of slot.hits) {
      if (h.start === undefined) {
        if (!slot.contextSeen.has(h.category)) {
          slot.contextSeen.add(h.category);
          tally.add(stage.stage, h);
        }
      } else if (h.start >= a && h.start < b) tally.add(stage.stage, h);
    }
    // Text inside a redaction already placed is dropped.
    let p = Math.max(a, slot.covered);
    if (p >= b) return '';
    let out = '';
    for (const s of redactSpans(slot.hits)) {
      if (s.end <= p) continue;
      if (s.start >= b) break;
      if (s.start > p) out += at(p, s.start);
      out += placeholder(s.category);
      slot.covered = Math.max(slot.covered, s.end);
      p = Math.min(s.end, b);
      if (p >= b) return out;
    }
    return out + at(p, b);
  };

  const serialize = (ev: QueuedEvent): Uint8Array => {
    let out = ev.lines;
    if (ev.obj && ev.segs.length) {
      const replaced = ev.segs.map(replacement);
      let i = 0;
      let changed = false;
      visitEventText(ev.obj, (_p, t) => {
        const next = replaced[i++];
        if (next !== t) changed = true;
        return next;
      });
      for (const seg of ev.segs) {
        const s = seg.slot;
        pending -= seg.end - seg.start;
        s.emitted = Math.max(s.emitted, seg.end);
        const keepFrom = Math.max(s.base, s.emitted - W);
        if (keepFrom > s.base) {
          s.text = s.text.slice(keepFrom - s.base);
          s.base = keepFrom;
          s.hits = s.hits.filter(
            (h) => h.start === undefined || h.end! > keepFrom
          );
        }
      }
      if (changed) {
        const data = `data: ${JSON.stringify(ev.obj)}`;
        out = [];
        let placed = false;
        for (const line of ev.lines) {
          if (line.startsWith('data:')) {
            if (!placed) out.push(data);
            placed = true;
          } else out.push(line);
        }
      }
    }
    return enc.encode(out.join('\n') + '\n\n');
  };

  /** Parses one event's lines and queues it; returns a deny hit, if any. */
  const accept = (evLines: string[]): Hit | undefined => {
    const data = evLines
      .filter((l) => l.startsWith('data:'))
      .map((l) => l.slice(5).replace(/^ /, ''))
      .join('\n');
    if (evLines.some((l) => l.startsWith('event:'))) named = true;
    const ev: QueuedEvent = { lines: evLines, segs: [] };
    let obj: any;
    if (data && data[0] === '{') {
      try {
        obj = JSON.parse(data);
      } catch {
        obj = undefined;
      }
    }
    const touched = new Set<Slot>();
    if (obj && typeof obj === 'object') {
      ev.obj = obj;
      visitEventText(obj, (path, text) => {
        const key = slotKey(obj, path);
        let slot = slots.get(key);
        if (!slot) {
          slot = {
            text: '',
            base: 0,
            total: 0,
            emitted: 0,
            covered: 0,
            hits: [],
            contextSeen: new Set(),
          };
          slots.set(key, slot);
        }
        ev.segs.push({
          slot,
          start: slot.total,
          end: slot.total + text.length,
        });
        slot.text += text;
        slot.total += text.length;
        pending += text.length;
        if (text) touched.add(slot);
        return text;
      });
    }
    queue.push(ev);
    for (const slot of touched) {
      rescan(slot);
      const deny = denyIn(slot);
      if (deny) return deny;
    }
    return undefined;
  };

  const ready = (ev: QueuedEvent) =>
    ev.segs.every((s) => s.end <= s.slot.total - W);

  /** Forwards the queued events that may go now (all of them when flushing). */
  const drain = (
    controller: ReadableStreamDefaultController<Uint8Array>,
    flush: boolean
  ) => {
    while (queue.length && (flush || ready(queue[0]) || pending > maxPending)) {
      controller.enqueue(serialize(queue.shift()!));
      forwarded++;
    }
  };

  const deny = (
    controller: ReadableStreamDefaultController<Uint8Array>,
    hit: Hit
  ) => {
    tally.add(stage.stage, hit);
    queue.length = 0;
    done = true;
    controller.enqueue(
      enc.encode(
        errorEventLines(
          named,
          'The response was blocked by a guardrail policy'
        ).join('\n') + '\n\n'
      )
    );
    controller.close();
    end();
    reader.cancel().catch(() => {});
  };

  /** Splits `text` into lines and events; false once a deny ended the stream. */
  const takeLines = (
    text: string,
    controller: ReadableStreamDefaultController<Uint8Array>
  ): boolean => {
    partial += text;
    let nl: number;
    while ((nl = partial.indexOf('\n')) >= 0) {
      let line = partial.slice(0, nl);
      partial = partial.slice(nl + 1);
      if (line.endsWith('\r')) line = line.slice(0, -1);
      if (line === '') {
        if (lines.length) {
          const hit = accept(lines);
          lines = [];
          if (hit) {
            deny(controller, hit);
            return false;
          }
        }
      } else lines.push(line);
    }
    return true;
  };

  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      if (done) return;
      try {
        for (;;) {
          const { value, done: upstreamDone } = await reader.read();
          if (upstreamDone) {
            // Terminate a last line and event the upstream left open.
            const tail = decoder.decode() + (partial ? '\n' : '') + '\n';
            if (!takeLines(tail, controller)) return;
            drain(controller, true);
            done = true;
            controller.close();
            end();
            return;
          }
          if (!takeLines(decoder.decode(value, { stream: true }), controller))
            return;
          const before = forwarded;
          drain(controller, false);
          // Read on until something was forwarded, so pull() makes progress.
          if (forwarded !== before) return;
        }
      } catch (err) {
        done = true;
        try {
          drain(controller, true);
        } catch {
          /* already failed */
        }
        controller.error(err);
        end();
      }
    },
    cancel(reason) {
      done = true;
      end();
      return reader.cancel(reason);
    },
  });
}
