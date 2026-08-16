export interface CacheEntry {
  responseBody: string;
  maxAge: number | null; // absolute expiry timestamp in ms, or null = no expiry
}

export interface CacheStore {
  get(key: string): Promise<CacheEntry | null>;
  set(key: string, value: CacheEntry): Promise<void>;
  delete(key: string): Promise<void>;
  clear(): Promise<void>;
}

interface Options {
  maxEntries: number;
  now?: () => number;
}

// In-memory LRU + lazy TTL. A Map preserves insertion order; deleting and
// re-inserting a key moves it to the most-recent position.
export class InMemoryLruStore implements CacheStore {
  private map = new Map<string, CacheEntry>();
  private maxEntries: number;
  private now: () => number;

  constructor(opts: Options) {
    this.maxEntries = Math.max(1, opts.maxEntries);
    this.now = opts.now ?? Date.now;
  }

  async get(key: string): Promise<CacheEntry | null> {
    const entry = this.map.get(key);
    if (!entry) return null;
    if (entry.maxAge && entry.maxAge < this.now()) {
      this.map.delete(key);
      return null;
    }
    this.map.delete(key);
    this.map.set(key, entry);
    return entry;
  }

  async set(key: string, value: CacheEntry): Promise<void> {
    if (this.map.has(key)) this.map.delete(key);
    this.map.set(key, value);
    while (this.map.size > this.maxEntries) {
      const oldest = this.map.keys().next().value;
      if (oldest === undefined) break;
      this.map.delete(oldest);
    }
  }

  async delete(key: string): Promise<void> {
    this.map.delete(key);
  }

  async clear(): Promise<void> {
    this.map.clear();
  }
}
