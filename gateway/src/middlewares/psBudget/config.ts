/**
 * Budget configuration: a budget.v1 document (schemas/budget/), shared with
 * the SDK, from PS_BUDGETS or, on Node, PS_BUDGETS_FILE (read by ./loader.ts
 * so edge bundles never import `fs`). Validation follows the SDK's
 * (`load_budget_policy`). An invalid document is an operator error and
 * throws: running without the budgets the operator asked for would silently
 * drop spend governance.
 */
import { BudgetScope, BudgetUnit, BudgetWindow } from './keys';

export type BudgetMode = 'warn' | 'downgrade' | 'block';

export interface Budget {
  id: string;
  scope: BudgetScope;
  /** Attribute value, or '*' for one counter per value; undefined for global. */
  subject?: string;
  window: BudgetWindow;
  /** What the budget limits: USD (limit_usd) or tokens (limit_tokens). */
  unit: BudgetUnit;
  /** The limit in `unit`. */
  limit: number;
  /** limit_usd, for a USD budget. */
  limitUsd?: number;
  /** limit_tokens, for a token budget. */
  limitTokens?: number;
  mode: BudgetMode;
  warnAt: number;
}

export interface BudgetConfig {
  namespace: string;
  onStoreError: 'open' | 'closed';
  budgets: Budget[];
}

export const SCOPES: BudgetScope[] = [
  'global',
  'business_unit',
  'use_case',
  'owner',
  'team',
  'project',
  'user',
  'service',
  'model',
];
const WINDOWS: BudgetWindow[] = ['day', 'week', 'month'];
const MODES: BudgetMode[] = ['warn', 'downgrade', 'block'];
const TOKEN_RE = /^[a-z0-9][a-z0-9._-]*$/;
const BUDGET_FIELDS = [
  'id',
  'scope',
  'subject',
  'window',
  'limit_usd',
  'limit_tokens',
  'mode',
  'warn_at',
  'downgrade_model',
];

function fail(msg: string): never {
  throw new Error(`[PS Budget] invalid budget config: ${msg}`);
}

const isNumber = (v: unknown): v is number =>
  typeof v === 'number' && Number.isFinite(v);

/** Parse and validate a budget.v1 document's `budgets` list. */
export function parseBudgets(list: unknown): Budget[] {
  if (!Array.isArray(list)) fail('budgets must be an array');
  const seen = new Set<string>();
  return list.map((raw: any): Budget => {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw))
      fail('each budget must be an object');
    const id = raw.id;
    if (typeof id !== 'string' || !TOKEN_RE.test(id))
      fail(`budget id must match ${TOKEN_RE}, got ${JSON.stringify(id)}`);
    for (const k of Object.keys(raw)) {
      if (!BUDGET_FIELDS.includes(k))
        fail(`budget "${id}": unknown field "${k}"`);
    }
    if (seen.has(id)) fail(`duplicate budget id "${id}"`);
    seen.add(id);
    if (!SCOPES.includes(raw.scope))
      fail(`budget "${id}": scope must be one of ${SCOPES.join(', ')}`);
    const subject = raw.subject;
    if (raw.scope === 'global') {
      if (subject !== undefined)
        fail(`budget "${id}": a global budget has no subject`);
    } else if (typeof subject !== 'string' || !subject) {
      fail(`budget "${id}": scope "${raw.scope}" needs a subject`);
    }
    if (!WINDOWS.includes(raw.window))
      fail(`budget "${id}": window must be one of ${WINDOWS.join(', ')}`);
    const hasUsd = raw.limit_usd !== undefined;
    const hasTokens = raw.limit_tokens !== undefined;
    if (hasUsd === hasTokens)
      fail(`budget "${id}": needs exactly one of limit_usd, limit_tokens`);
    if (hasUsd && (!isNumber(raw.limit_usd) || raw.limit_usd < 0))
      fail(`budget "${id}": limit_usd must be a number >= 0`);
    if (
      hasTokens &&
      (!Number.isInteger(raw.limit_tokens) || raw.limit_tokens < 0)
    )
      fail(`budget "${id}": limit_tokens must be an integer >= 0`);
    if (!MODES.includes(raw.mode))
      fail(`budget "${id}": mode must be one of ${MODES.join(', ')}`);
    if (hasTokens && raw.mode === 'downgrade')
      fail(`budget "${id}": a token budget's mode must be "warn" or "block"`);
    const warnAt = raw.warn_at ?? 0.8;
    if (!isNumber(warnAt) || !(warnAt > 0 && warnAt <= 1))
      fail(`budget "${id}": warn_at must be in (0, 1]`);
    // downgrade_model is for plain SDK clients; the gateway routes instead.
    if (raw.downgrade_model !== undefined) {
      if (raw.mode !== 'downgrade')
        fail(`budget "${id}": downgrade_model needs mode "downgrade"`);
      if (typeof raw.downgrade_model !== 'string' || !raw.downgrade_model)
        fail(`budget "${id}": downgrade_model must be a model id`);
    }
    return {
      id,
      scope: raw.scope,
      ...(subject !== undefined ? { subject } : {}),
      window: raw.window,
      unit: hasTokens ? 'tokens' : 'usd',
      limit: hasTokens ? raw.limit_tokens : raw.limit_usd,
      ...(hasTokens
        ? { limitTokens: raw.limit_tokens }
        : { limitUsd: raw.limit_usd }),
      mode: raw.mode,
      warnAt,
    };
  });
}

/** Parse and validate a budget.v1 document. */
export function parseBudgetConfig(doc: unknown): BudgetConfig {
  if (!doc || typeof doc !== 'object' || Array.isArray(doc))
    fail('expected an object');
  const d = doc as Record<string, any>;
  if (d.schema_version !== 'budget.v1')
    fail('schema_version must be "budget.v1"');
  for (const k of Object.keys(d)) {
    if (
      ![
        '$schema',
        'schema_version',
        'namespace',
        'on_store_error',
        'budgets',
      ].includes(k)
    )
      fail(`unknown field "${k}"`);
  }
  const namespace = d.namespace ?? 'default';
  if (typeof namespace !== 'string' || !TOKEN_RE.test(namespace))
    fail(`namespace must match ${TOKEN_RE}`);
  const onStoreError = d.on_store_error ?? 'open';
  if (onStoreError !== 'open' && onStoreError !== 'closed')
    fail('on_store_error must be "open" or "closed"');
  const budgets = parseBudgets(d.budgets);
  return { namespace, onStoreError, budgets };
}

/** A budget.v1 document as text -> its budgets (undefined when it has none). */
export function budgetConfigFromText(
  text: string | undefined,
  namespaceOverride?: string
): BudgetConfig | undefined {
  if (!text || !text.trim()) return undefined;
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch (err) {
    fail(`not JSON: ${(err as Error).message}`);
  }
  const config = parseBudgetConfig(doc);
  if (namespaceOverride) {
    if (!TOKEN_RE.test(namespaceOverride))
      fail(`PS_BUDGET_NAMESPACE must match ${TOKEN_RE}`);
    config.namespace = namespaceOverride;
  }
  return config.budgets.length ? config : undefined;
}

/** The central policy's `budgets` section (v1), see policy_doc.py. */
export interface CentralBudgets {
  onStoreError: 'open' | 'closed';
  budgets: Budget[];
}

/**
 * Parse a `budgets` v1 section of the central policy:
 * `{"version": 1, "on_store_error"?: "open"|"closed", "budgets": [...]}`,
 * the budgets as in budget.v1. No namespace: the gateway's own
 * (PS_BUDGET_NAMESPACE, else the local document's, else "default") applies.
 */
export function parseBudgetsSection(body: unknown): CentralBudgets {
  if (!body || typeof body !== 'object' || Array.isArray(body))
    fail('the budgets section must be an object');
  const b = body as Record<string, any>;
  for (const k of Object.keys(b)) {
    if (!['version', 'on_store_error', 'budgets'].includes(k))
      fail(`unknown field "${k}"`);
  }
  const onStoreError = b.on_store_error ?? 'open';
  if (onStoreError !== 'open' && onStoreError !== 'closed')
    fail('on_store_error must be "open" or "closed"');
  return { onStoreError, budgets: parseBudgets(b.budgets) };
}

const isTrue = (v?: string) =>
  ['true', '1', 'yes'].includes((v ?? '').trim().toLowerCase());

/**
 * True when the environment asks for budgets: inline or as a file, or a
 * central policy pull (its `budgets` section may carry them).
 */
export const budgetsRequested = (env: Record<string, string | undefined>) =>
  !!(env.PS_BUDGETS || env.PS_BUDGETS_FILE) ||
  isTrue(env.PS_POLICY_PULL_ENABLED);
