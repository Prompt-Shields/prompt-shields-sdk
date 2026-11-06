/**
 * Spend budgets (#28): budget.v1 (schemas/budget/, shared with the SDK) per
 * business unit, use case, owner or the whole deployment, by UTC day, ISO
 * week or month, from PS_BUDGETS or PS_BUDGETS_FILE. Spend lives in the
 * shared counters (./keys.ts), in Redis for every replica and the SDK.
 *
 * Per billed call, mounted after psRouter (it sees the routed model and its
 * estimate) and before psTelemetry (refused calls send no event; served
 * calls carry budget_state):
 *
 * 1. Read every matching budget's counter (plus the gateway's in-flight
 *    reservations) and decide as schemas/budget/README.md says (`decide`,
 *    checked against vectors/decisions.json).
 * 2. block: an exhausted block budget refuses the call: HTTP 429
 *    `budget_exceeded`, Retry-After at the period's end (503
 *    `budget_store_unavailable` when the store is down and on_store_error is
 *    closed).
 * 3. downgrade: the least remaining amount of the matching downgrade budgets
 *    is a cost ceiling. A call estimated above it moves to the most capable
 *    cheaper model that fits, else the cheapest, among the models the data
 *    policy allows (./downgrade.ts); never a non-compliant one.
 * 4. Reserve the call's estimate on each counter. For block budgets the
 *    reservation is an atomic check (spent + reserved must be under the
 *    limit), so calls racing on several replicas see each other.
 * 5. After the call, settle: drop the reservation and add the actual cost
 *    (priced as telemetry prices it; a cache hit or failed call costs
 *    nothing; an unpriced call keeps its estimate). Streams settle at the end.
 *
 * Attributes come from X-PS-Business-Unit, X-PS-Use-Case and X-PS-Owner, as
 * the caller declares them for attribution. They are not authenticated: a
 * caller can send another value, or none, and so move its spend to another
 * budget or out of one. Only `global` budgets (never header-controlled) and,
 * when OIDC is on, `owner` budgets are enforcement against the caller: with
 * a verified OIDC identity the owner is the identity's subject hash and
 * X-PS-Owner is ignored.
 */
import { Context, Next } from 'hono';
import { env } from 'hono/adapter';
import { isMcpProxyPath } from '../../mcp/paths';
import { oidcIdentityOf } from '../psOidc/identity';
import { RESPONSE_HEADER_KEYS } from '../../globals';
import { observeStream, StreamSummary } from '../psStreamObserver';
import {
  detectVendor,
  providerFromHeaders,
  reportsOwnUsage,
  requestBodyForTelemetry,
  TelemetryEvent,
  usageDetailFields,
  usageDetails,
  usageTokens,
  usageUnits,
  usageZeroFilled,
  withCost,
  withStreamSummary,
} from '../ps-telemetry';
import { estimateCost } from '../psPricing';
import { supportsResponsesApiNatively } from '../../adapters/responses';
import { classifySensitivity, dataPolicyRule } from '../router/constraints';
import { DATA_POLICY_GATE_KEY, DataPolicyGate } from '../router/dataPolicyGate';
import { escalatesOnPii, PSRoute, RouteCandidate } from '../router/middleware';
import { scanRequestBody } from '../router/pii';
import {
  DEFAULT_OUTPUT_TOKENS,
  defaultPolicy,
  estimateInputTokens,
  estimateTierCost,
} from '../router/policy';
import { DataPolicyRule, RoutePolicy } from '../router/types';
import { Budget, BudgetConfig } from './config';
import { DowngradePick, pickDowngrade } from './downgrade';
import {
  BudgetScope,
  counterKey,
  counterTtl,
  GLOBAL_SUBJECT,
  periodEnd,
  periodOf,
} from './keys';
import {
  budgetDecisionsTotal,
  budgetSpendUsd,
  budgetStoreErrorsTotal,
} from './metrics';
import { BudgetRuntime, getBudgetRuntime } from './runtime';

export type BudgetState = 'ok' | 'warn' | 'unknown' | 'exceeded';

/** What the budget check decided for one call (c.get('psBudget')). */
export interface BudgetDecision {
  state: BudgetState;
  /** The first budget, in document order, in that state. */
  budgetId: string;
  /** What was done to the call: none or downgraded. */
  action: 'none' | 'downgraded';
}

export const BUDGET_STATE_HEADER = 'x-ps-budget-state';

/**
 * Set on a call psBudget refused with 429, so the security events middleware
 * reports it as a budget refusal, not a rate limit.
 */
export const BUDGET_REFUSED_KEY = 'psBudgetRefused';

declare module 'hono' {
  interface ContextVariableMap {
    psBudget?: BudgetDecision;
    psBudgetRefused?: boolean;
  }
}

/** Endpoints that are billed per call. Files, batches and token counts are not. */
const BILLED_PATHS = new Set([
  '/v1/chat/completions',
  '/v1/completions',
  '/v1/messages',
  '/v1/responses',
  '/v1/embeddings',
  '/v1/rerank',
  '/v1/images/generations',
  '/v1/images/edits',
  '/v1/audio/speech',
  '/v1/audio/transcriptions',
  '/v1/audio/translations',
  '/v1/ocr',
]);

export type BudgetAttributes = Partial<
  Record<Exclude<BudgetScope, 'global'>, string>
>;

export interface Matched {
  budget: Budget;
  subject: string;
  key: string;
  ttlSeconds: number;
  periodEndsAt: number;
}

/**
 * The call's attribution attributes from its X-PS-* headers. With a verified
 * OIDC identity (`verifiedOwner`, its subject hash) the owner is that
 * identity, whatever X-PS-Owner says.
 */
export function attributesOf(
  headers: Record<string, string | undefined>,
  verifiedOwner?: string
): BudgetAttributes {
  const clean = (v: string | undefined) => (v && v.trim() ? v : undefined);
  return {
    business_unit: clean(headers['x-ps-business-unit']),
    use_case: clean(headers['x-ps-use-case']),
    owner: verifiedOwner || clean(headers['x-ps-owner']),
  };
}

/** The budgets that apply to a call, in document order, with their counters. */
export function matchBudgets(
  config: BudgetConfig,
  attributes: BudgetAttributes,
  nowMs: number
): Matched[] {
  const out: Matched[] = [];
  for (const budget of config.budgets) {
    let subject: string;
    if (budget.scope === 'global') subject = GLOBAL_SUBJECT;
    else {
      const value = attributes[budget.scope];
      if (typeof value !== 'string' || !value) continue;
      if (budget.subject !== '*' && budget.subject !== value) continue;
      subject = value;
    }
    out.push({
      budget,
      subject,
      key: counterKey(
        config.namespace,
        budget.scope,
        subject,
        budget.window,
        periodOf(budget.window, nowMs)
      ),
      ttlSeconds: counterTtl(budget.window, nowMs),
      periodEndsAt: periodEnd(budget.window, nowMs),
    });
  }
  return out;
}

export interface Status extends Matched {
  /** Counter (plus live reservations) in USD; null when unreadable. */
  spent: number | null;
  state: BudgetState;
}

export function budgetState(budget: Budget, spent: number | null): BudgetState {
  if (spent === null) return 'unknown';
  if (spent >= budget.limitUsd) return 'exceeded';
  if (spent >= budget.warnAt * budget.limitUsd) return 'warn';
  return 'ok';
}

const SEVERITY: Record<BudgetState, number> = {
  ok: 0,
  warn: 1,
  unknown: 2,
  exceeded: 3,
};

export interface Decision {
  state: BudgetState;
  budgetId: string;
  action: 'none' | 'downgrade' | 'block';
  ceilingUsd: number | null;
  blockedBy?: Status;
}

/** schemas/budget/README.md "Decision", as the SDK's `decide`. */
export function decide(
  statuses: Status[],
  onStoreError: 'open' | 'closed'
): Decision | undefined {
  if (!statuses.length) return undefined;
  let worst = statuses[0];
  for (const s of statuses)
    if (SEVERITY[s.state] > SEVERITY[worst.state]) worst = s;
  let action: Decision['action'] = 'none';
  let ceiling: number | null = null;
  let blockedBy: Status | undefined;
  for (const s of statuses) {
    const b = s.budget;
    const exhausted =
      s.state === 'exceeded' ||
      (s.state === 'unknown' && onStoreError === 'closed');
    if (b.mode === 'block') {
      if (exhausted && !blockedBy) blockedBy = s;
    } else if (b.mode === 'downgrade') {
      let remaining: number;
      if (s.spent === null) {
        if (onStoreError !== 'closed') continue;
        remaining = 0;
      } else remaining = Math.max(b.limitUsd - s.spent, 0);
      ceiling = ceiling === null ? remaining : Math.min(ceiling, remaining);
      if (exhausted && action === 'none') action = 'downgrade';
    }
  }
  if (blockedBy) action = 'block';
  return {
    state: worst.state,
    budgetId: worst.budget.id,
    action,
    ceilingUsd: ceiling,
    ...(blockedBy ? { blockedBy } : {}),
  };
}

/** USD with cents, or four decimals for a budget under $1. */
const fmt = (usd: number, limit: number) =>
  `$${usd.toFixed(limit < 1 ? 4 : 2)}`;

function budgetExceededResponse(
  c: Context,
  s: Matched,
  used: number,
  nowMs: number
): Response {
  const b = s.budget;
  const scope =
    b.scope === 'global' ? 'all calls' : `${b.scope} "${s.subject}"`;
  const resets = new Date(s.periodEndsAt).toISOString();
  c.set(BUDGET_REFUSED_KEY, true);
  const res = c.json(
    {
      error: {
        message:
          `Budget "${b.id}" (${scope}, per ${b.window}) is exhausted: ` +
          `${fmt(used, b.limitUsd)} of ${fmt(b.limitUsd, b.limitUsd)} used. ` +
          `It resets at ${resets}.`,
        type: 'budget_exceeded',
        param: null,
        code: 'budget_exceeded',
        budget_id: b.id,
        resets_at: resets,
      },
    },
    429
  );
  res.headers.set(
    'retry-after',
    String(Math.max(1, Math.ceil((s.periodEndsAt - nowMs) / 1000)))
  );
  res.headers.set(BUDGET_STATE_HEADER, 'exceeded');
  return res;
}

function storeUnavailableResponse(c: Context, budgetId: string): Response {
  const res = c.json(
    {
      error: {
        message: `Budget "${budgetId}" could not be checked (budget store unavailable) and on_store_error is closed.`,
        type: 'budget_store_unavailable',
        param: null,
        code: 'budget_store_unavailable',
        budget_id: budgetId,
      },
    },
    503
  );
  res.headers.set(BUDGET_STATE_HEADER, 'unknown');
  return res;
}

function setHeader(c: Context, name: string, value: string): void {
  try {
    c.res.headers.set(name, value);
  } catch {
    try {
      const res = new Response(c.res.body, c.res);
      res.headers.set(name, value);
      c.res = res;
    } catch {
      /* fail-open */
    }
  }
}

/** The data policy rule for this call, as the router (or tryPost) applies it. */
function ruleFor(
  c: Context,
  policy: RoutePolicy,
  headers: Record<string, string | undefined>,
  body: unknown
): DataPolicyRule | undefined {
  const gate = c.get(DATA_POLICY_GATE_KEY as any) as DataPolicyGate | undefined;
  if (gate) return gate.rule;
  if (!policy.dataPolicy) return undefined;
  const pii = escalatesOnPii(policy) ? scanRequestBody(body) : [];
  const { level } = classifySensitivity(
    policy,
    headers['x-ps-data-classification'],
    pii
  );
  return dataPolicyRule(policy, level);
}

/** Estimated cost of the call as it stands (the router's estimate if it routed). */
function estimateOf(
  policy: RoutePolicy,
  vendor: string,
  body: any,
  route: PSRoute | undefined
): number {
  if (route && Number.isFinite(route.estCost)) return route.estCost;
  const model = typeof body?.model === 'string' ? body.model : undefined;
  if (!model) return 0;
  const chat = Array.isArray(body?.messages);
  const tokensIn = chat
    ? estimateInputTokens(body.messages)
    : Math.ceil(JSON.stringify(body?.input ?? body?.prompt ?? '').length / 4);
  const tokensOut =
    body?.max_tokens ??
    body?.max_completion_tokens ??
    body?.max_output_tokens ??
    DEFAULT_OUTPUT_TOKENS;
  const priced = estimateCost(vendor, model, {
    tokens_in: tokensIn,
    tokens_out: chat ? tokensOut : 0,
  });
  if (priced !== null && Number.isFinite(priced)) return priced;
  for (const group of Object.keys(policy.groups)) {
    const tier = policy.groups[group].find((t) => t.model === model);
    if (tier) return estimateTierCost(tier, tokensIn, tokensOut);
  }
  return 0;
}

/** The call's actual cost, priced as psTelemetry prices it; undefined if unpriced. */
function actualCost(
  vendor: string,
  path: string,
  route: PSRoute | undefined,
  requestBody: any,
  responseBody: any,
  summary?: StreamSummary
): number | undefined {
  const adapted =
    path.startsWith('/v1/responses') && !supportsResponsesApiNatively(vendor);
  const zeroFilled = usageZeroFilled(vendor, adapted);
  let event: TelemetryEvent = {
    vendor,
    model: route?.requestedModel || requestBody?.model || 'unknown',
    source: 'gateway',
    ...(reportsOwnUsage(path)
      ? {
          ...usageTokens(responseBody),
          ...usageDetailFields(
            usageDetails(responseBody, !adapted, zeroFilled)
          ),
        }
      : {}),
    ...usageUnits(path, responseBody, requestBody),
  };
  let served = responseBody?.model || route?.servedModel;
  if (summary) {
    event = withStreamSummary(event, summary, !!route, !adapted, zeroFilled);
    served = summary.model || served;
  }
  const cost = withCost(event, served).cost;
  return typeof cost === 'number' && Number.isFinite(cost) && cost >= 0
    ? cost
    : undefined;
}

function recordSpend(s: Status, estimate: number) {
  if (s.spent === null) return;
  if (s.budget.subject === '*') return;
  budgetSpendUsd.set(
    { budget: s.budget.id, window: s.budget.window },
    s.spent + estimate
  );
}

/**
 * The failover candidates after a downgrade: the picked tier first, then the
 * router's other candidates whose tier costs no more than the pick.
 */
function downgradeCandidates(
  policy: RoutePolicy,
  candidates: RouteCandidate[] | undefined,
  pick: DowngradePick,
  tokensIn: number,
  maxOutput: number | undefined
): RouteCandidate[] {
  const out: RouteCandidate[] = [
    {
      model: pick.tier.model,
      ...(pick.tier.provider ? { provider: pick.tier.provider } : {}),
    },
  ];
  const tierOf = (model: string) => {
    for (const g of Object.keys(policy.groups)) {
      const t = policy.groups[g].find((x) => x.model === model);
      if (t) return t;
    }
    return undefined;
  };
  for (const cand of candidates ?? []) {
    if (cand.model === pick.tier.model) continue;
    const tier = tierOf(cand.model);
    if (!tier) continue;
    const cost = estimateTierCost(
      tier,
      tokensIn,
      maxOutput ?? DEFAULT_OUTPUT_TOKENS
    );
    if (cost <= pick.estCost) out.push(cand);
  }
  return out;
}

export interface PsBudgetOptions {
  /** The routing policy downgrades choose from (the router's). */
  policy?: RoutePolicy;
  now?: () => number;
}

export function psBudget(options: PsBudgetOptions = {}) {
  const policy = options.policy ?? defaultPolicy;
  const now = options.now ?? Date.now;

  return async (c: Context, next: Next) => {
    if (c.req.method !== 'POST') return next();
    const path = c.req.path;
    if (isMcpProxyPath(path) || !BILLED_PATHS.has(path)) return next();
    const rt: BudgetRuntime | null = getBudgetRuntime(
      env<Record<string, string | undefined>>(c)
    );
    if (!rt) return next();

    const nowMs = now();
    const headers = Object.fromEntries(c.req.raw.headers) as Record<
      string,
      string
    >;
    let verifiedOwner: string | undefined;
    try {
      verifiedOwner = oidcIdentityOf(c)?.subjectHash;
    } catch {
      verifiedOwner = undefined;
    }
    const matched = matchBudgets(
      rt.config,
      attributesOf(headers, verifiedOwner),
      nowMs
    );
    if (!matched.length) return next();
    const { onStoreError } = rt.config;
    const storeError = (op: string, err: unknown) => {
      budgetStoreErrorsTotal.inc({ store: rt.store.kind, op });
      console.warn(`[PS Budget] store ${op} failed:`, (err as Error)?.message);
    };

    const vendor = detectVendor(c.req.url, providerFromHeaders(headers));
    let body: any;
    if ((headers['content-type'] || '').includes('application/json')) {
      try {
        body = await c.req.json();
      } catch {
        body = undefined; // the handler rejects it
      }
    }
    let route = c.get('psRoute');
    let estimate = estimateOf(policy, vendor, body, route);

    // 1. Read and decide.
    const keys = [...new Set(matched.map((m) => m.key))];
    const used = new Map<string, number>();
    let readable = true;
    try {
      const usage = await rt.store.read(keys, nowMs);
      keys.forEach((k, i) => used.set(k, usage[i].spent + usage[i].reserved));
    } catch (err) {
      storeError('read', err);
      readable = false;
    }
    const statuses: Status[] = matched.map((m) => {
      const spent = readable ? used.get(m.key) ?? 0 : null;
      return { ...m, spent, state: budgetState(m.budget, spent) };
    });
    const decision = decide(statuses, onStoreError)!;
    const record = (action: string) => {
      for (const s of statuses)
        budgetDecisionsTotal.inc({
          budget: s.budget.id,
          state: s.state,
          action,
        });
    };

    // 2. Block.
    if (decision.blockedBy) {
      record('block');
      const s = decision.blockedBy;
      return s.spent === null
        ? storeUnavailableResponse(c, s.budget.id)
        : budgetExceededResponse(c, s, s.spent, nowMs);
    }

    // 3. Downgrade: clamp the estimate to the ceiling.
    let action: BudgetDecision['action'] = 'none';
    if (
      decision.ceilingUsd !== null &&
      estimate > decision.ceilingUsd &&
      body &&
      Array.isArray(body.messages)
    ) {
      const pick = pickDowngrade(
        policy,
        {
          approxInputTokens: estimateInputTokens(body.messages),
          maxOutputTokens: body.max_tokens ?? body.max_completion_tokens,
          useCase: headers['x-ps-use-case'],
        },
        ruleFor(c, policy, headers, body),
        estimate,
        decision.ceilingUsd
      );
      if (pick) {
        const requestedModel = route?.requestedModel ?? body.model ?? 'auto';
        body.model = pick.tier.model;
        route = {
          ...(route ?? {}),
          requestedModel,
          servedModel: pick.tier.model,
          group: pick.tier.group,
          reason: `${route?.reason ?? 'budget'}+budget-downgrade`,
          estCost: pick.estCost,
          // Failover (router/failover.ts) runs the candidates in order: keep
          // only the picked model and the router's candidates that cost no
          // more than it, so a failover never undoes the downgrade.
          candidates: downgradeCandidates(
            policy,
            route?.candidates,
            pick,
            estimateInputTokens(body.messages),
            body.max_tokens ?? body.max_completion_tokens
          ),
        } as PSRoute;
        c.set('psRoute', route);
        (c.req as any).bodyCache = { json: Promise.resolve(body) };
        estimate = pick.estCost;
        action = 'downgraded';
      }
    }

    // 4. Reserve (an atomic check for block budgets).
    const reservationId = crypto.randomUUID();
    const reserved: Matched[] = [];
    const release = () =>
      Promise.all(
        reserved.map((m) =>
          rt.store
            .settle(m.key, reservationId, estimate, 0, m.ttlSeconds)
            .catch(() => undefined)
        )
      );
    if (readable) {
      for (const key of keys) {
        const onKey = statuses.filter((s) => s.key === key);
        const blocking = onKey.filter((s) => s.budget.mode === 'block');
        const tightest = blocking.length
          ? blocking.reduce((x, y) =>
              y.budget.limitUsd < x.budget.limitUsd ? y : x
            )
          : undefined;
        try {
          const r = await rt.store.reserve({
            key,
            id: reservationId,
            amount: estimate,
            nowMs,
            reservationTtlMs: rt.reservationTtlMs,
            limit: tightest?.budget.limitUsd,
          });
          if (!r.ok) {
            // Another call (on any replica) used up the budget meanwhile.
            await release();
            record('block');
            return budgetExceededResponse(
              c,
              tightest!,
              r.spent + r.reserved,
              nowMs
            );
          }
          reserved.push(onKey[0]);
        } catch (err) {
          storeError('reserve', err);
          if (onStoreError === 'closed' && tightest) {
            await release();
            record('block');
            return storeUnavailableResponse(c, tightest.budget.id);
          }
        }
      }
    }

    c.set('psBudget', {
      state: decision.state,
      budgetId: decision.budgetId,
      action,
    });
    record(action);
    for (const s of statuses) recordSpend(s, estimate);

    // 5. The call, then settle with its actual cost on every counter.
    try {
      await next();
    } catch (err) {
      await release(); // reservations also lapse after reservationTtlMs
      throw err;
    }
    setHeader(c, BUDGET_STATE_HEADER, decision.state);

    const settle = (actual: number | undefined) =>
      Promise.all(
        keys.map((key) => {
          const m = matched.find((x) => x.key === key)!;
          return rt.store
            .settle(
              key,
              reservationId,
              estimate,
              actual ?? estimate,
              m.ttlSeconds
            )
            .catch((err) => storeError('settle', err));
        })
      ).then(() => undefined);

    try {
      const cacheHit = (
        c.res.headers.get(RESPONSE_HEADER_KEYS.CACHE_STATUS) || ''
      ).endsWith('HIT');
      if (c.res.status >= 400 || cacheHit) {
        await settle(0);
        return;
      }
      const requestBody = await requestBodyForTelemetry(c);
      const stream = observeStream(c);
      if (stream) {
        const done = stream
          .then((summary) =>
            settle(
              actualCost(vendor, path, route, requestBody, undefined, summary)
            )
          )
          .catch(() => settle(undefined));
        try {
          c.executionCtx?.waitUntil?.(done);
        } catch {
          /* no execution context on Node */
        }
        return;
      }
      let responseBody: any;
      if (
        (c.res.headers.get('content-type') || '').includes('application/json')
      ) {
        try {
          responseBody = await c.res.clone().json();
        } catch {
          responseBody = undefined;
        }
      }
      await settle(actualCost(vendor, path, route, requestBody, responseBody));
    } catch (err) {
      console.warn('[PS Budget] settle skipped:', (err as Error)?.message);
      await settle(undefined);
    }
  };
}
