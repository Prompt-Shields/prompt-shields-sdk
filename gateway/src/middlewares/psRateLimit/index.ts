/**
 * PS fork (#344): request and token rate limits.
 *
 * Rules come from the central policy's `rate_limits` section (the tenant's,
 * plus the sections of the team and project a call names in X-PS-Team /
 * X-PS-Project, which only add limits) or, without one, from the local
 * PS_RATE_LIMITS / PS_RATE_LIMITS_FILE document (./config.ts has the
 * format). With no rules the middleware does nothing: no body parsing, no
 * headers, no store calls.
 *
 * Per model call (the billed endpoints; mounted after psOidc for the user
 * and after the router for the routed model, before budgets):
 *
 * 1. Every rule whose `match` fits the call and whose scope has a value for
 *    it applies; each of its limits (rpm, tpm, tpd, concurrency) is a
 *    counter for that rule and scope value.
 * 2. All counters are checked and charged in one atomic step (./store.ts):
 *    one request on rpm, the estimated tokens (input estimate plus
 *    max_tokens, or the router's default output estimate) on tpm/tpd, one
 *    lease on concurrency. If any refuses, nothing is charged and the call
 *    gets 429 `rate_limit_exceeded` with the rule id and Retry-After.
 * 3. After the call the token counters are corrected to the provider's
 *    reported usage (streams once, when they end; a failed call or a cache
 *    hit is refunded; without reported usage the estimate stays) and the
 *    concurrency leases are released (on end, client disconnect or error;
 *    a lease a dead replica held lapses after PS_RATELIMIT_LEASE_MS).
 *    Reported usage counts the prompt as the provider received it, i.e.
 *    after any pseudonymisation; the estimate is taken before it.
 *
 * Responses carry x-ratelimit-limit/-remaining/-reset-requests and -tokens
 * for the tightest rpm and tpm counter that applied (replacing a provider's
 * headers of the same name).
 *
 * Scopes: `global` (every call), `user` (the verified OIDC subject; needs
 * PS_OIDC_ENABLED), `virtual_key` (x-portkey-virtual-key, else a SHA-256
 * fingerprint of the caller's provider key: Authorization bearer, x-api-key
 * or api-key), `team` / `project` (X-PS-Team / X-PS-Project). Like budgets,
 * the X-PS-* headers are declared by the caller, not authenticated: a rule
 * on them is attribution, and a caller that omits the header is not counted
 * by it. Subjects are hashed before they reach the store.
 */
import { Context, Next } from 'hono';
import { env } from 'hono/adapter';
import { isMcpProxyPath } from '../../mcp/paths';
import { RESPONSE_HEADER_KEYS } from '../../globals';
import { oidcIdentityOf } from '../psOidc/identity';
import { providerFromHeaders, usageTokens } from '../ps-telemetry';
import { observeStream } from '../psStreamObserver';
import { DEFAULT_OUTPUT_TOKENS, estimateInputTokens } from '../router/policy';
import { centralRateLimits } from '../policyPull/state';
import {
  CallAttributes,
  LimitKind,
  RateLimitConfig,
  RateLimitRule,
  RateLimitScope,
  ruleSubject,
} from './config';
import { rateLimitHitsTotal } from './metrics';
import {
  acquire,
  Acquired,
  localRateLimits,
  RateLimitStoreUnavailable,
  rateLimitStoreKind,
  settle,
} from './runtime';
import { Adjustment, Bucket, BucketResult } from './store';

/** Set on a call this middleware refused, for security events. */
export const RATE_LIMIT_HIT_KEY = 'psRateLimitHit';

export interface RateLimitHit {
  rule: string;
  scope: RateLimitScope;
  limit: LimitKind;
}

declare module 'hono' {
  interface ContextVariableMap {
    psRateLimitHit?: RateLimitHit;
  }
}

/** Model endpoints (as psBudget's billed paths). */
export const RATE_LIMITED_PATHS = new Set([
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

/** Endpoints whose output is tokens (max_tokens counts in the estimate). */
const GENERATIVE_PATHS = new Set([
  '/v1/chat/completions',
  '/v1/completions',
  '/v1/messages',
  '/v1/responses',
]);

const MINUTE_MS = 60_000;
const DAY_MS = 86_400_000;
const WINDOW_MS: Record<Exclude<LimitKind, 'concurrency'>, number> = {
  rpm: MINUTE_MS,
  tpm: MINUTE_MS,
  tpd: DAY_MS,
};

export interface Layer {
  /** `local`, `tenant`, `team:<id>` or `project:<id>`: counters are per layer. */
  origin: string;
  config: RateLimitConfig;
}

const clean = (v: string | undefined) => (v && v.trim() ? v.trim() : undefined);

/**
 * The rule layers for a call: the central tenant section, plus the named
 * team's and project's sections where they differ from what they would
 * inherit; else the local config.
 */
export function layersFor(
  headers: Record<string, string | undefined>,
  vars: Record<string, string | undefined>
): { layers: Layer[]; team?: string; project?: string } {
  const central = centralRateLimits();
  const teamHeader = clean(headers['x-ps-team']);
  const projectHeader = clean(headers['x-ps-project']);
  if (!central) {
    const local = localRateLimits(vars);
    return {
      layers: local ? [{ origin: 'local', config: local }] : [],
      team: teamHeader,
      project: projectHeader,
    };
  }
  const byIdOrName = <T extends { name?: string }>(
    entries: Record<string, T>,
    value: string | undefined
  ): [string, T] | undefined => {
    if (!value) return undefined;
    if (entries[value]) return [value, entries[value]];
    return Object.entries(entries).find(([, e]) => e.name === value);
  };
  const layers: Layer[] = [];
  const seen = new Set<string>();
  const add = (
    origin: string,
    layer: { config: RateLimitConfig } | undefined
  ) => {
    if (!layer || seen.has(origin)) return;
    seen.add(origin);
    layers.push({ origin, config: layer.config });
  };
  const tenant = central.tenant;
  add('tenant', tenant);
  const team = byIdOrName(central.teams, teamHeader);
  const project = byIdOrName(central.projects, projectHeader);
  const teamOwn = (id: string | undefined) => {
    const layer = id ? central.teams[id]?.layer : undefined;
    return layer && layer.canon !== tenant?.canon ? layer : undefined;
  };
  if (team) add(`team:${team[0]}`, teamOwn(team[0]));
  if (project) {
    const [id, p] = project;
    add(`team:${p.teamId}`, teamOwn(p.teamId));
    const inherited = (p.teamId && central.teams[p.teamId]?.layer) || tenant;
    if (p.layer && p.layer.canon !== inherited?.canon)
      add(`project:${id}`, p.layer);
  }
  return {
    layers,
    team: team?.[0] ?? (project?.[1].teamId || teamHeader),
    project: project?.[0] ?? projectHeader,
  };
}

const hex = (buf: ArrayBuffer) =>
  [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');

async function sha256(text: string): Promise<string> {
  return hex(
    await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  );
}

/** The caller's key: x-portkey-virtual-key, else a fingerprint of its provider key. */
async function keySubject(
  headers: Record<string, string | undefined>
): Promise<string | undefined> {
  const vk = clean(headers['x-portkey-virtual-key']);
  if (vk) return `vk:${vk}`;
  const auth = clean(headers['authorization']);
  const bearer = auth?.toLowerCase().startsWith('bearer ')
    ? clean(auth.slice(7))
    : undefined;
  const key =
    bearer ?? clean(headers['x-api-key']) ?? clean(headers['api-key']);
  return key ? `key:${(await sha256(key)).slice(0, 16)}` : undefined;
}

/** Estimated tokens of the call: input estimate plus its output allowance. */
export function estimateTokens(path: string, body: any): number {
  if (!body || typeof body !== 'object') return 0;
  const chars = (v: unknown) =>
    v === undefined || v === null
      ? 0
      : Math.ceil((typeof v === 'string' ? v : JSON.stringify(v)).length / 4);
  const input = Array.isArray(body.messages)
    ? estimateInputTokens(body.messages) + chars(body.system)
    : chars(body.input ?? body.prompt);
  if (!GENERATIVE_PATHS.has(path)) return input;
  const out = [
    body.max_tokens,
    body.max_completion_tokens,
    body.max_output_tokens,
  ].find((n) => Number.isSafeInteger(n) && n >= 0);
  return input + (out ?? DEFAULT_OUTPUT_TOKENS);
}

const sanitize = (ns: string | undefined) =>
  (ns || 'default').replace(/[^A-Za-z0-9._-]/g, '_').slice(0, 64);

interface Planned {
  layer: Layer;
  rule: RateLimitRule;
  limit: LimitKind;
  bucket: Bucket;
}

/** OpenAI-style duration: 20ms, 1.5s, 6m0s. */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.max(0, Math.round(ms))}ms`;
  const s = ms / 1000;
  if (s < 60) return `${Number(s.toFixed(3))}s`;
  const m = Math.floor(s / 60);
  const rest = Number((s - m * 60).toFixed(3));
  if (m < 60) return `${m}m${rest}s`;
  return `${Math.floor(m / 60)}h${m % 60}m${rest}s`;
}

function setHeaders(c: Context, headers: Record<string, string>): void {
  const apply = (res: Response) => {
    for (const [k, v] of Object.entries(headers)) res.headers.set(k, v);
  };
  try {
    apply(c.res);
  } catch {
    try {
      const res = new Response(c.res.body, c.res);
      apply(res);
      c.res = res;
    } catch {
      /* fail-open */
    }
  }
}

/** x-ratelimit-* for the tightest rpm and tpm counters. */
function rateLimitHeaders(
  planned: Planned[],
  results: BucketResult[]
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [kind, name] of [
    ['rpm', 'requests'],
    ['tpm', 'tokens'],
  ] as const) {
    let best = -1;
    planned.forEach((p, i) => {
      if (p.limit !== kind) return;
      if (best < 0 || results[i].remaining < results[best].remaining) best = i;
    });
    if (best < 0) continue;
    out[`x-ratelimit-limit-${name}`] = String(planned[best].bucket.limit);
    out[`x-ratelimit-remaining-${name}`] = String(results[best].remaining);
    out[`x-ratelimit-reset-${name}`] = formatDuration(results[best].resetMs);
  }
  return out;
}

const LIMIT_WORDS: Record<LimitKind, string> = {
  rpm: 'requests per minute',
  tpm: 'tokens per minute',
  tpd: 'tokens per day',
  concurrency: 'concurrent requests',
};

export interface PsRateLimitOptions {
  now?: () => number;
}

export function psRateLimit(options: PsRateLimitOptions = {}) {
  const now = options.now ?? Date.now;

  return async (c: Context, next: Next) => {
    if (c.req.method !== 'POST') return next();
    const path = c.req.path;
    if (isMcpProxyPath(path) || !RATE_LIMITED_PATHS.has(path)) return next();
    const vars = env<Record<string, string | undefined>>(c);
    const headers = Object.fromEntries(c.req.raw.headers) as Record<
      string,
      string
    >;
    const { layers, team, project } = layersFor(headers, vars);
    if (!layers.some((l) => l.config.rules.length)) return next();

    let body: any;
    if ((headers['content-type'] || '').includes('application/json')) {
      try {
        body = await c.req.json();
      } catch {
        body = undefined; // the handler rejects it
      }
    }
    let user: string | undefined;
    try {
      user = oidcIdentityOf(c)?.subjectHash;
    } catch {
      user = undefined;
    }
    const route = c.get('psRoute');
    const call: CallAttributes = {
      model:
        route?.servedModel ||
        (typeof body?.model === 'string' ? body.model : undefined),
      provider: providerFromHeaders(headers),
      route: path,
      subjects: {
        user,
        team,
        project,
        virtual_key: await keySubject(headers),
      },
    };
    const estimate = estimateTokens(path, body);
    const nowMs = now();
    const ns = sanitize(vars.PS_RATELIMIT_NAMESPACE);

    // 1. The counters of every rule that applies.
    const planned: Planned[] = [];
    let onStoreError: 'open' | 'closed' = 'open';
    for (const layer of layers) {
      for (const rule of layer.config.rules) {
        const subject = ruleSubject(rule, call);
        if (subject === undefined) continue;
        if (layer.config.onStoreError === 'closed') onStoreError = 'closed';
        const h = (await sha256(`${layer.origin}\n${subject}`)).slice(0, 24);
        const base = `ps:rl:v1:{${ns}}:${rule.id}`;
        for (const limit of ['rpm', 'tpm', 'tpd', 'concurrency'] as const) {
          const value = rule[limit];
          if (value === undefined) continue;
          if (limit === 'concurrency') {
            planned.push({
              layer,
              rule,
              limit,
              bucket: {
                kind: 'concurrency',
                key: `${base}:c:${h}`,
                limit: value,
              },
            });
            continue;
          }
          const windowMs = WINDOW_MS[limit];
          const index = Math.floor(nowMs / windowMs);
          planned.push({
            layer,
            rule,
            limit,
            bucket: {
              kind: 'window',
              key: `${base}:${limit}:${h}:${index}`,
              prevKey: `${base}:${limit}:${h}:${index - 1}`,
              limit: value,
              windowMs,
              windowStart: index * windowMs,
              units: limit === 'rpm' ? 1 : estimate,
            },
          });
        }
      }
    }
    if (!planned.length) return next();

    // 2. Check and charge them together.
    const leaseId = crypto.randomUUID();
    let acquired: Acquired;
    try {
      acquired = await acquire(
        planned.map((p) => p.bucket),
        nowMs,
        leaseId,
        onStoreError
      );
    } catch (err) {
      if (!(err instanceof RateLimitStoreUnavailable)) throw err;
      return c.json(
        {
          error: {
            message:
              'Rate limits could not be checked (rate-limit store unavailable) and on_store_error is closed.',
            type: 'rate_limit_store_unavailable',
            param: null,
            code: 'rate_limit_store_unavailable',
          },
        },
        503
      );
    }
    const limitHeaders = rateLimitHeaders(planned, acquired.results);

    if (!acquired.ok) {
      // Report the refusal that takes longest to clear (-1: never).
      const wait = (r: BucketResult) =>
        r.retryAfterMs < 0 ? Infinity : r.retryAfterMs;
      let worst = -1;
      acquired.results.forEach((r, i) => {
        if (r.allowed) return;
        if (worst < 0 || wait(r) > wait(acquired.results[worst])) worst = i;
      });
      const p = planned[worst];
      const r = acquired.results[worst];
      const hit: RateLimitHit = {
        rule: p.rule.id,
        scope: p.rule.scope,
        limit: p.limit,
      };
      c.set(RATE_LIMIT_HIT_KEY, hit);
      rateLimitHitsTotal.inc({
        scope: hit.scope,
        rule: hit.rule,
        limit: hit.limit,
      });
      const never = r.retryAfterMs < 0;
      const message = never
        ? `Rate limit "${p.rule.id}" (${p.bucket.limit} ${LIMIT_WORDS[p.limit]}) is lower than this request's estimated ${estimate} tokens; lower max_tokens or the prompt size.`
        : `Rate limit "${p.rule.id}" (${p.bucket.limit} ${LIMIT_WORDS[p.limit]}, scope ${p.rule.scope}) exceeded. Retry in ${formatDuration(r.retryAfterMs)}.`;
      const res = c.json(
        {
          error: {
            message,
            type: 'rate_limit_error',
            param: null,
            code: 'rate_limit_exceeded',
            rule_id: p.rule.id,
            scope: p.rule.scope,
            limit: p.limit,
          },
        },
        429
      );
      for (const [k, v] of Object.entries(limitHeaders)) res.headers.set(k, v);
      if (!never)
        res.headers.set(
          'retry-after',
          String(Math.max(1, Math.ceil(r.retryAfterMs / 1000)))
        );
      return res;
    }

    // 3. The call, then correct the token counters and release the leases.
    const store = acquired.store;
    const tokenKeys = planned
      .filter((p) => p.limit === 'tpm' || p.limit === 'tpd')
      .map((p) => p.bucket.key);
    const leaseKeys = planned
      .filter((p) => p.limit === 'concurrency')
      .map((p) => p.bucket.key);
    let settled = false;
    const finish = (actual: number | undefined) => {
      if (settled) return Promise.resolve();
      settled = true;
      const delta = actual === undefined ? 0 : actual - estimate;
      const adjustments: Adjustment[] = delta
        ? tokenKeys.map((key) => ({ key, delta }))
        : [];
      return settle(store, adjustments, leaseKeys, leaseId);
    };

    try {
      await next();
    } catch (err) {
      await finish(0);
      throw err;
    }
    setHeaders(c, limitHeaders);

    try {
      const cacheHit = (
        c.res.headers.get(RESPONSE_HEADER_KEYS.CACHE_STATUS) || ''
      ).endsWith('HIT');
      if (c.res.status >= 400 || cacheHit) {
        await finish(0);
        return;
      }
      const stream = observeStream(c);
      if (stream) {
        const done = stream
          .then((s) =>
            finish(
              s.tokens_in === undefined && s.tokens_out === undefined
                ? undefined
                : (s.tokens_in ?? 0) + (s.tokens_out ?? 0)
            )
          )
          .catch(() => finish(undefined));
        try {
          c.executionCtx?.waitUntil?.(done);
        } catch {
          /* no execution context on Node */
        }
        return;
      }
      let actual: number | undefined;
      if (
        tokenKeys.length &&
        (c.res.headers.get('content-type') || '').includes('application/json')
      ) {
        try {
          const u = usageTokens(await c.res.clone().json());
          if (u.tokens_in !== undefined || u.tokens_out !== undefined)
            actual = (u.tokens_in ?? 0) + (u.tokens_out ?? 0);
        } catch {
          actual = undefined;
        }
      }
      await finish(actual);
    } catch (err) {
      console.warn('[PS RateLimit] settle skipped:', (err as Error)?.message);
      await finish(undefined);
    }
  };
}

/** `rate_limits` for GET /v1/health, when any rules are configured. */
export function rateLimitHealth(vars: Record<string, string | undefined>): {
  rate_limits?: { source: string; rules: number; store: string };
} {
  const central = centralRateLimits();
  let source: string;
  let rules: number;
  if (central) {
    source = 'central';
    rules =
      (central.tenant?.config.rules.length ?? 0) +
      Object.values(central.teams).reduce(
        (n, t) => n + (t.layer?.config.rules.length ?? 0),
        0
      ) +
      Object.values(central.projects).reduce(
        (n, p) => n + (p.layer?.config.rules.length ?? 0),
        0
      );
  } else {
    const local = localRateLimits(vars);
    if (!local) return {};
    source = 'local';
    rules = local.rules.length;
  }
  // store memory: each replica counts on its own.
  return { rate_limits: { source, rules, store: rateLimitStoreKind() } };
}
