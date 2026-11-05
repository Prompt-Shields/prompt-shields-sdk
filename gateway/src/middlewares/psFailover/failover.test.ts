import { defaultRegistry } from '../psMetrics/registry';
import {
  BreakerRegistry,
  Candidate,
  failureFromError,
  NoCompliantCandidate,
  retryAfterOf,
  runFailover,
  UpstreamError,
} from './index';

const A: Candidate = { provider: 'berget', model: 'llama-70b' };
const B: Candidate = { provider: 'mistral-eu', model: 'mistral-large' };

describe('psFailover helpers', () => {
  test('reads Retry-After in seconds, milliseconds or as a date', () => {
    expect(retryAfterOf(new Headers({ 'retry-after': '3' }))).toBe(3);
    expect(retryAfterOf(new Headers({ 'retry-after-ms': '250' }))).toBe(0.25);
    expect(
      retryAfterOf(new Headers({ 'retry-after': 'soon' }))
    ).toBeUndefined();
    expect(
      retryAfterOf(
        new Headers({ 'retry-after': 'Wed, 21 Oct 2015 07:28:00 GMT' })
      )
    ).toBe(0);
    expect(retryAfterOf(new Headers())).toBeUndefined();
    expect(retryAfterOf(undefined)).toBeUndefined();
  });

  test('maps transport errors', () => {
    const abort = new Error('aborted');
    abort.name = 'AbortError';
    expect(failureFromError(abort)).toEqual({ kind: 'timeout' });
    expect(failureFromError(new TypeError('fetch failed'))).toEqual({
      kind: 'network',
    });
    expect(failureFromError(new Error('bug'))).toEqual({});
    expect(failureFromError(new UpstreamError({ status: 503 }))).toEqual({
      status: 503,
    });
  });
});

describe('runFailover', () => {
  test('a bug in the call is rethrown, not failed over', async () => {
    const calls: string[] = [];
    await expect(
      runFailover(
        [A, B],
        async (c) => {
          calls.push(c.provider);
          throw new Error('bug');
        },
        { breakers: new BreakerRegistry() }
      )
    ).rejects.toThrow('bug');
    expect(calls).toEqual(['berget']);
  });

  test('exhaustion fails closed with the last error and classes only', async () => {
    const last = new UpstreamError({ status: 502, message: 'secret detail' });
    const err = await runFailover(
      [A, B],
      async () => {
        throw last;
      },
      { breakers: new BreakerRegistry() }
    ).catch((e) => e);
    expect(err).toBeInstanceOf(NoCompliantCandidate);
    expect(err.lastError).toBe(last);
    expect(err.message).toContain('failing closed');
    expect(err.message).toContain('berget/llama-70b: server_error');
    expect(err.message).not.toContain('secret detail');
  });

  test('the breaker opens, skips the provider, then probes and closes', async () => {
    let now = 0;
    const seen: string[] = [];
    const breakers = new BreakerRegistry({
      config: { failureThreshold: 2, openSeconds: 10 },
      clock: () => now,
      onTransition: (p, from, to) => seen.push(`${p}:${from}->${to}`),
    });
    const calls: string[] = [];
    const flaky = async (c: Candidate) => {
      calls.push(c.provider);
      if (c === A) throw new UpstreamError({ status: 500 });
      return c.provider;
    };
    await runFailover([A, B], flaky, { breakers });
    await runFailover([A, B], flaky, { breakers });
    expect(breakers.state('berget')).toBe('open');
    calls.length = 0;
    const skipped = await runFailover([A, B], flaky, { breakers });
    expect(calls).toEqual(['mistral-eu']);
    expect(skipped.skipped).toEqual([
      { provider: 'berget', model: 'llama-70b', reason: 'circuit_open' },
    ]);
    now = 10;
    const probe = await runFailover([A, B], async (c) => c.provider, {
      breakers,
    });
    expect(probe.candidate).toBe(A);
    expect(breakers.state('berget')).toBe('closed');
    expect(seen).toEqual([
      'berget:closed->open',
      'berget:open->half_open',
      'berget:half_open->closed',
    ]);
    expect(breakers.metrics()).toMatchObject({
      opened: 1,
      half_opened: 1,
      closed: 1,
    });
    expect(defaultRegistry.render()).toContain(
      'ps_gateway_circuit_breaker_transitions_total{provider="berget",to="open"}'
    );
  });

  test('client errors and content filters do not trip the breaker', async () => {
    const breakers = new BreakerRegistry({ config: { failureThreshold: 1 } });
    for (const info of [
      { status: 401 },
      { status: 400, code: 'content_filter' },
    ]) {
      await expect(
        runFailover(
          [A, B],
          async () => {
            throw new UpstreamError(info);
          },
          { breakers }
        )
      ).rejects.toBeInstanceOf(UpstreamError);
    }
    expect(breakers.state('berget')).toBe('closed');
  });

  test('the registry is bounded (LRU) and evicted providers start closed', () => {
    const breakers = new BreakerRegistry({
      config: { failureThreshold: 1 },
      maxEntries: 2,
    });
    breakers.recordFailure('a');
    breakers.recordFailure('b');
    breakers.allow('a'); // a is now the most recently used
    breakers.recordFailure('c');
    expect(Object.keys(breakers.snapshot()).sort()).toEqual(['a', 'c']);
    expect(breakers.metrics()).toMatchObject({ tracked: 2, evicted: 1 });
    expect(breakers.state('b')).toBe('closed');
  });

  test('a throwing onTransition hook never breaks a call', () => {
    const breakers = new BreakerRegistry({
      config: { failureThreshold: 1 },
      onTransition: () => {
        throw new Error('metrics down');
      },
    });
    breakers.recordFailure('a');
    expect(breakers.state('a')).toBe('open');
  });

  test('rejects invalid breaker config', () => {
    expect(() =>
      new BreakerRegistry({ config: { failureThreshold: 0 } }).allow('a')
    ).toThrow(RangeError);
    expect(() => new BreakerRegistry({ maxEntries: 0 })).toThrow(RangeError);
  });
});
