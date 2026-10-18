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
export type LifecycleChange = OpenString<'stale' | 'retired' | 'reactivated'>;
export type AttestationState = OpenString<'unreviewed' | 'attested' | 'disputed' | 'retired'>;
/** Attestable fields. Sent and returned as these snake_case strings. */
export type AttestableField = OpenString<'asset' | 'owner' | 'data_classification'>;
export type AttestationActorType = OpenString<'tenant' | 'partner'>;
export type WebhookEventType = OpenString<
  'asset.created' | 'asset.changed' | 'asset.retired' | 'asset.attested'
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
  firstSeen: string | null;
  /** ISO 8601 timestamp. */
  lastSeen: string | null;
  /** Telemetry-driven lifecycle, independent of the governance `status`. */
  lifecycleState?: LifecycleState;
  /** ISO 8601 timestamp of the last `lifecycleState` change. */
  lifecycleChangedAt?: string | null;
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
  estimatedCostUsd: number;
  uniqueSessions: number;
  avgLatencyMs: number;
  period: UsagePeriod;
}

/** A telemetry source. `unidentified`: events sent without delivery metadata. */
export type TelemetrySource = OpenString<'sdk' | 'gateway' | 'otel' | 'unidentified'>;

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
  changedAt: string | null;
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

// Webhook subscriptions: the tenant admin API under /api/v1/admin/webhooks.
// Not part of docs/openapi.yaml; see docs/api-reference/sync/webhooks.mdx.

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
