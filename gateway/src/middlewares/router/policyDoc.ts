/**
 * The route policy loaded from PS_ROUTE_POLICY_FILE (#336).
 *
 * `parseRoutePolicyDoc` checks a route-policy.v1 document against the schema
 * (a copy of schemas/route-policy/route-policy.v1.schema.json, kept equal
 * by policyFile.test.ts, because the gateway image is built from gateway/
 * alone) and against the cross-field rules the schema states but cannot
 * express, then converts it with `fromRoutePolicyDoc`.
 *
 * The loaded policy lives here so edge bundles never import `fs`: the Node
 * loader (./policyFileLoader.ts) sets it at start and on reload, and the
 * router reads it per request through `loadedRoutePolicy`.
 */
import schema from './route-policy.v1.schema.json';
import { DEFAULT_LEVELS, fromRoutePolicyDoc } from './constraints';
import { validate } from './jsonSchema';
import { RoutePolicy } from './types';

export class RoutePolicyError extends Error {
  constructor(
    message: string,
    readonly problems: string[] = []
  ) {
    super(problems.length ? `${message}: ${problems.join('; ')}` : message);
    this.name = 'RoutePolicyError';
  }
}

const isObject = (v: unknown): v is Record<string, any> =>
  !!v && typeof v === 'object' && !Array.isArray(v);

/** Cross-field rules from the schema's description. */
function crossFieldProblems(doc: any): string[] {
  const problems: string[] = [];
  const groups = new Set(Object.keys(doc.groups));
  const group = (at: string, name: unknown) => {
    if (typeof name === 'string' && !groups.has(name))
      problems.push(`${at}: "${name}" is not a group`);
  };
  group('$.default_group', doc.default_group);
  for (const [k, v] of Object.entries(doc.quality_to_group || {}))
    group(`$.quality_to_group.${k}`, v);
  (doc.group_order || []).forEach((g: unknown, i: number) =>
    group(`$.group_order[${i}]`, g)
  );
  for (const [k, b] of Object.entries<any>(doc.use_cases || {}))
    group(`$.use_cases.${k}.min_group`, b?.min_group);

  const s = doc.sensitivity;
  const levels = new Set<string>(s?.levels ?? DEFAULT_LEVELS);
  const level = (at: string, name: unknown) => {
    if (typeof name === 'string' && !levels.has(name))
      problems.push(`${at}: "${name}" is not a sensitivity level`);
  };
  if (s) {
    level('$.sensitivity.unclassified_default', s.unclassified_default);
    for (const [k, v] of Object.entries(s.pii_escalation || {}))
      level(`$.sensitivity.pii_escalation.${k}`, v);
    (s.anonymise_then_route || []).forEach((l: unknown, i: number) =>
      level(`$.sensitivity.anonymise_then_route[${i}]`, l)
    );
  }
  for (const k of Object.keys(doc.data_policy || {}))
    level(`$.data_policy.${k}`, k);
  return problems;
}

/**
 * A RoutePolicy from a parsed route-policy.v1 document. Throws
 * RoutePolicyError listing every problem when the document is invalid.
 */
export function parseRoutePolicyDoc(doc: unknown): RoutePolicy {
  if (!isObject(doc))
    throw new RoutePolicyError('route policy must be a JSON object');
  const problems = validate(schema, schema, doc);
  if (problems.length)
    throw new RoutePolicyError(
      'route policy does not match route-policy.v1',
      problems
    );
  const cross = crossFieldProblems(doc);
  if (cross.length)
    throw new RoutePolicyError('route policy is inconsistent', cross);
  return fromRoutePolicyDoc(doc);
}

/** A RoutePolicy from route-policy.v1 JSON text. */
export function parseRoutePolicyText(text: string): RoutePolicy {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch (err) {
    throw new RoutePolicyError(
      `route policy is not valid JSON (${(err as Error).message})`
    );
  }
  return parseRoutePolicyDoc(doc);
}

let loaded: RoutePolicy | undefined;

/** Set (or, with undefined, clear) the policy loaded from the file. */
export function setLoadedRoutePolicy(policy: RoutePolicy | undefined): void {
  loaded = policy;
}

/** The policy loaded from PS_ROUTE_POLICY_FILE, if any. */
export function loadedRoutePolicy(): RoutePolicy | undefined {
  return loaded;
}
