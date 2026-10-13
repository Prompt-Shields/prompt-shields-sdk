/** Base class for every error the SDK throws. */
export class PartnerError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message);
    this.name = new.target.name;
    if (options && 'cause' in options) {
      (this as { cause?: unknown }).cause = options.cause;
    }
  }
}

/** The request never got an HTTP response: network failure or timeout. */
export class APIConnectionError extends PartnerError {}

/** The request exceeded `timeoutMs`. */
export class APITimeoutError extends APIConnectionError {}

/**
 * The API answered with a non-2xx status.
 *
 * `title` and `detail` come from the RFC 7807 Problem Details body, or from
 * the OAuth `error` / `error_description` fields on the token endpoint.
 */
export class APIError extends PartnerError {
  readonly status: number;
  readonly type: string;
  readonly title: string;
  readonly detail: string;
  readonly headers: Record<string, string>;
  /** The parsed response body, or the raw text when it was not JSON. */
  readonly body: unknown;

  constructor(args: {
    status: number;
    type?: string;
    title: string;
    detail: string;
    headers?: Record<string, string>;
    body?: unknown;
  }) {
    super(`${args.status} ${args.title}: ${args.detail}`);
    this.status = args.status;
    this.type = args.type ?? 'about:blank';
    this.title = args.title;
    this.detail = args.detail;
    this.headers = args.headers ?? {};
    this.body = args.body;
  }
}

/** 400 or 422: invalid parameters. */
export class BadRequestError extends APIError {}

/** 401: missing, invalid, or expired credentials. */
export class AuthenticationError extends APIError {}

/** 403: the credential lacks the required scope. */
export class PermissionDeniedError extends APIError {}

/** 404: the resource does not exist or belongs to another tenant. */
export class NotFoundError extends APIError {}

/** 429: rate limit exceeded. */
export class RateLimitError extends APIError {
  /** Seconds to wait before retrying, from the `Retry-After` header. `null` if absent. */
  readonly retryAfter: number | null;

  constructor(args: ConstructorParameters<typeof APIError>[0] & { retryAfter: number | null }) {
    super(args);
    this.retryAfter = args.retryAfter;
  }
}

/** 5xx. */
export class InternalServerError extends APIError {}

/**
 * Parse a `Retry-After` header: delta-seconds or an HTTP date.
 * Returns seconds (>= 0), or null when absent or unparseable.
 */
export function parseRetryAfter(value: string | null | undefined, now: number = Date.now()): number | null {
  if (value == null) return null;
  const trimmed = value.trim();
  if (trimmed === '') return null;
  if (/^\d+(\.\d+)?$/.test(trimmed)) return Number(trimmed);
  const date = Date.parse(trimmed);
  if (Number.isNaN(date)) return null;
  return Math.max(0, Math.ceil((date - now) / 1000));
}

const REASON: Record<number, string> = {
  400: 'Bad Request',
  401: 'Unauthorized',
  403: 'Forbidden',
  404: 'Not Found',
  422: 'Unprocessable Entity',
  429: 'Too Many Requests',
  500: 'Internal Server Error',
  502: 'Bad Gateway',
  503: 'Service Unavailable',
  504: 'Gateway Timeout',
};

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/**
 * Pull `type`, `title`, `detail` out of an error body. Accepts:
 *  - RFC 7807 at the top level: `{type, title, status, detail}`
 *  - FastAPI's wrapper around it: `{detail: {type, title, status, detail}}`
 *  - OAuth errors: `{error, error_description}` (optionally wrapped in `detail`)
 *  - FastAPI validation errors: `{detail: [{msg, loc}, ...]}`
 *  - a plain string `detail`
 */
export function extractProblem(
  body: unknown,
  status: number,
): { type?: string; title: string; detail: string } {
  const fallbackTitle = REASON[status] ?? `HTTP ${status}`;
  let p: unknown = body;
  if (isRecord(p) && isRecord(p.detail)) p = p.detail;

  if (isRecord(p)) {
    if (typeof p.error === 'string') {
      return {
        title: p.error,
        detail: typeof p.error_description === 'string' ? p.error_description : p.error,
      };
    }
    const type = typeof p.type === 'string' ? p.type : undefined;
    const title = typeof p.title === 'string' ? p.title : fallbackTitle;
    let detail: string;
    if (typeof p.detail === 'string') {
      detail = p.detail;
    } else if (Array.isArray(p.detail)) {
      detail = p.detail
        .map((d) => {
          if (isRecord(d) && typeof d.msg === 'string') {
            const loc = Array.isArray(d.loc) ? d.loc.join('.') : '';
            return loc ? `${loc}: ${d.msg}` : d.msg;
          }
          return JSON.stringify(d);
        })
        .join('; ');
    } else {
      detail = fallbackTitle;
    }
    return { type, title, detail };
  }
  if (typeof p === 'string' && p.trim() !== '') {
    return { title: fallbackTitle, detail: p.trim().slice(0, 500) };
  }
  return { title: fallbackTitle, detail: fallbackTitle };
}

/** Build the right APIError subclass for a status code. */
export function makeAPIError(
  status: number,
  body: unknown,
  headers: Record<string, string>,
): APIError {
  const problem = extractProblem(body, status);
  const args = { status, ...problem, headers, body };
  if (status === 400 || status === 422) return new BadRequestError(args);
  if (status === 401) return new AuthenticationError(args);
  if (status === 403) return new PermissionDeniedError(args);
  if (status === 404) return new NotFoundError(args);
  if (status === 429) {
    return new RateLimitError({ ...args, retryAfter: parseRetryAfter(headers['retry-after']) });
  }
  if (status >= 500) return new InternalServerError(args);
  return new APIError(args);
}
