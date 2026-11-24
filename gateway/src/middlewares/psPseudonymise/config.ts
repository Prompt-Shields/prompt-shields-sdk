/**
 * PS fork (#350): when the gateway pseudonymises, and what.
 *
 * Off by default. Turned on by the central policy's `pseudonymisation`
 * section (v1, pulled from the collector, see policyPull/state.ts), else by
 * PS_PSEUDONYMISE=true. A central section, when present, replaces the
 * local settings whole (it can also turn pseudonymisation off).
 *
 * Section v1 (the env vars below set the same fields locally):
 *   {"version": 1, "enabled": true,
 *    "categories": ["email", "phone", ...],       // optional, default all
 *    "on_unsupported": "refuse" | "passthrough"}  // optional, default refuse
 *
 * `on_unsupported: refuse` (the default) refuses, with a 400 and before any
 * upstream call, model calls the gateway cannot pseudonymise: endpoints
 * other than Chat Completions, Messages and Responses (embeddings,
 * completions, files, batches, realtime, proxied paths ...), non-JSON
 * bodies, and Responses calls that continue provider-side state
 * (`previous_response_id`, `conversation`: the provider would hold
 * placeholders minted by an earlier request, so a placeholder could be
 * restored to another request's value). `passthrough` sends those as they
 * are (Responses calls with provider state are pseudonymised anyway).
 */
import { ENTITY_CATEGORIES } from '../router/pii';

export type OnUnsupported = 'refuse' | 'passthrough';

export interface PseudonymisationSettings {
  enabled: boolean;
  /** Categories replaced; undefined: all of ENTITY_CATEGORIES. */
  categories?: string[];
  onUnsupported: OnUnsupported;
}

export const SECTION_KEYS = [
  'version',
  'enabled',
  'categories',
  'on_unsupported',
];

const isObject = (v: unknown): v is Record<string, any> =>
  !!v && typeof v === 'object' && !Array.isArray(v);

function parseCategories(raw: unknown, at: string): string[] {
  if (
    !Array.isArray(raw) ||
    !raw.length ||
    raw.some((c) => typeof c !== 'string')
  )
    throw new Error(`${at} must be a non-empty array of category names`);
  const unknown = raw.filter(
    (c) => !(ENTITY_CATEGORIES as readonly string[]).includes(c)
  );
  if (unknown.length)
    throw new Error(
      `${at}: unknown categories ${JSON.stringify(unknown)} (known: ${ENTITY_CATEGORIES.join(', ')})`
    );
  return [...new Set(raw as string[])];
}

function parseOnUnsupported(raw: unknown, at: string): OnUnsupported {
  if (raw === undefined) return 'refuse';
  if (raw !== 'refuse' && raw !== 'passthrough')
    throw new Error(`${at} must be "refuse" or "passthrough"`);
  return raw;
}

/** The central `pseudonymisation` section, v1. Throws on anything invalid. */
export function parsePseudonymisationSection(
  body: unknown
): PseudonymisationSettings {
  if (!isObject(body)) throw new Error('must be an object');
  const extra = Object.keys(body).filter((k) => !SECTION_KEYS.includes(k));
  if (extra.length) throw new Error(`unknown keys ${JSON.stringify(extra)}`);
  if (typeof body.enabled !== 'boolean')
    throw new Error('enabled must be true or false');
  return {
    enabled: body.enabled,
    categories:
      body.categories === undefined
        ? undefined
        : parseCategories(body.categories, 'categories'),
    onUnsupported: parseOnUnsupported(body.on_unsupported, 'on_unsupported'),
  };
}

const truthy = (v: string | undefined) =>
  ['true', '1', 'yes'].includes((v ?? '').trim().toLowerCase());

/**
 * Local settings from PS_PSEUDONYMISE, PS_PSEUDONYMISE_CATEGORIES (comma
 * separated) and PS_PSEUDONYMISE_ON_UNSUPPORTED. Throws on an invalid value
 * when enabled, so a misconfigured gateway fails at start rather than
 * sending values it was told to replace.
 */
export function pseudonymisationFromEnv(
  env: Record<string, string | undefined>
): PseudonymisationSettings | undefined {
  if (!truthy(env.PS_PSEUDONYMISE)) return undefined;
  const cats = env.PS_PSEUDONYMISE_CATEGORIES?.trim();
  return {
    enabled: true,
    categories: cats
      ? parseCategories(
          cats
            .split(',')
            .map((c) => c.trim())
            .filter(Boolean),
          'PS_PSEUDONYMISE_CATEGORIES'
        )
      : undefined,
    onUnsupported: parseOnUnsupported(
      env.PS_PSEUDONYMISE_ON_UNSUPPORTED?.trim() || undefined,
      'PS_PSEUDONYMISE_ON_UNSUPPORTED'
    ),
  };
}
