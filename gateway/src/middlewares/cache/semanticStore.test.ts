import { normalize, SemanticStore } from './semanticStore';

const v = (...xs: number[]) => normalize(xs)!;

describe('SemanticStore', () => {
  let now = 1_000;
  const make = (
    over: Partial<ConstructorParameters<typeof SemanticStore>[0]> = {}
  ) =>
    new SemanticStore({
      maxEntries: 100,
      maxBytes: 1024 * 1024,
      maxPerPartition: 10,
      now: () => now,
      ...over,
    });

  beforeEach(() => (now = 1_000));

  it('returns the most similar entry at or above the threshold', () => {
    const s = make();
    s.insert('p', v(1, 0), 'east', now + 1000);
    s.insert('p', v(0, 1), 'north', now + 1000);
    expect(s.lookup('p', v(0.9, 0.1), 0.9)?.responseBody).toBe('east');
    expect(s.lookup('p', v(1, 1), 0.9)).toBeNull(); // cos 0.707
  });

  it('never matches across partitions', () => {
    const s = make();
    s.insert('tenant-a', v(1, 0), 'secret-a', now + 1000);
    expect(s.lookup('tenant-b', v(1, 0), 0.5)).toBeNull();
    expect(s.lookup('tenant-a', v(1, 0), 0.5)?.responseBody).toBe('secret-a');
  });

  it('expires entries after their TTL', () => {
    const s = make();
    s.insert('p', v(1, 0), 'x', now + 100);
    now += 101;
    expect(s.lookup('p', v(1, 0), 0.5)).toBeNull();
    expect(s.size).toBe(0);
    expect(s.bytes).toBe(0);
  });

  it('ignores entries of another dimension', () => {
    const s = make();
    s.insert('p', v(1, 0, 0), 'x', now + 100);
    expect(s.lookup('p', v(1, 0), 0.5)).toBeNull();
  });

  it('replaces a near-identical entry instead of duplicating it', () => {
    const s = make();
    s.insert('p', v(1, 0), 'old', now + 100);
    s.insert('p', v(1, 0), 'new', now + 100);
    expect(s.size).toBe(1);
    expect(s.lookup('p', v(1, 0), 0.5)?.responseBody).toBe('new');
  });

  it('evicts least-recently-used entries past maxEntries', () => {
    const s = make({ maxEntries: 2 });
    s.insert('a', v(1, 0), 'a', now + 100);
    s.insert('b', v(1, 0), 'b', now + 100);
    s.lookup('a', v(1, 0), 0.5); // touch a
    s.insert('c', v(1, 0), 'c', now + 100);
    expect(s.size).toBe(2);
    expect(s.lookup('b', v(1, 0), 0.5)).toBeNull();
    expect(s.lookup('a', v(1, 0), 0.5)).not.toBeNull();
  });

  it('caps entries per partition and total bytes', () => {
    const s = make({ maxPerPartition: 2 });
    s.insert('p', v(1, 0, 0), '1', now + 100);
    s.insert('p', v(0, 1, 0), '2', now + 100);
    s.insert('p', v(0, 0, 1), '3', now + 100);
    expect(s.size).toBe(2);
    expect(s.lookup('p', v(1, 0, 0), 0.9)).toBeNull();

    const small = make({ maxBytes: 2000 });
    expect(small.insert('p', v(1, 0), 'x'.repeat(5000), now + 100)).toBe(false);
    small.insert('p', v(1, 0), 'x'.repeat(400), now + 100);
    small.insert('q', v(1, 0), 'y'.repeat(400), now + 100);
    small.insert('r', v(1, 0), 'z'.repeat(400), now + 100);
    expect(small.bytes).toBeLessThanOrEqual(2000);
    expect(small.size).toBeLessThan(3);
  });

  it('normalize rejects empty, zero and non-finite vectors', () => {
    expect(normalize([])).toBeNull();
    expect(normalize([0, 0])).toBeNull();
    expect(normalize([1, NaN])).toBeNull();
  });
});
