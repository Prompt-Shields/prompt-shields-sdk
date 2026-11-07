/**
 * Public types for the Partner API.
 *
 * The wire format is snake_case (see docs/openapi.yaml). The SDK converts
 * response keys to camelCase, so every type here uses camelCase names.
 */

/** Widens a string-literal union so unknown future values still type-check. */
type OpenString<T extends string> = T | (string & {});

export type Vendor = OpenString<
  'openai' | 'anthropic' | 'google' | 'microsoft' | 'meta' | 'cohere' | 'mistral' | 'custom'
>;
export type Environment = OpenString<'production' | 'staging' | 'dev'>;
export type AssetStatus = OpenString<'active' | 'deprecated' | 'under_review'>;
export type DataClassification = OpenString<'public' | 'internal' | 'confidential' | 'restricted'>;
export type DiscoverySource = OpenString<
  | 'sdk'
  | 'gateway'
  | 'browser_extension'
  | 'macos_app'
  | 'platform_signal'
  | 'survey'
  | 'otel'
  | 'code_scan'
  | 'kong'
>;
export type Confidence = OpenString<'low' | 'medium' | 'high' | 'verified'>;
export type ChangeType = OpenString<'created' | 'updated' | 'deactivated'>;
export type RiskCategory = OpenString<
  'strategic' | 'operational' | 'compliance' | 'cyber' | 'reputational'
>;
export type RiskLevel = OpenString<'critical' | 'high' | 'medium' | 'low'>;
export type Framework = OpenString<'NIST_AI_RMF' | 'ISO_42001' | 'EU_AI_ACT' | 'custom'>;
export type RiskStatus = OpenString<'suggested' | 'confirmed' | 'rejected'>;
export type RiskSource = OpenString<'manual' | 'rules'>;
export type UsagePeriod = OpenString<'7d' | '30d' | '90d'>;
export type LifecycleState = OpenString<'active' | 'stale' | 'retired'>;
export type LifecycleChange = OpenString<'stale' | 'retired' | 'reactivated' | 'retired_in_use'>;
export type AttestationState = OpenString<'unreviewed' | 'attested' | 'disputed' | 'retired'>;
/** Attestable fields. Sent and returned as these snake_case strings. */
export type AttestableField = OpenString<'asset' | 'owner' | 'data_classification'>;
export type AttestationActorType = OpenString<'tenant' | 'partner'>;
export type WebhookEventType = OpenString<
  'asset.created' | 'asset.changed' | 'asset.retired' | 'asset.attested' | 'audit.*' | 'spend.anomaly'
>;
export type WebhookDeliveryStatus = OpenString<'pending' | 'succeeded' | 'dead'>;

/** A discovered AI use case. */
export interface AIAsset {
  id: string;
  vendor: Vendor;
  model: string | null;
  useCaseName: string | null;
  businessUnit: string | null;
  ownerEmail: string | null;
  environment: Environment | null;
  /** Cloud region, from the cloud inventory platform signal. */
  region?: string | null;
  /**
   * What the asset works on (e.g. `text`, `audio`, `image`), from the first
   * telemetry event that reported it. `null` when no event reported one.
   */
  /** Set from the first telemetry event that reports a known modality. */
  modality?: Modality | null;
  status: AssetStatus;
  dataClassification: DataClassification | null;
  discoverySource: DiscoverySource[];
  confidence: Confidence;
  /** ISO 8601 timestamp. */
  firstSeen: string;
  /** ISO 8601 timestamp. */
  lastSeen: string;
  /** Telemetry-driven lifecycle, independent of the governance `status`. */
  lifecycleState?: LifecycleState;
  /** ISO 8601 timestamp of the last `lifecycleState` change. */
  lifecycleChangedAt?: string | null;
  /** Who retired the asset (an `asset` attestation with state `retired`); null unless its owner did. */
  retiredBy?: string | null;
  /** ISO 8601 timestamp of the first sighting since the owner retired it ("still in use"). */
  retiredInUseAt?: string | null;
  /**
   * Human sign-off per attestable field. Keys are the wire field names
   * (`asset`, `owner`, `data_classification`), kept as-is so they match
   * `Attestation.field`.
   */
  attestation?: Record<string, AttestationStatus>;
}

/** Data lineage record for an asset. */
export interface DataFlow {
  id: string;
  sourceSystem: string;
  destinationSystem: string;
  dataClassification: DataClassification | null;
  direction: OpenString<'input' | 'output'>;
  detectedPiiTypes: string[] | null;
}

/** One signal in a rule-based risk suggestion's evidence. */
export interface RiskEvidenceSignal {
  signal: string;
  value: unknown;
  matched: string[];
  role: OpenString<'trigger' | 'supporting'>;
}

/** Why a rule fired. */
export interface RiskEvidence {
  summary?: string;
  ruleTitle?: string;
  ruleset?: string;
  signals?: RiskEvidenceSignal[];
  [key: string]: unknown;
}

/**
 * Risk assessment linked to an asset. `status: 'suggested'` rows come from
 * the rules engine and are suggestions, not determinations.
 */
export interface RiskMapping {
  id: string;
  riskCategory: RiskCategory;
  riskLevel: RiskLevel;
  framework: Framework | null;
  /** Hand-entered mappings are `confirmed`. */
  status: RiskStatus;
  source: RiskSource;
  /** Rule that produced the suggestion. */
  ruleId?: string | null;
  rulesetVersion?: string | null;
  /** Where in the framework the rule points, e.g. `Annex III, point 4(a)`. */
  frameworkRef?: string | null;
  confidence?: OpenString<'low' | 'medium' | 'high'> | null;
  evidence?: RiskEvidence | null;
  /** ISO 8601 timestamp. */
  reviewedAt?: string | null;
  /** The person who confirmed or rejected the mapping. */
  reviewedBy?: string | null;
  /** The reviewer's comment on the latest review. */
  reviewComment?: string | null;
}

/** Current attestation of one field (its newest attestation). */
export interface AttestationStatus {
  state: AttestationState;
  attestedBy: string | null;
  /** ISO 8601 timestamp. */
  attestedAt: string | null;
  comment: string | null;
  /** The registry value differs from the value that was attested. */
  valueChanged: boolean;
}

/** One immutable attestation record. */
export interface Attestation {
  id: string;
  assetId: string;
  field: AttestableField;
  state: AttestationState;
  /** The field's value when the attestation was made. */
  value: unknown;
  comment: string | null;
  attestedBy: string;
  /** ISO 8601 timestamp. */
  attestedAt: string;
  actorType: AttestationActorType;
  partnerId: string | null;
}

/** Body for `client.assets.attest()`. Needs the `attestations:write` scope. */
export interface AttestationRequest {
  field: AttestableField;
  state: AttestationState;
  /** The person signing off, e.g. an email. */
  attestedBy: string;
  /** Required when `state` is `disputed`. */
  comment?: string | null;
}

export interface AttestationsMeta {
  total: number;
  /** The asset's discovery confidence. Attestations never change it. */
  confidence: Confidence;
  /** Current state per attestable field, keyed by wire field name. */
  current: Record<string, AttestationStatus>;
}

export interface AttestationList {
  data: Attestation[];
  meta: AttestationsMeta;
}

export interface AttestationResult {
  data: Attestation;
  meta: Omit<AttestationsMeta, 'total'>;
}

/** Aggregated usage statistics for an asset. Never includes prompt content. */
export interface UsageSummary {
  totalRequests: number;
  totalTokensIn: number;
  totalTokensOut: number;
  /** Prompt tokens read from the provider's prompt cache (part of `totalTokensIn`). */
  totalTokensCached: number;
  /** Prompt tokens written to the provider's prompt cache (part of `totalTokensIn`). */
  totalTokensCacheWrite: number;
  /** Reasoning tokens (part of `totalTokensOut`). */
  totalTokensReasoning: number;
  /** `null` when no event in the period reported a cost. */
  estimatedCostUsd: number | null;
  uniqueSessions: number;
  /** `null` when no event in the period reported a latency. */
  avgLatencyMs: number | null;
  /**
   * Sum of `counterfactual_cost - cost` (USD) over events that reported both.
   * `null` when none did.
   */
  savingsUsd: number | null;
  /**
   * Share (0 to 1) of events reporting a fallback chain in which at least one
   * attempt failed. `null` when no event reported one.
   */
  fallbackRate: number | null;
  /**
   * Share (0 to 1) of events reporting a served jurisdiction that were served
   * in the EEA. `null` when no event reported one. By jurisdiction only (as
   * stated by the route policy), not a US CLOUD Act assessment.
   */
  sovereignShare: number | null;
  /**
   * Events a spend budget downgraded to a cheaper model. `null` when no event
   * reported a budget action.
   */
  budgetDowngradedRequests: number | null;
  /**
   * Events reported as blocked by a spend budget (SDK and gateway refuse such
   * calls without an event today, so 0). `null` when no event reported a
   * budget action.
   */
  budgetBlockedRequests: number | null;
  /**
   * Share (0 to 1) of the gateway's cache lookups that were hits (exact or
   * semantic), each lookup counted once. `null` when no lookup was reported.
   */
  cacheHitRate: number | null;
  /** Upstream cost (USD) gateway cache hits avoided. `null` when none reported it. */
  cacheSavedUsd: number | null;
  /**
   * The gateway's estimate of the prompt tokens cache lookups saved (from
   * the request size, not a provider count). `null` when none reported it.
   */
  cacheTokensSaved: number | null;
  /**
   * Events the SDK routing learner decided in auto mode (exploit, explore or
   * policy). 0 when it decided none.
   */
  learnerRequests: number;
  /**
   * Share (0 to 1) of all events that the routing learner decided. `null` when
   * there were no events.
   */
  learnerRoutedShare: number | null;
  /**
   * Share (0 to 1) of the learner-decided events on which it explored.
   * `null` when the learner decided none.
   */
  learnerExploreShare: number | null;
  /**
   * Events that reported implicit outcome signals (#44), including those that
   * reported none happened.
   */
  outcomeSignalsReported: number;
  /**
   * Events per outcome class, keyed by class name as sent (`max_tokens`,
   * `refusal`, `fallback`, ...; every class listed, 0 when none carried it).
   * `null` when no event reported outcome signals.
   */
  outcomeSignalCounts: Record<string, number> | null;
  period: UsagePeriod;
}

/** How `usage.savings()` buckets its results. */
export type SavingsGroupBy = OpenString<'none' | 'day' | 'week' | 'month' | 'asset' | 'use_case' | 'business_unit'>;

/**
 * Realised routing savings over a set of events. Only events that reported
 * both a cost and a counterfactual cost count towards the savings figures;
 * `counterfactualCoverage` says what share of requests that is.
 */
export interface SavingsTotals {
  totalRequests: number;
  /** Sum of cost over every event; `null` when none reported a cost. */
  estimatedCostUsd: number | null;
  /** Events that reported both `cost` and `counterfactual_cost`. */
  requestsWithCounterfactual: number;
  /** `requestsWithCounterfactual / totalRequests`; `null` with no requests. */
  counterfactualCoverage: number | null;
  /** Cost of the compared events; `null` when none. */
  comparedCostUsd: number | null;
  /** Counterfactual cost of the compared events; `null` when none. */
  counterfactualCostUsd: number | null;
  /** `counterfactualCostUsd - comparedCostUsd`; negative when routing cost more. */
  savingsUsd: number | null;
  /** `savingsUsd / counterfactualCostUsd`. */
  savingsRate: number | null;
}

/** Savings for one bucket. */
export interface SavingsBreakdown extends SavingsTotals {
  /** UTC bucket start date, asset id, use case or business unit; `null` when unset. */
  key: string | null;
}

export interface SavingsReport {
  period: UsagePeriod;
  groupBy: SavingsGroupBy;
  total: SavingsTotals;
  /** Empty when `groupBy` is `none`. */
  data: SavingsBreakdown[];
}

export interface SavingsParams {
  period?: UsagePeriod;
  groupBy?: SavingsGroupBy;
}

/**
 * Share of calls and spend served in a sovereign (EEA) jurisdiction. Events
 * without a reported jurisdiction are left out of both sides of each share.
 * By jurisdiction only (as stated by the route policy), not a US CLOUD Act
 * assessment.
 */
export interface SovereignShareTotals {
  totalRequests: number;
  requestsWithJurisdiction: number;
  sovereignRequests: number;
  /** `sovereignRequests / requestsWithJurisdiction`; `null` when no jurisdiction was reported. */
  requestShare: number | null;
  costWithJurisdictionUsd: number | null;
  sovereignCostUsd: number | null;
  /** `sovereignCostUsd / costWithJurisdictionUsd`. */
  spendShare: number | null;
}

/** Sovereign share for one sensitivity level (`null`: none reported). */
export interface SovereignShareByLevel extends SovereignShareTotals {
  sensitivityLevel: string | null;
}

export interface SovereignShareReport {
  period: UsagePeriod;
  /** Jurisdiction codes counted as sovereign. */
  sovereignJurisdictions: string[];
  total: SovereignShareTotals;
  bySensitivityLevel: SovereignShareByLevel[];
}

export interface SovereignShareParams {
  period?: UsagePeriod;
}

/** A telemetry source. `unidentified`: events sent without delivery metadata. */
export type TelemetrySource = OpenString<'sdk' | 'gateway' | 'otel' | 'kong' | 'unidentified'>;

/** Telemetry delivery for one source. */
export interface SourceCoverage {
  source: TelemetrySource;
  /** Distinct clients seen (SDK or gateway processes, OTel services). */
  clients: number;
  /** Events the collector stored. */
  receivedEvents: number;
  /** Events known lost: client-reported drops, and for `otel` spans the collector rejected. */
  droppedEvents: number;
  /** Events a client queued that neither arrived nor were reported dropped. */
  unaccountedEvents: number;
  /** received / (received + dropped + unaccounted), 0 to 1; `null` when unknown. */
  coverage: number | null;
  /** ISO 8601 timestamp. */
  lastSeen: string | null;
}

/** How complete the tenant's usage telemetry is, overall and per source. */
export interface TelemetryCoverage {
  coverage: number | null;
  receivedEvents: number;
  droppedEvents: number;
  unaccountedEvents: number;
  sources: SourceCoverage[];
}

/** One entry in the delta feed. */
export interface ChangeEvent {
  changeType: ChangeType;
  asset: AIAsset;
  /** Lifecycle transition since `since`, or `null` if none. */
  lifecycleChange?: LifecycleChange | null;
  /** Fields whose attestation changed since `since`. */
  attestationChanges?: string[];
  /** ISO 8601 timestamp. */
  changedAt: string;
  /** Fields that changed. `null` for created assets and until field-level audit ships. */
  changedFields: string[] | null;
}

export interface PaginationMeta {
  total: number;
  hasMore: boolean;
  nextCursor: string | null;
  /** Only set on the changes feed. Pass as `since` on the next sync. */
  syncToken?: string | null;
  /** Tenant of the credential. Only set on the changes feed. */
  tenantId?: string;
}

export interface Page<T> {
  data: T[];
  meta: PaginationMeta;
}

export interface ExportMeta {
  total: number;
  exportedAt: string;
  /** Pass as `since` to `client.changes.list()` for the next delta sync. */
  syncToken: string;
  /** Tenant of the credential that made the request. */
  tenantId?: string;
}

export interface ExportResult {
  data: AIAsset[];
  meta: ExportMeta;
}

export interface HealthStatus {
  status: string;
  version: string;
}

export interface TokenResponse {
  accessToken: string;
  tokenType: string;
  expiresIn: number;
  scope: string;
}

/** A timestamp: an ISO 8601 string, or a Date (sent as `toISOString()`). */
export type Timestamp = string | Date;

export interface PaginationParams {
  /** Records per page, 1 to 200. Server default is 50. */
  limit?: number;
  /** Cursor from `meta.nextCursor` of the previous page. `null` means the first page. */
  after?: string | null;
}

export interface ListAssetsParams extends PaginationParams {
  businessUnit?: string;
  status?: AssetStatus;
  lifecycleState?: LifecycleState;
  discoverySource?: DiscoverySource;
  /** Only assets first seen at or after this time. */
  since?: Timestamp;
  vendor?: Vendor;
  /** Minimum confidence level. */
  confidence?: Confidence;
  dataClassification?: DataClassification;
  /**
   * With `attestationState`: only assets whose current attestation of this
   * field is in that state (a review queue). Send both or neither.
   */
  attestationField?: AttestableField;
  /** With `attestationField`. `unreviewed` includes fields nobody has attested. */
  attestationState?: AttestationState;
}

export interface ListChangesParams extends PaginationParams {
  /** Required. Use the `syncToken` from the previous export or changes page. */
  since: Timestamp;
}

export interface UsageSummaryParams {
  period?: UsagePeriod;
}

export interface RisksParams {
  /** Only mappings with this status. The server default is `confirmed`. */
  status?: RiskStatus;
}

export interface ListAttestationsParams {
  /** Only attestations of this field. */
  field?: AttestableField;
  /** 1 to 500. Server default is 100. */
  limit?: number;
}

// Webhook subscriptions: the tenant admin API under /api/v1/admin/webhooks
// (the "Webhooks" tag in docs/openapi.yaml; see also
// docs/api-reference/sync/webhooks.mdx).

/** A webhook subscription. The signing secret is never included. */
export interface WebhookSubscription {
  id: string;
  partnerId: string;
  url: string;
  eventTypes: WebhookEventType[];
  description: string | null;
  isActive: boolean;
  /** Set while a rotated-out secret still signs deliveries. ISO 8601. */
  rotationGraceEndsAt: string | null;
  createdAt: string | null;
  updatedAt: string | null;
}

/** Returned on create and on secret rotation: the only time `secret` is shown. */
export interface WebhookSubscriptionWithSecret extends WebhookSubscription {
  /** `whsec_...`. Save it now; it is not shown again. */
  secret: string;
  warning: string;
}

export interface CreateWebhookSubscriptionParams {
  /** Partner credential that receives the events. Needs `changes:read`. */
  partnerId: string;
  /** `https` URL with a public host. */
  url: string;
  /** Defaults to every event type. */
  eventTypes?: WebhookEventType[];
  description?: string | null;
}

/** Fields to change. Omitted fields keep their value; give at least one. */
export interface UpdateWebhookSubscriptionParams {
  url?: string;
  eventTypes?: WebhookEventType[];
  /** `false` pauses the subscription, `true` resumes it. */
  isActive?: boolean;
}

export interface RotateWebhookSecretParams {
  /** Keep the old secret signing too for this long (0 to 604800). Default 0. */
  gracePeriodSeconds?: number;
}

export interface ListWebhookSubscriptionsParams {
  partnerId?: string;
}

export interface WebhookDelivery {
  id: string;
  subscriptionId: string;
  eventId: string;
  eventType: WebhookEventType;
  assetId: string | null;
  status: WebhookDeliveryStatus;
  attempts: number;
  lastStatusCode: number | null;
  lastError: string | null;
  nextAttemptAt: string | null;
  lastAttemptAt: string | null;
  deliveredAt: string | null;
  createdAt: string | null;
}

export interface ListWebhookDeliveriesParams {
  /** Server default is `dead` (the dead-letter view). */
  status?: WebhookDeliveryStatus;
  subscriptionId?: string;
  /** 1 to 200. Server default is 50. */
  limit?: number;
}

/** What an asset works on (chat, embeddings and rerank are `text`; OCR is `document`). */
export type Modality = 'text' | 'image' | 'audio' | 'document';

export interface ExportParams {
  /** `json` (default) is the registry envelope; `cyclonedx` is a CycloneDX 1.6 AI-BOM. */
  format?: 'json' | 'cyclonedx';
}

/**
 * A CycloneDX 1.6 BOM (`format: 'cyclonedx'`), returned as the server sent it.
 * Prompt Shields data is in each component's `promptshields:<name>` properties.
 */
export interface CycloneDxBom {
  bomFormat: 'CycloneDX';
  specVersion: string;
  components?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

// Risk mapping reviews (partner routes under /partner/v1/assets/{id}/risks/{risk_id}).

/** The decision in a review. */
export type RiskReviewStatus = OpenString<'confirmed' | 'rejected'>;
/** The credential that recorded a review. */
export type RiskReviewActorType = OpenString<'tenant_key' | 'admin_token' | 'partner'>;

/** One immutable review of a risk mapping (append-only history). */
export interface RiskMappingReview {
  id: string;
  riskMappingId: string;
  /** The asset the mapping belonged to when it was reviewed. */
  assetId: string;
  status: RiskReviewStatus;
  /** Status before this review; `null` for reviews made before the history existed. */
  previousStatus: RiskStatus | null;
  comment: string | null;
  reviewedBy: string;
  /** ISO 8601 timestamp. */
  reviewedAt: string;
  actorType: RiskReviewActorType;
  partnerId: string | null;
  /**
   * The mapping as reviewed (`riskCategory`, `riskLevel`, `framework`,
   * `ruleId`, `rulesetVersion`, `frameworkRef`), camelCased.
   */
  mapping: Record<string, unknown> | null;
}

/** Body for `client.assets.reviewRisk()`. Needs the `risks:review` scope. */
export interface RiskReviewRequest {
  status: RiskReviewStatus;
  /** The person deciding, e.g. an email (1 to 320 characters). */
  reviewedBy: string;
  /** Why the mapping was confirmed or rejected (up to 4000 characters). */
  comment?: string | null;
}

export interface RiskReviewHistoryMeta {
  total: number;
  /** The mapping now; `null` if it no longer exists. */
  current: RiskMapping | null;
}

/** `client.assets.riskReviews()`: newest first. */
export interface RiskReviewHistory {
  data: RiskMappingReview[];
  meta: RiskReviewHistoryMeta;
}

/** `client.assets.reviewRisk()`: the review recorded and the mapping now. */
export interface RiskReviewResult {
  data: RiskMappingReview;
  meta: { current: RiskMapping };
}

export interface ListRiskReviewsParams {
  /** 1 to 500. Server default is 100. */
  limit?: number;
}

// Spend anomaly alerts (/partner/v1/spend-alerts).

export type SpendAlertGroupBy = OpenString<'use_case' | 'asset'>;
export type SpendAlertSeverity = OpenString<'warning' | 'critical'>;
export type SpendAlertStatus = OpenString<'open' | 'acknowledged'>;

/**
 * One UTC day whose spend for a use case or asset was far above its EWMA
 * baseline. Amounts are USD.
 */
export interface SpendAlert {
  id: string;
  groupBy: SpendAlertGroupBy;
  /** The use case name (`(none)` for assets without one), or the asset id. */
  groupKey: string;
  /** UTC day, `YYYY-MM-DD`. */
  day: string;
  /** The day's spend. */
  observedCost: number;
  /** EWMA of the days before. */
  baselineCost: number;
  /** The standard deviation used (after its floors). */
  baselineSigma: number;
  zScore: number;
  thresholdSigma: number;
  historyDays: number;
  severity: SpendAlertSeverity;
  status: SpendAlertStatus;
  /** ISO 8601 timestamp. */
  acknowledgedAt: string | null;
  /** `partner:<credential id>`, never a person. */
  acknowledgedBy: string | null;
  /** ISO 8601 timestamp. */
  createdAt: string;
}

export interface SpendAlertListMeta {
  /** Alerts in `data`. */
  total: number;
}

export interface SpendAlertList {
  data: SpendAlert[];
  meta: SpendAlertListMeta;
}

export interface ListSpendAlertsParams {
  status?: SpendAlertStatus;
  severity?: SpendAlertSeverity;
  /** First UTC day to include, `YYYY-MM-DD`. */
  since?: string;
  /** Last UTC day to include, `YYYY-MM-DD`. */
  until?: string;
  /** 1 to 500. Server default is 100. */
  limit?: number;
}

/**
 * Per-tenant spend anomaly thresholds (tenant `settings.spend_anomaly`).
 * An operator setting: the Partner API does not read or change it. Every
 * field falls back to its default when omitted.
 */
export interface SpendAnomalySettings {
  /** Default true. */
  enabled?: boolean;
  /** Default `use_case`. */
  groupBy?: SpendAlertGroupBy;
  /** EWMA smoothing factor, 0.01 to 1. Default 0.3. */
  alpha?: number;
  /** Alert at this many standard deviations above the baseline (0.5 to 20, default 3); 2x is `critical`. */
  sigma?: number;
  /** 3 to 90. Default 28. */
  baselineDays?: number;
  /** Days of spend a group needs before it is judged (2 to 90, default 7). */
  minHistoryDays?: number;
  /** The day must also be at least this many USD above the baseline. Default 1. */
  minDeltaUsd?: number;
  /** The standard deviation used is at least this fraction of the baseline. Default 0.1. */
  minSigmaFraction?: number;
}

// Provider admin credentials (/partner/v1/provider-credentials and
// /partner/v1/provider-key-fingerprints). Scope `provider-credentials:manage`.

/** A provider whose usage API the collector can pull with a stored admin key. */
export type CredentialProvider = OpenString<'openai' | 'anthropic'>;

/** A stored provider admin key, masked. The key itself is never returned. */
export interface ProviderCredential {
  provider: CredentialProvider;
  /** `****` plus the key's last four characters. */
  keyHint: string;
  /** Id of the collector keyring key the admin key is encrypted under. */
  keyId: string | null;
  /** ISO 8601 timestamp. */
  createdAt: string | null;
  /** ISO 8601 timestamp. */
  rotatedAt: string | null;
}

/** Body of `providerCredentials.create` and `rotate`. */
export interface AdminKeyRequest {
  /**
   * The provider's organization admin key (20 to 512 printable ASCII
   * characters, no whitespace). Write-only: never returned, logged or
   * audited.
   */
  adminKey: string;
}

export interface ProviderCredentialListMeta {
  /** Providers a key can be stored for. */
  providers: string[];
  /** Providers with no stored key that the usage pull takes from `PS_PROVIDER_ADMIN_KEYS`. */
  envFallback: string[];
}

export interface ProviderCredentialList {
  data: ProviderCredential[];
  meta: ProviderCredentialListMeta;
}

/** Links a provider's opaque API key id to the SDK's `api_key_fingerprint`. */
export interface ProviderKeyFingerprintLink {
  provider: CredentialProvider;
  /** The key id the provider's usage API reports. */
  apiKeyId: string;
  /** First 16 hex characters of sha256(api key), computed where the key lives. */
  apiKeyFingerprint: string;
  /** ISO 8601 timestamp. */
  createdAt: string | null;
}

/** Body of `providerKeyFingerprints.put`. */
export interface FingerprintRequest {
  /** First 16 lowercase hex characters of sha256(api key). Anything else is refused with a 400. */
  apiKeyFingerprint: string;
}

export interface FingerprintLinkMeta {
  /** Platform_signal assets that gained a corroborating source. */
  corroboratedAssets: number;
}

/** The link, and how many assets it corroborated. */
export interface FingerprintLinkResult {
  data: ProviderKeyFingerprintLink;
  meta: FingerprintLinkMeta;
}

// Usage time series (/partner/v1/usage/series and its export).

export type UsageSeriesGroupBy = OpenString<
  'none' | 'user' | 'project' | 'model' | 'provider' | 'api_surface' | 'asset'
>;
export type UsageSeriesGranularity = OpenString<'hour' | 'day' | 'week'>;
export type UsageSeriesExportFormat = OpenString<'csv' | 'jsonl'>;

export interface UsageSeriesParams {
  /**
   * Split each bucket by this dimension (`key` in each point). `user` is the
   * salted user id hash, never a raw id; `project` the asset's use case.
   * Default `none`.
   */
  groupBy?: UsageSeriesGroupBy;
  /** UTC hour, day (default) or ISO week starting Monday. */
  granularity?: UsageSeriesGranularity;
  /** Inclusive start, rounded down to a whole bucket. */
  from?: Timestamp;
  /** Exclusive end, rounded up to a whole bucket. Default: now. */
  to?: Timestamp;
  /** Points per page (server default 1000, up to 10000). */
  limit?: number;
  /** `meta.nextCursor` of the previous page. */
  cursor?: string;
}

export interface UsageSeriesExportParams {
  groupBy?: UsageSeriesGroupBy;
  granularity?: UsageSeriesGranularity;
  from?: Timestamp;
  to?: Timestamp;
  /** `csv` (default, with a header row) or `jsonl` (one JSON object per line). */
  format?: UsageSeriesExportFormat;
}

/**
 * One bucket of the usage time series. `costUsd` is `null` when no event
 * reported a cost, `cacheSavedUsd` and `cacheHitRate` when none reported a
 * cache lookup, `errorRate` when none reported per-attempt metadata.
 */
export interface UsageSeriesPoint {
  /** UTC start of the hour, day or week. */
  bucketStart: string;
  /** Value of `groupBy`; `null` for `none` or when not reported. */
  key: string | null;
  requests: number;
  tokensIn: number;
  tokensOut: number;
  tokensCached: number;
  tokensCacheWrite: number;
  tokensReasoning: number;
  costUsd: number | null;
  cacheSavedUsd: number | null;
  cacheHitRate: number | null;
  errorRate: number | null;
}

export interface UsageSeriesMeta {
  groupBy: UsageSeriesGroupBy;
  granularity: UsageSeriesGranularity;
  /** Effective inclusive start, after rounding and the retention clamp. */
  from: string;
  /** Effective exclusive end. */
  to: string;
  /** When the tenant's rollups were last refreshed; `null` before the first run. */
  rolledUpAt: string | null;
  hasMore: boolean;
  nextCursor: string | null;
}

export interface UsageSeries {
  data: UsageSeriesPoint[];
  meta: UsageSeriesMeta;
}

// FinOps FOCUS cost export (/partner/v1/usage/focus-export).

export type FocusExportFormat = OpenString<'csv' | 'jsonl'>;
export type FocusGranularity = OpenString<'hour' | 'day'>;
export type FocusCostMode = OpenString<'estimated' | 'merge_safe'>;

export interface FocusExportParams {
  /** Inclusive start. Default: 30 days before `end`. */
  start?: Timestamp;
  /** Exclusive end. Default: now. The window may span at most 366 days. */
  end?: Timestamp;
  /** `csv` (default, with a header row) or `jsonl` (one JSON object per line). */
  format?: FocusExportFormat;
  /** Charge period of each row (UTC). Default `day`. */
  granularity?: FocusGranularity;
  /**
   * `estimated` (default): the FOCUS cost columns carry the estimated cost.
   * `merge_safe`: they are 0 and the estimate is only in `x_EstimatedCost`.
   */
  costMode?: FocusCostMode;
}

/**
 * One FOCUS v1.4 Cost and Usage row from a `jsonl` export. The column names
 * are FOCUS's own and are kept exactly as sent (not camelCased).
 */
export interface FocusCostRow {
  BilledCost: number;
  EffectiveCost: number;
  ListCost: number;
  ContractedCost: number;
  BillingCurrency: string;
  BillingAccountId: string;
  BillingAccountName: string | null;
  BillingAccountType: string;
  BillingPeriodStart: string;
  BillingPeriodEnd: string;
  ChargePeriodStart: string;
  ChargePeriodEnd: string;
  ChargeCategory: string;
  ChargeClass: string | null;
  ChargeDescription: string | null;
  ChargeFrequency: string;
  ServiceProviderName: string;
  HostProviderName: string | null;
  InvoiceIssuerName: string;
  ServiceName: string;
  ServiceCategory: string;
  ServiceSubcategory: string;
  ResourceId: string | null;
  ResourceName: string | null;
  ResourceType: string | null;
  RegionId: string | null;
  RegionName: string | null;
  AvailabilityZone: string | null;
  SkuId: string | null;
  PricingQuantity: number | null;
  PricingUnit: string | null;
  ConsumedQuantity: number | null;
  ConsumedUnit: string | null;
  AllocatedMethodDetails: Record<string, unknown> | null;
  Tags: Record<string, string> | null;
  /** Estimated cost in USD, in both cost modes. */
  x_EstimatedCost: number;
  /** Calls in the row. */
  x_RequestCount: number;
  x_InputTokens: number | null;
  x_OutputTokens: number | null;
  /** Model that served the calls (same as `SkuId`). */
  x_ModelName: string | null;
  x_BusinessUnit: string | null;
  x_UseCaseName: string | null;
  x_Environment: string | null;
}

/** Options for `client.usage.focusExport()` and `client.getStream()` that are not query parameters. */
export interface DownloadOptions {
  /**
   * Cancels the request, and the download once it has started. The
   * client's `timeoutMs` only covers the wait for the response headers;
   * reading the body has no timeout, so use this to bound a download.
   */
  signal?: AbortSignal;
}

// MCP observability (/partner/v1/mcp). Needs `mcp:read`.

/** Where an MCP tool call was reported from. */
export type McpSource = OpenString<'sdk' | 'gateway' | 'otel' | 'scan'>;

/**
 * One observed (source, server, tool, calling service, environment, user)
 * combination, with lifetime counts. Names are reported by MCP servers and
 * apps: escape them when rendering.
 */
export interface McpTool {
  serverName: string;
  toolName: string;
  source: McpSource;
  callingService: string | null;
  environment: string | null;
  transport: string | null;
  callCount: number;
  errorCount: number;
  /** Calls a guardrail blocked at the gateway (not errors). */
  blockedCount: number;
  /** Pseudonymous caller (the gateway's OIDC subject hash). */
  userHash: string | null;
  userGroup: string | null;
  avgLatencyMs: number | null;
  /** ISO 8601 timestamp. */
  firstSeen: string | null;
  /** ISO 8601 timestamp. */
  lastSeen: string | null;
  /** The server's registry asset (vendor `mcp`); `null` for server `unknown`. */
  assetId: string | null;
  /** The server asset's owner, from the registry or the gateway's server config. */
  ownerEmail: string | null;
}

/** Tool-call metering for one MCP tool on one server on one UTC day, per source. */
export interface McpUsageDay {
  /** UTC day, `YYYY-MM-DD`. */
  day: string;
  source: McpSource;
  serverName: string;
  toolName: string;
  callCount: number;
  errorCount: number;
  blockedCount: number;
  latencyMsTotal: number;
  /** Calls that reported a latency. */
  latencyCount: number;
  avgLatencyMs: number | null;
  /** Request body bytes, summed over the calls that reported bytes. */
  requestBytes: number;
  /** Response message bytes, summed over the calls that reported bytes. */
  responseBytes: number;
  /** Calls that reported bytes. */
  bytesCount: number;
  assetId: string | null;
  ownerEmail: string | null;
}

/** Cursor meta of the MCP pages (no `total`). */
export interface McpPageMeta {
  hasMore: boolean;
  /** Opaque; pass as `cursor` for the next page. */
  nextCursor: string | null;
}

export interface McpToolPage {
  data: McpTool[];
  meta: McpPageMeta;
}

export interface McpUsagePageMeta extends McpPageMeta {
  /** First UTC day of the window, `YYYY-MM-DD`. */
  since: string;
  /** Last UTC day, inclusive, or `null` for up to today. */
  until: string | null;
}

export interface McpUsagePage {
  data: McpUsageDay[];
  meta: McpUsagePageMeta;
}

export interface ListMcpToolsParams {
  serverName?: string;
  source?: McpSource;
  /** Rows per page, 1 to 1000. Server default is 200. */
  limit?: number;
  /** `meta.nextCursor` of the previous page. */
  cursor?: string | null;
}

export interface ListMcpUsageParams {
  /** First UTC day, `YYYY-MM-DD`. Server default is 30 days ago. */
  since?: string;
  /** Last UTC day, inclusive, `YYYY-MM-DD`. */
  until?: string;
  toolName?: string;
  serverName?: string;
  source?: McpSource;
  /** Rows per page, 1 to 1000. Server default is 200. */
  limit?: number;
  /** `meta.nextCursor` of the previous page. */
  cursor?: string | null;
}

// Routing learner snapshots and recommendations (/partner/v1/learner).

export type LearnerMode = OpenString<'recommend'>;
export type LearnerRecommendationStatus = OpenString<'pending' | 'accepted' | 'rejected' | 'superseded'>;
export type LearnerDecision = OpenString<'accepted' | 'rejected'>;

/**
 * One routing learner snapshot, without its state. The learner runs in
 * the customer's environment in `recommend` mode and never routes traffic.
 */
export interface LearnerSnapshotSummary {
  id: string;
  /** Content hash of the snapshot, `lv1-` and 16 hex digits. */
  learnerVersion: string;
  /** The snapshot this one was warm-started from. */
  parentVersion: string | null;
  mode: LearnerMode;
  /** E.g. `linear-thompson.v1`. */
  algorithm: string;
  /** E.g. `ps-prompt-features.v1`. */
  featureSchema: string | null;
  /** The route policy whose candidates constrained the learner. */
  policyVersion: string | null;
  /** SHA-256 of the training rows. */
  dataFingerprint: string;
  trainingRows: number;
  /** Rows with a quality signal. */
  scoredRows: number;
  /** Number of recommendations in the snapshot. */
  recommendations: number;
  /** ISO 8601 timestamp. */
  createdAt: string;
}

/** `client.learner.getSnapshot()`: a snapshot with its full state. */
export interface LearnerSnapshot extends LearnerSnapshotSummary {
  /**
   * The whole document as uploaded (`ps-learner-snapshot.v1`), keys kept
   * exactly as sent (not camelCased): config, reward weights, per-group
   * candidates and status, per-arm statistics and the recommendations.
   */
  snapshot: Record<string, unknown>;
}

/** Off-policy estimate of routing a group's logged traffic to one model. */
export interface LearnerArmProjection {
  model?: string;
  quality?: number;
  costPerCall?: number;
  latencyMs?: number;
  /** quality − λ·cost − μ·latency(s). */
  reward?: number;
  /** Direct method (`dm`), or doubly robust (`dr`) when logged propensities exist. */
  qualityMethod?: OpenString<'dm' | 'dr'>;
  /** Counterfactual price of the logged tokens, or the cost regression. */
  costMethod?: OpenString<'priced' | 'regression'>;
}

/** Off-policy projection over a group's logged traffic. */
export interface LearnerProjection {
  calls?: number;
  current?: LearnerArmProjection;
  recommended?: LearnerArmProjection;
  qualityDelta?: number;
  rewardDelta?: number;
  savingsPerCall?: number;
  /** `savingsPerCall` × `calls` over the logged window. */
  savingsTotal?: number;
  savingsPct?: number;
  /** Share of Thompson-sampling draws in which the recommended model was best. */
  probBest?: number;
  /** Samples per model; keys are model names, kept as sent. */
  armSamples?: Record<string, number>;
}

/** Realised vs. projected, for an accepted recommendation. */
export interface LearnerRealised {
  /** ISO 8601 timestamp of the decision. */
  since: string;
  calls: number;
  callsOnRecommended: number;
  /** Share of calls served by the recommended model. */
  adoption: number | null;
  costPerCall: number | null;
  /** Projected current cost per call minus realised cost per call. */
  savingsPerCall: number | null;
  savingsPct: number | null;
  projectedSavingsPct: number | null;
  /** (realised − projected savings share) × 100. */
  deltaPoints: number | null;
  /** Explicit feedback on calls served by the recommended model. */
  feedbackCount: number;
  feedbackQuality: number | null;
  projectedQuality: number | null;
}

/**
 * A route change the learner proposes for one (use case, sensitivity),
 * always one of that group's policy-compliant candidates. Accepting it
 * changes no routing: the reviewer edits the route policy.
 */
export interface LearnerRecommendation {
  id: string;
  snapshotId: string;
  learnerVersion: string;
  useCase: string | null;
  sensitivityLevel: string;
  /** The model most of the group's logged traffic used. */
  currentModel: string;
  recommendedModel: string;
  /** The models the route policy allows for the group (`provider:model`). */
  candidates: string[];
  projection: LearnerProjection;
  status: LearnerRecommendationStatus;
  reviewedBy: string | null;
  /** ISO 8601 timestamp. */
  reviewedAt: string | null;
  reviewComment: string | null;
  reviewerActorType: OpenString<'partner'> | null;
  /** ISO 8601 timestamp. */
  createdAt: string;
  /** `null` unless accepted. */
  realised: LearnerRealised | null;
}

/** Body for `client.learner.reviewRecommendation()`. Needs the `learner:review` scope. */
export interface LearnerReviewRequest {
  decision: LearnerDecision;
  /** The person deciding, e.g. an email (1 to 320 characters). */
  reviewedBy: string;
  /** Why it was accepted or rejected (up to 4000 characters). */
  comment?: string | null;
}

/** Result of `POST /ingest/learner-snapshots` (the Python SDK's `ps-learn`); not called by this SDK. */
export interface LearnerSnapshotUploadResult {
  learnerVersion: string;
  snapshotId: string;
  /** False when this snapshot was already stored. */
  created: boolean;
  recommendations: number;
  superseded: number;
}

export interface LearnerListMeta {
  total: number;
}

export interface LearnerSnapshotList {
  data: LearnerSnapshotSummary[];
  meta: LearnerListMeta;
}

export interface LearnerRecommendationList {
  data: LearnerRecommendation[];
  meta: LearnerListMeta;
}

export interface ListLearnerSnapshotsParams {
  /** 1 to 200. Server default is 50. */
  limit?: number;
}

export interface ListLearnerRecommendationsParams {
  status?: LearnerRecommendationStatus;
  useCase?: string;
  learnerVersion?: string;
  /** 1 to 200. Server default is 50. */
  limit?: number;
}

// SIEM pull (/partner/v1/siem): NDJSON in the Sentinel table schema.

/** `AICall` (routing evidence of one model call) or `Audit` (one audit trail row). */
export type SiemRecordType = OpenString<'AICall' | 'Audit'>;

/**
 * One NDJSON record in the Microsoft Sentinel table schema
 * `PromptShieldsAudit_CL`. Column names are kept exactly as sent
 * (`TimeGenerated`, `PsServedModel`, `PsAuditRowHash`, ...); columns
 * without a value are left out.
 */
export interface SiemRecord {
  TimeGenerated: string;
  EventUid: string;
  EventType: SiemRecordType;
  PsTenantId: string;
  [column: string]: unknown;
}

export interface SiemCallsParams {
  /** `cursor` of the previous batch. */
  cursor?: string | null;
  /** Start time when there is no cursor. */
  since?: Timestamp;
  /** 1 to 5000. Server default is 1000. */
  limit?: number;
  /** Leave out calls newer than this (0 to 86400, server default 300), so late events are not skipped. */
  settleSeconds?: number;
}

// ── Audit trail (client.audit) ─────────────────────────────────────────

export interface AuditTrailActor {
  /** `tenant_key`, `partner`, `operator` or `system`. */
  type: string;
  /** Key prefix, partner credential id or job name; never a secret. */
  id: string | null;
}

export interface AuditTrailTarget {
  type: string | null;
  id: string | null;
}

/** One row of the tenant's hash-chained audit trail (`GET /partner/v1/audit`). */
export interface AuditTrailEvent {
  schemaVersion: string;
  id: string;
  tenantId: string;
  seq: number;
  createdAt: string;
  /** e.g. `user.erase`, `provider_credential.rotate`. */
  action: string;
  outcome: 'success' | 'failure';
  actor: AuditTrailActor;
  target: AuditTrailTarget;
  requestId: string | null;
  statusCode: number | null;
  /** Ids and counts, kept exactly as sent (keys not camelCased), so the chain can be re-verified. */
  details: Record<string, unknown>;
  prevHash: string;
  rowHash: string;
  hashVersion: number;
}

export interface AuditTrailPageMeta {
  hasMore: boolean;
  /** Pass as `cursor` for the next page. */
  nextCursor: string | null;
}

export interface AuditTrailPage {
  data: AuditTrailEvent[];
  meta: AuditTrailPageMeta;
}

export interface ListAuditParams {
  /** `meta.nextCursor` of the previous page. */
  cursor?: string | null;
  /** First `createdAt` to include; ignored with a cursor. */
  since?: Timestamp;
  /** `createdAt` before which to stop (exclusive). */
  until?: Timestamp;
  /** Only rows with this action, e.g. `user.erase`. */
  action?: string;
  /** Rows per page, 1 to 1000. Server default is 200. */
  limit?: number;
}

// ── Gateway security events (client.securityEvents) ────────────────────

/** One stored gateway security event (`GET /partner/v1/security-events`). */
export interface SecurityEventRow {
  id: string;
  /** When the collector stored it (page order). */
  receivedAt: string;
  /**
   * The event in the gateway's schema v1 field names, kept exactly as sent
   * (`@timestamp`, `event.name`, `severity`, `action`, `signature`, `ps.*`, ...).
   */
  event: Record<string, unknown>;
}

export interface SecurityEventPageMeta {
  hasMore: boolean;
  /** Opaque; pass as `cursor` for the next page. */
  nextCursor: string | null;
}

export interface SecurityEventPage {
  data: SecurityEventRow[];
  meta: SecurityEventPageMeta;
}

export interface ListSecurityEventsParams {
  /** `meta.nextCursor` of the previous page. */
  cursor?: string | null;
  /** First event time to include. */
  since?: Timestamp;
  /** Event time before which to stop (exclusive). */
  until?: Timestamp;
  /** Only this `event.name`, e.g. `injection.detected`. */
  name?: string;
  /** Only this severity: `informational`, `low`, `medium` or `high`. */
  severity?: string;
  /** Only this action: `allowed`, `blocked` or `failure`. */
  action?: string;
  /** Events per page, 1 to 1000. Server default is 200. */
  limit?: number;
}

export interface SiemAuditParams {
  /** `cursor` of the previous batch (the `seq` of its last row). */
  cursor?: string | number | null;
  /** Start time when there is no cursor. */
  since?: Timestamp;
  /** 1 to 5000. Server default is 1000. */
  limit?: number;
}

/** A team in the tenant (`GET /partner/v1/teams`). Needs `members:read`. */
export interface Team {
  id: string;
  /** Unique in the tenant. */
  name: string;
  description: string | null;
  createdAt: string;
  updatedAt: string | null;
}

/** A project, owned by a team (`GET /partner/v1/projects`). Needs `members:read`. */
export interface Project {
  id: string;
  teamId: string;
  /** Unique in the tenant. */
  name: string;
  description: string | null;
  createdAt: string;
  updatedAt: string | null;
}

export interface ListProjectsParams {
  /** Only this team's projects. */
  teamId?: string;
}
