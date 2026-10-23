/**
 * Drift test (#71): every price table in psPricing.ts must equal the SDK's
 * (packages/sdk/prompt_shields/pricing.py, which may build them from the
 * bundled catalog) for every priced model, not only the ones the shared cost
 * vectors cover. Runs the SDK's pricing module with python3 (stdlib only;
 * the package __init__ and its dependencies are not imported).
 */
import { spawnSync } from 'child_process';
import { join } from 'path';
import { PRICE_TABLES } from './psPricing';

const SDK_PACKAGE = join(__dirname, '../../../packages/sdk/prompt_shields');

const DUMP_SDK_TABLES = `
import json, sys, types
pkg = types.ModuleType("prompt_shields")
pkg.__path__ = [sys.argv[1]]
sys.modules["prompt_shields"] = pkg
from prompt_shields import pricing as p

def k(key):
    return "|".join(key) if isinstance(key, tuple) else key

def v(val):
    if isinstance(val, dict):
        return {k(a): v(b) for a, b in val.items()}
    if isinstance(val, (tuple, list)):
        return [v(x) for x in val]
    return val

names = sys.argv[2].split(",")
out = {n: v(getattr(p, n)) for n in names}
out["STANDARD_SERVICE_TIERS"] = sorted(p.STANDARD_SERVICE_TIERS)
print(json.dumps(out))
`;

/** Floats to 12 significant digits: the two sides may derive them differently. */
function normalise(val: unknown): unknown {
  if (typeof val === 'number') return Number(val.toPrecision(12));
  if (typeof val === 'string') return val.split('\u0000').join('|');
  if (val instanceof Set) return [...val].sort();
  if (val instanceof Map) {
    return Object.fromEntries(
      [...val.entries()].map(([a, b]) => [normalise(a), normalise(b)])
    );
  }
  if (Array.isArray(val)) return val.map(normalise);
  if (val && typeof val === 'object') {
    return Object.fromEntries(
      Object.entries(val).map(([a, b]) => [normalise(a), normalise(b)])
    );
  }
  return val;
}

describe('gateway price tables match the SDK', () => {
  const names = Object.keys(PRICE_TABLES).filter(
    (n) => n !== 'STANDARD_SERVICE_TIERS'
  );
  const run = spawnSync(
    'python3',
    ['-I', '-c', DUMP_SDK_TABLES, SDK_PACKAGE, names.join(',')],
    { encoding: 'utf8' }
  );
  if (run.error || run.status !== 0) {
    throw new Error(
      `python3 could not load the SDK pricing tables: ${run.error ?? run.stderr}`
    );
  }
  const sdk = normalise(JSON.parse(run.stdout)) as Record<string, unknown>;

  test.each(Object.keys(PRICE_TABLES))('%s', (name) => {
    expect(normalise((PRICE_TABLES as Record<string, unknown>)[name])).toEqual(
      sdk[name]
    );
  });
});
