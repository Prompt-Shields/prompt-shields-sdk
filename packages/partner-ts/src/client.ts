import { camelizeKeys, snakeKey, snakeizeKeys } from './casing';
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
  AttestationList,
  AttestationRequest,
  AttestationResult,
  ChangeEvent,
  CreateWebhookSubscriptionParams,
  DataFlow,
  CycloneDxBom,
  ExportParams,
  ExportResult,
  HealthStatus,
  ListAssetsParams,
  ListAttestationsParams,
  ListChangesParams,
  ListWebhookDeliveriesParams,
  ListWebhookSubscriptionsParams,
  Page,
  RiskMapping,
  RisksParams,
  RotateWebhookSecretParams,
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

export interface SendOptions {
  /** Query parameters, camelCase keys (sent as snake_case). */
  query?: Query;
  /** JSON request body, camelCase keys (sent as snake_case). */
  body?: unknown;
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
  readonly telemetry: TelemetryResource;
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
    return this.request<T>(method.toUpperCase(), path, { query: opts.query, body: opts.body });
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
      const headers: Record<string, string> = {
        Accept: 'application/json',
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
        if (retrySafe && attempt < this.maxRetries) {
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
