/**
 * Failover engine and per-provider circuit breaker (PRD 0001 §7.5, #21).
 *
 * The engine tries an ordered list of candidates and moves down the list only
 * when the failure says another provider may succeed:
 *
 *   429                        wait Retry-After when it is at most maxWait
 *                              (rateLimitRetries times, default once), then
 *                              the next candidate
 *   5xx, timeout, connection   the next candidate; counts against the
 *                              provider's circuit breaker
 *   context length exceeded    the next candidate whose contextWindow is known
 *                              and larger than the failed one's
 *   400 / 401 / 403 / 4xx      throw; no fallback
 *   content-filter refusal     throw; a laxer model would evade the policy (P7)
 *   no candidate left          NoCompliantCandidate: fail closed
 *
 * The engine never widens the constraint set: it only calls the candidates it
 * is given, in order, and every rule above can only remove candidates. Callers
 * pass candidates that already passed the data policy (the router's
 * sensitivity and jurisdiction stages). `allow` re-checks each candidate
 * before it is called, as defence in depth; one it rejects is skipped as
 * `non_compliant` and never called.
 *
 * Streams: the engine resolves as soon as `call` resolves, i.e. when the
 * upstream answered with its headers. A streamed body is read after that,
 * outside the engine, so a stream that started emitting is never retried.
 *
 * Breaker state is per process (per isolate on edge runtimes) and is not
 * shared between replicas. The registry keeps at most `maxEntries` providers
 * (least recently used evicted; an evicted breaker forgets it was open).
 * Transitions are counted in `ps_gateway_circuit_breaker_transitions_total`.
 *
 * The Python SDK has the same engine (packages/sdk/prompt_shields/failover.py);
 * both run schemas/failover/vectors.json.
 */
import { Counter, defaultRegistry } from '../psMetrics/registry';

export type BreakerState = 'closed' | 'open' | 'half_open';
export type FailoverAction =
  | 'retry_after'
  | 'next'
  | 'next_larger_context'
  | 'raise';
export type SkipReason = 'circuit_open' | 'context_window' | 'non_compliant';

// --- classification ----------------------------------------------------------

/** A provider failure, reduced to what classification needs. `code` and
 * `message` are read here and never stored. */
export interface FailureInfo {
  status?: number;
  /** `timeout`, `network` or `stream_interrupted` when there is no status. */
  kind?: string;
  code?: string;
  message?: string;
  /** Seconds. */
  retryAfter?: number;
}

export interface Classification {
  errorClass: string;
  action: FailoverAction;
  tripsBreaker: boolean;
}

const CONTENT_FILTER_CODES = new Set([
  'content_filter',
  'content_policy_violation',
]);
const CONTENT_FILTER_RE = /content[ _-]?(management )?(filter|policy)/i;
const CONTEXT_RE =
  /context[ _-]?(length|window)|prompt is too long|too many (input )?tokens/i;

const cls = (
  errorClass: string,
  action: FailoverAction,
  tripsBreaker: boolean
): Classification => ({ errorClass, action, tripsBreaker });

/** Maps a failure to its error class and the §7.5 action. */
export function classifyFailure(info: FailureInfo): Classification {
  if (info.kind === 'stream_interrupted')
    return cls('stream_interrupted', 'raise', true);
  if (info.kind === 'timeout') return cls('timeout', 'next', true);
  if (info.kind === 'network') return cls('network_error', 'next', true);
  const status = info.status;
  if (typeof status !== 'number') return cls('unknown_error', 'raise', false);
  if (status < 500) {
    const code = (info.code || '').toLowerCase();
    const message = info.message || '';
    if (CONTENT_FILTER_CODES.has(code) || CONTENT_FILTER_RE.test(message))
      return cls('content_filter', 'raise', false);
    if (
      status === 413 ||
      (status === 400 &&
        (code === 'context_length_exceeded' || CONTEXT_RE.test(message)))
    )
      return cls('context_length_exceeded', 'next_larger_context', false);
  }
  if (status === 408 || status === 504) return cls('timeout', 'next', true);
  if (status === 429) return cls('rate_limited', 'retry_after', true);
  if (status >= 500) return cls('server_error', 'next', true);
  if (status >= 400) return cls('client_error', 'raise', false);
  return cls('unknown_error', 'raise', false);
}

/** Seconds from `retry-after-ms` / `retry-after` (seconds or HTTP date). */
export function retryAfterOf(
  headers: { get(name: string): string | null } | undefined
): number | undefined {
  if (!headers) return undefined;
  const ms = headers.get('retry-after-ms');
  if (ms != null && ms.trim() !== '' && !Number.isNaN(Number(ms)))
    return Math.max(0, Number(ms) / 1000);
  const value = headers.get('retry-after');
  if (value == null || value.trim() === '') return undefined;
  if (!Number.isNaN(Number(value))) return Math.max(0, Number(value));
  const when = Date.parse(value);
  return Number.isNaN(when)
    ? undefined
    : Math.max(0, (when - Date.now()) / 1000);
}

/** An upstream answer that failed, as an error the engine can classify. */
export class UpstreamError extends Error {
  constructor(readonly info: FailureInfo) {
    super(`upstream failure (${info.status ?? info.kind ?? 'unknown'})`);
    this.name = 'UpstreamError';
  }
}

/** Reads a thrown error: an UpstreamError, an AbortError/TimeoutError, or a
 * fetch TypeError (connection failure). Anything else is `unknown_error`. */
export function failureFromError(err: unknown): FailureInfo {
  if (err instanceof UpstreamError) return err.info;
  const name = (err as any)?.name;
  if (name === 'AbortError' || name === 'TimeoutError')
    return { kind: 'timeout' };
  if (err instanceof TypeError && /fetch|network|socket/i.test(err.message))
    return { kind: 'network' };
  return {};
}

// --- circuit breaker ---------------------------------------------------------

export interface BreakerConfig {
  failureThreshold: number;
  /** Seconds. */
  openSeconds: number;
  halfOpenMaxProbes: number;
}

export const DEFAULT_BREAKER_CONFIG: BreakerConfig = {
  failureThreshold: 5,
  openSeconds: 30,
  halfOpenMaxProbes: 1,
};

/** Clock in seconds. */
export type Clock = () => number;
const monotonic: Clock = () => performance.now() / 1000;

export class CircuitBreaker {
  state: BreakerState = 'closed';
  failures = 0;
  openedAt = 0;
  probes = 0;

  constructor(
    readonly config: BreakerConfig,
    private readonly clock: Clock,
    private readonly onTransition?: (
      from: BreakerState,
      to: BreakerState
    ) => void
  ) {
    if (!(config.failureThreshold >= 1))
      throw new RangeError('failureThreshold must be at least 1');
    if (!(config.openSeconds >= 0))
      throw new RangeError('openSeconds must not be negative');
    if (!(config.halfOpenMaxProbes >= 1))
      throw new RangeError('halfOpenMaxProbes must be at least 1');
  }

  private move(to: BreakerState): void {
    if (to === this.state) return;
    const from = this.state;
    this.state = to;
    if (to === 'open') this.openedAt = this.clock();
    this.probes = 0;
    if (to === 'closed') this.failures = 0;
    this.onTransition?.(from, to);
  }

  /** The state, moving open to half_open once the wait is over. */
  currentState(): BreakerState {
    if (
      this.state === 'open' &&
      this.clock() - this.openedAt >= this.config.openSeconds
    )
      this.move('half_open');
    return this.state;
  }

  /** True when a call may go out now (takes a probe slot when half-open). */
  allow(): boolean {
    const state = this.currentState();
    if (state === 'closed') return true;
    if (state === 'half_open' && this.probes < this.config.halfOpenMaxProbes) {
      this.probes += 1;
      return true;
    }
    return false;
  }

  recordSuccess(): void {
    if (this.state === 'half_open') this.move('closed');
    else if (this.state === 'closed') this.failures = 0;
  }

  recordFailure(): void {
    if (this.state === 'half_open') this.move('open');
    else if (this.state === 'closed') {
      this.failures += 1;
      if (this.failures >= this.config.failureThreshold) this.move('open');
    }
  }

  /** Ends a call that says nothing about the provider's health. */
  release(): void {
    if (this.state === 'half_open' && this.probes > 0) this.probes -= 1;
  }

  /** Opens the breaker now, e.g. to drain a provider by hand. */
  forceOpen(): void {
    if (this.state === 'open') this.openedAt = this.clock();
    else this.move('open');
  }
}

export const breakerTransitionsTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_circuit_breaker_transitions_total',
    'Per-provider circuit breaker state changes, by provider and new state (open, half_open, closed).',
    ['provider', 'to']
  )
);

export interface BreakerRegistryOptions {
  config?: Partial<BreakerConfig>;
  maxEntries?: number;
  clock?: Clock;
  onTransition?: (
    provider: string,
    from: BreakerState,
    to: BreakerState
  ) => void;
}

/** Per-provider breakers for this process, bounded to `maxEntries`. */
export class BreakerRegistry {
  readonly config: BreakerConfig;
  readonly maxEntries: number;
  private readonly clock: Clock;
  private readonly breakers = new Map<string, CircuitBreaker>();
  private readonly transitions = { open: 0, half_open: 0, closed: 0 };
  private evictions = 0;

  constructor(private readonly opts: BreakerRegistryOptions = {}) {
    this.config = { ...DEFAULT_BREAKER_CONFIG, ...(opts.config || {}) };
    this.maxEntries = opts.maxEntries ?? 256;
    if (!(this.maxEntries >= 1))
      throw new RangeError('maxEntries must be at least 1');
    this.clock = opts.clock ?? monotonic;
  }

  private get(provider: string): CircuitBreaker {
    let breaker = this.breakers.get(provider);
    if (breaker) {
      // Re-insert to mark it most recently used.
      this.breakers.delete(provider);
      this.breakers.set(provider, breaker);
      return breaker;
    }
    breaker = new CircuitBreaker(this.config, this.clock, (from, to) =>
      this.moved(provider, from, to)
    );
    this.breakers.set(provider, breaker);
    while (this.breakers.size > this.maxEntries) {
      const oldest = this.breakers.keys().next().value as string;
      this.breakers.delete(oldest);
      this.evictions += 1;
    }
    return breaker;
  }

  private moved(provider: string, from: BreakerState, to: BreakerState) {
    this.transitions[to] += 1;
    try {
      breakerTransitionsTotal.inc({ provider: provider.slice(0, 128), to });
      this.opts.onTransition?.(provider, from, to);
    } catch {
      /* metrics must never break a call */
    }
  }

  allow(provider: string): boolean {
    return this.get(provider).allow();
  }
  recordSuccess(provider: string): void {
    this.get(provider).recordSuccess();
  }
  recordFailure(provider: string): void {
    this.get(provider).recordFailure();
  }
  release(provider: string): void {
    this.get(provider).release();
  }
  forceOpen(provider: string): void {
    this.get(provider).forceOpen();
  }

  /** The provider's state; `closed` for a provider never seen. */
  state(provider: string): BreakerState {
    return this.breakers.get(provider)?.currentState() ?? 'closed';
  }

  snapshot(): Record<string, BreakerState> {
    const out: Record<string, BreakerState> = {};
    for (const [p, b] of this.breakers) out[p] = b.currentState();
    return out;
  }

  metrics() {
    return {
      opened: this.transitions.open,
      half_opened: this.transitions.half_open,
      closed: this.transitions.closed,
      tracked: this.breakers.size,
      evicted: this.evictions,
    };
  }
}

/** The process-wide registry the engine uses when given none. */
export const defaultBreakers = new BreakerRegistry();

// --- engine ------------------------------------------------------------------

export interface Candidate<D = unknown> {
  provider: string;
  model: string;
  contextWindow?: number;
  /** The caller's own data (a target config); passed through untouched. */
  data?: D;
}

export interface FailoverAttempt {
  provider: string;
  model: string;
  ok: boolean;
  latency_ms: number;
  /** Set for a failed attempt; a class, never a message. */
  error_class?: string;
  status?: number;
}

export interface Skipped {
  provider: string;
  model: string;
  reason: SkipReason;
}

export interface FailoverResult<T, D = unknown> {
  response: T;
  candidate: Candidate<D>;
  attempts: FailoverAttempt[];
  skipped: Skipped[];
  /** The error class of each failed attempt, in order (PRD §7.12). */
  fallbackChain: string[];
}

export const chainOf = (attempts: FailoverAttempt[]): string[] =>
  attempts
    .filter((a) => !a.ok && a.error_class)
    .map((a) => a.error_class as string);

/** Every compliant candidate failed or was skipped: fail closed. */
export class NoCompliantCandidate extends Error {
  readonly fallbackChain: string[];
  constructor(
    message: string,
    readonly attempts: FailoverAttempt[],
    readonly skipped: Skipped[],
    readonly lastError?: unknown
  ) {
    super(message);
    this.name = 'NoCompliantCandidate';
    this.fallbackChain = chainOf(attempts);
  }
}

export interface FailoverOptions<D = unknown> {
  breakers?: BreakerRegistry;
  /** Longest Retry-After honoured, in seconds. */
  maxWait?: number;
  rateLimitRetries?: number;
  allow?: (c: Candidate<D>) => boolean;
  failureOf?: (err: unknown) => FailureInfo;
  /** Seconds. */
  sleep?: (seconds: number) => Promise<void>;
}

const realSleep = (s: number) =>
  new Promise<void>((resolve) => setTimeout(resolve, s * 1000));

/** Runs `call` across ordered, already-compliant candidates (§7.5). A failure
 * the matrix says to raise is rethrown as is, with `psAttempts` and
 * `psFallbackChain` attached when the error is an object. */
export async function runFailover<T, D = unknown>(
  candidates: Candidate<D>[],
  call: (c: Candidate<D>) => Promise<T>,
  opts: FailoverOptions<D> = {}
): Promise<FailoverResult<T, D>> {
  const breakers = opts.breakers ?? defaultBreakers;
  const maxWait = opts.maxWait ?? 10;
  const rateLimitRetries = opts.rateLimitRetries ?? 1;
  const failureOf = opts.failureOf ?? failureFromError;
  const sleep = opts.sleep ?? realSleep;
  const queue = [...candidates];
  const attempts: FailoverAttempt[] = [];
  const skipped: Skipped[] = [];
  let minWindow: number | undefined;
  let lastError: unknown;

  const next = (): Candidate<D> | undefined => {
    while (queue.length) {
      const c = queue.shift() as Candidate<D>;
      const skip = (reason: SkipReason) =>
        skipped.push({ provider: c.provider, model: c.model, reason });
      if (opts.allow && !opts.allow(c)) {
        skip('non_compliant');
        continue;
      }
      if (
        minWindow !== undefined &&
        (c.contextWindow === undefined || c.contextWindow <= minWindow)
      ) {
        skip('context_window');
        continue;
      }
      if (!breakers.allow(c.provider)) {
        skip('circuit_open');
        continue;
      }
      return c;
    }
    return undefined;
  };

  let c = next();
  let retries = 0;
  while (c) {
    const start = Date.now();
    try {
      const response = await call(c);
      breakers.recordSuccess(c.provider);
      attempts.push({
        provider: c.provider,
        model: c.model,
        ok: true,
        latency_ms: Date.now() - start,
      });
      return {
        response,
        candidate: c,
        attempts,
        skipped,
        fallbackChain: chainOf(attempts),
      };
    } catch (err) {
      lastError = err;
      const info = failureOf(err);
      const verdict = classifyFailure(info);
      if (verdict.tripsBreaker) breakers.recordFailure(c.provider);
      else breakers.release(c.provider);
      const attempt: FailoverAttempt = {
        provider: c.provider,
        model: c.model,
        ok: false,
        latency_ms: Date.now() - start,
        error_class: verdict.errorClass,
      };
      if (info.status !== undefined) attempt.status = info.status;
      attempts.push(attempt);
      if (verdict.action === 'raise') {
        if (err && typeof err === 'object') {
          try {
            (err as any).psAttempts = attempts;
            (err as any).psFallbackChain = chainOf(attempts);
          } catch {
            /* frozen error */
          }
        }
        throw err;
      }
      if (
        verdict.action === 'retry_after' &&
        info.retryAfter !== undefined &&
        info.retryAfter <= maxWait &&
        retries < rateLimitRetries &&
        breakers.allow(c.provider)
      ) {
        retries += 1;
        await sleep(info.retryAfter);
        continue;
      }
      if (verdict.action === 'next_larger_context') {
        minWindow = Math.max(minWindow ?? 0, c.contextWindow ?? 0);
      }
      c = next();
      retries = 0;
    }
  }

  const parts = [
    ...attempts
      .filter((a) => !a.ok)
      .map((a) => `${a.provider}/${a.model}: ${a.error_class}`),
    ...skipped.map((s) => `${s.provider}/${s.model}: skipped (${s.reason})`),
  ];
  const message = parts.length
    ? `no compliant candidate left after ${attempts.length} attempt(s); failing closed (${parts.join('; ')})`
    : 'no compliant candidate to serve the call; failing closed';
  throw new NoCompliantCandidate(message, attempts, skipped, lastError);
}
