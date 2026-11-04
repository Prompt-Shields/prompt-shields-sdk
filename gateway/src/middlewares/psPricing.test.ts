/**
 * The shared golden cost vectors (#71), run against the gateway's pricing.
 * The SDK runs the same file (packages/sdk/tests/test_cost_vectors.py), so
 * passing both shows the gateway and the SDK compute the same cost for the
 * same usage.
 */
import { readFileSync } from 'fs';
import { join } from 'path';
import {
  costFields,
  pricingCandidates,
  resolvePricingModel,
  usageCost,
  USAGE_UNIT_TYPES,
} from './psPricing';

const PRICING_DIR = join(__dirname, '../../../schemas/pricing');
const DOC = JSON.parse(
  readFileSync(join(PRICING_DIR, 'cost-vectors.v1.json'), 'utf8')
);
const VECTORS: any[] = DOC.vectors;
// The schema's comparison tolerance, in USD.
const TOLERANCE = 1e-9;

describe('shared cost vectors', () => {
  test('cover every pricing dimension and unit type', () => {
    const schema = JSON.parse(
      readFileSync(join(PRICING_DIR, 'cost-vectors.schema.json'), 'utf8')
    );
    const dims = new Set(VECTORS.map((v) => v.dimension));
    expect([...dims].sort()).toEqual(
      [...schema.$defs.vector.properties.dimension.enum].sort()
    );
    const units = new Set(
      VECTORS.filter(
        (v) => v.dimension === 'usage_units' && v.expected_cost !== null
      ).map((v) => v.usage.usage_unit_type)
    );
    expect([...units].sort()).toEqual([...USAGE_UNIT_TYPES].sort());
  });

  test.each(VECTORS.map((v) => [v.id, v]))('%s', (_id, vector: any) => {
    const cost = usageCost(vector.vendor, vector.model, vector.usage);
    if (vector.expected_cost === null) {
      expect(cost).toBeNull();
    } else {
      expect(cost).not.toBeNull();
      expect(Math.abs((cost as number) - vector.expected_cost)).toBeLessThan(
        TOLERANCE
      );
    }

    const fields = costFields(
      vector.vendor,
      vector.model,
      undefined,
      vector.usage
    );
    expect(fields.cost ?? null).toEqual(cost);
    if (vector.expected_cost_source) {
      expect(fields.cost_source).toBe(vector.expected_cost_source);
    }
  });
});

describe('served-model pricing (SDK pricing_candidates)', () => {
  test('served id, served id without version date, then requested', () => {
    expect(pricingCandidates('auto', 'gpt-4o-2024-08-06')).toEqual([
      'gpt-4o-2024-08-06',
      'gpt-4o',
      'auto',
    ]);
    expect(pricingCandidates('gpt-4o', undefined)).toEqual(['gpt-4o']);
    expect(pricingCandidates('gpt-4o', 'gpt-4o')).toEqual(['gpt-4o']);
  });

  test('a routed call is priced at the model that ran', () => {
    expect(
      resolvePricingModel('openai', pricingCandidates('auto', 'gpt-4o-mini'))
    ).toBe('gpt-4o-mini');
    const usage = { tokens_in: 1000, tokens_out: 2000 };
    expect(costFields('openai', 'gpt-4o', 'gpt-4o-mini', usage)).toEqual({
      cost: 0.00135,
      cost_source: 'pricing_table',
    });
  });

  test('a dated snapshot is priced as its base model', () => {
    const usage = { tokens_in: 1000, tokens_out: 2000 };
    expect(costFields('openai', 'auto', 'gpt-4o-2024-08-06', usage)).toEqual({
      cost: 0.0225,
      cost_source: 'pricing_table',
    });
  });

  test('an unpriced served id falls back to the requested model', () => {
    const usage = { tokens_in: 1000, tokens_out: 2000 };
    expect(
      costFields('anthropic', 'claude-sonnet-4-20250514', 'claude-x', usage)
    ).toEqual({ cost: 0.033, cost_source: 'pricing_table' });
  });

  test('no price anywhere: no cost (unmetered, not zero)', () => {
    expect(
      costFields('openai', 'auto', undefined, {
        tokens_in: 10,
        tokens_out: 10,
      })
    ).toEqual({});
  });

  test('Azure OpenAI (gateway slug azure-openai) is priced at OpenAI list prices', () => {
    expect(
      costFields('azure-openai', 'my-deployment', 'gpt-4o-2024-11-20', {
        tokens_in: 1000,
        tokens_out: 2000,
      })
    ).toEqual({ cost: 0.0225, cost_source: 'pricing_table' });
  });
});
