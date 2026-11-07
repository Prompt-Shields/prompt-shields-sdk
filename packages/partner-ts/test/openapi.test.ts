/**
 * Drift check: the hand-written types in src/types.ts and the client methods
 * against docs/openapi.yaml.
 *
 * The SDK camelCases the wire format, so the types are written by hand
 * rather than generated. This test is what keeps them in step: it parses
 * src/types.ts with the TypeScript compiler and compares every documented
 * schema, operation and query parameter with it. Run it alone with
 * `npm run check:openapi`.
 *
 * When this fails after an API change, update src/types.ts (and the client)
 * to match, or the tables below if an endpoint was added.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as ts from 'typescript';
import { parse } from 'yaml';
import { PartnerClient } from '../src';
import { camelKey } from '../src/casing';
import { mockFetch } from './helpers';

const SPEC_PATH = path.resolve(__dirname, '../../../docs/openapi.yaml');
const TYPES_PATH = path.resolve(__dirname, '../src/types.ts');

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = any;
const spec: Json = parse(fs.readFileSync(SPEC_PATH, 'utf8'));

// ── src/types.ts, read with the compiler ────────────────────────────────

interface Member {
  optional: boolean;
  /** String literals reachable from the member's type (through aliases). */
  literals: Set<string>;
  /** JSON shape of the member's type, or undefined when it is `unknown`/`any`. */
  shape?: Shape;
}

/**
 * The JSON shape of a value: which primitive kinds it can be (`integer` is
 * folded into `number`) and whether it can be null.
 */
interface Shape {
  kinds: Set<string>;
  nullable: boolean;
}

function showKinds(s: Shape): string {
  return [...s.kinds].sort().join(' | ') || 'null';
}

const source = ts.createSourceFile(TYPES_PATH, fs.readFileSync(TYPES_PATH, 'utf8'), ts.ScriptTarget.Latest, true);
const interfaces = new Map<string, ts.InterfaceDeclaration>();
const aliases = new Map<string, ts.TypeAliasDeclaration>();
for (const stmt of source.statements) {
  if (ts.isInterfaceDeclaration(stmt)) interfaces.set(stmt.name.text, stmt);
  if (ts.isTypeAliasDeclaration(stmt)) aliases.set(stmt.name.text, stmt);
}

function literalsOf(node: ts.TypeNode | undefined, out = new Set<string>(), seen = new Set<string>()): Set<string> {
  if (!node) return out;
  if (ts.isLiteralTypeNode(node) && ts.isStringLiteral(node.literal)) out.add(node.literal.text);
  else if (ts.isUnionTypeNode(node) || ts.isIntersectionTypeNode(node)) node.types.forEach((t) => literalsOf(t, out, seen));
  else if (ts.isArrayTypeNode(node)) literalsOf(node.elementType, out, seen);
  else if (ts.isParenthesizedTypeNode(node)) literalsOf(node.type, out, seen);
  else if (ts.isTypeReferenceNode(node)) {
    const name = node.typeName.getText(source);
    node.typeArguments?.forEach((t) => literalsOf(t, out, seen));
    const alias = aliases.get(name);
    if (alias && !seen.has(name)) {
      seen.add(name);
      literalsOf(alias.type, out, seen);
    }
  }
  return out;
}

const TS_KEYWORD_KINDS: Partial<Record<ts.SyntaxKind, string>> = {
  [ts.SyntaxKind.StringKeyword]: 'string',
  [ts.SyntaxKind.NumberKeyword]: 'number',
  [ts.SyntaxKind.BooleanKeyword]: 'boolean',
  [ts.SyntaxKind.ObjectKeyword]: 'object',
};
/** Built-in and utility types, by the JSON kind they stand for. */
const TS_REFERENCE_KINDS: Record<string, string> = {
  Array: 'array',
  ReadonlyArray: 'array',
  Record: 'object',
  Omit: 'object',
  Pick: 'object',
  Partial: 'object',
  Required: 'object',
  Readonly: 'object',
  Date: 'string', // a Timestamp parameter, sent as an ISO 8601 string
};

/**
 * The JSON shape of a type node, following aliases (with their type
 * arguments) and interfaces. Undefined when the type is `unknown` or `any`,
 * or names something this reader does not know; such members are skipped.
 */
function shapeOf(node: ts.TypeNode | undefined, env = new Map<string, ts.TypeNode>()): Shape | undefined {
  if (!node) return undefined;
  const one = (kind: string): Shape => ({ kinds: new Set([kind]), nullable: false });
  const keyword = TS_KEYWORD_KINDS[node.kind];
  if (keyword) return one(keyword);
  if (node.kind === ts.SyntaxKind.UndefinedKeyword) return { kinds: new Set(), nullable: false };
  if (ts.isParenthesizedTypeNode(node)) return shapeOf(node.type, env);
  if (ts.isArrayTypeNode(node) || ts.isTupleTypeNode(node)) return one('array');
  if (ts.isTypeLiteralNode(node) || ts.isMappedTypeNode(node)) return one('object');
  if (ts.isLiteralTypeNode(node)) {
    const lit = node.literal;
    if (lit.kind === ts.SyntaxKind.NullKeyword) return { kinds: new Set(), nullable: true };
    if (ts.isStringLiteral(lit)) return one('string');
    if (ts.isNumericLiteral(lit) || ts.isPrefixUnaryExpression(lit)) return one('number');
    if (lit.kind === ts.SyntaxKind.TrueKeyword || lit.kind === ts.SyntaxKind.FalseKeyword) return one('boolean');
    return undefined;
  }
  if (ts.isUnionTypeNode(node)) {
    const out: Shape = { kinds: new Set(), nullable: false };
    for (const t of node.types) {
      const sub = shapeOf(t, env);
      if (!sub) return undefined;
      sub.kinds.forEach((k) => out.kinds.add(k));
      out.nullable ||= sub.nullable;
    }
    return out;
  }
  if (ts.isIntersectionTypeNode(node)) {
    // `string & {}` (OpenString) is a string: a primitive part wins.
    const parts = node.types.map((t) => shapeOf(t, env));
    if (parts.some((p) => !p)) return undefined;
    return (parts as Shape[]).find((p) => [...p.kinds].some((k) => k !== 'object')) ?? one('object');
  }
  if (ts.isTypeReferenceNode(node)) {
    const name = node.typeName.getText(source);
    const bound = env.get(name);
    if (bound) return shapeOf(bound);
    const alias = aliases.get(name);
    if (alias) {
      // Bind the alias's type parameters, reading each argument in this scope.
      const inner = new Map<string, ts.TypeNode>();
      alias.typeParameters?.forEach((p, i) => {
        let arg = node.typeArguments?.[i] ?? p.default;
        if (arg && ts.isTypeReferenceNode(arg)) arg = env.get(arg.typeName.getText(source)) ?? arg;
        if (arg) inner.set(p.name.text, arg);
      });
      return shapeOf(alias.type, inner);
    }
    if (interfaces.has(name)) return one('object');
    if (name in TS_REFERENCE_KINDS) return one(TS_REFERENCE_KINDS[name]);
  }
  return undefined;
}

function membersOf(name: string): Map<string, Member> {
  const decl = interfaces.get(name);
  if (!decl) throw new Error(`src/types.ts has no interface ${name}`);
  const out = new Map<string, Member>();
  for (const clause of decl.heritageClauses ?? []) {
    for (const t of clause.types) {
      const parent = t.expression.getText(source);
      if (interfaces.has(parent)) membersOf(parent).forEach((m, k) => out.set(k, m));
    }
  }
  for (const m of decl.members) {
    if (!ts.isPropertySignature(m) || !m.name) continue;
    out.set(m.name.getText(source), {
      optional: Boolean(m.questionToken),
      literals: literalsOf(m.type),
      shape: shapeOf(m.type),
    });
  }
  return out;
}

// ── docs/openapi.yaml ───────────────────────────────────────────────────

function deref(node: Json): Json {
  if (node && typeof node.$ref === 'string') {
    const parts = node.$ref.replace(/^#\//, '').split('/');
    return deref(parts.reduce((acc: Json, p: string) => acc[p], spec));
  }
  return node;
}

function enumOf(prop: Json): string[] {
  const p = deref(prop);
  const values = p.enum ?? (p.type === 'array' ? deref(p.items)?.enum : undefined) ?? [];
  return values.filter((v: unknown) => typeof v === 'string');
}

const SPEC_KINDS: Record<string, string> = {
  string: 'string',
  number: 'number',
  integer: 'number',
  boolean: 'boolean',
  array: 'array',
  object: 'object',
};

/**
 * The JSON shape a property schema allows: its `type` (one name or a list,
 * `'null'` included), OpenAPI 3.0's `nullable: true`, and `oneOf`/`anyOf`
 * branches. `allOf` narrows, so the first part with a known shape is used.
 * Undefined when the schema does not say (no `type`); such properties are
 * skipped.
 */
function specShape(node: Json): Shape | undefined {
  if (!node || typeof node !== 'object') return undefined;
  const s = deref(node);
  let shape: Shape | undefined;
  const branches = s.oneOf ?? s.anyOf;
  if (branches) {
    const out: Shape = { kinds: new Set(), nullable: false };
    for (const b of branches) {
      const sub = specShape(b);
      if (!sub) return undefined;
      sub.kinds.forEach((k) => out.kinds.add(k));
      out.nullable ||= sub.nullable;
    }
    shape = out;
  } else if (s.type !== undefined) {
    const out: Shape = { kinds: new Set(), nullable: false };
    for (const t of Array.isArray(s.type) ? s.type : [s.type]) {
      if (t === 'null') out.nullable = true;
      else if (SPEC_KINDS[t]) out.kinds.add(SPEC_KINDS[t]);
      else return undefined;
    }
    shape = out;
  } else if (s.allOf) {
    for (const part of s.allOf) {
      shape = specShape(part);
      if (shape) break;
    }
  } else if (s.properties) {
    shape = { kinds: new Set(['object']), nullable: false };
  }
  if (shape && (node.nullable === true || s.nullable === true)) shape.nullable = true;
  return shape;
}

/**
 * Problems with one member's type against its spec schema: the primitive
 * kinds must be the same, and the member can be null exactly when the spec
 * allows null. Optionality (`?`) is checked against `required` separately.
 *
 * A query parameter (`query: true`) may accept more than the spec: the
 * client turns every value into a string and leaves out `null`, so e.g.
 * `cursor?: string | number | null` for an integer parameter is fine. It
 * must still accept every kind the spec documents.
 */
function compareShape(where: string, prop: Json, m: Member, { query = false } = {}): string[] {
  const want = specShape(prop);
  if (!want || !m.shape) return [];
  const have = m.shape;
  const problems: string[] = [];
  const kindsMatch = query
    ? [...want.kinds].every((k) => have.kinds.has(k))
    : showKinds(want) === showKinds(have);
  if (!kindsMatch) problems.push(`${where} is ${showKinds(have)} but ${showKinds(want)} in the spec`);
  if (want.nullable && !have.nullable) problems.push(`${where} is not nullable but nullable in the spec`);
  if (!want.nullable && have.nullable && !query) problems.push(`${where} is nullable but not nullable in the spec`);
  return problems;
}

/** An object schema's properties and required names, through `allOf`. */
function flatten(schema: Json): { props: Record<string, Json>; required: Set<string> } {
  const s = deref(schema);
  const props: Record<string, Json> = { ...(s.properties ?? {}) };
  const required = new Set<string>(s.required ?? []);
  for (const part of s.allOf ?? []) {
    const sub = flatten(part);
    Object.assign(props, sub.props);
    sub.required.forEach((r) => required.add(r));
  }
  return { props, required };
}

/**
 * Compare one object schema with one interface, both ways: every property
 * exists on both sides, a property is optional (`?`) exactly when the spec
 * does not list it in `required`, its primitive type and nullability match
 * (see compareShape), and every enum value is in the type.
 */
function compareSchema(schema: Json, iface: string, rename: (wire: string) => string = camelKey): string[] {
  const members = membersOf(iface);
  const { props, required } = flatten(schema);
  const problems: string[] = [];
  for (const [wire, prop] of Object.entries(props)) {
    const name = rename(wire);
    const m = members.get(name);
    if (!m) {
      problems.push(`${iface}.${name} is missing (spec property ${wire})`);
      continue;
    }
    if (required.has(wire) && m.optional) problems.push(`${iface}.${name} is optional but required in the spec`);
    if (!required.has(wire) && !m.optional) problems.push(`${iface}.${name} is required but optional in the spec`);
    problems.push(...compareShape(`${iface}.${name}`, prop, m));
    for (const v of enumOf(prop)) {
      if (!m.literals.has(v)) problems.push(`${iface}.${name} does not list enum value '${v}'`);
    }
  }
  const documented = new Set(Object.keys(props).map(rename));
  for (const name of members.keys()) {
    if (!documented.has(name)) problems.push(`${iface}.${name} is not in the spec`);
  }
  return problems;
}

// ── What maps to what ───────────────────────────────────────────────────

/** components.schemas -> interface in src/types.ts. */
const SCHEMAS: Record<string, string> = {
  AIAsset: 'AIAsset',
  DataFlow: 'DataFlow',
  RiskMapping: 'RiskMapping',
  UsageSummary: 'UsageSummary',
  SourceCoverage: 'SourceCoverage',
  TelemetryCoverage: 'TelemetryCoverage',
  SavingsTotals: 'SavingsTotals',
  SavingsBreakdown: 'SavingsBreakdown',
  SavingsReport: 'SavingsReport',
  SovereignShareTotals: 'SovereignShareTotals',
  SovereignShareByLevel: 'SovereignShareByLevel',
  SovereignShareReport: 'SovereignShareReport',
  ChangeEvent: 'ChangeEvent',
  AttestationStatus: 'AttestationStatus',
  Attestation: 'Attestation',
  AttestationRequest: 'AttestationRequest',
  PaginationMeta: 'PaginationMeta',
  TokenResponse: 'TokenResponse',
  // Tenant admin webhook API (client.webhooks).
  WebhookSubscription: 'WebhookSubscription',
  WebhookSubscriptionWithSecret: 'WebhookSubscriptionWithSecret',
  WebhookDelivery: 'WebhookDelivery',
  CreateWebhookSubscriptionRequest: 'CreateWebhookSubscriptionParams',
  UpdateWebhookSubscriptionRequest: 'UpdateWebhookSubscriptionParams',
  RotateWebhookSecretRequest: 'RotateWebhookSecretParams',
  // Risk mapping reviews (client.assets.riskReviews / reviewRisk).
  RiskMappingReview: 'RiskMappingReview',
  RiskReviewRequest: 'RiskReviewRequest',
  RiskReviewHistory: 'RiskReviewHistory',
  // Spend anomaly alerts (client.spendAlerts). The settings are an operator
  // tenant setting, typed for convenience.
  SpendAlert: 'SpendAlert',
  SpendAnomalySettings: 'SpendAnomalySettings',
  // MCP observability (client.mcp).
  McpTool: 'McpTool',
  McpUsageDay: 'McpUsageDay',
  McpPageMeta: 'McpPageMeta',
  McpToolPage: 'McpToolPage',
  McpUsagePage: 'McpUsagePage',
  // Routing learner (client.learner). The upload result is what the Python
  // SDK's ps-learn gets back from the ingest route, typed for convenience.
  LearnerSnapshotSummary: 'LearnerSnapshotSummary',
  LearnerSnapshot: 'LearnerSnapshot',
  LearnerArmProjection: 'LearnerArmProjection',
  LearnerRealised: 'LearnerRealised',
  LearnerRecommendation: 'LearnerRecommendation',
  LearnerReviewRequest: 'LearnerReviewRequest',
  LearnerSnapshotUploadResult: 'LearnerSnapshotUploadResult',
  // Provider admin credentials (client.providerCredentials,
  // client.providerKeyFingerprints).
  ProviderCredential: 'ProviderCredential',
  AdminKeyRequest: 'AdminKeyRequest',
  ProviderKeyFingerprintLink: 'ProviderKeyFingerprintLink',
  FingerprintRequest: 'FingerprintRequest',
  // Audit trail (client.audit).
  AuditTrailActor: 'AuditTrailActor',
  AuditTrailTarget: 'AuditTrailTarget',
  AuditTrailEvent: 'AuditTrailEvent',
  AuditTrailPageMeta: 'AuditTrailPageMeta',
  AuditTrailPage: 'AuditTrailPage',
  // Gateway security events (client.securityEvents).
  SecurityEventRow: 'SecurityEventRow',
  SecurityEventPageMeta: 'SecurityEventPageMeta',
  SecurityEventPage: 'SecurityEventPage',
};
/**
 * Schemas whose property names the SDK keeps as sent (not camelCased):
 * FOCUS column names (client.usage.focusExport rows).
 */
const VERBATIM_SCHEMAS: Record<string, string> = {
  FocusCostRow: 'FocusCostRow',
};
/** Schemas the SDK handles without a matching interface. */
const UNMAPPED_SCHEMAS = new Set([
  'Error', // parsed into APIError (src/errors.ts)
  'WebhookEventType', // a string enum; checked through the properties that use it
  // Call feedback (#43): ingest-key route, not wrapped by the partner SDK.
  'CallFeedbackRequest',
  'CallFeedbackResult',
  // Tenant admin automation (admin tokens, ingest keys, partner
  // credentials) and operator tenant routes: documented for Terraform and
  // other automation, not wrapped by the partner SDK.
  'AdminScope',
  'CredentialStatus',
  'AdminToken',
  'AdminTokenWithSecret',
  'CreateAdminTokenRequest',
  'BootstrapAdminTokenRequest',
  'TenantApiKey',
  'TenantApiKeyWithSecret',
  'CreateTenantApiKeyRequest',
  'RotateTenantApiKeyRequest',
  'TenantApiKeyScope',
  // Named operator tokens (#341): operator routes, not wrapped.
  'OperatorScope',
  'OperatorTokenRecord',
  'OperatorTokenWithSecret',
  'CreateOperatorTokenRequest',
  'PartnerCredential',
  'PartnerCredentialWithSecrets',
  'IssuePartnerCredentialRequest',
  'UpdatePartnerCredentialRequest',
  'Tenant',
  'TenantCreated',
  'CreateTenantRequest',
  'UpdateTenantRequest',
  'IngestRateLimitSettings', // settings.ingest_rate_limits (#345)
  'ConnectorSyncRequest',
  'LeanIXSyncRequest',
  'ServiceNowSyncRequest',
  // Erasure by end user (#353): tenant admin route, not wrapped.
  'EraseUserRequest',
  'UserErasureResult',
  // Security event ingest (#353): ingest-key route, not wrapped.
  'SecurityEventIngestResult',
]);

interface Operation {
  /** Client method(s) that call it, as paths from the client, e.g. `assets.get`. */
  methods: string[];
  /** Interface holding the query parameters, if the operation has any. */
  params?: string;
  /** Interface the JSON request body is built from, if the operation takes one. */
  body?: string;
}

/**
 * In the spec, not wrapped by the client: the tenant admin API (other than
 * webhooks), the operator routes and the ingest-key routes. Every
 * `/partner/v1` route is wrapped.
 */
const NOT_WRAPPED: Operation = { methods: [] };

/** Every operation in the spec, by `METHOD path`. */
const OPERATIONS: Record<string, Operation> = {
  'POST /oauth/token': { methods: [] }, // used internally for OAuth
  'GET /partner/v1/assets': { methods: ['assets.list', 'assets.listPage'], params: 'ListAssetsParams' },
  'GET /partner/v1/assets/{asset_id}': { methods: ['assets.get'] },
  'GET /partner/v1/assets/{asset_id}/data-flows': { methods: ['assets.dataFlows'] },
  'GET /partner/v1/assets/{asset_id}/risks': { methods: ['assets.risks'], params: 'RisksParams' },
  'GET /partner/v1/assets/{asset_id}/attestations': {
    methods: ['assets.attestations'],
    params: 'ListAttestationsParams',
  },
  'POST /partner/v1/assets/{asset_id}/attestations': { methods: ['assets.attest'], body: 'AttestationRequest' },
  'GET /partner/v1/assets/{asset_id}/usage-summary': { methods: ['assets.usageSummary'], params: 'UsageSummaryParams' },
  'GET /partner/v1/telemetry/coverage': { methods: ['telemetry.coverage'] },
  'GET /partner/v1/usage/savings': { methods: ['usage.savings'], params: 'SavingsParams' },
  'GET /partner/v1/usage/sovereign-share': { methods: ['usage.sovereignShare'], params: 'SovereignShareParams' },
  'GET /partner/v1/vendors': { methods: ['vendors.list'] },
  'GET /partner/v1/models': { methods: ['models.list'] },
  'GET /partner/v1/business-units': { methods: ['businessUnits.list'] },
  'GET /partner/v1/changes': { methods: ['changes.list', 'changes.listPage'], params: 'ListChangesParams' },
  'GET /partner/v1/export': { methods: ['export.get'], params: 'ExportParams' },
  'GET /partner/v1/usage/focus-export': { methods: ['usage.focusExport'], params: 'FocusExportParams' },
  'GET /partner/v1/health': { methods: ['health'] },
  // Provider admin credentials (#93): client.providerCredentials and
  // client.providerKeyFingerprints. The /api/v1/admin copies take the tenant
  // API key and are not wrapped.
  'GET /partner/v1/provider-credentials': { methods: ['providerCredentials.list'] },
  'GET /partner/v1/provider-credentials/{provider}': { methods: ['providerCredentials.get'] },
  'POST /partner/v1/provider-credentials/{provider}': {
    methods: ['providerCredentials.create'],
    body: 'AdminKeyRequest',
  },
  'DELETE /partner/v1/provider-credentials/{provider}': { methods: ['providerCredentials.delete'] },
  'POST /partner/v1/provider-credentials/{provider}/rotate': {
    methods: ['providerCredentials.rotate'],
    body: 'AdminKeyRequest',
  },
  'GET /partner/v1/provider-key-fingerprints': { methods: ['providerKeyFingerprints.list'] },
  'PUT /partner/v1/provider-key-fingerprints/{provider}/{api_key_id}': {
    methods: ['providerKeyFingerprints.put'],
    body: 'FingerprintRequest',
  },
  'DELETE /partner/v1/provider-key-fingerprints/{provider}/{api_key_id}': {
    methods: ['providerKeyFingerprints.delete'],
  },
  'GET /api/v1/admin/provider-credentials': NOT_WRAPPED,
  'GET /api/v1/admin/provider-credentials/{provider}': NOT_WRAPPED,
  'POST /api/v1/admin/provider-credentials/{provider}': NOT_WRAPPED,
  'DELETE /api/v1/admin/provider-credentials/{provider}': NOT_WRAPPED,
  'POST /api/v1/admin/provider-credentials/{provider}/rotate': NOT_WRAPPED,
  'GET /api/v1/admin/provider-key-fingerprints': NOT_WRAPPED,
  'PUT /api/v1/admin/provider-key-fingerprints/{provider}/{api_key_id}': NOT_WRAPPED,
  'DELETE /api/v1/admin/provider-key-fingerprints/{provider}/{api_key_id}': NOT_WRAPPED,
  // Tenant admin webhook API: client.webhooks (needs the tenant API key).
  'POST /api/v1/admin/webhooks/subscriptions': {
    methods: ['webhooks.subscriptions.create'],
    body: 'CreateWebhookSubscriptionParams',
  },
  'GET /api/v1/admin/webhooks/subscriptions': {
    methods: ['webhooks.subscriptions.list'],
    params: 'ListWebhookSubscriptionsParams',
  },
  'GET /api/v1/admin/webhooks/subscriptions/{subscription_id}': { methods: ['webhooks.subscriptions.get'] },
  'PATCH /api/v1/admin/webhooks/subscriptions/{subscription_id}': {
    methods: ['webhooks.subscriptions.update'],
    body: 'UpdateWebhookSubscriptionParams',
  },
  'DELETE /api/v1/admin/webhooks/subscriptions/{subscription_id}': { methods: ['webhooks.subscriptions.delete'] },
  'POST /api/v1/admin/webhooks/subscriptions/{subscription_id}/rotate-secret': {
    methods: ['webhooks.subscriptions.rotateSecret'],
    body: 'RotateWebhookSecretParams',
  },
  'GET /api/v1/admin/webhooks/deliveries': {
    methods: ['webhooks.deliveries.list'],
    params: 'ListWebhookDeliveriesParams',
  },
  'POST /api/v1/admin/webhooks/deliveries/{delivery_id}/redeliver': { methods: ['webhooks.deliveries.redeliver'] },
  // Tenant admin automation and operator routes (not wrapped).
  'GET /api/v1/admin/token-scopes': NOT_WRAPPED,
  'GET /api/v1/admin/tokens': NOT_WRAPPED,
  'POST /api/v1/admin/tokens': NOT_WRAPPED,
  'GET /api/v1/admin/tokens/{token_id}': NOT_WRAPPED,
  'DELETE /api/v1/admin/tokens/{token_id}': NOT_WRAPPED,
  'GET /api/v1/admin/api-keys': NOT_WRAPPED,
  'POST /api/v1/admin/api-keys': NOT_WRAPPED,
  'GET /api/v1/admin/api-keys/{key_id}': NOT_WRAPPED,
  'DELETE /api/v1/admin/api-keys/{key_id}': NOT_WRAPPED,
  'POST /api/v1/admin/api-keys/{key_id}/rotate': NOT_WRAPPED,
  'GET /api/v1/admin/partner-scopes': NOT_WRAPPED,
  'GET /api/v1/admin/partner-credentials': NOT_WRAPPED,
  'POST /api/v1/admin/partner-credentials': NOT_WRAPPED,
  'GET /api/v1/admin/partner-credentials/{credential_id}': NOT_WRAPPED,
  'PATCH /api/v1/admin/partner-credentials/{credential_id}': NOT_WRAPPED,
  'DELETE /api/v1/admin/partner-credentials/{credential_id}': NOT_WRAPPED,
  'POST /api/v1/admin/asset-lifecycle/sweep': NOT_WRAPPED,
  'POST /api/v1/admin/risk-suggestions/run': NOT_WRAPPED,
  'POST /api/v1/admin/risk-mappings/{risk_id}/review': NOT_WRAPPED,
  'GET /api/v1/admin/risk-mappings/{risk_id}/reviews': NOT_WRAPPED,
  'GET /partner/v1/assets/{asset_id}/risks/{risk_id}/reviews': {
    methods: ['assets.riskReviews'],
    params: 'ListRiskReviewsParams',
  },
  'POST /partner/v1/assets/{asset_id}/risks/{risk_id}/review': {
    methods: ['assets.reviewRisk'],
    body: 'RiskReviewRequest',
  },
  'GET /partner/v1/spend-alerts': { methods: ['spendAlerts.list'], params: 'ListSpendAlertsParams' },
  'GET /partner/v1/spend-alerts/{alert_id}': { methods: ['spendAlerts.get'] },
  'POST /partner/v1/spend-alerts/{alert_id}/acknowledge': { methods: ['spendAlerts.acknowledge'] },
  'POST /api/v1/admin/provider-usage/pull': NOT_WRAPPED,
  'POST /api/v1/admin/cloud-inventory/pull': NOT_WRAPPED,
  'POST /api/v1/admin/cloud-inventory/import': NOT_WRAPPED,
  'POST /api/v1/admin/connectors/leanix/sync': NOT_WRAPPED,
  'GET /api/v1/admin/connectors/leanix/status': NOT_WRAPPED,
  'POST /api/v1/admin/connectors/servicenow/sync': NOT_WRAPPED,
  'GET /api/v1/admin/connectors/servicenow/status': NOT_WRAPPED,
  'POST /api/v1/admin/connectors/sentinel/sync': NOT_WRAPPED,
  'GET /api/v1/admin/connectors/sentinel/status': NOT_WRAPPED,
  // SIEM export (#32): NDJSON for log forwarders, streamed (client.siem).
  'GET /partner/v1/siem/calls': { methods: ['siem.calls', 'siem.callsFeed'], params: 'SiemCallsParams' },
  'GET /partner/v1/siem/audit': { methods: ['siem.audit', 'siem.auditFeed'], params: 'SiemAuditParams' },
  // Audit trail read API (#353): client.audit.
  'GET /partner/v1/audit': { methods: ['audit.list', 'audit.listPage'], params: 'ListAuditParams' },
  // Erasure by end user (#353): tenant admin, not wrapped.
  'POST /api/v1/admin/users/erase': NOT_WRAPPED,
  'DELETE /api/v1/admin/users/{user_id_hash}/data': NOT_WRAPPED,
  // Gateway security events (#353): client.securityEvents; ingest and the
  // SIEM connector operator routes are not wrapped.
  'GET /partner/v1/security-events': {
    methods: ['securityEvents.list', 'securityEvents.listPage'],
    params: 'ListSecurityEventsParams',
  },
  'POST /ingest/security-events': NOT_WRAPPED,
  'POST /ingest/otel/v1/logs': NOT_WRAPPED,
  'POST /api/v1/admin/connectors/splunk-hec/sync': NOT_WRAPPED,
  'GET /api/v1/admin/connectors/splunk-hec/status': NOT_WRAPPED,
  'POST /api/v1/admin/connectors/syslog/sync': NOT_WRAPPED,
  'GET /api/v1/admin/connectors/syslog/status': NOT_WRAPPED,
  'POST /api/v1/admin/connectors/webhook/sync': NOT_WRAPPED,
  'GET /api/v1/admin/connectors/webhook/status': NOT_WRAPPED,
  'GET /api/v1/operator/tenants': NOT_WRAPPED,
  'POST /api/v1/operator/tenants': NOT_WRAPPED,
  'GET /api/v1/operator/tenants/{tenant_id}': NOT_WRAPPED,
  'PATCH /api/v1/operator/tenants/{tenant_id}': NOT_WRAPPED,
  'DELETE /api/v1/operator/tenants/{tenant_id}': NOT_WRAPPED,
  'POST /api/v1/operator/tenants/{tenant_id}/admin-tokens': NOT_WRAPPED,
  'GET /api/v1/operator/tenants/{tenant_id}/retention': NOT_WRAPPED,
  'PUT /api/v1/operator/tenants/{tenant_id}/retention': NOT_WRAPPED,
  'POST /api/v1/operator/retention/purge': NOT_WRAPPED,
  'GET /api/v1/operator/audit/verify': NOT_WRAPPED,
  'GET /api/v1/operator/token-scopes': NOT_WRAPPED,
  'POST /api/v1/operator/tokens': NOT_WRAPPED,
  'GET /api/v1/operator/tokens': NOT_WRAPPED,
  'GET /api/v1/operator/tokens/{token_id}': NOT_WRAPPED,
  'DELETE /api/v1/operator/tokens/{token_id}': NOT_WRAPPED,
  'POST /api/v1/operator/tokens/{token_id}/rotate': NOT_WRAPPED,
  // Call feedback (#43) is sent by the Python SDK and the gateway with the
  // tenant ingest key, never by a partner.
  'POST /ingest/feedback': NOT_WRAPPED,
  // MCP observability (#77): client.mcp. The registry route takes the
  // tenant ingest key and is not wrapped.
  'GET /partner/v1/mcp/tools': { methods: ['mcp.listTools', 'mcp.listToolsPage'], params: 'ListMcpToolsParams' },
  'GET /partner/v1/mcp/usage': { methods: ['mcp.listUsage', 'mcp.listUsagePage'], params: 'ListMcpUsageParams' },
  'GET /api/v1/registry/mcp-tools': NOT_WRAPPED,
  // Routing learner (#47, #48): client.learner. Snapshots are uploaded by
  // the Python SDK's ps-learn with the tenant ingest key, not by a partner.
  'POST /ingest/learner-snapshots': NOT_WRAPPED,
  'GET /partner/v1/learner/snapshots': { methods: ['learner.listSnapshots'], params: 'ListLearnerSnapshotsParams' },
  'GET /partner/v1/learner/snapshots/{learner_version}': { methods: ['learner.getSnapshot'] },
  'GET /partner/v1/learner/recommendations': {
    methods: ['learner.listRecommendations'],
    params: 'ListLearnerRecommendationsParams',
  },
  'GET /partner/v1/learner/recommendations/{rec_id}': { methods: ['learner.getRecommendation'] },
  'POST /partner/v1/learner/recommendations/{rec_id}/review': {
    methods: ['learner.reviewRecommendation'],
    body: 'LearnerReviewRequest',
  },
};

const HTTP_METHODS = ['get', 'post', 'put', 'patch', 'delete'];

function specOperations(): Map<string, Json> {
  const out = new Map<string, Json>();
  for (const [p, item] of Object.entries<Json>(spec.paths)) {
    for (const method of HTTP_METHODS) {
      if (item[method]) out.set(`${method.toUpperCase()} ${p}`, { ...item[method], shared: item.parameters ?? [] });
    }
  }
  return out;
}

function responseSchema(key: string, status = '200'): Json {
  const op = specOperations().get(key);
  return deref(deref(op.responses[status]).content['application/json'].schema);
}

// ── Tests ───────────────────────────────────────────────────────────────

describe('docs/openapi.yaml drift check', () => {
  it.each(Object.entries(SCHEMAS))('schema %s matches interface %s', (schema, iface) => {
    expect(spec.components.schemas[schema]).toBeDefined();
    expect(compareSchema(spec.components.schemas[schema], iface)).toEqual([]);
  });

  it.each(Object.entries(VERBATIM_SCHEMAS))('schema %s matches interface %s, names as sent', (schema, iface) => {
    expect(spec.components.schemas[schema]).toBeDefined();
    expect(compareSchema(spec.components.schemas[schema], iface, (wire) => wire)).toEqual([]);
  });

  it('maps every documented schema', () => {
    const unmapped = Object.keys(spec.components.schemas).filter(
      (s) => !(s in SCHEMAS) && !(s in VERBATIM_SCHEMAS) && !UNMAPPED_SCHEMAS.has(s),
    );
    expect(unmapped).toEqual([]);
  });

  it('matches the inline response objects', () => {
    expect(compareSchema(responseSchema('GET /partner/v1/export').properties.meta, 'ExportMeta')).toEqual([]);
    expect(
      compareSchema(responseSchema('GET /partner/v1/assets/{asset_id}/attestations').properties.meta, 'AttestationsMeta'),
    ).toEqual([]);
    expect(compareSchema(responseSchema('GET /partner/v1/health'), 'HealthStatus')).toEqual([]);
    expect(compareSchema(responseSchema('GET /partner/v1/spend-alerts').properties.meta, 'SpendAlertListMeta')).toEqual(
      [],
    );
    expect(
      compareSchema(responseSchema('POST /partner/v1/assets/{asset_id}/risks/{risk_id}/review', '201'), 'RiskReviewResult'),
    ).toEqual([]);
    expect(compareSchema(spec.components.schemas.McpUsagePage.properties.meta, 'McpUsagePageMeta')).toEqual([]);
    expect(
      compareSchema(spec.components.schemas.LearnerRecommendation.properties.projection, 'LearnerProjection'),
    ).toEqual([]);
    expect(
      compareSchema(responseSchema('GET /partner/v1/provider-credentials').properties.meta, 'ProviderCredentialListMeta'),
    ).toEqual([]);
    for (const status of ['200', '201']) {
      const link = responseSchema('PUT /partner/v1/provider-key-fingerprints/{provider}/{api_key_id}', status);
      expect(compareSchema(link, 'FingerprintLinkResult')).toEqual([]);
      expect(compareSchema(link.properties.meta, 'FingerprintLinkMeta')).toEqual([]);
    }
    for (const [key, iface] of [
      ['GET /partner/v1/learner/snapshots', 'LearnerSnapshotList'],
      ['GET /partner/v1/learner/recommendations', 'LearnerRecommendationList'],
    ]) {
      expect(compareSchema(responseSchema(key), iface)).toEqual([]);
      expect(compareSchema(responseSchema(key).properties.meta, 'LearnerListMeta')).toEqual([]);
    }
  });

  it('documents the SIEM pull as NDJSON with cursor headers', () => {
    for (const key of ['GET /partner/v1/siem/calls', 'GET /partner/v1/siem/audit']) {
      const ok = specOperations().get(key).responses['200'];
      expect(Object.keys(ok.content)).toEqual(['application/x-ndjson']);
      expect(Object.keys(ok.headers).sort()).toEqual(['X-PS-Cursor', 'X-PS-Has-More']);
    }
  });

  it('documents the FOCUS export as CSV and JSON Lines', () => {
    const content = specOperations().get('GET /partner/v1/usage/focus-export').responses['200'].content;
    expect(Object.keys(content).sort()).toEqual(['application/x-ndjson', 'text/csv']);
    expect(deref(content['application/x-ndjson'].schema)).toBe(spec.components.schemas.FocusCostRow);
  });

  it('wraps every /partner/v1 operation', () => {
    const unwrapped = Object.entries(OPERATIONS)
      .filter(([key, op]) => key.split(' ')[1].startsWith('/partner/v1/') && op.methods.length === 0)
      .map(([key]) => key);
    expect(unwrapped).toEqual([]);
  });

  it('lists exactly the documented operations', () => {
    expect(Object.keys(OPERATIONS).sort()).toEqual([...specOperations().keys()].sort());
  });

  it.each(Object.entries(OPERATIONS))('%s takes the documented request body', (key, op) => {
    const operation = specOperations().get(key);
    const content = deref(operation.requestBody)?.content?.['application/json'];
    if (!content) {
      expect([key, op.body]).toEqual([key, undefined]);
      return;
    }
    if (op.methods.length === 0) return; // not wrapped by the client
    expect([key, op.body]).toEqual([key, expect.any(String)]);
    expect(compareSchema(content.schema, op.body as string)).toEqual([]);
  });

  it.each(Object.entries(OPERATIONS))('%s has a client method and every query parameter', (key, op) => {
    const client = new PartnerClient({ apiKey: 'k', fetch: mockFetch().fetch }) as unknown as Record<string, Json>;
    for (const method of op.methods) {
      const fn = method.split('.').reduce((obj: Json, part) => obj?.[part], client);
      expect([method, typeof fn]).toEqual([method, 'function']);
    }
    if (op.methods.length === 0) return; // not wrapped by the client

    const operation = specOperations().get(key);
    const queryParams = [...operation.shared, ...(operation.parameters ?? [])]
      .map(deref)
      .filter((p: Json) => p.in === 'query');
    if (queryParams.length === 0) {
      expect(op.params).toBeUndefined();
      return;
    }
    expect(op.params).toBeDefined();
    const members = membersOf(op.params as string);
    const problems: string[] = [];
    for (const p of queryParams) {
      const name = camelKey(p.name);
      const m = members.get(name);
      if (!m) {
        problems.push(`${op.params}.${name} is missing (query parameter ${p.name})`);
        continue;
      }
      if (p.required && m.optional) problems.push(`${op.params}.${name} is optional but required in the spec`);
      problems.push(...compareShape(`${op.params}.${name}`, p.schema, m, { query: true }));
      for (const v of enumOf(p.schema)) {
        if (!m.literals.has(v)) problems.push(`${op.params}.${name} does not list enum value '${v}'`);
      }
    }
    const documented = new Set(queryParams.map((p: Json) => camelKey(p.name)));
    for (const name of members.keys()) {
      if (!documented.has(name)) problems.push(`${op.params}.${name} is not a documented query parameter`);
    }
    expect(problems).toEqual([]);
  });

  it('catches drift (self-test)', () => {
    const schema = {
      required: ['total_requests'],
      properties: { total_requests: {}, brand_new_field: {}, period: { enum: ['7d', '1y'] } },
    };
    expect(compareSchema(schema, 'UsageSummary')).toEqual(
      expect.arrayContaining([
        'UsageSummary.brandNewField is missing (spec property brand_new_field)',
        "UsageSummary.period does not list enum value '1y'",
        'UsageSummary.totalTokensIn is not in the spec',
        // In the type without `?`, but not in the spec's `required`.
        'UsageSummary.period is required but optional in the spec',
      ]),
    );
    expect(
      compareSchema({ required: ['since'], properties: { since: {}, limit: {}, after: {} } }, 'ListChangesParams'),
    ).toEqual([]);
    expect(compareSchema({ properties: { since: {}, limit: {}, after: {} } }, 'ListChangesParams')).toEqual([
      'ListChangesParams.since is required but optional in the spec',
    ]);
    expect(
      compareSchema({ required: ['since', 'limit'], properties: { since: {}, limit: {}, after: {} } }, 'ListChangesParams'),
    ).toEqual(['ListChangesParams.limit is optional but required in the spec']);
  });

  it('catches type and nullability drift (self-test)', () => {
    const props = {
      source: { type: 'string' },
      clients: { type: 'integer' },
      received_events: { type: 'integer' },
      dropped_events: { type: 'integer' },
      unaccounted_events: { type: 'integer' },
      coverage: { type: ['number', 'null'] },
      last_seen: { type: 'string', format: 'date-time', nullable: true },
    };
    const required = Object.keys(props);
    expect(compareSchema({ required, properties: props }, 'SourceCoverage')).toEqual([]);
    expect(
      compareSchema(
        {
          required,
          properties: {
            ...props,
            clients: { type: 'string' }, // integer -> string
            received_events: { type: ['integer', 'null'] }, // became nullable
            coverage: { type: 'number' }, // no longer nullable
            last_seen: { oneOf: [{ type: 'boolean' }, { type: 'null' }] }, // string -> boolean
          },
        },
        'SourceCoverage',
      ),
    ).toEqual([
      'SourceCoverage.clients is number but string in the spec',
      'SourceCoverage.receivedEvents is not nullable but nullable in the spec',
      'SourceCoverage.coverage is nullable but not nullable in the spec',
      'SourceCoverage.lastSeen is string but boolean in the spec',
    ]);
    // Aliases, OpenString and references to interfaces resolve to their JSON kind.
    const shape = (iface: string, name: string) => {
      const s = membersOf(iface).get(name)?.shape;
      return s && showKinds(s) + (s.nullable ? ' | null' : '');
    };
    expect(shape('AIAsset', 'vendor')).toBe('string'); // OpenString<...>
    expect(shape('AIAsset', 'dataClassification')).toBe('string | null');
    expect(shape('AIAsset', 'discoverySource')).toBe('array');
    expect(shape('SavingsReport', 'total')).toBe('object');
    expect(shape('SiemAuditParams', 'since')).toBe('string'); // Timestamp = string | Date
    // Query parameters may accept more kinds and null (the client stringifies
    // values and drops null), but not fewer.
    const cursor = membersOf('SiemAuditParams').get('cursor') as Member;
    expect(compareShape('cursor', { type: 'integer' }, cursor, { query: true })).toEqual([]);
    expect(compareShape('cursor', { type: 'boolean' }, cursor, { query: true })).toEqual([
      'cursor is number | string but boolean in the spec',
    ]);
  });

  it('compares the type of every mapped field', () => {
    // A member typed `unknown`, or a property with no `type`, has no shape
    // and is skipped by compareShape. Keep that list short and deliberate.
    const skipped: string[] = [];
    const mapped: [string, string, (wire: string) => string][] = [
      ...Object.entries(SCHEMAS).map(([s, i]): [string, string, (w: string) => string] => [s, i, camelKey]),
      ...Object.entries(VERBATIM_SCHEMAS).map(([s, i]): [string, string, (w: string) => string] => [s, i, (w) => w]),
    ];
    for (const [schema, iface, rename] of mapped) {
      const members = membersOf(iface);
      for (const [wire, prop] of Object.entries(flatten(spec.components.schemas[schema]).props)) {
        if (!members.get(rename(wire))?.shape || !specShape(prop)) skipped.push(`${iface}.${rename(wire)}`);
      }
    }
    expect(skipped.sort()).toEqual([
      'Attestation.value', // any JSON value: `unknown` in the type, no `type` in the spec
    ]);
  });

  it('reads allOf schemas (self-test)', () => {
    const withSecret = flatten(spec.components.schemas.WebhookSubscriptionWithSecret);
    expect(Object.keys(withSecret.props)).toEqual(expect.arrayContaining(['id', 'url', 'secret', 'warning']));
    expect(withSecret.required.has('secret')).toBe(true);
    expect(withSecret.required.has('id')).toBe(true);
  });
});
