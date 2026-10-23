/**
 * Token- and unit-to-USD cost for gateway telemetry events (#71).
 *
 * A port of the SDK's `prompt_shields/pricing.py` (tables and rules) so the
 * gateway and the SDK report the same `cost` for the same usage. Both run
 * the shared golden vectors in `schemas/pricing/cost-vectors.v1.json`
 * (gateway: psPricing.test.ts). A change to one side's prices must be made
 * on the other; the generated price catalog (#7/#8) is meant to replace
 * these hand-copied tables, in `lookupRates` and `unitRate`.
 *
 * Usage fields use the SDK event names: `tokens_in` is the whole prompt
 * (cache reads and writes and audio included), `tokens_out` includes
 * reasoning and audio tokens, the other counts are subsets of those. A null
 * cost means the call is unmetered, never zero.
 */

type Rates = readonly number[];

const key = (...parts: string[]) => parts.join('\u0000');

// Cache rates as a multiple of the input rate when an entry gives none.
const CACHE_READ_MULTIPLIER: Record<string, number> = {
  openai: 0.5,
  anthropic: 0.1,
  google: 0.25,
};
const CACHE_WRITE_MULTIPLIER: Record<string, number> = { anthropic: 1.25 };
const CACHE_WRITE_1H_MULTIPLIER: Record<string, number> = { anthropic: 2.0 };

const STANDARD_SERVICE_TIERS = new Set(['default', 'standard', 'auto']);
const SERVICE_TIER_MULTIPLIER: Record<string, Record<string, number>> = {
  openai: { batch: 0.5, flex: 0.5 },
  anthropic: { batch: 0.5 },
  google: { batch: 0.5 },
};

// Per-request server tool fees in USD. Anthropic web search: $10 / 1,000.
const WEB_SEARCH_FEE: Record<string, number> = { anthropic: 0.01 };

// Prompts above the threshold are priced at the band's rates.
const LONG_CONTEXT_PRICING = new Map<string, [number, Rates]>([
  [
    key('google', 'gemini-2.5-pro'),
    [200_000, [0.0025, 0.015, 0.00025, 0.0025]],
  ],
  [key('anthropic', 'claude-sonnet-4-20250514'), [200_000, [0.006, 0.0225]]],
]);

const AUDIO_PRICING = new Map<string, [number, number]>([
  [key('openai', 'gpt-4o-audio-preview'), [0.04, 0.08]],
  [key('openai', 'gpt-4o-mini-audio-preview'), [0.01, 0.02]],
]);

// Vendors priced at another vendor's list prices when they have no entry.
const PRICING_VENDOR_FALLBACK: Record<string, string> = {
  azure_openai: 'openai',
  // The gateway's provider slugs.
  'azure-openai': 'openai',
  'mistral-ai': 'mistral',
};

export const USAGE_UNIT_TYPES = new Set([
  'seconds',
  'characters',
  'pages',
  'search_units',
  'images',
]);

const UNIT_PRICING = new Map<string, number>([
  [key('openai', 'whisper-1', 'seconds'), 0.006 / 60],
  [key('openai', 'tts-1', 'characters'), 15.0 / 1_000_000],
  [key('openai', 'tts-1-hd', 'characters'), 30.0 / 1_000_000],
  [key('mistral', 'mistral-ocr-2512', 'pages'), 2.0 / 1000],
  [key('elevenlabs', 'scribe_v1', 'seconds'), 0.22 / 3600],
  [key('elevenlabs', 'scribe_v2', 'seconds'), 0.22 / 3600],
  [key('cohere', 'rerank-v3.5', 'search_units'), 2.0 / 1000],
]);

const IMAGE_PRICING = new Map<string, Record<string, number>>([
  [
    key('openai', 'gpt-image-1'),
    {
      [key('low', '1024x1024')]: 0.011,
      [key('low', '1024x1536')]: 0.016,
      [key('low', '1536x1024')]: 0.016,
      [key('medium', '1024x1024')]: 0.042,
      [key('medium', '1024x1536')]: 0.063,
      [key('medium', '1536x1024')]: 0.063,
      [key('high', '1024x1024')]: 0.167,
      [key('high', '1024x1536')]: 0.25,
      [key('high', '1536x1024')]: 0.25,
    },
  ],
]);

// (input_per_1k, output_per_1k[, cached_per_1k[, cache_write_per_1k
// [, cache_write_1h_per_1k]]]) in USD, as pricing.py DEFAULT_PRICING.
const DEFAULT_PRICING = new Map<string, Rates>([
  // OpenAI
  [key('openai', 'gpt-4o'), [0.0025, 0.01]],
  [key('openai', 'gpt-4o-mini'), [0.00015, 0.0006]],
  [key('openai', 'gpt-4-turbo'), [0.01, 0.03]],
  [key('openai', 'gpt-4'), [0.03, 0.06]],
  [key('openai', 'gpt-3.5-turbo'), [0.0005, 0.0015]],
  [key('openai', 'o1'), [0.015, 0.06]],
  [key('openai', 'o1-mini'), [0.003, 0.012]],
  [key('openai', 'o3'), [0.002, 0.008, 0.0005]],
  [key('openai', 'o4-mini'), [0.0011, 0.0044, 0.000275]],
  [key('openai', 'gpt-4o-audio-preview'), [0.0025, 0.01]],
  [key('openai', 'gpt-4o-mini-audio-preview'), [0.00015, 0.0006]],
  // Anthropic
  [key('anthropic', 'claude-opus-4-20250514'), [0.015, 0.075]],
  [key('anthropic', 'claude-sonnet-4-20250514'), [0.003, 0.015]],
  [key('anthropic', 'claude-3-5-sonnet-20241022'), [0.003, 0.015]],
  [key('anthropic', 'claude-3-5-haiku-20241022'), [0.0008, 0.004]],
  [key('anthropic', 'claude-3-opus-20240229'), [0.015, 0.075]],
  // Google
  [key('google', 'gemini-1.5-pro'), [0.00125, 0.005]],
  [key('google', 'gemini-1.5-flash'), [0.000075, 0.0003]],
  [key('google', 'gemini-2.0-flash'), [0.0001, 0.0004]],
  [key('google', 'gemini-2.5-flash'), [0.0003, 0.0025, 0.00003, 0.0003]],
  [key('google', 'gemini-2.5-pro'), [0.00125, 0.01, 0.000125, 0.00125]],
  // Amazon Bedrock (on-demand, us-east-1)
  [
    key('bedrock', 'anthropic.claude-sonnet-4-20250514-v1:0'),
    [0.003, 0.015, 0.0003, 0.00375],
  ],
  [
    key('bedrock', 'anthropic.claude-3-5-sonnet-20241022-v2:0'),
    [0.003, 0.015, 0.0003, 0.00375],
  ],
  [
    key('bedrock', 'anthropic.claude-3-5-haiku-20241022-v1:0'),
    [0.0008, 0.004, 0.00008, 0.001],
  ],
  [
    key('bedrock', 'anthropic.claude-3-haiku-20240307-v1:0'),
    [0.00025, 0.00125],
  ],
  [key('bedrock', 'amazon.nova-pro-v1:0'), [0.0008, 0.0032, 0.0002, 0.0]],
  [key('bedrock', 'amazon.nova-lite-v1:0'), [0.00006, 0.00024, 0.000015, 0.0]],
  [
    key('bedrock', 'amazon.nova-micro-v1:0'),
    [0.000035, 0.00014, 0.00000875, 0.0],
  ],
  [key('bedrock', 'meta.llama3-1-70b-instruct-v1:0'), [0.00072, 0.00072]],
  // Mistral (La Plateforme)
  [key('mistral', 'mistral-large-latest'), [0.002, 0.006]],
  [key('mistral', 'mistral-medium-latest'), [0.0004, 0.002]],
  [key('mistral', 'mistral-small-latest'), [0.0001, 0.0003]],
  [key('mistral', 'codestral-latest'), [0.0003, 0.0009]],
  // Cohere
  [key('cohere', 'command-a-03-2025'), [0.0025, 0.01]],
  [key('cohere', 'command-r-plus-08-2024'), [0.0025, 0.01]],
  [key('cohere', 'command-r-08-2024'), [0.00015, 0.0006]],
  [key('cohere', 'command-r7b-12-2024'), [0.0000375, 0.00015]],
]);

/** Usage of one call, with the SDK's event field names. */
export interface PricingUsage {
  tokens_in?: number;
  tokens_out?: number;
  tokens_cached?: number;
  tokens_cache_write?: number;
  tokens_cache_write_1h?: number;
  tokens_audio_in?: number;
  tokens_audio_out?: number;
  web_search_requests?: number;
  service_tier?: string;
  provider_cost?: number;
  usage_units?: number;
  usage_unit_type?: string;
  image_quality?: string;
  image_size?: string;
}

/** Python's `round(x, 6)`: no float is an exact tie at 7 decimals. */
const round6 = (x: number) => Number(x.toFixed(6));

/** `[pricingVendor, rates]` for a model, or null when it has no price. */
function lookupRates(vendor: string, model: string): [string, Rates] | null {
  let rates = DEFAULT_PRICING.get(key(vendor, model));
  if (rates === undefined && vendor in PRICING_VENDOR_FALLBACK) {
    vendor = PRICING_VENDOR_FALLBACK[vendor];
    rates = DEFAULT_PRICING.get(key(vendor, model));
  }
  return rates === undefined ? null : [vendor, rates];
}

/** True when (vendor, model) has token or per-unit prices. */
export function hasPrice(vendor: string, model: string | undefined): boolean {
  if (!model) return false;
  if (lookupRates(vendor, model)) return true;
  const prefix = key(vendor, model) + '\u0000';
  if (IMAGE_PRICING.has(key(vendor, model))) return true;
  for (const k of UNIT_PRICING.keys()) if (k.startsWith(prefix)) return true;
  return false;
}

const DATE_SUFFIX = /-\d{4}-\d{2}-\d{2}$/;

/**
 * Model ids to price a call at, best first, as the SDK's
 * `ProviderAdapter.pricing_candidates`: the served model, the served id
 * without an OpenAI-style version date (`gpt-4o-2024-08-06` -> `gpt-4o`),
 * then the requested model.
 */
export function pricingCandidates(
  model: string | undefined,
  servedModel: string | undefined
): string[] {
  const names = [
    servedModel,
    typeof servedModel === 'string'
      ? servedModel.replace(DATE_SUFFIX, '')
      : undefined,
    model,
  ];
  const out: string[] = [];
  for (const n of names) {
    if (typeof n === 'string' && n && !out.includes(n)) out.push(n);
  }
  return out;
}

/** The first candidate with a price, else the last (SDK resolve_pricing_model). */
export function resolvePricingModel(
  vendor: string,
  candidates: string[]
): string | undefined {
  for (const m of candidates) if (hasPrice(vendor, m)) return m;
  return candidates[candidates.length - 1];
}

function tierMultiplier(
  vendor: string,
  serviceTier: string | undefined
): number | null {
  if (serviceTier == null || STANDARD_SERVICE_TIERS.has(serviceTier))
    return 1.0;
  return SERVICE_TIER_MULTIPLIER[vendor]?.[serviceTier] ?? null;
}

/** USD cost of a token-billed call, or null when it cannot be priced. */
export function estimateCost(
  vendor: string,
  model: string | undefined,
  u: PricingUsage
): number | null {
  const tokensIn = u.tokens_in;
  const tokensOut = u.tokens_out;
  if (model == null || tokensIn == null || tokensOut == null) return null;

  const found = lookupRates(vendor, model);
  if (!found) return null;
  let rates: Rates;
  [vendor, rates] = found;

  const tier = tierMultiplier(vendor, u.service_tier);
  if (tier === null) return null;

  const band = LONG_CONTEXT_PRICING.get(key(vendor, model));
  if (band && tokensIn > band[0]) rates = band[1];

  const audioIn = u.tokens_audio_in || 0;
  const audioOut = u.tokens_audio_out || 0;
  let audioInRate = 0.0;
  let audioOutRate = 0.0;
  if (audioIn || audioOut) {
    const audioRates = AUDIO_PRICING.get(key(vendor, model));
    if (!audioRates) return null;
    [audioInRate, audioOutRate] = audioRates;
  }

  const [inRate, outRate] = rates;
  const cachedRate =
    rates.length > 2
      ? rates[2]
      : inRate * (CACHE_READ_MULTIPLIER[vendor] ?? 1.0);
  const writeRate =
    rates.length > 3
      ? rates[3]
      : inRate * (CACHE_WRITE_MULTIPLIER[vendor] ?? 1.0);
  let write1hRate: number;
  if (rates.length > 4) write1hRate = rates[4];
  else if (vendor in CACHE_WRITE_1H_MULTIPLIER)
    write1hRate = inRate * CACHE_WRITE_1H_MULTIPLIER[vendor];
  else write1hRate = writeRate;

  const cached = u.tokens_cached || 0;
  const cacheWrite = u.tokens_cache_write || 0;
  const write1h = Math.min(u.tokens_cache_write_1h || 0, cacheWrite);
  // Never charge a negative remainder if counts are inconsistent.
  const uncached = Math.max(tokensIn - cached - cacheWrite - audioIn, 0);
  const textOut = Math.max(tokensOut - audioOut, 0);
  // Same order of operations as pricing.py, so the floats agree exactly.
  const tokenCost =
    (uncached * inRate +
      cached * cachedRate +
      (cacheWrite - write1h) * writeRate +
      write1h * write1hRate +
      audioIn * audioInRate +
      textOut * outRate +
      audioOut * audioOutRate) /
    1000.0;
  const fees = (u.web_search_requests || 0) * (WEB_SEARCH_FEE[vendor] ?? 0.0);
  return round6(tokenCost * tier + fees);
}

function unitRate(
  vendor: string,
  model: string,
  unitType: string,
  quality: string | undefined,
  size: string | undefined
): number | null {
  if (unitType === 'images') {
    const byVariant = IMAGE_PRICING.get(key(vendor, model));
    const rate = byVariant?.[key(String(quality), String(size))];
    return typeof rate === 'number' ? rate : null;
  }
  return UNIT_PRICING.get(key(vendor, model, unitType)) ?? null;
}

/** USD cost of a call billed by non-token units, or null when unpriced. */
export function estimateUnitCost(
  vendor: string,
  model: string | undefined,
  u: PricingUsage
): number | null {
  const units = u.usage_units;
  const unitType = u.usage_unit_type;
  if (model == null || unitType == null) return null;
  if (typeof units !== 'number' || !Number.isFinite(units) || units < 0) {
    return null;
  }
  let rate = unitRate(vendor, model, unitType, u.image_quality, u.image_size);
  if (rate === null && vendor in PRICING_VENDOR_FALLBACK) {
    rate = unitRate(
      PRICING_VENDOR_FALLBACK[vendor],
      model,
      unitType,
      u.image_quality,
      u.image_size
    );
  }
  if (rate === null) return null;
  // Unit prices are standard-tier list prices.
  if (u.service_tier != null && !STANDARD_SERVICE_TIERS.has(u.service_tier)) {
    return null;
  }
  return round6(units * rate);
}

/**
 * Cost of one call (SDK `usage_cost`): a provider-reported cost wins, then a
 * per-unit price, then the token price. Null when it cannot be priced.
 */
export function usageCost(
  vendor: string,
  model: string | undefined,
  u: PricingUsage
): number | null {
  if (u.provider_cost != null) return round6(u.provider_cost);
  if (u.usage_unit_type != null) {
    const unitCost = estimateUnitCost(vendor, model, u);
    if (unitCost !== null) return unitCost;
  }
  return estimateCost(vendor, model, u);
}

/** `cost_source` of a table-priced cost (SDK COST_SOURCE_PRICING_TABLE). */
export const COST_SOURCE_PRICING_TABLE = 'pricing_table';

/** `cost_source` of a cost the provider billed (SDK pricing.py). */
export const COST_SOURCE_PROVIDER_REPORTED = 'provider_reported';

/**
 * `cost` and `cost_source` for a telemetry event, as the SDK's `_build_event`
 * and `add_usage_detail_fields` set them: priced at the served model when it
 * has a price (then without its version date, then the requested model).
 * Empty when the call cannot be priced, so it is reported as unmetered.
 */
export function costFields(
  vendor: string,
  model: string | undefined,
  servedModel: string | undefined,
  u: PricingUsage
): { cost?: number; cost_source?: string } {
  const pricingModel = resolvePricingModel(
    vendor,
    pricingCandidates(model, servedModel)
  );
  const cost = usageCost(vendor, pricingModel, u);
  if (cost === null) return {};
  return {
    cost,
    cost_source:
      u.provider_cost != null
        ? COST_SOURCE_PROVIDER_REPORTED
        : COST_SOURCE_PRICING_TABLE,
  };
}
