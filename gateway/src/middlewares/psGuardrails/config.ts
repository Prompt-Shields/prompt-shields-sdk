/**
 * PS fork (#349): the central policy's `guardrails` v1 section.
 *
 * Mirrors packages/collector/collector/policy_doc.py `_guardrails_v1`: the
 * collector refuses a bad section when it is saved, and this gateway
 * refuses a bundle carrying one (policyPull/state.ts keeps the last good
 * policy), so a malformed section never replaces working guardrails.
 *
 *   {"version": 1,
 *    "stream_window_chars": 256,
 *    "rules": [{"id": "redact-email", "check": "pii", "action": "redact",
 *               "stages": ["input", "output"], "categories": ["email"],
 *               "models": ["gpt-4o*"]}]}
 *
 * docs/guides/guardrails.mdx is the user guide.
 */

export type GuardrailCheck = 'pii' | 'secrets' | 'injection';
export type GuardrailAction = 'deny' | 'redact' | 'log';
export type GuardrailStageName = 'input' | 'output';

/** PII categories with a value findPiiSpans can redact. */
export const PII_VALUE_CATEGORIES = [
  'credit_card',
  'email',
  'iban',
  'ip_address',
  'national_id',
  'phone',
  'ssn',
] as const;
/** Keyword categories: context, not a value (deny or log only). */
export const PII_CONTEXT_CATEGORIES = [
  'financial_data',
  'health_data',
] as const;
export const SECRET_CATEGORIES = [
  'anthropic_key',
  'aws_access_key',
  'github_token',
  'google_api_key',
  'jwt',
  'openai_key',
  'private_key',
  'slack_token',
  'stripe_key',
] as const;
export const INJECTION_CATEGORY = 'prompt_injection';

const CHECKS: Record<
  GuardrailCheck,
  { categories: readonly string[]; actions: GuardrailAction[] }
> = {
  pii: {
    categories: [...PII_VALUE_CATEGORIES, ...PII_CONTEXT_CATEGORIES],
    actions: ['deny', 'redact', 'log'],
  },
  secrets: {
    categories: SECRET_CATEGORIES,
    actions: ['deny', 'redact', 'log'],
  },
  injection: { categories: [], actions: ['deny', 'log'] },
};

/** Categories a rule covers when it names none. */
const DEFAULT_CATEGORIES: Record<GuardrailCheck, readonly string[]> = {
  pii: PII_VALUE_CATEGORIES,
  secrets: SECRET_CATEGORIES,
  injection: [INJECTION_CATEGORY],
};

export const DEFAULT_STREAM_WINDOW_CHARS = 256;
export const STREAM_WINDOW_RANGE = [64, 4096] as const;
export const MAX_RULES = 64;
const RULE_ID = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
const MODEL_PATTERN = /^[A-Za-z0-9_.:@/ +*-]{1,128}$/;
const RULE_KEYS = new Set([
  'id',
  'check',
  'action',
  'stages',
  'categories',
  'models',
]);

/** Stricter actions win when several rules match one finding. */
export const ACTION_RANK: Record<GuardrailAction, number> = {
  log: 0,
  redact: 1,
  deny: 2,
};

export interface GuardrailRule {
  id: string;
  check: GuardrailCheck;
  action: GuardrailAction;
  stages: GuardrailStageName[];
  categories: Set<string>;
  /** Model patterns (glob, case-insensitive); undefined: every model. */
  models?: RegExp[];
  /** The policy version the rule came from (`default@3+team-eu@2`). */
  policyVersion?: string;
  /** The rule's content, for dropping the same rule inherited twice. */
  key: string;
}

export interface GuardrailsSection {
  rules: GuardrailRule[];
  streamWindowChars: number;
}

export class GuardrailsConfigError extends Error {}

const isObject = (v: unknown): v is Record<string, any> =>
  !!v && typeof v === 'object' && !Array.isArray(v);

const globToRegExp = (pattern: string): RegExp =>
  new RegExp(
    '^' +
      pattern
        .split('*')
        .map((p) => p.replace(/[.+?^${}()|[\]\\/]/g, '\\$&'))
        .join('.*') +
      '$',
    'i'
  );

/**
 * A guardrails v1 section (the section object, `version` included).
 * Throws GuardrailsConfigError listing every problem.
 */
export function parseGuardrailsSection(
  raw: unknown,
  policyVersion?: string
): GuardrailsSection {
  const problems: string[] = [];
  if (!isObject(raw)) throw new GuardrailsConfigError('must be an object');
  const extra = Object.keys(raw).filter(
    (k) => !['version', 'rules', 'stream_window_chars'].includes(k)
  );
  if (extra.length) problems.push(`unknown keys ${JSON.stringify(extra)}`);
  let streamWindowChars = DEFAULT_STREAM_WINDOW_CHARS;
  if ('stream_window_chars' in raw) {
    const w = raw.stream_window_chars;
    const [lo, hi] = STREAM_WINDOW_RANGE;
    if (!Number.isInteger(w) || w < lo || w > hi)
      problems.push(`stream_window_chars must be an integer ${lo}-${hi}`);
    else streamWindowChars = w;
  }
  const rules: GuardrailRule[] = [];
  if (!Array.isArray(raw.rules)) {
    problems.push('rules must be an array');
  } else {
    if (raw.rules.length > MAX_RULES)
      problems.push(`rules: at most ${MAX_RULES} rules`);
    const seen = new Set<string>();
    raw.rules.forEach((r: unknown, i: number) => {
      const at = `rules[${i}]`;
      if (!isObject(r)) {
        problems.push(`${at} must be an object`);
        return;
      }
      const unknown = Object.keys(r).filter((k) => !RULE_KEYS.has(k));
      if (unknown.length)
        problems.push(`${at}: unknown keys ${JSON.stringify(unknown)}`);
      const id = r.id;
      if (typeof id !== 'string' || !RULE_ID.test(id))
        problems.push(`${at}.id must be 1-64 characters of [a-z0-9_.-]`);
      else if (seen.has(id)) problems.push(`${at}.id "${id}" is used twice`);
      else seen.add(id);
      const check = r.check as GuardrailCheck;
      if (
        typeof check !== 'string' ||
        !Object.prototype.hasOwnProperty.call(CHECKS, check)
      ) {
        problems.push(`${at}.check must be one of ${Object.keys(CHECKS)}`);
        return;
      }
      const spec = CHECKS[check];
      const action = r.action as GuardrailAction;
      if (!spec.actions.includes(action))
        problems.push(
          `${at}.action must be one of ${spec.actions.join(', ')} for ${check}`
        );
      let stages: GuardrailStageName[] = ['input', 'output'];
      if ('stages' in r) {
        const st = r.stages;
        if (
          !Array.isArray(st) ||
          !st.length ||
          !st.every((s) => s === 'input' || s === 'output') ||
          new Set(st).size !== st.length
        )
          problems.push(
            `${at}.stages must be a non-empty subset of input, output`
          );
        else stages = st;
      }
      let categories = new Set<string>(DEFAULT_CATEGORIES[check]);
      if ('categories' in r) {
        const cats = r.categories;
        if (!spec.categories.length)
          problems.push(`${at}.categories: ${check} has no categories`);
        else if (
          !Array.isArray(cats) ||
          !cats.length ||
          !cats.every(
            (c) => typeof c === 'string' && spec.categories.includes(c)
          )
        )
          problems.push(
            `${at}.categories must be a non-empty array of ${spec.categories.join(', ')}`
          );
        else if (
          action === 'redact' &&
          cats.some((c) =>
            (PII_CONTEXT_CATEGORIES as readonly string[]).includes(c)
          )
        )
          problems.push(
            `${at}: health_data and financial_data mark context, not a value, and cannot be redacted (use deny or log)`
          );
        else categories = new Set(cats);
      }
      let models: RegExp[] | undefined;
      if ('models' in r) {
        const ms = r.models;
        if (
          !Array.isArray(ms) ||
          !ms.length ||
          !ms.every((m) => typeof m === 'string' && MODEL_PATTERN.test(m))
        )
          problems.push(
            `${at}.models must be a non-empty array of model patterns`
          );
        else models = ms.map(globToRegExp);
      }
      rules.push({
        id,
        check,
        action,
        stages,
        categories,
        models,
        policyVersion,
        key: JSON.stringify([
          id,
          check,
          action,
          stages,
          [...categories].sort(),
          r.models ?? null,
        ]),
      });
    });
  }
  if (problems.length) throw new GuardrailsConfigError(problems.join('; '));
  return { rules, streamWindowChars };
}

/**
 * The rules that apply to `stage` for a call that may run any of `models`
 * (the body's model, config overrides, the routed model). A rule with
 * `models` applies when any of them matches, and also when none is known:
 * a call whose model cannot be told is not let off a model-scoped rule.
 */
export function rulesFor(
  rules: GuardrailRule[],
  stage: GuardrailStageName,
  models: (string | undefined)[]
): GuardrailRule[] {
  const known = models.filter((m): m is string => !!m);
  return rules.filter(
    (r) =>
      r.stages.includes(stage) &&
      (!r.models ||
        !known.length ||
        known.some((m) => r.models!.some((re) => re.test(m))))
  );
}

/**
 * Model names an `x-portkey-config` header can send a call to: every
 * `model` (override_params) and Azure deployment in it, at any depth
 * (targets, fallbacks). A config id or unparsable value gives none.
 */
export function configModels(header: string | undefined): string[] {
  if (!header || header.trim()[0] !== '{') return [];
  let config: unknown;
  try {
    config = JSON.parse(header);
  } catch {
    return [];
  }
  const out: string[] = [];
  const keys = [
    'model',
    'deployment_id',
    'deploymentId',
    'azure_deployment_name',
  ];
  const walk = (node: unknown, depth: number) => {
    if (depth > 16 || !node || typeof node !== 'object') return;
    for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
      if (typeof v === 'string' && keys.includes(k)) out.push(v);
      else walk(v, depth + 1);
    }
  };
  walk(config, 0);
  return out.slice(0, 64);
}

/**
 * Several sections as one: every rule of each (guardrails add up across
 * tenant, team and project), a rule a team or project inherited from the
 * tenant once (the first, most general, kept), and the widest stream
 * window.
 */
export function combineSections(
  sections: (GuardrailsSection | undefined)[]
): GuardrailsSection | undefined {
  const present = sections.filter((s): s is GuardrailsSection => !!s);
  if (!present.length) return undefined;
  if (present.length === 1) return present[0];
  const seen = new Set<string>();
  const rules: GuardrailRule[] = [];
  for (const s of present)
    for (const r of s.rules) {
      if (seen.has(r.key)) continue;
      seen.add(r.key);
      rules.push(r);
    }
  return {
    rules,
    streamWindowChars: Math.max(...present.map((s) => s.streamWindowChars)),
  };
}
