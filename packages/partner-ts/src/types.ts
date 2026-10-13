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
  'sdk' | 'gateway' | 'browser_extension' | 'macos_app' | 'platform_signal' | 'survey'
>;
export type Confidence = OpenString<'low' | 'medium' | 'high' | 'verified'>;
export type ChangeType = OpenString<'created' | 'updated' | 'deactivated'>;
export type RiskCategory = OpenString<
  'strategic' | 'operational' | 'compliance' | 'cyber' | 'reputational'
>;
export type RiskLevel = OpenString<'critical' | 'high' | 'medium' | 'low'>;
export type Framework = OpenString<'NIST_AI_RMF' | 'ISO_42001' | 'EU_AI_ACT' | 'custom'>;
export type UsagePeriod = OpenString<'7d' | '30d' | '90d'>;

/** A discovered AI use case. */
export interface AIAsset {
  id: string;
  vendor: Vendor;
  model: string | null;
  useCaseName: string | null;
  businessUnit: string | null;
  ownerEmail: string | null;
  environment: Environment | null;
  status: AssetStatus;
  dataClassification: DataClassification | null;
  discoverySource: DiscoverySource[];
  confidence: Confidence;
  /** ISO 8601 timestamp. */
  firstSeen: string | null;
  /** ISO 8601 timestamp. */
  lastSeen: string | null;
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

/** Risk assessment linked to an asset. */
export interface RiskMapping {
  id: string;
  riskCategory: RiskCategory;
  riskLevel: RiskLevel;
  framework: Framework | null;
}

/**
 * Aggregated usage statistics for an asset.
 *
 * The collector endpoint for this is tracked in #99; until it ships the
 * request returns 404.
 */
export interface UsageSummary {
  totalRequests: number;
  totalTokensIn: number;
  totalTokensOut: number;
  estimatedCostUsd: number;
  uniqueSessions: number;
  avgLatencyMs: number;
  period: UsagePeriod;
}

/** One entry in the delta feed. */
export interface ChangeEvent {
  changeType: ChangeType;
  asset: AIAsset;
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

export interface ExportParams {
  format?: 'json';
}
