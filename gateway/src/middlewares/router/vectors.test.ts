/**
 * Conformance tests for the shared route-policy.v1 contract.
 *
 * The schema, example policies and golden decision vectors live in
 * schemas/route-policy/ and are shared with the Python SDK runner
 * (packages/sdk/tests/test_route_policy_vectors.py). Only data is shared.
 *
 * Every active vector runs through the real decision path:
 *   hints -> X-PS-* headers -> parseRouteHeaders -> buildRouteRequest -> resolveRoute.
 * The declared classification and use case go in as X-PS-Data-Classification
 * and X-PS-Use-Case; pii_detected is given to the router directly, so the
 * vectors do not depend on the detector (routeVectors.test.ts covers it).
 * Pending vectors are registered with it.skip and their pending_reason in
 * the test name.
 *
 * ajv is not a gateway dependency, so the schema check uses the small
 * validator below. It covers exactly the keywords the two schemas use, and a
 * test fails if a schema starts using a keyword it does not understand.
 */
import * as fs from 'fs';
import * as path from 'path';
import {
  buildRouteRequest,
  fromRoutePolicyDoc,
  HeuristicStrategy,
  parseRouteHeaders,
  PolicyViolation,
  resolveRoute,
} from './index';

const SCHEMA_DIR = path.resolve(__dirname, '../../../../schemas/route-policy');

const readJson = (p: string) => JSON.parse(fs.readFileSync(p, 'utf8'));
const listJson = (dir: string) =>
  fs
    .readdirSync(path.join(SCHEMA_DIR, dir))
    .filter((f) => f.endsWith('.json'))
    .sort()
    .map((f) => path.join(SCHEMA_DIR, dir, f));

const POLICY_SCHEMA = readJson(
  path.join(SCHEMA_DIR, 'route-policy.v1.schema.json')
);
const VECTORS_SCHEMA = readJson(path.join(SCHEMA_DIR, 'vectors.schema.json'));
const EXAMPLES = listJson('examples');
const VECTOR_FILES = listJson('vectors');

// --- minimal JSON Schema (2020-12 subset) validator -------------------------

const ANNOTATIONS = new Set([
  '$schema',
  '$id',
  '$defs',
  'title',
  'description',
  'default',
]);
const ASSERTIONS = new Set([
  '$ref',
  'type',
  'properties',
  'required',
  'additionalProperties',
  'propertyNames',
  'minProperties',
  'items',
  'minItems',
  'uniqueItems',
  'enum',
  'const',
  'minLength',
  'pattern',
  'minimum',
  'maximum',
]);

function typeOf(v: unknown): string {
  if (v === null) return 'null';
  if (Array.isArray(v)) return 'array';
  if (typeof v === 'number') return Number.isInteger(v) ? 'integer' : 'number';
  return typeof v;
}

function validate(
  root: any,
  schema: any,
  value: any,
  at = '$',
  errors: string[] = []
): string[] {
  if (schema === true) return errors;
  if (schema === false) {
    errors.push(`${at}: not allowed`);
    return errors;
  }
  if (schema.$ref) {
    const name = String(schema.$ref).replace(/^#\/\$defs\//, '');
    validate(root, root.$defs[name], value, at, errors);
  }
  const t = typeOf(value);
  if (schema.type) {
    const ok =
      schema.type === t || (schema.type === 'number' && t === 'integer');
    if (!ok) {
      errors.push(`${at}: expected ${schema.type}, got ${t}`);
      return errors;
    }
  }
  if (
    schema.enum &&
    !schema.enum.some((e: any) => JSON.stringify(e) === JSON.stringify(value))
  ) {
    errors.push(`${at}: not one of ${JSON.stringify(schema.enum)}`);
  }
  if (
    'const' in schema &&
    JSON.stringify(schema.const) !== JSON.stringify(value)
  ) {
    errors.push(`${at}: must be ${JSON.stringify(schema.const)}`);
  }
  if (t === 'string') {
    if (schema.minLength != null && value.length < schema.minLength)
      errors.push(`${at}: shorter than ${schema.minLength}`);
    if (schema.pattern && !new RegExp(schema.pattern, 'u').test(value))
      errors.push(`${at}: does not match ${schema.pattern}`);
  }
  if (t === 'number' || t === 'integer') {
    if (schema.minimum != null && value < schema.minimum)
      errors.push(`${at}: below ${schema.minimum}`);
    if (schema.maximum != null && value > schema.maximum)
      errors.push(`${at}: above ${schema.maximum}`);
  }
  if (t === 'array') {
    if (schema.minItems != null && value.length < schema.minItems)
      errors.push(`${at}: fewer than ${schema.minItems} items`);
    if (schema.uniqueItems) {
      const seen = value.map((v: any) => JSON.stringify(v));
      if (new Set(seen).size !== seen.length)
        errors.push(`${at}: items not unique`);
    }
    if (schema.items) {
      value.forEach((v: any, i: number) =>
        validate(root, schema.items, v, `${at}[${i}]`, errors)
      );
    }
  }
  if (t === 'object') {
    const keys = Object.keys(value);
    for (const r of schema.required || []) {
      if (!(r in value)) errors.push(`${at}: missing ${r}`);
    }
    if (schema.minProperties != null && keys.length < schema.minProperties)
      errors.push(`${at}: fewer than ${schema.minProperties} properties`);
    for (const k of keys) {
      const sub = `${at}.${k}`;
      if (schema.propertyNames)
        validate(root, schema.propertyNames, k, `${sub} (name)`, errors);
      if (schema.properties && k in schema.properties) {
        validate(root, schema.properties[k], value[k], sub, errors);
      } else if (schema.additionalProperties !== undefined) {
        validate(root, schema.additionalProperties, value[k], sub, errors);
      }
    }
  }
  return errors;
}

/** Every keyword used anywhere in a schema, so unsupported ones fail loudly. */
function keywordsIn(schema: any, out = new Set<string>()): Set<string> {
  if (!schema || typeof schema !== 'object') return out;
  for (const [k, v] of Object.entries(schema)) {
    out.add(k);
    if (k === 'properties' || k === '$defs') {
      for (const sub of Object.values(v as object)) keywordsIn(sub, out);
    } else if (
      k === 'items' ||
      k === 'additionalProperties' ||
      k === 'propertyNames'
    ) {
      keywordsIn(v, out);
    }
  }
  return out;
}

// RouteHint field -> wire header. Mirrors packages/sdk RouteHint.to_headers.
const HINT_HEADERS: Record<string, string> = {
  quality: 'X-PS-Quality',
  max_cost: 'X-PS-Max-Cost',
  model_group: 'X-PS-Route',
};

/** Expected fields this implementation can decide today. */
const DECIDED_FIELDS = new Set([
  'model',
  'group',
  'reason',
  'est_cost',
  'sensitivity_level',
  'sensitivity_source',
  'error',
]);

/** Vector `expected` field -> RouteDecision field. */
const DECISION_FIELD: Record<string, string> = {
  model: 'model',
  group: 'group',
  reason: 'reason',
  sensitivity_level: 'sensitivityLevel',
  sensitivity_source: 'sensitivitySource',
};

// --- tests ------------------------------------------------------------------

describe('route-policy.v1 schema', () => {
  it.each([
    ['policy', POLICY_SCHEMA],
    ['vectors', VECTORS_SCHEMA],
  ])('%s schema uses only keywords the validator supports', (_, schema) => {
    expect(schema.$schema).toBe('https://json-schema.org/draft/2020-12/schema');
    const unsupported = [...keywordsIn(schema)].filter(
      (k) => !ANNOTATIONS.has(k) && !ASSERTIONS.has(k)
    );
    expect(unsupported).toEqual([]);
  });

  it.each(EXAMPLES.map((p) => [path.basename(p), p]))(
    'example %s validates',
    (_, p) => {
      expect(validate(POLICY_SCHEMA, POLICY_SCHEMA, readJson(p))).toEqual([]);
    }
  );

  it.each<[string, (p: any) => void]>([
    ['missing groups', (p) => delete p.groups],
    ['wrong version', (p) => (p.schema_version = 'route-policy.v2')],
    ['unknown key', (p) => (p.unexpected = true)],
    ['negative price', (p) => (p.groups.cheap[0].output_cost_per_1k = -1)],
    [
      'fail-open fallback',
      (p) => (p.fallback.no_compliant_candidate = 'downgrade'),
    ],
    ['bad jurisdiction', (p) => (p.data_policy.public.jurisdictions = ['eu'])],
  ])('rejects a policy with %s', (_, mutate) => {
    const policy = readJson(
      path.join(SCHEMA_DIR, 'examples', 'nordic-sovereign.json')
    );
    mutate(policy);
    expect(validate(POLICY_SCHEMA, POLICY_SCHEMA, policy)).not.toEqual([]);
  });

  it.each(VECTOR_FILES.map((p) => [path.basename(p), p]))(
    'vector file %s is well formed',
    (_, p) => {
      const doc = readJson(p);
      expect(validate(VECTORS_SCHEMA, VECTORS_SCHEMA, doc)).toEqual([]);
      expect(
        fs.existsSync(path.join(SCHEMA_DIR, 'examples', `${doc.policy}.json`))
      ).toBe(true);
      for (const v of doc.vectors) {
        if (v.status === 'pending') expect(v.pending_reason).toBeTruthy();
      }
    }
  );
});

describe.each(VECTOR_FILES.map((p) => [path.basename(p, '.json'), p]))(
  'golden vectors: %s',
  (_, file) => {
    const doc = readJson(file);
    const policyDoc = readJson(
      path.join(SCHEMA_DIR, 'examples', `${doc.policy}.json`)
    );
    const policy = fromRoutePolicyDoc(policyDoc);

    for (const v of doc.vectors) {
      if (v.status === 'pending') {
        it.skip(`${v.id} [pending: ${v.pending_reason}]`, () => {});
        continue;
      }

      it(v.id, async () => {
        // An active vector must only expect what this router decides today;
        // anything else has to be marked pending, not silently ignored.
        expect(
          Object.keys(v.expected).filter((k) => !DECIDED_FIELDS.has(k))
        ).toEqual([]);
        expect(policyDoc.strategy ?? 'heuristic').toBe('heuristic');

        const headers: Record<string, string> = {};
        for (const [k, val] of Object.entries(v.request.hints || {})) {
          headers[HINT_HEADERS[k]] = String(val);
        }
        if (v.request.data_classification != null)
          headers['X-PS-Data-Classification'] = v.request.data_classification;
        if (v.request.use_case != null)
          headers['X-PS-Use-Case'] = v.request.use_case;
        const req = buildRouteRequest(
          {
            model: v.request.model ?? 'auto',
            messages: v.request.messages,
            max_tokens: v.request.max_tokens,
          },
          parseRouteHeaders(headers)
        );
        if (v.request.approx_input_tokens != null) {
          req.approxInputTokens = v.request.approx_input_tokens;
        }
        if (v.request.pii_detected) req.piiDetected = v.request.pii_detected;

        const run = resolveRoute(req, policy, new HeuristicStrategy());
        if (v.expected.error === 'PolicyViolation') {
          await expect(run).rejects.toBeInstanceOf(PolicyViolation);
          return;
        }
        const decision: any = await run;

        const { est_cost, ...rest } = v.expected;
        const want: Record<string, unknown> = {};
        for (const [k, val] of Object.entries(rest)) {
          want[DECISION_FIELD[k]] = val;
        }
        expect(decision).toMatchObject(want);
        if (est_cost != null) {
          expect(Math.abs(decision.estCost - est_cost)).toBeLessThanOrEqual(
            1e-9 * Math.abs(est_cost) + 1e-15
          );
        }
      });
    }
  }
);
