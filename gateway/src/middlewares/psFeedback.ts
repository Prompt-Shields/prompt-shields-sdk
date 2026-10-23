/**
 * Per-call ids and explicit call feedback (#43, PRD 0001 §7.13).
 *
 * call_id. Every LLM call the gateway reports to the collector gets one id,
 * `call_id` on its telemetry event and the `X-PS-Call-Id` response header.
 * A caller's own `X-PS-Call-Id` request header (1-128 of `A-Za-z0-9._:-`;
 * the Prompt Shields SDK sends one on every call) is kept, so a call made
 * through the SDK and the gateway is reported under the same id by both.
 * Otherwise the id is a new UUID. It is not the `x-portkey-trace-id`: a
 * trace id may be shared by several calls (it links retries and fallbacks
 * in the logs), a call_id names exactly one.
 *
 * POST /v1/feedback. `{"call_id", "score", "reason", "user_id"}`: a score
 * from 0 (bad) to 1 (good) (or true/false), optionally a reason label from
 * FEEDBACK_REASONS and an opaque user id. Metadata only; there is no field
 * for free text and other fields are dropped. The gateway checks it,
 * rate-limits it per user (PS_FEEDBACK_RATE_LIMIT_PER_MINUTE, default 60,
 * per replica; 0 turns it off) and forwards it to the collector's
 * `/ingest/feedback` with PS_API_KEY, which stores one feedback per call.
 * "Per user" is the OIDC subject when PS_OIDC_ENABLED, else a fingerprint
 * of the caller's credential header, else one shared anonymous bucket.
 * With OIDC the user_id sent is the subject pseudonym (as on telemetry),
 * never the body's.
 */
import type { Context } from 'hono';
import { oidcIdentityOf } from './psOidc/identity';
import { oidcTelemetryFields } from './psOidc/telemetry';

export const CALL_ID_HEADER = 'x-ps-call-id';
export const CALL_ID_RE = /^[A-Za-z0-9._:-]{1,128}$/;
const CALL_ID_KEY = 'psCallId';

/** The request's call id: the caller's X-PS-Call-Id when valid, else a new UUID. */
export function callIdOf(c: Context): string {
  let id = c.get(CALL_ID_KEY as any) as string | undefined;
  if (!id) {
    const header = c.req.header(CALL_ID_HEADER);
    id =
      typeof header === 'string' && CALL_ID_RE.test(header)
        ? header
        : crypto.randomUUID();
    c.set(CALL_ID_KEY as any, id);
  }
  return id;
}

/** Sets X-PS-Call-Id on the response. Fail-open. */
export function setCallIdHeader(c: Context, id: string): void {
  try {
    c.res.headers.set(CALL_ID_HEADER, id);
  } catch {
    try {
      // Immutable headers (a fetched Response): copy, then set.
      const res = new Response(c.res.body, c.res);
      res.headers.set(CALL_ID_HEADER, id);
      c.res = res;
    } catch {
      /* fail-open */
    }
  }
}

// Kept in step with the collector (collector/feedback.py REASONS) and the
// SDK (prompt_shields.feedback.FEEDBACK_REASONS).
export const FEEDBACK_REASONS = [
  'correct',
  'helpful',
  'incorrect',
  'incomplete',
  'hallucination',
  'off_topic',
  'refusal',
  'format_error',
  'unsafe',
  'too_slow',
  'too_long',
  'other',
] as const;

export const FEEDBACK_PATH = '/v1/feedback';
export const MAX_FEEDBACK_BYTES = 4096;
export const MAX_USER_ID_LENGTH = 128;
export const DEFAULT_FEEDBACK_RATE_LIMIT = 60;
const COLLECTOR_TIMEOUT_MS = 5000;

export interface Feedback {
  call_id: string;
  score: number;
  reason?: string;
  user_id?: string;
}

// NUL and lone surrogates cannot be stored (collector/sanitize.py).
// eslint-disable-next-line no-control-regex
const UNSTORABLE =
  /\u0000|[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/;

function hasUnstorable(value: unknown, depth = 0): boolean {
  if (typeof value === 'string') return UNSTORABLE.test(value);
  if (depth > 64) return true;
  if (Array.isArray(value))
    return value.some((v) => hasUnstorable(v, depth + 1));
  if (value && typeof value === 'object')
    return Object.entries(value).some(
      ([k, v]) => UNSTORABLE.test(k) || hasUnstorable(v, depth + 1)
    );
  return false;
}

/** The feedback to forward, or the reason it is refused. */
export function parseFeedback(body: unknown): Feedback | { error: string } {
  if (!body || typeof body !== 'object' || Array.isArray(body))
    return { error: 'Expected a JSON object' };
  if (hasUnstorable(body))
    return {
      error: 'Body must not contain NUL characters or unpaired surrogates',
    };
  const b = body as Record<string, unknown>;
  if (typeof b.call_id !== 'string' || !CALL_ID_RE.test(b.call_id))
    return {
      error: 'call_id must be 1-128 characters of A-Z a-z 0-9 . _ : -',
    };
  let score = b.score;
  if (typeof score === 'boolean') score = score ? 1 : 0;
  if (
    typeof score !== 'number' ||
    !Number.isFinite(score) ||
    score < 0 ||
    score > 1
  )
    return { error: 'score must be a number from 0 to 1 (or a boolean)' };
  const out: Feedback = { call_id: b.call_id, score };
  if (b.reason !== undefined && b.reason !== null) {
    if (!(FEEDBACK_REASONS as readonly unknown[]).includes(b.reason))
      return {
        error: `reason must be one of: ${FEEDBACK_REASONS.join(', ')}`,
      };
    out.reason = b.reason as string;
  }
  if (b.user_id !== undefined && b.user_id !== null) {
    if (
      typeof b.user_id !== 'string' ||
      !b.user_id ||
      b.user_id.length > MAX_USER_ID_LENGTH ||
      // printable only
      // eslint-disable-next-line no-control-regex
      /[\u0000-\u001f\u007f-\u009f]/.test(b.user_id)
    )
      return {
        error: `user_id must be 1-${MAX_USER_ID_LENGTH} printable characters`,
      };
    out.user_id = b.user_id;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Per-user rate limit: fixed one-minute windows, in memory, per replica.

const WINDOW_MS = 60_000;
const MAX_BUCKETS = 50_000;
const buckets = new Map<string, { windowStart: number; count: number }>();
let now = () => Date.now();

/** Seconds until the window resets when `key` is over `limit`, else undefined. */
export function takeFeedbackRateLimit(
  key: string,
  limit: number
): number | undefined {
  if (!(limit > 0)) return undefined;
  const t = now();
  const windowStart = t - (t % WINDOW_MS);
  let bucket = buckets.get(key);
  if (!bucket || bucket.windowStart !== windowStart) {
    if (!bucket && buckets.size >= MAX_BUCKETS) {
      for (const [k, b] of buckets)
        if (b.windowStart !== windowStart) buckets.delete(k);
      if (buckets.size >= MAX_BUCKETS) {
        let n = Math.ceil(MAX_BUCKETS / 10);
        for (const k of buckets.keys()) {
          if (n-- <= 0) break;
          buckets.delete(k);
        }
      }
    }
    bucket = { windowStart, count: 0 };
    buckets.set(key, bucket);
  }
  if (bucket.count >= limit)
    return Math.max(1, Math.ceil((windowStart + WINDOW_MS - t) / 1000));
  bucket.count++;
  return undefined;
}

export function _resetFeedbackRateLimitsForTests() {
  buckets.clear();
  now = () => Date.now();
}
export function _setFeedbackClockForTests(fn: () => number) {
  now = fn;
}

const hex = (buf: ArrayBuffer) =>
  Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');

/** The rate-limit bucket of the caller: OIDC subject, credential fingerprint or anonymous. */
export async function feedbackUserKey(c: Context): Promise<string> {
  const identity = oidcIdentityOf(c);
  if (identity) return `u:${identity.subjectHash}`;
  const credential =
    c.req.header('authorization') ||
    c.req.header('x-api-key') ||
    c.req.header('x-portkey-api-key');
  if (credential) {
    const digest = await crypto.subtle.digest(
      'SHA-256',
      new TextEncoder().encode(credential)
    );
    return `k:${hex(digest).slice(0, 32)}`;
  }
  return 'anonymous';
}

function rateLimit(env: Record<string, string | undefined>): number {
  const raw = env.PS_FEEDBACK_RATE_LIMIT_PER_MINUTE;
  if (raw === undefined || raw.trim() === '')
    return DEFAULT_FEEDBACK_RATE_LIMIT;
  const n = Number(raw);
  return Number.isFinite(n) ? Math.floor(n) : DEFAULT_FEEDBACK_RATE_LIMIT;
}

const errorBody = (message: string, type: string, code: string) => ({
  error: { message, type, param: null, code },
});

/** POST /v1/feedback. See the module comment. */
export async function psFeedbackHandler(c: Context): Promise<Response> {
  const env = process.env;
  if (!env.PS_API_KEY)
    return c.json(
      errorBody(
        'Feedback needs the gateway to report to a Prompt Shields collector (PS_API_KEY)',
        'service_unavailable',
        'feedback_not_configured'
      ),
      503
    );

  const declared = Number(c.req.header('content-length'));
  if (Number.isFinite(declared) && declared > MAX_FEEDBACK_BYTES)
    return c.json(
      errorBody(
        `Body exceeds ${MAX_FEEDBACK_BYTES} bytes`,
        'invalid_request_error',
        'payload_too_large'
      ),
      413
    );
  let raw: string;
  try {
    raw = await c.req.text();
  } catch {
    raw = '';
  }
  if (new TextEncoder().encode(raw).length > MAX_FEEDBACK_BYTES)
    return c.json(
      errorBody(
        `Body exceeds ${MAX_FEEDBACK_BYTES} bytes`,
        'invalid_request_error',
        'payload_too_large'
      ),
      413
    );
  let body: unknown;
  try {
    body = JSON.parse(raw);
  } catch {
    return c.json(
      errorBody(
        'Body is not valid JSON',
        'invalid_request_error',
        'invalid_json'
      ),
      400
    );
  }
  const parsed = parseFeedback(body);
  if ('error' in parsed)
    return c.json(
      errorBody(parsed.error, 'invalid_request_error', 'invalid_feedback'),
      422
    );

  const retryAfter = takeFeedbackRateLimit(
    await feedbackUserKey(c),
    rateLimit(env)
  );
  if (retryAfter !== undefined) {
    c.header('Retry-After', String(retryAfter));
    return c.json(
      errorBody(
        'Feedback rate limit exceeded',
        'rate_limit_error',
        'rate_limit_exceeded'
      ),
      429
    );
  }

  // With OIDC the user is the authenticated subject, as on telemetry.
  const identity = oidcIdentityOf(c);
  const oidcUser = oidcTelemetryFields(identity).user_id;
  const forward: Record<string, unknown> = {
    call_id: parsed.call_id,
    score: parsed.score,
    source: 'gateway',
  };
  if (parsed.reason) forward.reason = parsed.reason;
  const userId = identity ? oidcUser : parsed.user_id;
  if (userId) forward.user_id = userId;

  const base = (env.PS_COLLECTOR_URL || 'http://localhost:8000').replace(
    /\/+$/,
    ''
  );
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), COLLECTOR_TIMEOUT_MS);
  let res: Response;
  try {
    res = await fetch(`${base}/ingest/feedback`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        authorization: `Bearer ${env.PS_API_KEY}`,
      },
      body: JSON.stringify(forward),
      signal: controller.signal,
      redirect: 'error',
    });
  } catch {
    return c.json(
      errorBody('Collector unreachable', 'api_error', 'collector_unreachable'),
      502
    );
  } finally {
    clearTimeout(timer);
  }

  let answer: any = undefined;
  try {
    answer = await res.json();
  } catch {
    answer = undefined;
  }
  if (res.status === 200) {
    return c.json({
      call_id: parsed.call_id,
      created: answer?.created === true,
      matched: answer?.matched === true,
    });
  }
  if (res.status === 429) {
    const after = res.headers.get('retry-after');
    if (after && /^\d+$/.test(after)) c.header('Retry-After', after);
    return c.json(
      errorBody(
        'Feedback rate limit exceeded at the collector',
        'rate_limit_error',
        'rate_limit_exceeded'
      ),
      429
    );
  }
  if (res.status === 413 || res.status === 422) {
    const detail =
      typeof answer?.detail?.detail === 'string'
        ? answer.detail.detail
        : 'Feedback refused by the collector';
    return c.json(
      errorBody(detail, 'invalid_request_error', 'invalid_feedback'),
      422
    );
  }
  // 401/403 (the gateway's PS_API_KEY) and 5xx are the gateway's problem,
  // not the caller's.
  return c.json(
    errorBody(
      `Collector refused the feedback (${res.status})`,
      'api_error',
      'collector_error'
    ),
    502
  );
}
