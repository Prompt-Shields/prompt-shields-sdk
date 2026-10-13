/**
 * Per-attempt telemetry for retries and fallbacks (#78).
 *
 * Every upstream call the gateway makes for one request (retries of a target
 * and calls to fallback targets) is recorded on the Hono context, and
 * ps-telemetry folds the list into the request's event. An attempt holds only
 * metadata: provider, model, HTTP status, latency and a coarse error class.
 * It never holds request or response content, error messages or headers.
 */

export interface AttemptStatus {
  /** HTTP status of the attempt; absent when no response was received. */
  status?: number;
  latency_ms: number;
  /** Set for a failed attempt; derived from the status, never a message. */
  error_class?: string;
}

export interface AttemptRecord extends AttemptStatus {
  /** 1-based position of the attempt across all targets of the request. */
  attempt: number;
  provider?: string;
  model?: string;
}

const CONTEXT_KEY = 'psAttempts';
const COUNT_KEY = 'psAttemptCount';

/** At most this many attempt records are kept per request; later ones are only counted. */
export const MAX_ATTEMPT_RECORDS = 32;

const TRACE_ID_RE = /^[A-Za-z0-9._:-]{1,128}$/;

/** The caller's trace id if it is a short token, else a generated one. */
export function traceIdOf(header: unknown): string {
  return typeof header === 'string' && TRACE_ID_RE.test(header)
    ? header
    : crypto.randomUUID();
}

/** A coarse, content-free class for a failed attempt. */
export function errorClassOf(
  status: number | undefined,
  errorName?: string
): string | undefined {
  if (status === undefined) {
    return errorName === 'AbortError' ? 'timeout' : 'network_error';
  }
  if (status >= 200 && status < 300) return undefined;
  if (status === 408 || status === 504) return 'timeout';
  if (status === 429) return 'rate_limited';
  if (status >= 500) return 'server_error';
  if (status >= 400) return 'client_error';
  return 'unexpected_status';
}

/** Appends one attempt to the request's list. Fail-open. */
export function recordAttempt(
  c: any,
  target: { provider?: string; model?: string },
  result: AttemptStatus
): void {
  try {
    let list: AttemptRecord[] | undefined = c.get(CONTEXT_KEY);
    if (!Array.isArray(list)) {
      list = [];
      c.set(CONTEXT_KEY, list);
    }
    const count = (Number(c.get(COUNT_KEY)) || list.length) + 1;
    c.set(COUNT_KEY, count);
    if (list.length >= MAX_ATTEMPT_RECORDS) return;
    const record: AttemptRecord = {
      attempt: count,
      latency_ms: result.latency_ms,
    };
    if (typeof target.provider === 'string')
      record.provider = target.provider.slice(0, 128);
    if (typeof target.model === 'string')
      record.model = target.model.slice(0, 256);
    if (result.status !== undefined) record.status = result.status;
    if (result.error_class) record.error_class = result.error_class;
    list.push(record);
  } catch {
    /* fail-open */
  }
}

/** How many attempts were made for the request, including unrecorded ones. */
export function attemptCountOf(c: any): number {
  try {
    return Number(c.get(COUNT_KEY)) || attemptsOf(c).length;
  } catch {
    return 0;
  }
}

/** The attempts recorded for the request, in order (at most MAX_ATTEMPT_RECORDS). */
export function attemptsOf(c: any): AttemptRecord[] {
  try {
    const list = c.get(CONTEXT_KEY);
    return Array.isArray(list) ? list : [];
  } catch {
    return [];
  }
}
