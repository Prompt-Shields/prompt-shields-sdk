import { camelizeKeys, snakeKey } from './casing';
import {
  APIConnectionError,
  APIError,
  APITimeoutError,
  PartnerError,
  RateLimitError,
  makeAPIError,
} from './errors';
import { PageIterator } from './pagination';
import type {
  AIAsset,
  ChangeEvent,
  DataFlow,
  ExportParams,
  ExportResult,
  HealthStatus,
  ListAssetsParams,
  ListChangesParams,
  Page,
  RiskMapping,
  TokenResponse,
  UsageSummary,
  UsageSummaryParams,
} from './types';

export const VERSION = '0.1.0';
export const DEFAULT_BASE_URL = 'https://api.promptshields.io';

/** The subset of `fetch` the client uses. Pass your own to add proxies or for tests. */
export type FetchLike = (input: string, init: RequestInit) => Promise<Response>;

export interface PartnerClientOptions {
  /** Partner API key (`ps-pk-...`). Used when OAuth credentials are not given. */
  apiKey?: string;
  /** OAuth client ID. Requires `clientSecret`. Preferred over `apiKey`. */
  clientId?: string;
  /** OAuth client secret. */
  clientSecret?: string;
  /** API origin. Defaults to `https://api.promptshields.io`. */
  baseUrl?: string;
  /** Retries for 429, 502, 503, 504 and network errors. Default 2. */
  maxRetries?: number;
  /**
   * Longest `Retry-After` (seconds) the client will wait out on a 429.
   * A longer one throws `RateLimitError` straight away. Default 60.
   */
  maxRetryAfterSeconds?: number;
  /** Per-request timeout in milliseconds. Default 30000. */
  timeoutMs?: number;
  /** Extra headers sent on every request. */
  headers?: Record<string, string>;
  /** `fetch` implementation. Defaults to the global `fetch` (Node 18+). */
  fetch?: FetchLike;
  /** Sleep used between retries. Override in tests. */
  sleep?: (ms: number) => Promise<void>;
}

type Query = Record<string, unknown>;

interface RequestOptions {
  query?: Query;
  /** Send `Authorization`. Default true. */
  auth?: boolean;
  /** URL-encoded form body (token endpoint). */
  form?: Record<string, string>;
}

interface CachedToken {
  value: string;
  /** Epoch ms after which the token is refreshed. */
  refreshAt: number;
}

const RETRY_STATUSES = new Set([429, 502, 503, 504]);
const TOKEN_REFRESH_SKEW_MS = 60_000;

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function headersToRecord(headers: Headers | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  headers?.forEach((value, key) => {
    out[key.toLowerCase()] = value;
  });
  return out;
}

function buildQuery(query: Query | undefined): string {
  if (!query) return '';
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null) continue;
    const v = value instanceof Date ? value.toISOString() : String(value);
    params.append(snakeKey(key), v);
  }
  const s = params.toString();
  return s ? `?${s}` : '';
}

function requireId(id: string, what: string): string {
  if (typeof id !== 'string' || id.trim() === '') {
    throw new PartnerError(`${what} must be a non-empty string`);
  }
  return encodeURIComponent(id);
}

function parseJson(text: string): unknown {
  if (text === '') return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/**
 * Client for the Prompt Shields Partner API.
 *
 * ```ts
 * const client = new PartnerClient({ apiKey: 'ps-pk-...' });
 * for await (const asset of client.assets.list({ limit: 200 })) { ... }
 * ```
 */
export class PartnerClient {
  readonly baseUrl: string;
  readonly assets: AssetsResource;
  readonly changes: ChangesResource;
  readonly export: ExportResource;
  readonly vendors: TaxonomyResource;
  readonly models: TaxonomyResource;
  readonly businessUnits: TaxonomyResource;

  private readonly apiKey?: string;
  private readonly clientId?: string;
  private readonly clientSecret?: string;
  private readonly maxRetries: number;
  private readonly maxRetryAfterSeconds: number;
  private readonly timeoutMs: number;
  private readonly extraHeaders: Record<string, string>;
  private readonly fetchImpl: FetchLike;
  private readonly sleep: (ms: number) => Promise<void>;
  private token: CachedToken | undefined;
  private tokenInFlight: Promise<string> | undefined;

  constructor(options: PartnerClientOptions) {
    const { apiKey, clientId, clientSecret } = options ?? {};
    if ((clientId && !clientSecret) || (!clientId && clientSecret)) {
      throw new PartnerError('clientId and clientSecret must be given together');
    }
    if (!clientId && !apiKey) {
      throw new PartnerError('Provide either apiKey, or clientId and clientSecret');
    }
    this.apiKey = apiKey;
    this.clientId = clientId;
    this.clientSecret = clientSecret;
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/+$/, '');
    this.maxRetries = Math.max(0, options.maxRetries ?? 2);
    this.maxRetryAfterSeconds = options.maxRetryAfterSeconds ?? 60;
    this.timeoutMs = options.timeoutMs ?? 30_000;
    this.extraHeaders = { ...(options.headers ?? {}) };
    this.sleep = options.sleep ?? defaultSleep;

    if (options.fetch) {
      this.fetchImpl = options.fetch;
    } else if (typeof globalThis.fetch === 'function') {
      this.fetchImpl = (input, init) => globalThis.fetch(input, init);
    } else {
      throw new PartnerError('No global fetch found. Use Node 18+ or pass `fetch` in the options.');
    }

    this.assets = new AssetsResource(this);
    this.changes = new ChangesResource(this);
    this.export = new ExportResource(this);
    this.vendors = new TaxonomyResource(this, '/partner/v1/vendors');
    this.models = new TaxonomyResource(this, '/partner/v1/models');
    this.businessUnits = new TaxonomyResource(this, '/partner/v1/business-units');
  }

  /** `GET /partner/v1/health`. No authentication. */
  health(): Promise<HealthStatus> {
    return this.request<HealthStatus>('GET', '/partner/v1/health', { auth: false });
  }

  /** True when the client authenticates with OAuth client credentials. */
  get usesOAuth(): boolean {
    return Boolean(this.clientId);
  }

  /** Drop any cached OAuth token so the next request fetches a new one. */
  clearToken(): void {
    this.token = undefined;
  }

  /** Make an authenticated GET and return the camelCased JSON body. */
  get<T>(path: string, query?: Query): Promise<T> {
    return this.request<T>('GET', path, { query });
  }

  private async bearer(): Promise<string> {
    if (!this.clientId) return this.apiKey as string;
    if (this.token && Date.now() < this.token.refreshAt) return this.token.value;
    if (!this.tokenInFlight) {
      this.tokenInFlight = this.fetchToken().finally(() => {
        this.tokenInFlight = undefined;
      });
    }
    return this.tokenInFlight;
  }

  private async fetchToken(): Promise<string> {
    const res = await this.request<TokenResponse>('POST', '/oauth/token', {
      auth: false,
      form: {
        grant_type: 'client_credentials',
        client_id: this.clientId as string,
        client_secret: this.clientSecret as string,
      },
    });
    if (!res || typeof res.accessToken !== 'string') {
      throw new PartnerError('Token endpoint returned no access_token');
    }
    const lifetimeMs = (Number(res.expiresIn) || 3600) * 1000;
    // Refresh a minute early, or halfway through very short-lived tokens.
    const skew = Math.min(TOKEN_REFRESH_SKEW_MS, lifetimeMs / 2);
    this.token = { value: res.accessToken, refreshAt: Date.now() + lifetimeMs - skew };
    return res.accessToken;
  }

  private backoffMs(attempt: number): number {
    const base = 500 * 2 ** attempt;
    return Math.min(base + Math.random() * base * 0.25, 8_000);
  }

  private async request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
    const auth = opts.auth ?? true;
    const url = `${this.baseUrl}${path}${buildQuery(opts.query)}`;
    let reauthed = false;
    let attempt = 0;

    for (;;) {
      const headers: Record<string, string> = {
        Accept: 'application/json',
        ...this.extraHeaders,
      };
      if (auth) headers.Authorization = `Bearer ${await this.bearer()}`;
      let body: string | undefined;
      if (opts.form) {
        headers['Content-Type'] = 'application/x-www-form-urlencoded';
        body = new URLSearchParams(opts.form).toString();
      }

      let status: number;
      let resHeaders: Record<string, string>;
      let text: string;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.timeoutMs);
      try {
        const res = await this.fetchImpl(url, { method, headers, body, signal: controller.signal });
        status = res.status;
        resHeaders = headersToRecord(res.headers);
        text = await res.text();
      } catch (err) {
        const timedOut = controller.signal.aborted;
        const wrapped = timedOut
          ? new APITimeoutError(`Request to ${method} ${path} timed out after ${this.timeoutMs}ms`, { cause: err })
          : new APIConnectionError(`Request to ${method} ${path} failed: ${(err as Error)?.message ?? err}`, { cause: err });
        if (attempt < this.maxRetries) {
          await this.sleep(this.backoffMs(attempt));
          attempt++;
          continue;
        }
        throw wrapped;
      } finally {
        clearTimeout(timer);
      }

      const parsed = parseJson(text);
      if (status >= 200 && status < 300) {
        if (typeof parsed === 'string') {
          throw new PartnerError(`Expected JSON from ${method} ${path}, got: ${parsed.slice(0, 200)}`);
        }
        return camelizeKeys<T>(parsed);
      }

      // An OAuth token can expire or be revoked mid-flight: refresh once.
      if (status === 401 && auth && this.clientId && !reauthed) {
        reauthed = true;
        this.token = undefined;
        continue;
      }

      const error: APIError = makeAPIError(status, parsed, resHeaders);
      if (attempt < this.maxRetries && RETRY_STATUSES.has(status)) {
        let delay = this.backoffMs(attempt);
        if (error instanceof RateLimitError && error.retryAfter !== null) {
          if (error.retryAfter > this.maxRetryAfterSeconds) throw error;
          delay = error.retryAfter * 1000;
        }
        await this.sleep(delay);
        attempt++;
        continue;
      }
      throw error;
    }
  }
}

/** `/partner/v1/assets` and its sub-resources. */
export class AssetsResource {
  constructor(private readonly client: PartnerClient) {}

  /** One page of assets. */
  listPage(params: ListAssetsParams = {}): Promise<Page<AIAsset>> {
    return this.client.get<Page<AIAsset>>('/partner/v1/assets', { ...params });
  }

  /**
   * All assets matching the filters, fetched page by page as you iterate.
   * `await` it for an array, or `for await` to stream.
   */
  list(params: ListAssetsParams = {}): PageIterator<AIAsset> {
    const { after, ...rest } = params;
    return new PageIterator((cursor) => this.listPage({ ...rest, after: cursor }), after ?? undefined);
  }

  /** `GET /partner/v1/assets/{id}`. Throws `NotFoundError` if absent. */
  get(assetId: string): Promise<AIAsset> {
    return this.client.get<AIAsset>(`/partner/v1/assets/${requireId(assetId, 'assetId')}`);
  }

  /** Data lineage records for an asset. */
  async dataFlows(assetId: string): Promise<DataFlow[]> {
    const res = await this.client.get<{ data: DataFlow[] }>(
      `/partner/v1/assets/${requireId(assetId, 'assetId')}/data-flows`,
    );
    return res.data;
  }

  /** Risk mappings for an asset. */
  async risks(assetId: string): Promise<RiskMapping[]> {
    const res = await this.client.get<{ data: RiskMapping[] }>(
      `/partner/v1/assets/${requireId(assetId, 'assetId')}/risks`,
    );
    return res.data;
  }

  /**
   * Aggregated usage for an asset over `period` (default `30d`).
   *
   * Needs the collector endpoint from #99. Until that is deployed this
   * throws `NotFoundError`.
   */
  usageSummary(assetId: string, params: UsageSummaryParams = {}): Promise<UsageSummary> {
    return this.client.get<UsageSummary>(
      `/partner/v1/assets/${requireId(assetId, 'assetId')}/usage-summary`,
      { ...params },
    );
  }
}

/** `/partner/v1/changes`: the delta feed. */
export class ChangesResource {
  constructor(private readonly client: PartnerClient) {}

  /** One page of changes. `meta.syncToken` is the `since` for your next sync. */
  listPage(params: ListChangesParams): Promise<Page<ChangeEvent>> {
    if (!params || params.since === undefined || params.since === null || params.since === '') {
      throw new PartnerError('changes.listPage requires `since`');
    }
    return this.client.get<Page<ChangeEvent>>('/partner/v1/changes', { ...params });
  }

  /**
   * Every change since `since`, page by page. After iterating, read
   * `iterator.lastPage?.meta.syncToken` for the next sync.
   */
  list(params: ListChangesParams): PageIterator<ChangeEvent> {
    if (!params || params.since === undefined || params.since === null || params.since === '') {
      throw new PartnerError('changes.list requires `since`');
    }
    const { after, ...rest } = params;
    return new PageIterator((cursor) => this.listPage({ ...rest, after: cursor }), after ?? undefined);
  }
}

/** `/partner/v1/export`: the full registry in one response. */
export class ExportResource {
  constructor(private readonly client: PartnerClient) {}

  get(params: ExportParams = {}): Promise<ExportResult> {
    return this.client.get<ExportResult>('/partner/v1/export', { ...params });
  }
}

/** `/partner/v1/vendors`, `/models`, `/business-units`. */
export class TaxonomyResource {
  constructor(
    private readonly client: PartnerClient,
    private readonly path: string,
  ) {}

  async list(): Promise<string[]> {
    const res = await this.client.get<{ data: string[] }>(this.path);
    return res.data;
  }
}
