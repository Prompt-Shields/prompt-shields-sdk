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
    out.set(m.name.getText(source), { optional: Boolean(m.questionToken), literals: literalsOf(m.type) });
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
 * does not list it in `required`, and every enum value is in the type.
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
  // Provider credentials (#93) are not wrapped by the SDK: a tenant stores
  // its own provider admin keys. Call them with client.send() if needed.
  'ProviderCredential',
  'AdminKeyRequest',
  'ProviderKeyFingerprintLink',
  'FingerprintRequest',
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
  'PartnerCredential',
  'PartnerCredentialWithSecrets',
  'IssuePartnerCredentialRequest',
  'UpdatePartnerCredentialRequest',
  'Tenant',
  'TenantCreated',
  'CreateTenantRequest',
  'UpdateTenantRequest',
  'ConnectorSyncRequest',
  // MCP observability (#77): not wrapped yet; call them with client.send().
  'McpTool',
  'McpUsageDay',
  'McpPageMeta',
  'McpToolPage',
  'McpUsagePage',
  // Routing learner snapshots and recommendation review (#47, #48): not
  // wrapped yet; call them with client.send().
  'LearnerSnapshotSummary',
  'LearnerSnapshot',
  'LearnerArmProjection',
  'LearnerRealised',
  'LearnerRecommendation',
  'LearnerReviewRequest',
  'LearnerSnapshotUploadResult',
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
 * In the spec, not wrapped by the client: provider credentials, the rest of
 * the tenant admin API and the operator routes.
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
  'GET /partner/v1/provider-credentials': NOT_WRAPPED,
  'GET /partner/v1/provider-credentials/{provider}': NOT_WRAPPED,
  'POST /partner/v1/provider-credentials/{provider}': NOT_WRAPPED,
  'DELETE /partner/v1/provider-credentials/{provider}': NOT_WRAPPED,
  'POST /partner/v1/provider-credentials/{provider}/rotate': NOT_WRAPPED,
  'GET /partner/v1/provider-key-fingerprints': NOT_WRAPPED,
  'PUT /partner/v1/provider-key-fingerprints/{provider}/{api_key_id}': NOT_WRAPPED,
  'DELETE /partner/v1/provider-key-fingerprints/{provider}/{api_key_id}': NOT_WRAPPED,
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
  'POST /api/v1/admin/connectors/leanix/sync': NOT_WRAPPED,
  'GET /api/v1/admin/connectors/leanix/status': NOT_WRAPPED,
  'POST /api/v1/admin/connectors/servicenow/sync': NOT_WRAPPED,
  'GET /api/v1/admin/connectors/servicenow/status': NOT_WRAPPED,
  'POST /api/v1/admin/connectors/sentinel/sync': NOT_WRAPPED,
  'GET /api/v1/admin/connectors/sentinel/status': NOT_WRAPPED,
  // SIEM export (#32): NDJSON for log forwarders, not wrapped.
  'GET /partner/v1/siem/calls': NOT_WRAPPED,
  'GET /partner/v1/siem/audit': NOT_WRAPPED,
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
  // Call feedback (#43) is sent by the Python SDK and the gateway with the
  // tenant ingest key, never by a partner.
  'POST /ingest/feedback': NOT_WRAPPED,
  // MCP observability (#77): not wrapped yet. The registry route takes the
  // tenant ingest key.
  'GET /partner/v1/mcp/tools': NOT_WRAPPED,
  'GET /partner/v1/mcp/usage': NOT_WRAPPED,
  'GET /api/v1/registry/mcp-tools': NOT_WRAPPED,
  // Learner snapshots are uploaded by the Python SDK's ps-learn with the
  // tenant ingest key; the learner routes are not wrapped yet (#47, #48).
  'POST /ingest/learner-snapshots': NOT_WRAPPED,
  'GET /partner/v1/learner/snapshots': NOT_WRAPPED,
  'GET /partner/v1/learner/snapshots/{learner_version}': NOT_WRAPPED,
  'GET /partner/v1/learner/recommendations': NOT_WRAPPED,
  'GET /partner/v1/learner/recommendations/{rec_id}': NOT_WRAPPED,
  'POST /partner/v1/learner/recommendations/{rec_id}/review': NOT_WRAPPED,
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
  return deref(op.responses[status].content['application/json'].schema);
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
  });

  it('documents the FOCUS export as CSV and JSON Lines', () => {
    const content = specOperations().get('GET /partner/v1/usage/focus-export').responses['200'].content;
    expect(Object.keys(content).sort()).toEqual(['application/x-ndjson', 'text/csv']);
    expect(deref(content['application/x-ndjson'].schema)).toBe(spec.components.schemas.FocusCostRow);
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

  it('reads allOf schemas (self-test)', () => {
    const withSecret = flatten(spec.components.schemas.WebhookSubscriptionWithSecret);
    expect(Object.keys(withSecret.props)).toEqual(expect.arrayContaining(['id', 'url', 'secret', 'warning']));
    expect(withSecret.required.has('secret')).toBe(true);
    expect(withSecret.required.has('id')).toBe(true);
  });
});
