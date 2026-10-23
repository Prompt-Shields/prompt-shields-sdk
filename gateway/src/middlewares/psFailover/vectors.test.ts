/**
 * Shared failover vectors (schemas/failover/vectors.json, #21). The Python SDK
 * runs the same file in packages/sdk/tests/test_failover_vectors.py.
 */
import fs from 'fs';
import path from 'path';
import {
  BreakerRegistry,
  Candidate,
  CircuitBreaker,
  classifyFailure,
  FailureInfo,
  NoCompliantCandidate,
  runFailover,
  UpstreamError,
} from './index';

const VECTORS = JSON.parse(
  fs.readFileSync(
    path.resolve(__dirname, '../../../../schemas/failover/vectors.json'),
    'utf8'
  )
);

const infoOf = (spec: any): FailureInfo => ({
  status: spec.status,
  kind: spec.kind,
  code: spec.code,
  message: spec.message,
  retryAfter: spec.retry_after,
});

describe('failover vectors: classify', () => {
  test.each(VECTORS.classify as any[])('$id', (v: any) => {
    const got = classifyFailure(infoOf(v.input));
    expect([got.errorClass, got.action, got.tripsBreaker]).toEqual([
      v.expected.error_class,
      v.expected.action,
      v.expected.trips_breaker,
    ]);
  });
});

describe('failover vectors: breaker', () => {
  test.each(VECTORS.breaker as any[])('$id', (v: any) => {
    let now = 0;
    const seen: string[] = [];
    const breaker = new CircuitBreaker(
      {
        failureThreshold: v.config.failure_threshold,
        openSeconds: v.config.open_seconds,
        halfOpenMaxProbes: v.config.half_open_max_probes ?? 1,
      },
      () => now,
      (_from, to) => seen.push(to)
    );
    for (const step of v.steps) {
      now = step.t;
      if (step.op === 'allow') expect(breaker.allow()).toBe(step.allowed);
      else if (step.op === 'failure') breaker.recordFailure();
      else if (step.op === 'success') breaker.recordSuccess();
      else if (step.op === 'release') breaker.release();
      else throw new Error(`unknown op ${step.op}`);
      expect(breaker.currentState()).toBe(step.state);
    }
    if (v.transitions) expect(seen).toEqual(v.transitions);
  });
});

describe('failover vectors: engine', () => {
  test.each(VECTORS.failover as any[])('$id', async (v: any) => {
    const breakers = new BreakerRegistry({ clock: () => 0 });
    for (const p of v.open || []) breakers.forceOpen(p);
    const outcomes: Record<string, any[]> = {};
    for (const [k, list] of Object.entries(v.outcomes))
      outcomes[k] = [...(list as any[])];
    const waits: number[] = [];
    const called: string[] = [];
    const deny = new Set<string>(v.deny || []);
    const candidates: Candidate[] = v.candidates.map((c: any) => ({
      provider: c.provider,
      model: c.model,
      contextWindow: c.context_window,
    }));
    const call = async (c: Candidate) => {
      called.push(c.provider);
      const outcome = outcomes[c.provider].shift();
      if (outcome === 'ok') return c.provider;
      throw new UpstreamError(infoOf(outcome));
    };
    const opts = {
      breakers,
      maxWait: v.config?.max_wait ?? 10,
      sleep: async (s: number) => {
        waits.push(s);
      },
      allow: deny.size ? (c: Candidate) => !deny.has(c.provider) : undefined,
    };
    const exp = v.expected;
    let attempts: any[];
    let chain: string[];
    let skipped: any[];
    if (exp.error === 'no_compliant_candidate') {
      const err = await runFailover(candidates, call, opts).then(
        () => undefined,
        (e) => e
      );
      expect(err).toBeInstanceOf(NoCompliantCandidate);
      ({ attempts, fallbackChain: chain, skipped } = err);
    } else if (exp.error === 'raise') {
      const err = await runFailover(candidates, call, opts).then(
        () => undefined,
        (e) => e
      );
      expect(err).toBeInstanceOf(UpstreamError);
      attempts = err.psAttempts;
      chain = err.psFallbackChain;
      skipped = [];
      expect(attempts[attempts.length - 1].error_class).toBe(exp.error_class);
    } else {
      const result = await runFailover(candidates, call, opts);
      expect(result.response).toBe(exp.served);
      expect(result.candidate.provider).toBe(exp.served);
      ({ attempts, fallbackChain: chain, skipped } = result);
    }
    expect(attempts.map((a) => a.provider)).toEqual(exp.attempts);
    expect(called).toEqual(exp.attempts);
    expect(chain).toEqual(exp.fallback_chain);
    expect(skipped.map((s) => [s.provider, s.reason])).toEqual(
      exp.skipped || []
    );
    if (exp.waits) expect(waits).toEqual(exp.waits);
  });
});
