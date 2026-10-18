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

/** Compare one object schema with one interface, both ways. */
function compareSchema(schema: Json, iface: string): string[] {
  const s = deref(schema);
  const members = membersOf(iface);
  const required = new Set<string>(s.required ?? []);
  const problems: string[] = [];
  const props: Record<string, Json> = s.properties ?? {};
  for (const [wire, prop] of Object.entries(props)) {
    const name = camelKey(wire);
    const m = members.get(name);
    if (!m) {
      problems.push(`${iface}.${name} is missing (spec property ${wire})`);
      continue;
    }
    if (required.has(wire) && m.optional) problems.push(`${iface}.${name} is optional but required in the spec`);
    for (const v of enumOf(prop)) {
      if (!m.literals.has(v)) problems.push(`${iface}.${name} does not list enum value '${v}'`);
    }
  }
  const documented = new Set(Object.keys(props).map(camelKey));
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
  ChangeEvent: 'ChangeEvent',
  AttestationStatus: 'AttestationStatus',
  Attestation: 'Attestation',
  AttestationRequest: 'AttestationRequest',
  PaginationMeta: 'PaginationMeta',
  TokenResponse: 'TokenResponse',
};
/** Schemas the SDK handles without a matching interface. */
const UNMAPPED_SCHEMAS = new Set(['Error']); // parsed into APIError (src/errors.ts)

interface Operation {
  /** Client method(s) that call it, as paths from the client, e.g. `assets.get`. */
  methods: string[];
  /** Interface holding the query parameters, if the operation has any. */
  params?: string;
}

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
  'POST /partner/v1/assets/{asset_id}/attestations': { methods: ['assets.attest'] },
  'GET /partner/v1/assets/{asset_id}/usage-summary': { methods: ['assets.usageSummary'], params: 'UsageSummaryParams' },
  'GET /partner/v1/vendors': { methods: ['vendors.list'] },
  'GET /partner/v1/models': { methods: ['models.list'] },
  'GET /partner/v1/business-units': { methods: ['businessUnits.list'] },
  'GET /partner/v1/changes': { methods: ['changes.list', 'changes.listPage'], params: 'ListChangesParams' },
  'GET /partner/v1/export': { methods: ['export.get'], params: 'ExportParams' },
  'GET /partner/v1/health': { methods: ['health'] },
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

  it('maps every documented schema', () => {
    const unmapped = Object.keys(spec.components.schemas).filter((s) => !(s in SCHEMAS) && !UNMAPPED_SCHEMAS.has(s));
    expect(unmapped).toEqual([]);
  });

  it('matches the inline response objects', () => {
    expect(compareSchema(responseSchema('GET /partner/v1/export').properties.meta, 'ExportMeta')).toEqual([]);
    expect(
      compareSchema(responseSchema('GET /partner/v1/assets/{asset_id}/attestations').properties.meta, 'AttestationsMeta'),
    ).toEqual([]);
    expect(compareSchema(responseSchema('GET /partner/v1/health'), 'HealthStatus')).toEqual([]);
  });

  it('lists exactly the documented operations', () => {
    expect(Object.keys(OPERATIONS).sort()).toEqual([...specOperations().keys()].sort());
  });

  it.each(Object.entries(OPERATIONS))('%s has a client method and every query parameter', (key, op) => {
    const client = new PartnerClient({ apiKey: 'k', fetch: mockFetch().fetch }) as unknown as Record<string, Json>;
    for (const method of op.methods) {
      const fn = method.split('.').reduce((obj: Json, part) => obj?.[part], client);
      expect([method, typeof fn]).toEqual([method, 'function']);
    }

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
      ]),
    );
  });
});
