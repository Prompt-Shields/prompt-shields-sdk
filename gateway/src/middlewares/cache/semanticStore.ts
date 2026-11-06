/**
 * PS fork (#33): bounded in-memory store for the semantic cache.
 *
 * Entries live in partitions. A partition key already pins everything that
 * must match exactly (caller scope, tenant, principal, use case, model,
 * policy version, sensitivity level, endpoint, embedder, request params and
 * conversation history; see semantic.ts), so a lookup only ever compares the
 * embedding of the final user message against entries of its own partition.
 * There is no global nearest-neighbour search, so an entry can never be
 * served across partitions however similar the text.
 *
 * Bounded three ways: total entries, total approximate bytes, and entries
 * per partition (which also bounds the linear scan a lookup does). Eviction
 * is least-recently-used; expired entries are dropped lazily.
 */

export interface SemanticStoreOptions {
  maxEntries: number;
  maxBytes: number;
  maxPerPartition: number;
  now?: () => number;
}

interface SemanticEntry {
  id: number;
  partition: string;
  vector: Float32Array;
  responseBody: string;
  expiresAt: number;
  bytes: number;
}

export interface SemanticMatch {
  responseBody: string;
  similarity: number;
}

/** Unit-length copy of `v`, or null for an empty or zero vector. */
export function normalize(v: ArrayLike<number>): Float32Array | null {
  let norm = 0;
  for (let i = 0; i < v.length; i++) {
    const x = v[i];
    if (!Number.isFinite(x)) return null;
    norm += x * x;
  }
  if (!v.length || norm === 0) return null;
  const inv = 1 / Math.sqrt(norm);
  const out = new Float32Array(v.length);
  for (let i = 0; i < v.length; i++) out[i] = v[i] * inv;
  return out;
}

/** Cosine similarity of two unit vectors of the same length. */
export function dot(a: Float32Array, b: Float32Array): number {
  let s = 0;
  for (let i = 0; i < a.length; i++) s += a[i] * b[i];
  return s;
}

// An insert this close to an existing entry replaces it instead of adding a
// near-duplicate.
const DUPLICATE_SIMILARITY = 0.9999;
// Per-entry bookkeeping, roughly: ids, keys, Map/Set slots.
const ENTRY_OVERHEAD_BYTES = 256;

export class SemanticStore {
  private entries = new Map<number, SemanticEntry>(); // LRU order
  private partitions = new Map<string, Set<number>>();
  private totalBytes = 0;
  private seq = 0;
  private readonly now: () => number;
  readonly maxEntries: number;
  readonly maxBytes: number;
  readonly maxPerPartition: number;

  constructor(opts: SemanticStoreOptions) {
    this.maxEntries = Math.max(1, opts.maxEntries);
    this.maxBytes = Math.max(1, opts.maxBytes);
    this.maxPerPartition = Math.max(1, opts.maxPerPartition);
    this.now = opts.now ?? Date.now;
  }

  get size(): number {
    return this.entries.size;
  }

  get bytes(): number {
    return this.totalBytes;
  }

  /** The most similar live entry of `partition` at or above `threshold`. */
  lookup(
    partition: string,
    vector: Float32Array,
    threshold: number
  ): SemanticMatch | null {
    const ids = this.partitions.get(partition);
    if (!ids) return null;
    const now = this.now();
    let best: SemanticEntry | undefined;
    let bestSim = -Infinity;
    for (const id of [...ids]) {
      const e = this.entries.get(id);
      if (!e) continue;
      if (e.expiresAt <= now) {
        this.remove(e);
        continue;
      }
      if (e.vector.length !== vector.length) continue;
      const sim = dot(e.vector, vector);
      if (sim > bestSim) {
        bestSim = sim;
        best = e;
      }
    }
    if (!best || bestSim < threshold) return null;
    // Touch: move to the most-recently-used end.
    this.entries.delete(best.id);
    this.entries.set(best.id, best);
    return { responseBody: best.responseBody, similarity: bestSim };
  }

  /** Stores one entry. False when it is too large for the store. */
  insert(
    partition: string,
    vector: Float32Array,
    responseBody: string,
    expiresAt: number
  ): boolean {
    const bytes =
      responseBody.length * 2 +
      vector.length * 4 +
      partition.length * 2 +
      ENTRY_OVERHEAD_BYTES;
    if (bytes > this.maxBytes || expiresAt <= this.now()) return false;

    let ids = this.partitions.get(partition);
    if (ids) {
      for (const id of [...ids]) {
        const e = this.entries.get(id);
        if (
          e &&
          e.vector.length === vector.length &&
          dot(e.vector, vector) >= DUPLICATE_SIMILARITY
        ) {
          this.remove(e);
        }
      }
    }
    ids = this.partitions.get(partition);
    if (!ids) {
      ids = new Set();
      this.partitions.set(partition, ids);
    }

    const entry: SemanticEntry = {
      id: ++this.seq,
      partition,
      vector,
      responseBody,
      expiresAt,
      bytes,
    };
    this.entries.set(entry.id, entry);
    ids.add(entry.id);
    this.totalBytes += bytes;

    // Per-partition cap: drop the partition's oldest entries.
    while (ids.size > this.maxPerPartition) {
      const oldest = this.entries.get(ids.values().next().value as number);
      if (!oldest) break;
      this.remove(oldest);
    }
    // Global caps: least-recently-used first.
    while (
      this.entries.size > this.maxEntries ||
      this.totalBytes > this.maxBytes
    ) {
      const oldest = this.entries.values().next().value;
      if (!oldest) break;
      this.remove(oldest);
    }
    return this.entries.has(entry.id);
  }

  clear(): void {
    this.entries.clear();
    this.partitions.clear();
    this.totalBytes = 0;
  }

  private remove(e: SemanticEntry): void {
    if (!this.entries.delete(e.id)) return;
    this.totalBytes -= e.bytes;
    const ids = this.partitions.get(e.partition);
    if (ids) {
      ids.delete(e.id);
      if (!ids.size) this.partitions.delete(e.partition);
    }
  }
}
