import { camelizeKeys, snakeKey, snakeizeKeys } from './casing';
import {
  APIConnectionError,
  APIError,
  APITimeoutError,
  PartnerError,
  RateLimitError,
  makeAPIError,
} from './errors';
import { FocusExport, SiemBatch, SiemFeed } from './download';
import { PageIterator } from './pagination';
import type {
  AdminKeyRequest,
  AIAsset,
  AttestationList,
  AttestationRequest,
  AttestationResult,
  ChangeEvent,
  CreateWebhookSubscriptionParams,
  CredentialProvider,
  DataFlow,
  CycloneDxBom,
  DownloadOptions,
  ExportParams,
  ExportResult,
  FingerprintLinkResult,
  FingerprintRequest,
  FocusExportParams,
  HealthStatus,
  LearnerRecommendation,
  LearnerRecommendationList,
  LearnerReviewRequest,
  LearnerSnapshot,
  LearnerSnapshotList,
  ListAssetsParams,
  ListAttestationsParams,
  ListChangesParams,
  ListLearnerRecommendationsParams,
  ListLearnerSnapshotsParams,
  ListMcpToolsParams,
  ListMcpUsageParams,
  ListRiskReviewsParams,
  ListSpendAlertsParams,
  ListWebhookDeliveriesParams,
  ListWebhookSubscriptionsParams,
  McpTool,
  McpToolPage,
  McpUsageDay,
  McpUsagePage,
  Page,
  ProviderCredential,
  ProviderCredentialList,
  ProviderKeyFingerprintLink,
  RiskMapping,
  RiskReviewHistory,
  RiskReviewRequest,
  RiskReviewResult,
  RisksParams,
  RotateWebhookSecretParams,
  SavingsParams,
  SavingsReport,
  SiemAuditParams,
  SiemCallsParams,
  SovereignShareParams,
  SovereignShareReport,
  SpendAlert,
  SpendAlertList,
  TelemetryCoverage,
  TokenResponse,
  UpdateWebhookSubscriptionParams,
  UsageSummary,
  UsageSummaryParams,
  WebhookDelivery,
  WebhookSubscription,
  WebhookSubscriptionWithSecret,
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
  /**
   * Retries for 429, 502, 503, 504 and network errors. Default 2.
   * POST requests (other than the OAuth token request) are retried only on
   * 429: a POST that reached the server could record an attestation or
   * rotate a secret twice, so it is not sent again after a 5xx or a
   * network error.
   */
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

export interface SendOptions {
  /** Query parameters, camelCase keys (sent as snake_case). */
  query?: Query;
  /** JSON request body, camelCase keys (sent as snake_case). */
  body?: unknown;
  /**
   * Set `true` for an idempotent POST, so it is also retried after a
   * network error or a 502/503/504. Default: true for every method but POST.
   */
  retrySafe?: boolean;
}

interface RequestOptions extends SendOptions {
  /** Send `Authorization`. Default true. */
  auth?: boolean;
  /** URL-encoded form body (token endpoint). */
  form?: Record<string, string>;
  /**
   * Whether a request that may have reached the server can be sent again
   * (network errors, timeouts, 502/503/504). Defaults to true for every
   * method except POST, which can create a record or rotate a secret twice.
   * 429 and 401 are retried either way: the server did nothing.
   */
  retrySafe?: boolean;
  /** Return the 2xx `Response` with its body unread, instead of parsed JSON. */
  raw?: boolean;
  /** `Accept` header. Default `application/json`. */
  accept?: string;
  /** Caller's cancellation signal. */
  signal?: AbortSignal;
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

/**
 * camelCase the keys of a `meta.current` that holds one record (a risk
 * mapping) rather than the attestation map that `camelizeKeys` keeps as sent.
 */
function recamelize<T>(value: T): T {
  return value === undefined || value === null ? value : camelizeKeys<T>(value);
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
  readonly telemetry: TelemetryResource;
  readonly usage: UsageResource;
  readonly spendAlerts: SpendAlertsResource;
  readonly mcp: McpResource;
  readonly learner: LearnerResource;
  readonly siem: SiemResource;
  /** Stored provider admin keys (write-only, masked). Needs `provider-credentials:manage`. */
  readonly providerCredentials: ProviderCredentialsResource;
  /** Provider key id to SDK `api_key_fingerprint` links. Needs `provider-credentials:manage`. */
  readonly providerKeyFingerprints: ProviderKeyFingerprintsResource;
  /**
   * Webhook subscriptions: the tenant admin API (`/api/v1/admin/webhooks`).
   * Needs a client built with the tenant's own API key (`apiKey`), not
   * partner credentials.
   */
  readonly webhooks: WebhooksResource;

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
    this.telemetry = new TelemetryResource(this);
    this.usage = new UsageResource(this);
    this.spendAlerts = new SpendAlertsResource(this);
    this.mcp = new McpResource(this);
    this.learner = new LearnerResource(this);
    this.siem = new SiemResource(this);
    this.providerCredentials = new ProviderCredentialsResource(this);
    this.providerKeyFingerprints = new ProviderKeyFingerprintsResource(this);
    this.webhooks = new WebhooksResource(this);
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

  /**
   * Make an authenticated request and return the camelCased JSON body
   * (`undefined` for an empty response). For endpoints the SDK does not
   * wrap yet.
   */
  send<T>(method: string, path: string, opts: SendOptions = {}): Promise<T> {
    return this.request<T>(method.toUpperCase(), path, {
      query: opts.query,
      body: opts.body,
      retrySafe: opts.retrySafe,
    });
  }

  /**
   * Make an authenticated GET and return the `Response` with its body
   * unread, for streamed downloads. Auth, retries and errors work as for
   * `get`: a non-2xx status throws an `APIError`. `timeoutMs` covers the
   * wait for the response headers only; pass `signal` to cancel the
   * download.
   */
  getStream(path: string, query?: Query, options: DownloadOptions & { accept?: string } = {}): Promise<Response> {
    return this.request<Response>('GET', path, {
      query,
      raw: true,
      accept: options.accept ?? '*/*',
      signal: options.signal,
    });
  }

  /**
   * @internal Throws unless the client holds a tenant API key. The webhook
   * admin API takes the tenant's key; a partner OAuth token or partner API
   * key (`ps-pk-...`) is never sent to it.
   */
  assertTenantKey(what: string): void {
    if (this.clientId || !this.apiKey || this.apiKey.startsWith('ps-pk-')) {
      throw new PartnerError(
        `${what} uses the tenant admin API and needs a client built with the tenant's API key ` +
          '(`apiKey`), not partner credentials',
      );
    }
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
      retrySafe: true,
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
    const retrySafe = opts.retrySafe ?? method !== 'POST';
    // `path` is relative to baseUrl. Anything that could change the host
    // (`//evil.com`, `@evil.com`, `.evil.com`, an absolute URL) would send
    // the bearer token elsewhere, so it is refused.
    if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//') || /[\\\s]/.test(path)) {
      throw new PartnerError(`path must be relative to baseUrl and start with a single "/": ${String(path)}`);
    }
    const url = `${this.baseUrl}${path}${buildQuery(opts.query)}`;
    if (new URL(url).origin !== new URL(this.baseUrl).origin) {
      throw new PartnerError(`path ${path} does not stay on ${this.baseUrl}`);
    }
    let reauthed = false;
    let attempt = 0;

    for (;;) {
      if (opts.signal?.aborted) throw new APIConnectionError(`Request to ${method} ${path} was aborted`);
      const headers: Record<string, string> = {
        Accept: opts.accept ?? 'application/json',
        ...this.extraHeaders,
      };
      if (auth) headers.Authorization = `Bearer ${await this.bearer()}`;
      let body: string | undefined;
      if (opts.form) {
        headers['Content-Type'] = 'application/x-www-form-urlencoded';
        body = new URLSearchParams(opts.form).toString();
      } else if (opts.body !== undefined) {
        headers['Content-Type'] = 'application/json';
        body = JSON.stringify(snakeizeKeys(opts.body));
      }

      let status: number;
      let resHeaders: Record<string, string>;
      let text: string;
      const controller = new AbortController();
      let timedOut = false;
      const timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, this.timeoutMs);
      const callerSignal = opts.signal;
      const onCallerAbort = () => controller.abort();
      if (callerSignal?.aborted) controller.abort();
      else callerSignal?.addEventListener('abort', onCallerAbort, { once: true });
      let keepListener = false;
      try {
        const res = await this.fetchImpl(url, { method, headers, body, signal: controller.signal });
        status = res.status;
        resHeaders = headersToRecord(res.headers);
        if (opts.raw && status >= 200 && status < 300) {
          // The body is the caller's to read. Their signal still cancels
          // it; the timeout stops here.
          keepListener = true;
          return res as unknown as T;
        }
        text = await res.text();
      } catch (err) {
        if (callerSignal?.aborted) {
          throw new APIConnectionError(`Request to ${method} ${path} was aborted`, { cause: err });
        }
        const wrapped = timedOut
          ? new APITimeoutError(`Request to ${method} ${path} timed out after ${this.timeoutMs}ms`, { cause: err })
          : new APIConnectionError(`Request to ${method} ${path} failed: ${(err as Error)?.message ?? err}`, { cause: err });
        if (retrySafe && attempt < this.maxRetries) {
          await this.sleep(this.backoffMs(attempt));
          attempt++;
          continue;
        }
        throw wrapped;
      } finally {
        clearTimeout(timer);
        if (!keepListener) callerSignal?.removeEventListener('abort', onCallerAbort);
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
      const retryable = status === 429 || (retrySafe && RETRY_STATUSES.has(status));
      if (attempt < this.maxRetries && retryable) {
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

  /**
   * Risk mappings for an asset. By default the server returns `confirmed`
   * mappings only; pass `{ status: 'suggested' }` for unreviewed rule-based
   * suggestions, or `'rejected'`.
   */
  async risks(assetId: string, params: RisksParams = {}): Promise<RiskMapping[]> {
    const res = await this.client.get<{ data: RiskMapping[] }>(
      `/partner/v1/assets/${requireId(assetId, 'assetId')}/risks`,
      { ...params },
    );
    return res.data;
  }

  /**
   * Review history of one risk mapping, newest first, with the mapping as
   * it is now in `meta.current`. Needs `risks:read`.
   */
  async riskReviews(assetId: string, riskId: string, params: ListRiskReviewsParams = {}): Promise<RiskReviewHistory> {
    const res = await this.client.get<RiskReviewHistory>(
      `/partner/v1/assets/${requireId(assetId, 'assetId')}/risks/${requireId(riskId, 'riskId')}/reviews`,
      { ...params },
    );
    return { ...res, meta: { ...res.meta, current: recamelize(res.meta?.current) } };
  }

  /**
   * Confirm or reject a risk mapping, typically a rule-based suggestion.
   * Needs the `risks:review` scope. Every review is appended to the
   * mapping's history; `confidence` never changes. Not retried on network
   * errors or 5xx, since a retry could record the review twice.
   */
  async reviewRisk(assetId: string, riskId: string, review: RiskReviewRequest): Promise<RiskReviewResult> {
    const res = await this.client.send<RiskReviewResult>(
      'POST',
      `/partner/v1/assets/${requireId(assetId, 'assetId')}/risks/${requireId(riskId, 'riskId')}/review`,
      { body: review },
    );
    return { ...res, meta: { ...res.meta, current: recamelize(res.meta?.current) } };
  }

  /**
   * Attestation history for an asset, newest first, with the current state
   * of every attestable field in `meta.current`. Needs `assets:read`.
   */
  attestations(assetId: string, params: ListAttestationsParams = {}): Promise<AttestationList> {
    return this.client.get<AttestationList>(
      `/partner/v1/assets/${requireId(assetId, 'assetId')}/attestations`,
      { ...params },
    );
  }

  /**
   * Record a human attestation for one field of an asset. Needs the
   * `attestations:write` scope. Never changes `confidence`. Not retried on
   * network errors or 5xx, since a retry could record it twice.
   */
  attest(assetId: string, attestation: AttestationRequest): Promise<AttestationResult> {
    return this.client.send<AttestationResult>(
      'POST',
      `/partner/v1/assets/${requireId(assetId, 'assetId')}/attestations`,
      { body: attestation },
    );
  }

  /** Aggregated usage for an asset over `period` (default `30d`). */
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

  get(params: ExportParams & { format: 'cyclonedx' }): Promise<CycloneDxBom>;
  get(params?: ExportParams & { format?: 'json' }): Promise<ExportResult>;
  get(params: ExportParams = {}): Promise<ExportResult | CycloneDxBom> {
    // CycloneDX keys are camelCase or hyphenated, so the camelCasing of
    // snake_case response keys leaves the BOM unchanged.
    return this.client.get<ExportResult | CycloneDxBom>('/partner/v1/export', { ...params });
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

/** `/partner/v1/telemetry/*`: how complete the registry's usage data is. */
export class TelemetryResource {
  constructor(private readonly client: PartnerClient) {}

  /** `GET /partner/v1/telemetry/coverage`: coverage overall and per source. Needs `usage:read`. */
  coverage(): Promise<TelemetryCoverage> {
    return this.client.get<TelemetryCoverage>('/partner/v1/telemetry/coverage');
  }
}

/** `/partner/v1/usage/*`: tenant-wide routing savings and sovereign share. Needs `usage:read`. */
export class UsageResource {
  constructor(private readonly client: PartnerClient) {}

  /** `GET /partner/v1/usage/savings`: realised savings against the counterfactual. */
  savings(params: SavingsParams = {}): Promise<SavingsReport> {
    return this.client.get<SavingsReport>('/partner/v1/usage/savings', { ...params });
  }

  /** `GET /partner/v1/usage/sovereign-share`: calls and spend served in the EEA, per sensitivity level. */
  sovereignShare(params: SovereignShareParams = {}): Promise<SovereignShareReport> {
    return this.client.get<SovereignShareReport>('/partner/v1/usage/sovereign-share', { ...params });
  }

  /**
   * `GET /partner/v1/usage/focus-export`: AI usage cost as FinOps FOCUS v1.4
   * Cost and Usage rows, streamed as CSV (default) or JSON Lines. Needs
   * `usage:export`. Resolves once the response headers arrive; the body is
   * read as you consume it and never held whole in memory (unless you
   * call `text()`):
   *
   * ```ts
   * const exp = await client.usage.focusExport({ format: 'jsonl', start: '2026-10-01', end: '2026-11-01' });
   * for await (const row of exp.rows()) console.log(row.ChargePeriodStart, row.x_EstimatedCost);
   * // or, in Node: await (await client.usage.focusExport()).saveTo('focus.csv');
   * ```
   *
   * Errors before the body starts (400, 403, 429, ...) throw as usual. The
   * server streams rows as it reads them, so a failure part way through
   * ends the body early.
   */
  async focusExport(params: FocusExportParams = {}, options: DownloadOptions = {}): Promise<FocusExport> {
    const format = params.format ?? 'csv';
    const res = await this.client.getStream(
      '/partner/v1/usage/focus-export',
      { ...params },
      { signal: options.signal, accept: format === 'jsonl' ? 'application/x-ndjson' : 'text/csv' },
    );
    return new FocusExport(res, format);
  }
}

/** `/partner/v1/spend-alerts`: spend anomaly alerts. */
export class SpendAlertsResource {
  constructor(private readonly client: PartnerClient) {}

  /**
   * Alerts, newest day first, at most `limit` (server default 100, up to
   * 500). The endpoint has no cursor; narrow with `since` / `until` to
   * reach older alerts. Needs `spend-alerts:read`.
   */
  list(params: ListSpendAlertsParams = {}): Promise<SpendAlertList> {
    return this.client.get<SpendAlertList>('/partner/v1/spend-alerts', { ...params });
  }

  /** `GET /partner/v1/spend-alerts/{id}`. Throws `NotFoundError` if absent. Needs `spend-alerts:read`. */
  async get(alertId: string): Promise<SpendAlert> {
    const res = await this.client.get<{ data: SpendAlert }>(`/partner/v1/spend-alerts/${requireId(alertId, 'alertId')}`);
    return res.data;
  }

  /**
   * Mark an alert `acknowledged`. Acknowledging it again changes nothing,
   * so unlike other POSTs it is retried after a network error or 5xx.
   * Needs `spend-alerts:acknowledge`.
   */
  async acknowledge(alertId: string): Promise<SpendAlert> {
    const res = await this.client.send<{ data: SpendAlert }>(
      'POST',
      `/partner/v1/spend-alerts/${requireId(alertId, 'alertId')}/acknowledge`,
      { retrySafe: true },
    );
    return res.data;
  }
}

/** `/partner/v1/mcp`: MCP servers and tools seen in telemetry, and daily tool-call metering. Needs `mcp:read`. */
export class McpResource {
  constructor(private readonly client: PartnerClient) {}

  /** One page of observed MCP tools, most recently seen first (up to 1000 rows). */
  listToolsPage(params: ListMcpToolsParams = {}): Promise<McpToolPage> {
    return this.client.get<McpToolPage>('/partner/v1/mcp/tools', { ...params });
  }

  /**
   * Every observed MCP tool, page by page as you iterate. A row seen again
   * while you page moves to the front, so a long walk may yield it twice.
   */
  listTools(params: ListMcpToolsParams = {}): PageIterator<McpTool, McpToolPage> {
    const { cursor, ...rest } = params;
    return new PageIterator((next) => this.listToolsPage({ ...rest, cursor: next }), cursor ?? undefined);
  }

  /** One page of daily metering rows, newest day first. `meta.since` / `meta.until` give the window. */
  listUsagePage(params: ListMcpUsageParams = {}): Promise<McpUsagePage> {
    return this.client.get<McpUsagePage>('/partner/v1/mcp/usage', { ...params });
  }

  /**
   * Calls, errors, blocked calls, latency and bytes per tool per server per
   * UTC day (and per source), page by page. `since` defaults to 30 days ago.
   */
  listUsage(params: ListMcpUsageParams = {}): PageIterator<McpUsageDay, McpUsagePage> {
    const { cursor, ...rest } = params;
    return new PageIterator((next) => this.listUsagePage({ ...rest, cursor: next }), cursor ?? undefined);
  }
}

/**
 * `/partner/v1/learner`: routing learner snapshots and the route changes
 * they recommend. Reading needs `learner:read`; reviewing needs
 * `learner:review`. The learner never routes traffic, and accepting a
 * recommendation changes no routing: apply it by editing the route policy.
 */
export class LearnerResource {
  constructor(private readonly client: PartnerClient) {}

  /** Snapshots, newest first, without their state. No cursor: up to `limit` (default 50, at most 200). */
  listSnapshots(params: ListLearnerSnapshotsParams = {}): Promise<LearnerSnapshotList> {
    return this.client.get<LearnerSnapshotList>('/partner/v1/learner/snapshots', { ...params });
  }

  /**
   * One snapshot with its full state (`snapshot`, kept exactly as
   * uploaded). Throws `NotFoundError` if the tenant has no such version.
   */
  async getSnapshot(learnerVersion: string): Promise<LearnerSnapshot> {
    const res = await this.client.get<{ data: LearnerSnapshot }>(
      `/partner/v1/learner/snapshots/${requireId(learnerVersion, 'learnerVersion')}`,
    );
    return res.data;
  }

  /**
   * Recommendations, newest first, each with its off-policy projection
   * and, once accepted, realised vs. projected savings. No cursor: up to
   * `limit` (default 50, at most 200).
   */
  listRecommendations(params: ListLearnerRecommendationsParams = {}): Promise<LearnerRecommendationList> {
    return this.client.get<LearnerRecommendationList>('/partner/v1/learner/recommendations', { ...params });
  }

  /** One recommendation. Throws `NotFoundError` if absent. */
  async getRecommendation(recommendationId: string): Promise<LearnerRecommendation> {
    const res = await this.client.get<{ data: LearnerRecommendation }>(
      `/partner/v1/learner/recommendations/${requireId(recommendationId, 'recommendationId')}`,
    );
    return res.data;
  }

  /**
   * Accept or reject a pending recommendation. Decided once: a second
   * review throws a 409 `APIError`. Not retried on network errors or 5xx,
   * since a review that reached the server would then fail with 409.
   * Needs `learner:review`.
   */
  async reviewRecommendation(recommendationId: string, review: LearnerReviewRequest): Promise<LearnerRecommendation> {
    const res = await this.client.send<{ data: LearnerRecommendation }>(
      'POST',
      `/partner/v1/learner/recommendations/${requireId(recommendationId, 'recommendationId')}/review`,
      { body: review },
    );
    return res.data;
  }
}

/**
 * `/partner/v1/siem`: routing evidence and the audit trail as NDJSON in the
 * Microsoft Sentinel table schema, pulled with a cursor, for log
 * forwarders and SIEMs.
 */
export class SiemResource {
  constructor(private readonly client: PartnerClient) {}

  /**
   * One batch of per-call routing evidence (`AICall` records), in call
   * order. Only calls older than `settleSeconds` are returned. Needs
   * `evidence:read`. Pass the batch's `cursor` back on the next call;
   * `hasMore` means call again now.
   */
  async calls(params: SiemCallsParams = {}, options: DownloadOptions = {}): Promise<SiemBatch> {
    const res = await this.client.getStream('/partner/v1/siem/calls', { ...params }, {
      signal: options.signal,
      accept: 'application/x-ndjson',
    });
    return new SiemBatch(res);
  }

  /** One batch of audit trail rows (`Audit` records) in chain order, with chain hashes. Needs `audit:read`. */
  async audit(params: SiemAuditParams = {}, options: DownloadOptions = {}): Promise<SiemBatch> {
    const res = await this.client.getStream('/partner/v1/siem/audit', { ...params }, {
      signal: options.signal,
      accept: 'application/x-ndjson',
    });
    return new SiemBatch(res);
  }

  /**
   * Every call record from `cursor` (or `since`) until caught up, batch by
   * batch. Save `feed.cursor` afterwards for the next run.
   */
  callsFeed(params: SiemCallsParams = {}, options: DownloadOptions = {}): SiemFeed {
    const { cursor, since, ...rest } = params;
    return new SiemFeed(
      (next) => this.calls(next === null ? { ...rest, since } : { ...rest, cursor: next }, options),
      cursor ?? null,
    );
  }

  /** Every audit row from `cursor` (or `since`) until caught up. Save `feed.cursor` for the next run. */
  auditFeed(params: SiemAuditParams = {}, options: DownloadOptions = {}): SiemFeed {
    const { cursor, since, ...rest } = params;
    return new SiemFeed(
      (next) => this.audit(next === null ? { ...rest, since } : { ...rest, cursor: next }, options),
      cursor === undefined || cursor === null ? null : String(cursor),
    );
  }
}

const FINGERPRINT_RE = /^[0-9a-f]{16}$/;

/**
 * `/partner/v1/provider-credentials`: the tenant's provider admin keys,
 * which the collector uses to pull provider usage. Needs the
 * `provider-credentials:manage` scope (never granted by default). Keys are
 * write-only: every response is masked (`keyHint`), and the SDK never puts
 * the key in an error message.
 */
export class ProviderCredentialsResource {
  constructor(private readonly client: PartnerClient) {}

  /** Stored keys, masked. `meta.envFallback` lists providers the pull takes from the collector's environment. */
  list(): Promise<ProviderCredentialList> {
    return this.client.get<ProviderCredentialList>('/partner/v1/provider-credentials');
  }

  /** One stored key, masked. Throws `NotFoundError` if none is stored. */
  get(provider: CredentialProvider): Promise<ProviderCredential> {
    return this.client.get<ProviderCredential>(`/partner/v1/provider-credentials/${requireId(provider, 'provider')}`);
  }

  /**
   * Store a provider admin key. A 409 `APIError` means one is already
   * stored: `rotate` it instead. A 503 means the collector has no keyring.
   * Not retried on network errors or 5xx, since a retry would then be a 409.
   */
  create(provider: CredentialProvider, request: AdminKeyRequest): Promise<ProviderCredential> {
    return this.client.send<ProviderCredential>(
      'POST',
      `/partner/v1/provider-credentials/${requireId(provider, 'provider')}`,
      { body: adminKeyBody(request) },
    );
  }

  /** Replace a stored key. Throws `NotFoundError` if none is stored. Not retried on network errors or 5xx. */
  rotate(provider: CredentialProvider, request: AdminKeyRequest): Promise<ProviderCredential> {
    return this.client.send<ProviderCredential>(
      'POST',
      `/partner/v1/provider-credentials/${requireId(provider, 'provider')}/rotate`,
      { body: adminKeyBody(request) },
    );
  }

  /** Delete a stored key. Throws `NotFoundError` if none is stored. */
  async delete(provider: CredentialProvider): Promise<void> {
    await this.client.send<void>('DELETE', `/partner/v1/provider-credentials/${requireId(provider, 'provider')}`);
  }
}

function adminKeyBody(request: AdminKeyRequest): AdminKeyRequest {
  const adminKey = request?.adminKey;
  if (typeof adminKey !== 'string' || adminKey === '') {
    throw new PartnerError('adminKey must be a non-empty string');
  }
  return { adminKey };
}

/**
 * `/partner/v1/provider-key-fingerprints`: which SDK `api_key_fingerprint`
 * belongs to which provider key id, so provider usage corroborates SDK
 * telemetry. Needs `provider-credentials:manage`.
 */
export class ProviderKeyFingerprintsResource {
  constructor(private readonly client: PartnerClient) {}

  /** Every link of the tenant. */
  async list(): Promise<ProviderKeyFingerprintLink[]> {
    const res = await this.client.get<{ data: ProviderKeyFingerprintLink[] }>('/partner/v1/provider-key-fingerprints');
    return res.data;
  }

  /**
   * Link a provider key id to a fingerprint (first 16 hex characters of
   * sha256 of the key, computed where the key lives), creating or
   * replacing the link. Anything that is not 16 lowercase hex characters
   * (a raw key, for example) is refused before it is sent. Idempotent, so
   * it is retried like a GET. `meta.corroboratedAssets` counts the assets
   * the link verified.
   */
  put(provider: CredentialProvider, apiKeyId: string, request: FingerprintRequest): Promise<FingerprintLinkResult> {
    const apiKeyFingerprint = request?.apiKeyFingerprint;
    if (typeof apiKeyFingerprint !== 'string' || !FINGERPRINT_RE.test(apiKeyFingerprint)) {
      throw new PartnerError('apiKeyFingerprint must be 16 lowercase hex characters (never the key itself)');
    }
    return this.client.send<FingerprintLinkResult>(
      'PUT',
      `/partner/v1/provider-key-fingerprints/${requireId(provider, 'provider')}/${requireId(apiKeyId, 'apiKeyId')}`,
      { body: { apiKeyFingerprint } },
    );
  }

  /** Delete a link. Throws `NotFoundError` if absent. */
  async delete(provider: CredentialProvider, apiKeyId: string): Promise<void> {
    await this.client.send<void>(
      'DELETE',
      `/partner/v1/provider-key-fingerprints/${requireId(provider, 'provider')}/${requireId(apiKeyId, 'apiKeyId')}`,
    );
  }
}

const WEBHOOKS_PATH = '/api/v1/admin/webhooks';

/**
 * A view of `client` whose `get` and `send` first check that it holds a
 * tenant API key, so partner credentials never reach the admin API.
 */
function guardTenantKey(client: PartnerClient, what: string): PartnerClient {
  return {
    get: <T>(path: string, query?: Query): Promise<T> => {
      client.assertTenantKey(what);
      return client.get<T>(path, query);
    },
    send: <T>(method: string, path: string, opts?: SendOptions): Promise<T> => {
      client.assertTenantKey(what);
      return client.send<T>(method, path, opts);
    },
  } as PartnerClient;
}

/**
 * Webhook subscriptions and deliveries: the tenant admin API under
 * `/api/v1/admin/webhooks`. The tenant, not the partner, manages these, so
 * build the client with the tenant's API key:
 *
 * ```ts
 * const admin = new PartnerClient({ apiKey: process.env.PS_API_KEY, baseUrl: collectorUrl });
 * const sub = await admin.webhooks.subscriptions.create({ partnerId, url });
 * ```
 */
export class WebhooksResource {
  readonly subscriptions: WebhookSubscriptionsResource;
  readonly deliveries: WebhookDeliveriesResource;

  constructor(client: PartnerClient) {
    this.subscriptions = new WebhookSubscriptionsResource(client);
    this.deliveries = new WebhookDeliveriesResource(client);
  }
}

/** `/api/v1/admin/webhooks/subscriptions`. */
export class WebhookSubscriptionsResource {
  private readonly client: PartnerClient;

  constructor(client: PartnerClient) {
    this.client = guardTenantKey(client, 'webhooks.subscriptions');
  }

  /** Create a subscription. The response's `secret` is shown only this once. */
  create(params: CreateWebhookSubscriptionParams): Promise<WebhookSubscriptionWithSecret> {
    return this.client.send<WebhookSubscriptionWithSecret>('POST', `${WEBHOOKS_PATH}/subscriptions`, {
      body: params,
    });
  }

  /** Every subscription of the tenant, optionally for one partner credential. */
  async list(params: ListWebhookSubscriptionsParams = {}): Promise<WebhookSubscription[]> {
    const res = await this.client.get<{ data: WebhookSubscription[] }>(`${WEBHOOKS_PATH}/subscriptions`, {
      ...params,
    });
    return res.data;
  }

  get(subscriptionId: string): Promise<WebhookSubscription> {
    return this.client.get<WebhookSubscription>(
      `${WEBHOOKS_PATH}/subscriptions/${requireId(subscriptionId, 'subscriptionId')}`,
    );
  }

  /**
   * Change `url` or `eventTypes`, or pause (`isActive: false`) and resume
   * (`isActive: true`). Omitted fields keep their value.
   */
  update(subscriptionId: string, params: UpdateWebhookSubscriptionParams): Promise<WebhookSubscription> {
    const defined = Object.entries(params ?? {}).filter(([, v]) => v !== undefined);
    if (defined.length === 0) {
      throw new PartnerError('webhooks.subscriptions.update needs at least one of url, eventTypes, isActive');
    }
    return this.client.send<WebhookSubscription>(
      'PATCH',
      `${WEBHOOKS_PATH}/subscriptions/${requireId(subscriptionId, 'subscriptionId')}`,
      { body: Object.fromEntries(defined) },
    );
  }

  /** Delete a subscription and its queued and past deliveries. */
  async delete(subscriptionId: string): Promise<void> {
    await this.client.send<void>(
      'DELETE',
      `${WEBHOOKS_PATH}/subscriptions/${requireId(subscriptionId, 'subscriptionId')}`,
    );
  }

  /**
   * Issue a new signing secret, returned only this once. With
   * `gracePeriodSeconds` (up to 7 days) the old secret keeps signing too
   * until `rotationGraceEndsAt`. Not retried on network errors or 5xx.
   */
  rotateSecret(
    subscriptionId: string,
    params: RotateWebhookSecretParams = {},
  ): Promise<WebhookSubscriptionWithSecret> {
    return this.client.send<WebhookSubscriptionWithSecret>(
      'POST',
      `${WEBHOOKS_PATH}/subscriptions/${requireId(subscriptionId, 'subscriptionId')}/rotate-secret`,
      { body: params },
    );
  }
}

/** `/api/v1/admin/webhooks/deliveries`. */
export class WebhookDeliveriesResource {
  private readonly client: PartnerClient;

  constructor(client: PartnerClient) {
    this.client = guardTenantKey(client, 'webhooks.deliveries');
  }

  /** Deliveries by status, newest first. The server default is `dead` (dead letters). */
  async list(params: ListWebhookDeliveriesParams = {}): Promise<WebhookDelivery[]> {
    const res = await this.client.get<{ data: WebhookDelivery[] }>(`${WEBHOOKS_PATH}/deliveries`, {
      ...params,
    });
    return res.data;
  }

  /** Queue a dead or succeeded delivery again. */
  redeliver(deliveryId: string): Promise<WebhookDelivery> {
    return this.client.send<WebhookDelivery>(
      'POST',
      `${WEBHOOKS_PATH}/deliveries/${requireId(deliveryId, 'deliveryId')}/redeliver`,
    );
  }
}
