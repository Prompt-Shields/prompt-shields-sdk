/**
 * Frozen baseline + counterfactual cost (#24): the shared vectors in
 * schemas/baseline/vectors/ (also run by the SDK, packages/sdk/tests/
 * test_baseline.py), the PS_BASELINE_FILE loader and the telemetry hook.
 */
process.env.PS_API_KEY = 'ps-test';

import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import {
  applyBaseline,
  BaselineError,
  baselineModelFor,
  getBaseline,
  parseBaseline,
  setBaseline,
} from './baseline';
import { loadBaselineFromEnv } from './baselineLoader';
import { withBaseline } from '../ps-telemetry';

const VECTORS_DIR = path.resolve(
  __dirname,
  '../../../../schemas/baseline/vectors'
);
const readJson = (p: string) => JSON.parse(fs.readFileSync(p, 'utf8'));
const VECTOR_FILES = fs
  .readdirSync(VECTORS_DIR)
  .filter((f) => f.endsWith('.json'))
  .sort();

afterEach(() => setBaseline(undefined));

describe.each(VECTOR_FILES)('baseline vectors %s', (file) => {
  const doc = readJson(path.join(VECTORS_DIR, file));
  const baselineDoc = readJson(path.resolve(VECTORS_DIR, doc.baseline));
  const baseline = parseBaseline(baselineDoc);

  const vectors: any[] = doc.vectors;
  const invalid: any[] = doc.invalid_baselines || [];

  test.each(vectors)('$id', (v: any) => {
    const event = { ...v.event };
    applyBaseline(baseline, event);
    expect(event.baseline_id).toBe(baselineDoc.baseline_id);
    expect(baselineModelFor(baseline, v.event.use_case_name)).toBe(
      v.expected.baseline_model
    );
    if (v.expected.counterfactual_cost === null) {
      expect(event).not.toHaveProperty('counterfactual_cost');
    } else {
      expect(
        Math.abs(event.counterfactual_cost - v.expected.counterfactual_cost)
      ).toBeLessThanOrEqual(1e-12);
    }
  });

  test.each(invalid)('rejects invalid baseline $id', (c: any) => {
    expect(() => parseBaseline({ ...baselineDoc, ...c.patch })).toThrow(
      BaselineError
    );
  });
});

describe('loadBaselineFromEnv', () => {
  const example = path.resolve(
    __dirname,
    '../../../../schemas/baseline/examples/onboarding.json'
  );

  test('does nothing without PS_BASELINE_FILE', async () => {
    await expect(loadBaselineFromEnv({})).resolves.toBeUndefined();
    expect(getBaseline()).toBeUndefined();
  });

  test('loads and activates the file', async () => {
    const b = await loadBaselineFromEnv({ PS_BASELINE_FILE: example });
    expect(b?.baselineId).toBe('acme-onboarding-2026-09');
    expect(getBaseline()).toBe(b);
  });

  test('rejects a missing or invalid file', async () => {
    await expect(
      loadBaselineFromEnv({ PS_BASELINE_FILE: '/nonexistent/baseline.json' })
    ).rejects.toThrow(BaselineError);
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ps-baseline-'));
    const bad = path.join(dir, 'bad.json');
    fs.writeFileSync(bad, '{"schema_version": "baseline.v1"}');
    await expect(
      loadBaselineFromEnv({ PS_BASELINE_FILE: bad })
    ).rejects.toThrow(BaselineError);
    fs.rmSync(dir, { recursive: true, force: true });
    expect(getBaseline()).toBeUndefined();
  });
});

describe('withBaseline (telemetry)', () => {
  const routed = {
    vendor: 'openai',
    model: 'auto',
    source: 'gateway',
    route_mode: 'gateway',
    served_model: 'gpt-4o-mini',
    tokens_in: 1000,
    tokens_out: 500,
    cost: 0.00045,
  };

  test('adds baseline_id and counterfactual_cost to routed events', () => {
    setBaseline(
      parseBaseline(
        readJson(
          path.resolve(
            __dirname,
            '../../../../schemas/baseline/examples/onboarding.json'
          )
        )
      )
    );
    const out = withBaseline(routed);
    expect(out.baseline_id).toBe('acme-onboarding-2026-09');
    expect(out.counterfactual_cost).toBeCloseTo(0.0125, 12);
    expect(routed).not.toHaveProperty('baseline_id');
  });

  test('leaves unrouted events and gateways without a baseline alone', () => {
    expect(withBaseline(routed)).toBe(routed);
    setBaseline(
      parseBaseline(
        readJson(
          path.resolve(
            __dirname,
            '../../../../schemas/baseline/examples/onboarding.json'
          )
        )
      )
    );
    const unrouted = { ...routed, route_mode: undefined };
    expect(withBaseline(unrouted)).toBe(unrouted);
  });
});
