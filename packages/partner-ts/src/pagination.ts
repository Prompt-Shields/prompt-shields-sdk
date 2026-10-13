import { PartnerError } from './errors';
import type { Page } from './types';

/**
 * Lazily walks a cursor-paginated list.
 *
 * - `for await (const item of iter)` streams items, fetching pages on demand.
 * - `await iter` or `await iter.toArray()` collects every item.
 * - `iter.pages()` yields whole pages.
 * - `iter.lastPage` holds the most recent page fetched, e.g. to read
 *   `meta.syncToken` after walking the changes feed.
 *
 * Each iteration starts from the first page again.
 */
export class PageIterator<T> implements AsyncIterable<T>, PromiseLike<T[]> {
  /** The most recent page fetched by any iteration, or `undefined` before the first fetch. */
  lastPage: Page<T> | undefined;

  constructor(
    private readonly fetchPage: (after: string | undefined) => Promise<Page<T>>,
    private readonly startAfter?: string,
  ) {}

  async *pages(): AsyncGenerator<Page<T>, void, undefined> {
    let after = this.startAfter;
    const seen = new Set<string>();
    for (;;) {
      const page = await this.fetchPage(after);
      this.lastPage = page;
      yield page;
      const next = page.meta?.nextCursor;
      if (!page.meta?.hasMore || !next) return;
      if (seen.has(next)) {
        throw new PartnerError(`Pagination cursor repeated (${next}); stopping to avoid an infinite loop`);
      }
      seen.add(next);
      after = next;
    }
  }

  async *[Symbol.asyncIterator](): AsyncGenerator<T, void, undefined> {
    for await (const page of this.pages()) {
      for (const item of page.data) yield item;
    }
  }

  async toArray(): Promise<T[]> {
    const out: T[] = [];
    for await (const item of this) out.push(item);
    return out;
  }

  then<R1 = T[], R2 = never>(
    onfulfilled?: ((value: T[]) => R1 | PromiseLike<R1>) | null,
    onrejected?: ((reason: unknown) => R2 | PromiseLike<R2>) | null,
  ): Promise<R1 | R2> {
    return this.toArray().then(onfulfilled, onrejected);
  }
}
