/**
 * Frozen savings baseline and counterfactual cost (PRD 0001 §7.10, #24).
 *
 * A `baseline.v1` document (schemas/baseline/) names the model each use case
 * would have used without routing, with that model's per-token USD rates
 * copied from the catalog when the baseline was frozen. A routed event gets
 * `baseline_id` and `counterfactual_cost`: its actual tokens priced at the
 * baseline model's frozen rates, so a later catalog release never moves it.
 *
 * Same rules as the SDK (packages/sdk/prompt_shields/baseline.py); both run
 * the shared vectors in schemas/baseline/vectors/. No `fs` here: the Node
 * loader is ./baselineLoader.ts.
 */

export const BASELINE_SCHEMA_VERSION = 'baseline.v1';
export const MAX_BASELINE_ID_LENGTH = 128;

const RATE_FIELDS = [
  'input_cost_per_token',
  'output_cost_per_token',
  'cache_read_input_token_cost',
  'cache_creation_input_token_cost',
] as const;

type RateField = (typeof RATE_FIELDS)[number];
export type BaselineRates = Record<RateField, number>;

/** Standard service tiers (pricing.py STANDARD_SERVICE_TIERS). */
const STANDARD_SERVICE_TIERS = new Set(['default', 'standard', 'auto']);

export interface Baseline {
  baselineId: string;
  catalogVersion: string;
  frozenAt: string;
  defaultModel: string;
  useCases: Record<string, string>;
  models: Record<string, BaselineRates>;
}

export class BaselineError extends Error {}

const isObject = (v: unknown): v is Record<string, any> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

const own = (obj: Record<string, any>, key: string) =>
  Object.prototype.hasOwnProperty.call(obj, key);

function rate(value: unknown, where: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    throw new BaselineError(`${where} must be a non-negative number`);
  }
  return value;
}

/** Validate a `baseline.v1` document. Throws `BaselineError`. */
export function parseBaseline(doc: unknown): Baseline {
  if (!isObject(doc)) throw new BaselineError('baseline must be a JSON object');
  if (doc.schema_version !== BASELINE_SCHEMA_VERSION) {
    throw new BaselineError(
      `baseline must have schema_version '${BASELINE_SCHEMA_VERSION}'`
    );
  }
  const id = doc.baseline_id;
  if (
    typeof id !== 'string' ||
    !id.trim() ||
    id.length > MAX_BASELINE_ID_LENGTH ||
    id.includes('\u0000')
  ) {
    throw new BaselineError(
      `baseline_id must be a non-empty string of at most ${MAX_BASELINE_ID_LENGTH} characters`
    );
  }
  for (const key of ['catalog_version', 'frozen_at']) {
    if (typeof doc[key] !== 'string' || !doc[key]) {
      throw new BaselineError(`baseline is missing '${key}'`);
    }
  }
  if (own(doc, 'currency') && doc.currency !== 'USD') {
    throw new BaselineError("baseline currency must be 'USD'");
  }
  if (!isObject(doc.models) || !Object.keys(doc.models).length) {
    throw new BaselineError('baseline must list its models');
  }
  const models: Record<string, BaselineRates> = Object.create(null);
  for (const [key, entry] of Object.entries(doc.models)) {
    if (!isObject(entry)) {
      throw new BaselineError(`models['${key}'] must be an object`);
    }
    const rates = {} as BaselineRates;
    for (const field of RATE_FIELDS) {
      rates[field] = rate(entry[field], `models['${key}'].${field}`);
    }
    models[key] = rates;
  }
  const defaultModel = doc.default_model;
  if (typeof defaultModel !== 'string' || !own(models, defaultModel)) {
    throw new BaselineError('default_model must be one of the baseline models');
  }
  const useCases: Record<string, string> = Object.create(null);
  const rawUseCases = doc.use_cases ?? {};
  if (!isObject(rawUseCases)) {
    throw new BaselineError('use_cases must be an object');
  }
  for (const [useCase, model] of Object.entries(rawUseCases)) {
    if (typeof model !== 'string' || !own(models, model)) {
      throw new BaselineError(
        `use_cases['${useCase}'] names a model that is not in models`
      );
    }
    useCases[useCase] = model;
  }
  return {
    baselineId: id,
    catalogVersion: doc.catalog_version,
    frozenAt: doc.frozen_at,
    defaultModel,
    useCases,
    models,
  };
}

/** The baseline model of `useCase`, else the default model. */
export function baselineModelFor(baseline: Baseline, useCase: unknown): string {
  if (typeof useCase === 'string' && own(baseline.useCases, useCase)) {
    return baseline.useCases[useCase];
  }
  return baseline.defaultModel;
}

function count(event: Record<string, any>, name: string): number | undefined {
  const v = event[name];
  return Number.isInteger(v) && v >= 0 ? v : undefined;
}

/**
 * What the event's tokens would have cost on its baseline model (USD), or
 * undefined when it cannot be priced: tokens_in or tokens_out missing, or a
 * non-standard service tier (batch/flex are discounted).
 */
export function counterfactualCost(
  baseline: Baseline,
  event: Record<string, any>
): number | undefined {
  const tier = event.service_tier;
  if (tier != null && !STANDARD_SERVICE_TIERS.has(tier)) return undefined;
  const tokensIn = count(event, 'tokens_in');
  const tokensOut = count(event, 'tokens_out');
  if (tokensIn === undefined || tokensOut === undefined) return undefined;
  const cached = count(event, 'tokens_cached') ?? 0;
  const write = count(event, 'tokens_cache_write') ?? 0;
  const r = baseline.models[baselineModelFor(baseline, event.use_case_name)];
  const uncached = Math.max(tokensIn - cached - write, 0);
  // Same order of operations as the SDK, so both give the same float.
  return (
    uncached * r.input_cost_per_token +
    cached * r.cache_read_input_token_cost +
    write * r.cache_creation_input_token_cost +
    tokensOut * r.output_cost_per_token
  );
}

/** Set `baseline_id` and, when priced, `counterfactual_cost` on `event`. */
export function applyBaseline(
  baseline: Baseline,
  event: Record<string, any>
): void {
  event.baseline_id = baseline.baselineId;
  const cost = counterfactualCost(baseline, event);
  if (cost !== undefined) event.counterfactual_cost = cost;
}

let activeBaseline: Baseline | undefined;

/** The gateway's baseline (PS_BASELINE_FILE), if one was loaded. */
export function getBaseline(): Baseline | undefined {
  return activeBaseline;
}

export function setBaseline(baseline: Baseline | undefined): void {
  activeBaseline = baseline;
}
