/**
 * Minimal JSON Schema (2020-12 subset) validator.
 *
 * ajv is not a gateway dependency. This covers exactly the keywords the
 * route-policy.v1 and vectors schemas use (schemas/route-policy/);
 * vectors.test.ts fails if a schema starts using a keyword it does not
 * understand. Used by the conformance tests and by the PS_ROUTE_POLICY_FILE
 * loader (./policyDoc.ts).
 */

export const ANNOTATIONS = new Set([
  '$schema',
  '$id',
  '$defs',
  'title',
  'description',
  'default',
]);
export const ASSERTIONS = new Set([
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

/** Errors (`<json path>: <problem>`) for `value` against `schema`; [] if valid. */
export function validate(
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
      if (!Object.prototype.hasOwnProperty.call(value, r))
        errors.push(`${at}: missing ${r}`);
    }
    if (schema.minProperties != null && keys.length < schema.minProperties)
      errors.push(`${at}: fewer than ${schema.minProperties} properties`);
    for (const k of keys) {
      const sub = `${at}.${k}`;
      if (schema.propertyNames)
        validate(root, schema.propertyNames, k, `${sub} (name)`, errors);
      if (
        schema.properties &&
        Object.prototype.hasOwnProperty.call(schema.properties, k)
      ) {
        validate(root, schema.properties[k], value[k], sub, errors);
      } else if (schema.additionalProperties !== undefined) {
        validate(root, schema.additionalProperties, value[k], sub, errors);
      }
    }
  }
  return errors;
}

/** Every keyword used anywhere in a schema, so unsupported ones fail loudly. */
export function keywordsIn(schema: any, out = new Set<string>()): Set<string> {
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
