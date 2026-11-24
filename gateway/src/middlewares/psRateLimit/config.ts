/**
 * PS fork (#344): the `rate_limits` v1 policy section.
 *
 * The same body is the central policy's `rate_limits` section
 * (packages/collector/collector/policy_doc.py validates it there) and the
 * local PS_RATE_LIMITS / PS_RATE_LIMITS_FILE document:
 *
 *   {
 *     "version": 1,
 *     "on_store_error": "open",            // or "closed"; default "open"
 *     "rules": [
 *       {
 *         "id": "team-chat",               // [A-Za-z0-9_.:-]{1,64}, unique
 *         "scope": "team",                 // global | virtual_key | team | project | user
 *         "subjects": ["team-a"],          // optional: only these values (user: the
 *                                          // OIDC sub or its hash; virtual_key: the id,
 *                                          // or key:<fingerprint> for a provider key)
 *         "match": {                       // optional; all given keys must match
 *           "model": ["gpt-4o*"],          // string or array; `*` is a wildcard
 *           "provider": "openai",
 *           "route": "/v1/chat/completions"
 *         },
 *         "rpm": 600, "tpm": 200000, "tpd": 5000000, "concurrency": 20
 *       }
 *     ]
 *   }
 *
 * Each rule needs at least one of rpm, tpm, tpd, concurrency (positive
 * integers). A rule counts per distinct value of its scope (one counter for
 * `global`); every rule that applies to a call is enforced, so the strictest
 * one decides.
 */

export const RATE_LIMIT_SECTION_VERSION = 1;
export const MAX_RULES = 200;
export const MAX_MATCH_VALUES = 50;

export type RateLimitScope =
  | 'global'
  | 'virtual_key'
  | 'team'
  | 'project'
  | 'user';

export const SCOPES: RateLimitScope[] = [
  'global',
  'virtual_key',
  'team',
  'project',
  'user',
];

/** One limit of a rule: requests/min, tokens/min, tokens/day or in-flight calls. */
export type LimitKind = 'rpm' | 'tpm' | 'tpd' | 'concurrency';
export const LIMIT_KINDS: LimitKind[] = ['rpm', 'tpm', 'tpd', 'concurrency'];

export interface RateLimitRule {
  id: string;
  scope: RateLimitScope;
  subjects?: string[];
  match: { model?: RegExp[]; provider?: RegExp[]; route?: RegExp[] };
  rpm?: number;
  tpm?: number;
  tpd?: number;
  concurrency?: number;
}

export interface RateLimitConfig {
  onStoreError: 'open' | 'closed';
  rules: RateLimitRule[];
}

export class RateLimitConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'RateLimitConfigError';
  }
}

const RULE_ID = /^[A-Za-z0-9_.:-]{1,64}$/;
const RULE_KEYS = new Set(['id', 'scope', 'subjects', 'match', ...LIMIT_KINDS]);
const MATCH_KEYS = ['model', 'provider', 'route'] as const;

const isObject = (v: unknown): v is Record<string, any> =>
  !!v && typeof v === 'object' && !Array.isArray(v);

/** A `*` wildcard pattern as an anchored RegExp (`i` for providers). */
export function globToRegExp(glob: string, flags = ''): RegExp {
  const escaped = glob
    .split('*')
    .map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .join('.*');
  return new RegExp(`^${escaped}$`, flags);
}

function strings(at: string, v: unknown): string[] {
  const list = typeof v === 'string' ? [v] : v;
  if (
    !Array.isArray(list) ||
    !list.length ||
    list.length > MAX_MATCH_VALUES ||
    !list.every((s) => typeof s === 'string' && s.trim() && s.length <= 256)
  )
    throw new RateLimitConfigError(
      `${at} must be a non-empty string or array of up to ${MAX_MATCH_VALUES} non-empty strings`
    );
  return list as string[];
}

/** Parses a `rate_limits` v1 section body. Throws RateLimitConfigError. */
export function parseRateLimitSection(raw: unknown): RateLimitConfig {
  if (!isObject(raw))
    throw new RateLimitConfigError('the rate_limits section must be an object');
  const extra = Object.keys(raw).filter(
    (k) => !['version', 'on_store_error', 'rules'].includes(k)
  );
  if (extra.length)
    throw new RateLimitConfigError(`unknown keys ${JSON.stringify(extra)}`);
  if (raw.version !== RATE_LIMIT_SECTION_VERSION)
    throw new RateLimitConfigError(
      `version must be ${RATE_LIMIT_SECTION_VERSION}`
    );
  const onStoreError = raw.on_store_error ?? 'open';
  if (onStoreError !== 'open' && onStoreError !== 'closed')
    throw new RateLimitConfigError('on_store_error must be "open" or "closed"');
  if (!Array.isArray(raw.rules))
    throw new RateLimitConfigError('rules must be an array');
  if (raw.rules.length > MAX_RULES)
    throw new RateLimitConfigError(`at most ${MAX_RULES} rules`);
  const seen = new Set<string>();
  const rules = raw.rules.map((r: unknown, i: number): RateLimitRule => {
    const at = `rules[${i}]`;
    if (!isObject(r)) throw new RateLimitConfigError(`${at} must be an object`);
    const unknown = Object.keys(r).filter((k) => !RULE_KEYS.has(k));
    if (unknown.length)
      throw new RateLimitConfigError(
        `${at}: unknown keys ${JSON.stringify(unknown)}`
      );
    if (typeof r.id !== 'string' || !RULE_ID.test(r.id))
      throw new RateLimitConfigError(
        `${at}.id must be 1-64 characters of [A-Za-z0-9_.:-]`
      );
    if (seen.has(r.id))
      throw new RateLimitConfigError(`${at}.id "${r.id}" is used twice`);
    seen.add(r.id);
    if (!SCOPES.includes(r.scope))
      throw new RateLimitConfigError(
        `${at}.scope must be one of ${SCOPES.join(', ')}`
      );
    const rule: RateLimitRule = { id: r.id, scope: r.scope, match: {} };
    if (r.subjects !== undefined) {
      if (r.scope === 'global')
        throw new RateLimitConfigError(
          `${at}.subjects is not allowed with scope "global"`
        );
      rule.subjects = strings(`${at}.subjects`, r.subjects);
    }
    if (r.match !== undefined) {
      if (!isObject(r.match))
        throw new RateLimitConfigError(`${at}.match must be an object`);
      const bad = Object.keys(r.match).filter(
        (k) => !(MATCH_KEYS as readonly string[]).includes(k)
      );
      if (bad.length)
        throw new RateLimitConfigError(
          `${at}.match: unknown keys ${JSON.stringify(bad)} (known: ${MATCH_KEYS.join(', ')})`
        );
      for (const k of MATCH_KEYS) {
        if (r.match[k] === undefined) continue;
        rule.match[k] = strings(`${at}.match.${k}`, r.match[k]).map((g) =>
          globToRegExp(g, k === 'provider' ? 'i' : '')
        );
      }
    }
    let any = false;
    for (const k of LIMIT_KINDS) {
      if (r[k] === undefined) continue;
      if (!Number.isSafeInteger(r[k]) || r[k] < 1)
        throw new RateLimitConfigError(`${at}.${k} must be a positive integer`);
      rule[k] = r[k];
      any = true;
    }
    if (!any)
      throw new RateLimitConfigError(
        `${at} needs at least one of ${LIMIT_KINDS.join(', ')}`
      );
    return rule;
  });
  return { onStoreError, rules };
}

/**
 * The local document (PS_RATE_LIMITS, or the text of PS_RATE_LIMITS_FILE):
 * a `rate_limits` v1 section body. Empty: none.
 */
export function rateLimitConfigFromText(
  text: string | undefined
): RateLimitConfig | undefined {
  if (!text || !text.trim()) return undefined;
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (err) {
    throw new RateLimitConfigError(
      `rate limits are not valid JSON: ${(err as Error).message}`
    );
  }
  return parseRateLimitSection(raw);
}

export interface CallAttributes {
  model?: string;
  provider?: string;
  route: string;
  /** Subject per non-global scope (the counter key); absent when none. */
  subjects: Partial<Record<Exclude<RateLimitScope, 'global'>, string>>;
  /**
   * Other values a rule's `subjects` may name for a scope, when they differ
   * from the counter subject: the OIDC `sub` claim for `user` (the subject is
   * its hash), the virtual-key id for `virtual_key` (the subject is
   * `vk:<id>` or `key:<fingerprint>`).
   */
  subjectNames?: Partial<Record<Exclude<RateLimitScope, 'global'>, string[]>>;
}

/** The subject the rule counts this call under, or undefined if it does not apply. */
export function ruleSubject(
  rule: RateLimitRule,
  call: CallAttributes
): string | undefined {
  const m = rule.match;
  const test = (patterns: RegExp[] | undefined, value: string | undefined) =>
    !patterns || (value !== undefined && patterns.some((p) => p.test(value)));
  if (!test(m.model, call.model)) return undefined;
  if (!test(m.provider, call.provider)) return undefined;
  if (!test(m.route, call.route)) return undefined;
  if (rule.scope === 'global') return '_';
  const subject = call.subjects[rule.scope];
  if (!subject) return undefined;
  if (rule.subjects) {
    const names = [subject, ...(call.subjectNames?.[rule.scope] ?? [])];
    if (!names.some((n) => rule.subjects!.includes(n))) return undefined;
  }
  return subject;
}
