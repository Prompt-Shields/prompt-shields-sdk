import { InMemoryLruStore } from './store';

describe('InMemoryLruStore', () => {
  it('returns null on miss', async () => {
    const s = new InMemoryLruStore({ maxEntries: 10 });
    expect(await s.get('nope')).toBeNull();
  });

  it('stores and retrieves an entry', async () => {
    const s = new InMemoryLruStore({ maxEntries: 10 });
    await s.set('k', { responseBody: 'v', maxAge: null });
    expect((await s.get('k'))?.responseBody).toBe('v');
  });

  it('evicts least-recently-used past the cap', async () => {
    const s = new InMemoryLruStore({ maxEntries: 2 });
    await s.set('a', { responseBody: 'A', maxAge: null });
    await s.set('b', { responseBody: 'B', maxAge: null });
    await s.get('a');                                   // 'a' now most-recent
    await s.set('c', { responseBody: 'C', maxAge: null }); // evicts 'b'
    expect(await s.get('b')).toBeNull();
    expect((await s.get('a'))?.responseBody).toBe('A');
    expect((await s.get('c'))?.responseBody).toBe('C');
  });

  it('lazily expires entries past maxAge', async () => {
    let now = 1000;
    const s = new InMemoryLruStore({ maxEntries: 10, now: () => now });
    await s.set('k', { responseBody: 'v', maxAge: 1500 }); // absolute expiry
    expect((await s.get('k'))?.responseBody).toBe('v');
    now = 1600;
    expect(await s.get('k')).toBeNull();
  });

  it('delete and clear work', async () => {
    const s = new InMemoryLruStore({ maxEntries: 10 });
    await s.set('k', { responseBody: 'v', maxAge: null });
    await s.delete('k');
    expect(await s.get('k')).toBeNull();
    await s.set('k2', { responseBody: 'v2', maxAge: null });
    await s.clear();
    expect(await s.get('k2')).toBeNull();
  });
});
