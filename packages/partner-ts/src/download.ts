import { PartnerError } from './errors';
import type { FocusCostRow, FocusExportFormat } from './types';

/** The parts of Node's `fs` and `stream/promises` that `saveTo()` uses. */
interface NodeFs {
  createWriteStream(path: string): unknown;
  promises: { unlink(path: string): Promise<void> };
}
interface NodeStreamPromises {
  pipeline(source: AsyncIterable<Uint8Array>, destination: unknown): Promise<void>;
}

// Loaded only when saveTo() runs, so the package still loads where Node's
// modules are absent, and its typings do not need @types/node.
const loadNodeModule = (name: string): Promise<unknown> => import(name);

/**
 * A streamed download. The body is read once, by exactly one of
 * `chunks()` / `for await`, `lines()`, `rows()`, `text()`, `saveTo()` or
 * `body`; only `text()` holds the whole body in memory.
 */
export class Download implements AsyncIterable<Uint8Array> {
  /** The HTTP response, with the body still unread. */
  readonly response: Response;
  /** File name from `Content-Disposition`, if the server sent one. */
  readonly filename: string | null;
  /** `Content-Type` of the body. */
  readonly contentType: string | null;
  private used = false;

  constructor(response: Response) {
    this.response = response;
    this.contentType = response.headers.get('content-type');
    const disposition = response.headers.get('content-disposition') ?? '';
    this.filename = /filename="?([^";]+)"?/i.exec(disposition)?.[1] ?? null;
  }

  /** The raw body stream (Web Streams API). Marks the download as read. */
  get body(): ReadableStream<Uint8Array> {
    this.claim();
    return this.response.body ?? new ReadableStream<Uint8Array>({ start: (c) => c.close() });
  }

  /** Whether the body has been (or is being) read. */
  get bodyUsed(): boolean {
    return this.used;
  }

  /** The body as byte chunks, as they arrive. Stopping early cancels the download. */
  async *chunks(): AsyncGenerator<Uint8Array, void, undefined> {
    const reader = this.body.getReader();
    let done = false;
    try {
      for (;;) {
        const next = await reader.read();
        if (next.done) {
          done = true;
          return;
        }
        if (next.value && next.value.byteLength > 0) yield next.value;
      }
    } finally {
      if (!done) await reader.cancel().catch(() => undefined);
      reader.releaseLock();
    }
  }

  [Symbol.asyncIterator](): AsyncGenerator<Uint8Array, void, undefined> {
    return this.chunks();
  }

  /**
   * The body as UTF-8 lines, without line endings. Empty lines are skipped.
   * A CSV cell may itself hold a line break, so parse CSV from `chunks()`
   * with a CSV parser rather than line by line.
   */
  async *lines(): AsyncGenerator<string, void, undefined> {
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    for await (const chunk of this.chunks()) {
      buffer += decoder.decode(chunk, { stream: true });
      let nl = buffer.indexOf('\n');
      while (nl !== -1) {
        const line = buffer.slice(0, nl).replace(/\r$/, '');
        buffer = buffer.slice(nl + 1);
        if (line !== '') yield line;
        nl = buffer.indexOf('\n');
      }
    }
    buffer += decoder.decode();
    const last = buffer.replace(/\r$/, '');
    if (last !== '') yield last;
  }

  /** The whole body as a string. Buffers it in memory. */
  async text(): Promise<string> {
    const decoder = new TextDecoder('utf-8');
    let out = '';
    for await (const chunk of this.chunks()) out += decoder.decode(chunk, { stream: true });
    return out + decoder.decode();
  }

  /**
   * Node only: stream the body into a file at `path`, with backpressure and
   * without holding it in memory. Returns the bytes written. If the
   * download fails part way, the partial file is removed and the error is
   * thrown.
   */
  async saveTo(path: string): Promise<number> {
    const fs = (await loadNodeModule('fs')) as NodeFs;
    const { pipeline } = (await loadNodeModule('stream/promises')) as NodeStreamPromises;
    let bytes = 0;
    const counted = async function* (source: AsyncIterable<Uint8Array>) {
      for await (const chunk of source) {
        bytes += chunk.byteLength;
        yield chunk;
      }
    };
    try {
      await pipeline(counted(this.chunks()), fs.createWriteStream(path));
    } catch (err) {
      await fs.promises.unlink(path).catch(() => undefined);
      throw err;
    }
    return bytes;
  }

  /** Stop the download without reading the rest of the body. */
  async cancel(): Promise<void> {
    if (this.used) return;
    this.used = true;
    await this.response.body?.cancel().catch(() => undefined);
  }

  private claim(): void {
    if (this.used) throw new PartnerError('The download body has already been read');
    this.used = true;
  }
}

/** A FinOps FOCUS v1.4 cost export: `client.usage.focusExport()`. */
export class FocusExport extends Download {
  /** `csv` or `jsonl`, as requested. */
  readonly format: FocusExportFormat;
  /** FOCUS version of the columns (`X-Focus-Version`), e.g. `1.4`. */
  readonly focusVersion: string | null;
  /** Effective inclusive start after the retention clamp (`X-Export-Window-Start`). */
  readonly windowStart: string | null;
  /** Effective exclusive end (`X-Export-Window-End`). */
  readonly windowEnd: string | null;

  constructor(response: Response, format: FocusExportFormat) {
    super(response);
    this.format = format;
    this.focusVersion = response.headers.get('x-focus-version');
    this.windowStart = response.headers.get('x-export-window-start');
    this.windowEnd = response.headers.get('x-export-window-end');
  }

  /**
   * Parsed rows of a `jsonl` export, one at a time. Column names are kept
   * as FOCUS defines them (`BilledCost`, `x_EstimatedCost`). Throws for a
   * `csv` export.
   */
  async *rows(): AsyncGenerator<FocusCostRow, void, undefined> {
    if (this.format !== 'jsonl') {
      throw new PartnerError("rows() needs format: 'jsonl'; parse a CSV export from chunks() or saveTo() a file");
    }
    let n = 0;
    for await (const line of this.lines()) {
      n++;
      let row: FocusCostRow;
      try {
        row = JSON.parse(line) as FocusCostRow;
      } catch (err) {
        throw new PartnerError(`FOCUS export line ${n} is not JSON: ${line.slice(0, 200)}`, { cause: err });
      }
      yield row;
    }
  }
}
