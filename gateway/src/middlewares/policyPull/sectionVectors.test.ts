/**
 * PS fork (#383): the central policy's route and access sections against
 * the shared vectors in schemas/central-policy/. The collector runs the
 * same files (packages/collector/tests/test_route_policy_schema.py) before
 * it activates a version, so a document the collector accepts is one this
 * gateway applies, and one it refuses is one this gateway would refuse.
 */
import * as fs from 'fs';
import * as path from 'path';
import { parsePolicyBundle } from './state';

const DIR = path.resolve(__dirname, '../../../../schemas/central-policy');
const read = (name: string) =>
  JSON.parse(fs.readFileSync(path.join(DIR, name), 'utf8'));

const bundle = (sections: Record<string, unknown>) => ({
  schema: 'ps-policy-bundle.v1',
  tenant: { version: 'default@1', sections },
  teams: {},
  projects: {},
});

const accepts = (sections: Record<string, unknown>) => {
  try {
    parsePolicyBundle(bundle(sections));
    return true;
  } catch {
    return false;
  }
};

describe('route section vectors', () => {
  const cases: [string, any][] = read('route-v1.vectors.json').cases.map(
    (c: any) => [c.name, c]
  );
  it.each(cases)('%s', (_n, c) => {
    expect(accepts({ route: { version: 1, policy: c.policy } })).toBe(c.valid);
  });
});

describe('access section vectors', () => {
  const cases: [string, any][] = read('access-v1.vectors.json').cases.map(
    (c: any) => [c.name, c]
  );
  it.each(cases)('%s', (_n, c) => {
    expect(accepts({ access: { version: 1, ...c.access } })).toBe(c.valid);
  });
});
