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
  endpointScopeOf,
  normaliseClaudeModel,
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

describe('per-model service-tier rates', () => {
  test('a served snapshot id is priced at its base model priority rates', () => {
    expect(
      costFields('openai', 'gpt-4o', 'gpt-4o-2024-08-06', {
        tokens_in: 1000,
        tokens_out: 2000,
        service_tier: 'priority',
      })
    ).toEqual({ cost: 0.03825, cost_source: 'pricing_table' });
  });

  test('a tier named like an object property is not a rate table', () => {
    for (const tier of ['constructor', 'toString', '__proto__']) {
      expect(
        usageCost('openai', 'gpt-4o', {
          tokens_in: 1000,
          tokens_out: 2000,
          service_tier: tier,
        })
      ).toBeNull();
    }
  });

  test('long-context prompts keep the tier multiplier', () => {
    // Gemini 2.5 Pro over 200k at batch: $2.50 / $15.00 x 0.5.
    expect(
      usageCost('google', 'gemini-2.5-pro', {
        tokens_in: 300000,
        tokens_out: 1000,
        service_tier: 'batch',
      })
    ).toBeCloseTo((300000 * 2.5 + 1000 * 15.0) / 1e6 / 2, 9);
  });
});

describe('Claude on Vertex AI and Bedrock (#388)', () => {
  const IDS: { platform_id: string; anthropic_id: string | null }[] =
    JSON.parse(
      readFileSync(join(PRICING_DIR, 'claude-model-ids.v1.json'), 'utf8')
    ).ids;

  test.each(IDS.map((v) => [v.platform_id, v.anthropic_id]))(
    'normalises %s as the SDK does',
    (platformId, anthropicId) => {
      expect(normaliseClaudeModel(platformId as string)).toBe(anthropicId);
    }
  );

  const usage = { tokens_in: 1000, tokens_out: 2000 };

  test('a vertex-ai request id is priced at Anthropic list prices', () => {
    expect(
      costFields(
        'vertex-ai',
        'anthropic.claude-sonnet-4-5@20250929',
        undefined,
        {
          ...usage,
          endpoint_scope: 'global',
        }
      )
    ).toEqual({ cost: 0.033, cost_source: 'pricing_table' });
  });

  test('a regional vertex-ai endpoint adds the premium', () => {
    const fields = costFields(
      'vertex-ai',
      'anthropic.claude-sonnet-4-5@20250929',
      'claude-sonnet-4-5-20250929',
      { ...usage, endpoint_scope: 'regional' }
    );
    expect(fields.cost).toBeCloseTo(0.0363, 9);
  });

  test('a Bedrock geographic profile is regional, global. is not', () => {
    const us = 'us.anthropic.claude-sonnet-4-5-20250929-v1:0';
    const global = 'global.anthropic.claude-sonnet-4-5-20250929-v1:0';
    expect(endpointScopeOf('bedrock', us, undefined)).toBe('regional');
    expect(endpointScopeOf('bedrock', global, undefined)).toBe('global');
    expect(
      costFields('bedrock', us, undefined, {
        ...usage,
        endpoint_scope: endpointScopeOf('bedrock', us, undefined),
      }).cost
    ).toBeCloseTo(0.0363, 9);
    expect(
      costFields('bedrock', global, undefined, {
        ...usage,
        endpoint_scope: endpointScopeOf('bedrock', global, undefined),
      }).cost
    ).toBeCloseTo(0.033, 9);
  });

  test('endpoint scope needs a Claude model, and a region on Vertex AI', () => {
    const claude = 'anthropic.claude-sonnet-4-5@20250929';
    expect(endpointScopeOf('vertex-ai', claude, 'global')).toBe('global');
    expect(endpointScopeOf('vertex-ai', claude, 'europe-west1')).toBe(
      'regional'
    );
    expect(endpointScopeOf('vertex-ai', claude, undefined)).toBeUndefined();
    expect(
      endpointScopeOf('vertex-ai', 'gemini-2.5-pro', 'us-central1')
    ).toBeUndefined();
    expect(endpointScopeOf('openai', 'gpt-4o', 'us')).toBeUndefined();
  });

  test('existing Bedrock rows still win over Anthropic list prices', () => {
    // The bedrock row's own cache rate: 0.08 per 1M cached tokens.
    expect(
      costFields(
        'bedrock',
        'us.anthropic.claude-3-5-haiku-20241022-v1:0',
        undefined,
        { tokens_in: 1000, tokens_out: 1000, tokens_cached: 500 }
      ).cost
    ).toBeCloseTo(0.00444, 9);
  });

  test('an unknown Claude id stays unmetered, never zero', () => {
    expect(
      costFields('vertex-ai', 'claude-unknown-9@20990101', undefined, usage)
    ).toEqual({});
  });
});
