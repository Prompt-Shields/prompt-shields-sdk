import { APIConnectionError, PartnerError } from './errors';
import type { FocusCostRow, FocusExportFormat, SiemRecord } from './types';

/** The parts of Node's `fs` and `stream/promises` that `saveTo()` uses. */
interface NodeFs {
  createWriteStream(path: string, options: { flags: string }): unknown;
  promises: { unlink(path: string): Promise<void>; rename(from: string, to: string): Promise<void> };
}
interface NodeStreamPromises {
  pipeline(source: AsyncIterable<Uint8Array>, destination: unknown): Promise<void>;
}

// Loaded only when saveTo() runs, so the package still loads where Node's
// modules are absent, and its typings do not need @types/node.
const loadNodeModule = (name: string): Promise<unknown> => import(name);

/** The last path segment of a server-sent file name, or null if nothing usable is left. */
function safeFilename(raw: string | undefined): string | null {
  if (raw === undefined) return null;
  // eslint-disable-next-line no-control-regex
  const name = (raw.split(/[\\/]/).pop() ?? '').replace(/[\u0000-\u001f\u007f]/g, '').trim();
  return name === '' || name === '.' || name === '..' ? null : name;
}

/**
 * A streamed download. The body is read once, by exactly one of
 * `chunks()` / `for await`, `lines()`, `rows()`, `text()`, `saveTo()` or
 * `body`; only `text()` holds the whole body in memory.
 */
export class Download implements AsyncIterable<Uint8Array> {
  /** The HTTP response, with the body still unread. */
  readonly response: Response;
  /**
   * File name from `Content-Disposition`, if the server sent one, reduced
   * to a bare name (no directory part, no control characters, never `.` or
   * `..`), so `saveTo(download.filename)` stays in the current directory.
   */
  readonly filename: string | null;
  /** `Content-Type` of the body. */
  readonly contentType: string | null;
  private used = false;
  /**
   * Set by a subclass whose server ends every non-empty body with `\n`, so
   * a body that stops mid-line is reported as truncated.
   */
  protected endsWithNewline = false;

  constructor(response: Response) {
    this.response = response;
    this.contentType = response.headers.get('content-type');
    const disposition = response.headers.get('content-disposition') ?? '';
    this.filename = safeFilename(/filename="?([^";]+)"?/i.exec(disposition)?.[1]);
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

  /**
   * The body as byte chunks, as they arrive. Stopping early cancels the
   * download. A connection lost or aborted part way throws an
   * `APIConnectionError`.
   */
  async *chunks(): AsyncGenerator<Uint8Array, void, undefined> {
    const reader = this.body.getReader();
    let done = false;
    let last = -1;
    try {
      for (;;) {
        let next: ReadableStreamReadResult<Uint8Array>;
        try {
          next = await reader.read();
        } catch (err) {
          done = true;
          if (err instanceof PartnerError) throw err;
          const aborted = (err as Error)?.name === 'AbortError';
          throw new APIConnectionError(
            aborted ? 'Download was aborted' : `Download failed part way: ${(err as Error)?.message ?? err}`,
            { cause: err },
          );
        }
        if (next.done) {
          done = true;
          if (this.endsWithNewline && last !== -1 && last !== 0x0a) {
            throw new APIConnectionError('Download ended mid-line: the body is truncated');
          }
          return;
        }
        if (next.value && next.value.byteLength > 0) {
          last = next.value[next.value.byteLength - 1];
          yield next.value;
        }
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
   * without holding it in memory. Returns the bytes written. The body is
   * written to a new temporary file beside `path` and renamed over `path`
   * only once complete, so a failed download leaves no partial file and
   * any existing file at `path` untouched. `path` is yours; the server's
   * `filename` is never used for it.
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
    const tmp = `${path}.${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}.partial`;
    try {
      // `wx`: never write through an existing file or symlink.
      await pipeline(counted(this.chunks()), fs.createWriteStream(tmp, { flags: 'wx' }));
      await fs.promises.rename(tmp, path);
    } catch (err) {
      await fs.promises.unlink(tmp).catch(() => undefined);
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
    this.endsWithNewline = true;
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

/**
 * One batch of the SIEM pull: `client.siem.calls()` or `client.siem.audit()`.
 * NDJSON, one Sentinel-schema record per line.
 */
export class SiemBatch extends Download {
  /**
   * `X-PS-Cursor`: the position after the last line. Pass it as `cursor`
   * on the next call. `null` when the server sent none (no rows yet and no
   * cursor given); keep your previous cursor then.
   */
  readonly cursor: string | null;
  /** `X-PS-Has-More`: true when the batch was full, so call again now. */
  readonly hasMore: boolean;

  constructor(response: Response) {
    super(response);
    this.endsWithNewline = true;
    this.cursor = response.headers.get('x-ps-cursor');
    this.hasMore = (response.headers.get('x-ps-has-more') ?? '').toLowerCase() === 'true';
  }

  /** Parsed records, one at a time. Column names are kept as sent (`TimeGenerated`, `PsServedModel`). */
  async *records(): AsyncGenerator<SiemRecord, void, undefined> {
    let n = 0;
    for await (const line of this.lines()) {
      n++;
      let record: SiemRecord;
      try {
        record = JSON.parse(line) as SiemRecord;
      } catch (err) {
        throw new PartnerError(`SIEM record line ${n} is not JSON: ${line.slice(0, 200)}`, { cause: err });
      }
      yield record;
    }
  }
}

/**
 * Follows the SIEM pull cursor: `client.siem.callsFeed()` or
 * `client.siem.auditFeed()`. `for await` yields records across batches
 * until a batch is not full; `cursor` then holds the position to start the
 * next run from.
 *
 * `cursor` moves only after a whole batch has been yielded, so a loop that
 * stops part way through a batch (or throws) leaves it at the start of
 * that batch: save it and that batch is read again next time (at least
 * once delivery). Each iteration continues from `cursor`.
 */
export class SiemFeed implements AsyncIterable<SiemRecord> {
  /** Where the next batch starts: the last fully read batch's cursor, or the starting cursor. */
  cursor: string | null;

  constructor(
    private readonly fetchBatch: (cursor: string | null) => Promise<SiemBatch>,
    cursor: string | null = null,
  ) {
    this.cursor = cursor;
  }

  /** Whole batches. Read each batch's body before asking for the next. */
  async *batches(): AsyncGenerator<SiemBatch, void, undefined> {
    const seen = new Set<string>();
    for (;;) {
      const batch = await this.fetchBatch(this.cursor);
      try {
        yield batch;
      } finally {
        if (!batch.bodyUsed) await batch.cancel();
      }
      if (batch.cursor !== null) this.cursor = batch.cursor;
      if (!batch.hasMore) return;
      if (this.cursor === null || seen.has(this.cursor)) {
        throw new PartnerError(`SIEM cursor did not advance (${this.cursor}); stopping to avoid an infinite loop`);
      }
      seen.add(this.cursor);
    }
  }

  async *[Symbol.asyncIterator](): AsyncGenerator<SiemRecord, void, undefined> {
    for await (const batch of this.batches()) {
      yield* batch.records();
    }
  }

  /** Every record until the feed is caught up. Buffers them in memory. */
  async toArray(): Promise<SiemRecord[]> {
    const out: SiemRecord[] = [];
    for await (const record of this) out.push(record);
    return out;
  }
}
