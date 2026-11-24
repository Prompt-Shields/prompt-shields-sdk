/**
 * PS fork (#348): negotiated prices from the central policy's `prices`
 * section, and their effect on telemetry cost.
 */
import {
  findOverride,
  negotiatedCost,
  parseInstant,
  parsePricesSection,
} from './prices';
import {
  parsePolicyBundle,
  PolicyBundleError,
  setCentralPolicy,
} from './state';
import { withCost } from '../ps-telemetry';

const RATE = {
  provider: 'openai',
  model: 'gpt-4o',
  valid_from: '2026-11-01',
  input_per_1k: 0.002,
  output_per_1k: 0.008,
};
const NOV = Date.parse('2026-11-05T00:00:00Z');
const OCT = Date.parse('2026-10-31T23:59:00Z');
const USAGE = { tokens_in: 1000, tokens_out: 1000 };

function bundle(prices: unknown) {
  return {
    schema: 'ps-policy-bundle.v1',
    tenant: { version: 'default@1', sections: { prices } },
    teams: {},
    projects: {},
  };
}

afterEach(() => setCentralPolicy(undefined));

describe('parsePricesSection', () => {
  test('parses rates, multipliers and converts currency', () => {
    const book = parsePricesSection({
      version: 1,
      currency: 'EUR',
      usd_per_unit: 1.5,
      overrides: [
        RATE,
        {
          provider: 'anthropic',
          model: '*',
          valid_from: '2026-11-01',
          multiplier: 0.8,
        },
      ],
    });
    expect(book.overrides[0].input).toBeCloseTo(0.003);
    expect(book.overrides[1].multiplier).toBe(0.8);
  });

  test.each([
    ['no overrides', { version: 1 }],
    ['bad currency', { version: 1, currency: 'eur', overrides: [] }],
    ['missing fx', { version: 1, currency: 'EUR', overrides: [] }],
    [
      'no rates',
      { version: 1, overrides: [{ ...RATE, output_per_1k: undefined }] },
    ],
    [
      'naive date-time',
      {
        version: 1,
        overrides: [{ ...RATE, valid_from: '2026-11-01T00:00:00' }],
      },
    ],
    [
      'bad multiplier',
      {
        version: 1,
        overrides: [
          {
            provider: 'x',
            model: '*',
            valid_from: '2026-01-01',
            multiplier: 0,
          },
        ],
      },
    ],
    ['unknown key', { version: 1, overrides: [{ ...RATE, unit: 'tokens' }] }],
  ])('refuses %s', (_name, body) => {
    expect(() => parsePricesSection(body)).toThrow();
  });

  test('a bad prices section refuses the whole bundle', () => {
    expect(() =>
      parsePolicyBundle(bundle({ version: 1, overrides: 'x' }))
    ).toThrow(PolicyBundleError);
    expect(() =>
      parsePolicyBundle(bundle({ version: 2, overrides: [] }))
    ).toThrow(PolicyBundleError);
  });

  test('parseInstant takes dates and offset date-times only', () => {
    expect(parseInstant('2026-11-01')).toBe(Date.parse('2026-11-01T00:00:00Z'));
    expect(parseInstant('2026-11-01T01:00:00+01:00')).toBe(
      Date.parse('2026-11-01T00:00:00Z')
    );
    expect(parseInstant('2026-11-01T00:00:00')).toBeUndefined();
  });
});

describe('negotiatedCost', () => {
  const book = parsePricesSection({
    version: 1,
    overrides: [
      { ...RATE, valid_to: '2026-12-01' },
      {
        provider: 'openai',
        model: '*',
        valid_from: '2026-11-01',
        multiplier: 0.5,
      },
    ],
  });

  test('applies only on or after valid_from and before valid_to', () => {
    expect(
      negotiatedCost(
        book,
        'openai',
        ['gpt-4o'],
        USAGE,
        0.0125,
        'pricing_table',
        OCT
      )
    ).toBeNull();
    expect(
      negotiatedCost(
        book,
        'openai',
        ['gpt-4o'],
        USAGE,
        0.0125,
        'pricing_table',
        Date.parse('2026-11-01T00:00:00Z')
      )
    ).toBe(0.01);
    // After valid_to the provider-wide multiplier applies instead.
    expect(
      negotiatedCost(
        book,
        'openai',
        ['gpt-4o'],
        USAGE,
        0.0125,
        'pricing_table',
        Date.parse('2026-12-01T00:00:00Z')
      )
    ).toBe(0.00625);
  });

  test('exact model before *, cache ratios and tiers', () => {
    expect(
      findOverride(book, 'openai', ['gpt-4o-mini', 'gpt-4o'], NOV)?.model
    ).toBe('gpt-4o');
    expect(
      negotiatedCost(
        book,
        'openai',
        ['gpt-4o'],
        { tokens_in: 1000, tokens_cached: 400, tokens_out: 0 },
        undefined,
        undefined,
        NOV
      )
    ).toBeCloseTo(0.0016);
    expect(
      negotiatedCost(
        book,
        'openai',
        ['gpt-4o'],
        { ...USAGE, service_tier: 'batch' },
        0.00625,
        'pricing_table',
        NOV
      )
    ).toBe(0.005);
    expect(
      negotiatedCost(
        book,
        'openai',
        ['gpt-4o'],
        { ...USAGE, service_tier: 'priority' },
        0.02,
        'pricing_table',
        NOV
      )
    ).toBeNull();
  });

  test('never replaces provider-reported or cache costs, or other vendors', () => {
    expect(
      negotiatedCost(
        book,
        'openai',
        ['gpt-4o'],
        USAGE,
        0.02,
        'provider_reported',
        NOV
      )
    ).toBeNull();
    expect(
      negotiatedCost(book, 'openai', ['gpt-4o'], USAGE, 0, 'cache', NOV)
    ).toBeNull();
    expect(
      negotiatedCost(
        book,
        'azure-openai',
        ['gpt-4o'],
        USAGE,
        0.0125,
        'pricing_table',
        NOV
      )
    ).toBeNull();
    // A multiplier needs a list price.
    expect(
      negotiatedCost(book, 'openai', ['odd'], USAGE, undefined, undefined, NOV)
    ).toBeNull();
  });
});

describe('withCost', () => {
  const event = {
    vendor: 'openai',
    model: 'gpt-4o',
    source: 'gateway',
    tokens_in: 1000,
    tokens_out: 1000,
  };

  test('list price without a central prices section', () => {
    const out = withCost({ ...event });
    expect(out.cost_source).toBe('pricing_table');
  });

  test('negotiated price from the central policy', () => {
    setCentralPolicy(
      parsePolicyBundle(
        bundle({
          version: 1,
          overrides: [{ ...RATE, valid_from: '2020-01-01' }],
        })
      )
    );
    const out = withCost({ ...event }, 'gpt-4o-2024-08-06');
    expect(out.cost).toBe(0.01);
    expect(out.cost_source).toBe('negotiated');
  });
});
