/**
 * PS fork (#348): negotiated model prices from the central policy's
 * `prices` v1 section (tenant-scope policies only; validated by the
 * collector, packages/collector/collector/policy_doc.py, and applied the
 * same way by the collector at ingest, collector/prices.py).
 *
 * An override applies to calls made at or after `valid_from` and before
 * `valid_to` (exclusive). `model: "*"` covers every model of the provider;
 * an exact model wins. Either:
 *   - `multiplier`: the list price (cost_source `pricing_table`) times it;
 *   - per-1k token rates (`input_per_1k`, `output_per_1k`, optional
 *     `cached_input_per_1k`, `cache_write_per_1k`), in the section's
 *     currency converted at `usd_per_unit`. Cache rates left out follow the
 *     provider's list ratios; batch/flex tiers get the provider's tier
 *     multiplier, an unknown tier stays at list; per-unit calls stay at list.
 * Provider-reported and cache costs are never replaced. A negotiated cost
 * is reported with `cost_source: negotiated`, so the collector keeps it.
 */
import { PRICE_TABLES, PricingUsage } from '../psPricing';

export const COST_SOURCE_NEGOTIATED = 'negotiated';
const KEPT_COST_SOURCES = new Set(['provider_reported', 'cache', 'negotiated']);
const RATE_KEYS = [
  'input_per_1k',
  'output_per_1k',
  'cached_input_per_1k',
  'cache_write_per_1k',
] as const;
const OVERRIDE_KEYS = new Set([
  'provider',
  'model',
  'valid_from',
  'valid_to',
  'multiplier',
  ...RATE_KEYS,
]);

export interface PriceOverride {
  provider: string;
  model: string;
  validFrom: number;
  validTo?: number;
  multiplier?: number;
  /** USD per 1k tokens, converted from the section's currency. */
  input?: number;
  output?: number;
  cachedInput?: number;
  cacheWrite?: number;
}

export interface PriceBook {
  overrides: PriceOverride[];
}

const isObject = (v: unknown): v is Record<string, any> =>
  !!v && typeof v === 'object' && !Array.isArray(v);
const isNumber = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v);

export const normalizeProvider = (v: string | undefined) =>
  (v ?? '').trim().toLowerCase().replace(/-/g, '_');

/** `YYYY-MM-DD` (midnight UTC) or an ISO 8601 date-time with an offset. */
export function parseInstant(v: unknown): number | undefined {
  if (typeof v !== 'string') return undefined;
  if (/^\d{4}-\d{2}-\d{2}$/.test(v)) {
    const t = Date.parse(`${v}T00:00:00Z`);
    return Number.isNaN(t) ? undefined : t;
  }
  if (!/(Z|[+-]\d{2}:\d{2})$/.test(v)) return undefined;
  const t = Date.parse(v);
  return Number.isNaN(t) ? undefined : t;
}

/** A PriceBook from a `prices` v1 section. Throws Error when invalid. */
export function parsePricesSection(body: unknown): PriceBook {
  if (!isObject(body)) throw new Error('must be an object');
  const currency = body.currency ?? 'USD';
  if (typeof currency !== 'string' || !/^[A-Z]{3}$/.test(currency))
    throw new Error('currency must be an ISO 4217 code');
  const fx = currency === 'USD' ? 1 : body.usd_per_unit;
  if (!isNumber(fx) || fx <= 0)
    throw new Error('usd_per_unit must be a positive number');
  if (!Array.isArray(body.overrides))
    throw new Error('overrides must be an array');
  const overrides = body.overrides.map((o: unknown, i: number) => {
    const at = `overrides[${i}]`;
    if (!isObject(o)) throw new Error(`${at} must be an object`);
    for (const k of Object.keys(o))
      if (!OVERRIDE_KEYS.has(k)) throw new Error(`${at}: unknown key ${k}`);
    if (typeof o.provider !== 'string' || !o.provider)
      throw new Error(`${at}.provider must be a string`);
    if (typeof o.model !== 'string' || !o.model.trim())
      throw new Error(`${at}.model must be a string`);
    const validFrom = parseInstant(o.valid_from);
    if (validFrom === undefined)
      throw new Error(`${at}.valid_from must be a date or date-time`);
    let validTo: number | undefined;
    if (o.valid_to !== undefined && o.valid_to !== null) {
      validTo = parseInstant(o.valid_to);
      if (validTo === undefined || validTo <= validFrom)
        throw new Error(`${at}.valid_to must be a date-time after valid_from`);
    }
    const out: PriceOverride = {
      provider: normalizeProvider(o.provider),
      model: o.model,
      validFrom,
      validTo,
    };
    if (o.multiplier !== undefined) {
      if (!isNumber(o.multiplier) || o.multiplier <= 0 || o.multiplier > 10)
        throw new Error(`${at}.multiplier must be above 0 and at most 10`);
      out.multiplier = o.multiplier;
      return out;
    }
    for (const k of RATE_KEYS) {
      if (o[k] !== undefined && (!isNumber(o[k]) || o[k] < 0))
        throw new Error(`${at}.${k} must be a number of at least 0`);
    }
    if (o.input_per_1k === undefined || o.output_per_1k === undefined)
      throw new Error(
        `${at} needs a multiplier or input_per_1k and output_per_1k`
      );
    out.input = o.input_per_1k * fx;
    out.output = o.output_per_1k * fx;
    if (o.cached_input_per_1k !== undefined)
      out.cachedInput = o.cached_input_per_1k * fx;
    if (o.cache_write_per_1k !== undefined)
      out.cacheWrite = o.cache_write_per_1k * fx;
    return out;
  });
  return { overrides };
}

/** The override for the first of `models` in force at `at`, else `*`. */
export function findOverride(
  book: PriceBook,
  vendor: string,
  models: string[],
  at: number
): PriceOverride | undefined {
  const v = normalizeProvider(vendor);
  const live = book.overrides.filter(
    (o) =>
      o.provider === v &&
      o.validFrom <= at &&
      (o.validTo === undefined || at < o.validTo)
  );
  for (const m of models) {
    const exact = live.find((o) => o.model !== '*' && o.model === m);
    if (exact) return exact;
  }
  return live.find((o) => o.model === '*');
}

const round6 = (x: number) => Number(x.toFixed(6));

function tokenCost(
  o: PriceOverride,
  vendor: string,
  u: PricingUsage
): number | null {
  const tokensIn = u.tokens_in;
  const tokensOut = u.tokens_out;
  if (tokensIn == null || tokensOut == null || u.usage_unit_type != null)
    return null;
  let scale = 1;
  const tier = u.service_tier;
  if (tier != null && !PRICE_TABLES.STANDARD_SERVICE_TIERS.has(tier)) {
    const m = PRICE_TABLES.SERVICE_TIER_MULTIPLIER[vendor]?.[tier];
    if (m === undefined) return null;
    scale = m;
  }
  const inRate = o.input ?? 0;
  const outRate = o.output ?? 0;
  const cachedRate =
    o.cachedInput ??
    inRate * (PRICE_TABLES.CACHE_READ_MULTIPLIER[vendor] ?? 1.0);
  const writeMult = PRICE_TABLES.CACHE_WRITE_MULTIPLIER[vendor] ?? 1.0;
  const writeRate = o.cacheWrite ?? inRate * writeMult;
  const write1hRate =
    o.cacheWrite ??
    inRate * (PRICE_TABLES.CACHE_WRITE_1H_MULTIPLIER[vendor] ?? writeMult);
  const cached = u.tokens_cached || 0;
  const write = u.tokens_cache_write || 0;
  const write1h = Math.min(u.tokens_cache_write_1h || 0, write);
  const uncached = Math.max(tokensIn - cached - write, 0);
  const cost =
    (uncached * inRate +
      cached * cachedRate +
      (write - write1h) * writeRate +
      write1h * write1hRate +
      tokensOut * outRate) /
    1000.0;
  return round6(cost * scale);
}

/**
 * The negotiated cost of a call, or null when no override changes it.
 * `cost`/`costSource` are what the pricing table produced (undefined when
 * the call has no list price).
 */
export function negotiatedCost(
  book: PriceBook | undefined,
  vendor: string,
  models: string[],
  usage: PricingUsage,
  cost: number | undefined,
  costSource: string | undefined,
  at: number = Date.now()
): number | null {
  if (!book || !book.overrides.length) return null;
  if (costSource !== undefined && KEPT_COST_SOURCES.has(costSource))
    return null;
  const o = findOverride(book, vendor, models, at);
  if (!o) return null;
  if (o.multiplier !== undefined)
    return cost === undefined ? null : round6(cost * o.multiplier);
  return tokenCost(o, normalizeProvider(vendor), usage);
}
