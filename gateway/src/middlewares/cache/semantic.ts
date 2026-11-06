/**
 * PS fork (#33): semantic cache mode.
 *
 * `cache.mode: "semantic"` (config), `X-PS-Cache: semantic` or
 * `PS_CACHE_DEFAULT=semantic` serves a cached response when the final user
 * message is close enough (cosine similarity >= PS_SEMANTIC_CACHE_THRESHOLD)
 * to one answered before, and everything else about the request is
 * identical. Upstream Portkey's semantic mode needs its hosted service; in
 * this fork it used to read like the exact cache and never write.
 *
 * Isolation. Entries are partitioned by a SHA-256 over:
 * - the caller scope (provider, credential and identity fields such as the
 *   virtual key, forwarded headers, upstream URL: computeCacheScope),
 * - the tenant (a digest of this deployment's PS_API_KEY) and, with OIDC,
 *   the authenticated principal (unless PS_SEMANTIC_CACHE_SHARE_PRINCIPALS),
 * - the use case (X-PS-Use-Case), the model, the route policy version,
 *   the call's effective sensitivity level and the endpoint,
 * - the embedder, all request parameters, the conversation history and the
 *   final message apart from its text.
 * Only the final user message's text is compared by embedding, and only
 * within its own partition.
 *
 * Off (no read, no write; `SEMANTIC BYPASS`) when:
 * - no embedder is configured, the endpoint is not chat or messages, the
 *   request streams, or the final message is not plain user text,
 * - the call's sensitivity is above PS_SEMANTIC_CACHE_MAX_SENSITIVITY
 *   (default: the level below `confidential`) or is a level the policy does
 *   not know,
 * - PII is detected in the request (unless PS_SEMANTIC_CACHE_ALLOW_PII),
 * - under a data policy, the embedder does not meet the rule for the call's
 *   level (the local embedder always does: nothing leaves the gateway),
 * - the embedding call fails (fail open: the request goes upstream).
 *
 * Never written: errors, incomplete or streamed responses, failed hooks
 * (see isCacheableResponse / isCompleteResponseBody) and refusals
 * (isRefusalResponse). See gateway/docs/semantic-cache.md.
 */
import { Context } from 'hono';
import type { DataPolicyGate } from '../router/dataPolicyGate';
import type { OidcIdentity } from '../psOidc/identity';
import type { PSRoute } from '../router/middleware';
import {
  classifySensitivity,
  DEFAULT_LEVELS,
  modelComplies,
  tierComplies,
} from '../router/constraints';
import { parseRouteHeaders } from '../router/index';
import { scanRequestBody } from '../router/pii';
import { ModelTier, RoutePolicy } from '../router/types';
import {
  semanticBypassTotal,
  semanticBytes,
  semanticEntries,
} from '../psMetrics/cache';
import { upstreamFetch } from '../../utils/fetch';
import { canonicalJSON, sha256Hex } from './hash';
import { normalize, SemanticStore } from './semanticStore';

// Context keys set elsewhere, read by name to keep this module free of the
// router/OIDC middleware imports (and their import cycles).
const DATA_POLICY_GATE_KEY = 'psDataPolicyGate';
const OIDC_IDENTITY_KEY = 'psOidcIdentity';
/** The active route policy (set by the router's Hono adapter). */
export const PS_ROUTE_POLICY_KEY = 'psRoutePolicy';
const PENDING_KEY = 'psSemanticPending';

export const SEMANTIC_STATUS = {
  HIT: 'SEMANTIC HIT',
  MISS: 'SEMANTIC MISS',
  BYPASS: 'SEMANTIC BYPASS',
  REFRESH: 'REFRESH',
} as const;

const SEMANTIC_ENDPOINTS = new Set(['chatComplete', 'messages']);

type Env = Record<string, string | undefined>;

const num = (raw: string | undefined, fallback: number, min = 0) => {
  const n = Number(raw);
  return raw !== undefined && raw !== '' && Number.isFinite(n) && n >= min
    ? n
    : fallback;
};

export interface SemanticConfig {
  threshold: number;
  ttlSeconds: number;
  maxInputChars: number;
  maxSensitivity?: string;
  allowPii: boolean;
  sharePrincipals: boolean;
}

export function semanticConfig(env: Env = process.env): SemanticConfig {
  const threshold = num(env.PS_SEMANTIC_CACHE_THRESHOLD, 0.95, 0);
  return {
    // Values outside (0, 1] would match everything or nothing.
    threshold: threshold > 0 && threshold <= 1 ? threshold : 0.95,
    ttlSeconds: num(env.PS_SEMANTIC_CACHE_TTL_SECONDS, 3600, 1),
    maxInputChars: num(env.PS_SEMANTIC_CACHE_MAX_INPUT_CHARS, 4000, 1),
    maxSensitivity: env.PS_SEMANTIC_CACHE_MAX_SENSITIVITY?.trim() || undefined,
    allowPii: env.PS_SEMANTIC_CACHE_ALLOW_PII === 'true',
    sharePrincipals: env.PS_SEMANTIC_CACHE_SHARE_PRINCIPALS === 'true',
  };
}

// ---------------------------------------------------------------- embedders

export interface Embedder {
  /** Part of the partition key: vectors of two embedders never compare. */
  id: string;
  /** Runs inside the gateway process: no prompt text leaves it. */
  local: boolean;
  model: string;
  /** The endpoint a remote embedder posts the prompt text to. */
  url?: string;
  /** Sovereignty facts checked against the data policy rule. */
  facts?: ModelTier;
  embed(text: string): Promise<ArrayLike<number>>;
}

/** 32-bit FNV-1a. */
function fnv1a(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/**
 * Built-in local embedder: feature-hashed word unigrams and character
 * trigrams. Lexical, not semantic: it matches rephrasings that share most
 * words (case, punctuation, whitespace and word-order changes, small edits),
 * not paraphrases. No network, so it meets every data policy.
 */
export function localHashEmbedder(dims = 512): Embedder {
  const d = Math.max(16, Math.floor(dims));
  return {
    id: `local-hash-v1:${d}`,
    local: true,
    model: `local-hash-v1`,
    async embed(text: string) {
      const v = new Float32Array(d);
      const words =
        text
          .normalize('NFKC')
          .toLowerCase()
          .match(/[\p{L}\p{N}]+/gu) ?? [];
      const add = (feature: string, weight: number) => {
        const h = fnv1a(feature);
        v[h % d] += h & 0x80000000 ? -weight : weight;
      };
      for (const w of words) {
        add(`w:${w}`, 1);
        const padded = ` ${w} `;
        for (let i = 0; i + 3 <= padded.length; i++)
          add(`t:${padded.slice(i, i + 3)}`, 0.5);
      }
      return v;
    },
  };
}

/** An OpenAI-compatible `/v1/embeddings` endpoint. */
export function httpEmbedder(env: Env): Embedder | undefined {
  const url = env.PS_SEMANTIC_CACHE_EMBEDDING_URL?.trim();
  const model = env.PS_SEMANTIC_CACHE_EMBEDDING_MODEL?.trim();
  if (!url || !model) return undefined;
  const timeoutMs = num(env.PS_SEMANTIC_CACHE_EMBEDDING_TIMEOUT_MS, 2000, 1);
  const apiKey = env.PS_SEMANTIC_CACHE_EMBEDDING_API_KEY;
  const cloudAct = env.PS_SEMANTIC_CACHE_EMBEDDING_US_CLOUD_ACT_EXPOSURE;
  const facts: ModelTier = {
    model,
    group: '',
    inputCostPer1k: 0,
    outputCostPer1k: 0,
  };
  if (env.PS_SEMANTIC_CACHE_EMBEDDING_PROVIDER)
    facts.provider = env.PS_SEMANTIC_CACHE_EMBEDDING_PROVIDER;
  if (env.PS_SEMANTIC_CACHE_EMBEDDING_JURISDICTION)
    facts.jurisdiction = env.PS_SEMANTIC_CACHE_EMBEDDING_JURISDICTION;
  if (env.PS_SEMANTIC_CACHE_EMBEDDING_ZDR !== undefined)
    facts.zdr = env.PS_SEMANTIC_CACHE_EMBEDDING_ZDR === 'true';
  if (cloudAct === 'true' || cloudAct === 'false')
    facts.usCloudActExposure = cloudAct === 'true';
  return {
    id: `http:${url}#${model}`,
    local: false,
    model,
    url,
    facts,
    async embed(text: string) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        // Never follows a redirect: a 3xx would re-send the prompt and the
        // key to a host the egress and data policy checks never saw.
        const res = await upstreamFetch(url, {
          method: 'POST',
          headers: {
            'content-type': 'application/json',
            ...(apiKey ? { authorization: `Bearer ${apiKey}` } : {}),
          },
          body: JSON.stringify({ model, input: text }),
          signal: controller.signal,
        });
        if (!res.ok) throw new Error(`embedding endpoint ${res.status}`);
        const body: any = await res.json();
        const vec = body?.data?.[0]?.embedding;
        if (!Array.isArray(vec)) throw new Error('no embedding in response');
        return vec as number[];
      } finally {
        clearTimeout(timer);
      }
    },
  };
}

/** The configured embedder (PS_SEMANTIC_CACHE_EMBEDDER), if any. */
export function configuredEmbedder(env: Env = process.env) {
  const kind = (env.PS_SEMANTIC_CACHE_EMBEDDER || '').trim().toLowerCase();
  if (kind === 'local')
    return localHashEmbedder(num(env.PS_SEMANTIC_CACHE_LOCAL_DIMS, 512, 16));
  if (kind === 'http') return httpEmbedder(env);
  return undefined;
}

// -------------------------------------------------------------------- store

let store: SemanticStore | undefined;

function getStore(): SemanticStore {
  if (!store) {
    store = new SemanticStore({
      maxEntries: num(process.env.PS_SEMANTIC_CACHE_MAX_ENTRIES, 1000, 1),
      maxBytes:
        num(process.env.PS_SEMANTIC_CACHE_MAX_MB, 64, 0.001) * 1024 * 1024,
      maxPerPartition: num(
        process.env.PS_SEMANTIC_CACHE_MAX_PER_PARTITION,
        256,
        1
      ),
    });
  }
  return store;
}

const updateGauges = () => {
  semanticEntries.set({}, store?.size ?? 0);
  semanticBytes.set({}, store?.bytes ?? 0);
};

/** test-only hook: drop every entry and re-read the store limits. */
export const __resetSemanticCacheForTests = () => {
  store = undefined;
  updateGauges();
};

// ------------------------------------------------------------- the request

/** Text of a message's content when it is text only, else undefined. */
function textOf(content: unknown): string | undefined {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content) || !content.length) return undefined;
  const parts: string[] = [];
  for (const p of content) {
    if (p?.type !== 'text' || typeof p.text !== 'string') return undefined;
    parts.push(p.text);
  }
  return parts.join('\n');
}

type Split =
  | { ok: true; text: string; exact: unknown }
  | { ok: false; reason: string };

/**
 * The final user message's text (compared by embedding) and everything else
 * (matched exactly through the partition key).
 */
export function splitPrompt(params: any, maxInputChars: number): Split {
  const messages = params?.messages;
  if (!Array.isArray(messages) || !messages.length)
    return { ok: false, reason: 'no_messages' };
  const last = messages[messages.length - 1];
  if (last?.role !== 'user') return { ok: false, reason: 'last_not_user' };
  const text = textOf(last.content);
  if (text === undefined) return { ok: false, reason: 'non_text_content' };
  if (!text.trim()) return { ok: false, reason: 'empty_text' };
  if (text.length > maxInputChars) return { ok: false, reason: 'too_long' };
  const { messages: _m, ...rest } = params;
  const { content: _c, ...lastRest } = last;
  return {
    ok: true,
    text,
    exact: { params: rest, history: messages.slice(0, -1), last: lastRest },
  };
}

type Sensitivity =
  | { ok: true; level: string; policy?: RoutePolicy; gate?: DataPolicyGate }
  | { ok: false; reason: string };

const ctxGet = <T>(c: Context, key: string): T | undefined => {
  try {
    return c.get(key as any) as T | undefined;
  } catch {
    return undefined;
  }
};

/**
 * Whether the call's sensitivity lets it use the semantic cache. The level
 * is the data policy gate's, else the router's, else the declared
 * X-PS-Data-Classification (escalated by the policy's PII rules).
 */
export function sensitivityAllows(
  c: Context,
  params: any,
  headers: Record<string, string>,
  cfg: SemanticConfig
): Sensitivity {
  const gate = ctxGet<DataPolicyGate>(c, DATA_POLICY_GATE_KEY);
  const route = ctxGet<PSRoute>(c, 'psRoute');
  const policy = gate?.policy ?? ctxGet<RoutePolicy>(c, PS_ROUTE_POLICY_KEY);

  const pii = scanRequestBody(params);
  if (pii.length && !cfg.allowPii) return { ok: false, reason: 'pii' };

  let level = gate?.level ?? route?.sensitivityLevel;
  if (level === undefined) {
    const declared = parseRouteHeaders(headers).dataClassification;
    level = policy
      ? classifySensitivity(policy, declared, pii).level
      : declared || undefined;
  }
  const levels = policy?.sensitivity?.levels?.length
    ? policy.sensitivity.levels
    : DEFAULT_LEVELS;
  if (level === undefined)
    return { ok: true, level: 'unclassified', policy, gate };
  const rank = levels.indexOf(level);
  if (rank < 0) return { ok: false, reason: 'unknown_sensitivity' };
  const confidential = levels.indexOf('confidential');
  const max = cfg.maxSensitivity
    ? levels.indexOf(cfg.maxSensitivity)
    : confidential > 0
      ? confidential - 1
      : 0;
  // A max level the policy does not know fails closed.
  if (max < 0 || rank > max) return { ok: false, reason: 'sensitivity' };
  return { ok: true, level, policy, gate };
}

/** Under a data policy, whether the embedder may see this call's text. */
export function embedderAllowed(
  embedder: Embedder,
  gate: DataPolicyGate | undefined
): boolean {
  if (!gate?.rule || embedder.local) return true;
  // Facts the operator declared for the embedding endpoint decide alone: a
  // policy tier that happens to list the same model name describes another
  // endpoint. Only without declared facts does such a tier count.
  const f = embedder.facts;
  const declared =
    !!f &&
    (f.provider !== undefined ||
      f.jurisdiction !== undefined ||
      f.zdr !== undefined ||
      f.usCloudActExposure !== undefined);
  if (declared) return tierComplies(f!, gate.rule);
  return modelComplies(gate.policy, embedder.model, gate.rule);
}

/** `policy_version`, else a digest of the policy, else `none`. */
export function policyVersionOf(policy?: RoutePolicy): Promise<string> {
  if (!policy) return Promise.resolve('none');
  if (policy.policyVersion) return Promise.resolve(`v:${policy.policyVersion}`);
  // Digest per call: tests (and hot reloads) may mutate a policy in place.
  return sha256Hex(canonicalJSON(policy)).then((h) => `sha256:${h}`);
}

interface Pending {
  partition: string;
  vector: Float32Array;
  expiresAt: number;
}

const bypass = (reason: string) => {
  semanticBypassTotal.inc({ reason });
  return { status: SEMANTIC_STATUS.BYPASS, reason } as SemanticResult;
};

export interface SemanticLookupInput {
  endpoint: string;
  params: any;
  requestHeaders: Record<string, string>;
  index: number | string;
  cacheScope: string;
  /** Config `cache.max_age`, in seconds; capped at the TTL setting. */
  maxAge?: number;
  forceRefresh: boolean;
}

export interface SemanticResult {
  status: string;
  responseBody?: string;
  key?: string;
  similarity?: number;
  reason?: string;
}

/** The partition key (see the module comment). */
export async function partitionKey(
  c: Context,
  input: SemanticLookupInput,
  exact: unknown,
  level: string,
  policy: RoutePolicy | undefined,
  embedder: Embedder,
  cfg: SemanticConfig
): Promise<string> {
  const oidc = ctxGet<OidcIdentity>(c, OIDC_IDENTITY_KEY);
  const apiKey = process.env.PS_API_KEY;
  return sha256Hex(
    canonicalJSON({
      v: 1,
      scope: input.cacheScope,
      tenant: apiKey ? await sha256Hex(`ps-tenant\n${apiKey}`) : '',
      principal: cfg.sharePrincipals ? '' : oidc?.subjectHash ?? '',
      use_case: parseRouteHeaders(input.requestHeaders).useCase ?? '',
      model: input.params?.model ?? '',
      policy_version: await policyVersionOf(policy),
      sensitivity: level,
      endpoint: input.endpoint,
      embedder: embedder.id,
      exact,
    })
  );
}

/** Semantic cache read for one target; records what a write-back needs. */
export async function semanticLookup(
  c: Context,
  input: SemanticLookupInput,
  env: Env = process.env
): Promise<SemanticResult> {
  const cfg = semanticConfig(env);
  if (!input.cacheScope) return bypass('no_scope');
  if (!SEMANTIC_ENDPOINTS.has(input.endpoint)) return bypass('endpoint');
  if (input.params?.stream === true) return bypass('stream');
  const embedder = configuredEmbedder(env);
  if (!embedder) return bypass('no_embedder');
  const split = splitPrompt(input.params, cfg.maxInputChars);
  if (!split.ok) return bypass(split.reason);
  const sens = sensitivityAllows(c, input.params, input.requestHeaders, cfg);
  if (!sens.ok) return bypass(sens.reason);
  if (!embedderAllowed(embedder, sens.gate))
    return bypass('embedder_not_allowed');
  // The same egress check as other operator-configured endpoints (OTel,
  // telemetry): no metadata, private or loopback hosts unless listed in
  // TRUSTED_CUSTOM_HOSTS. Checked per call, before any text is sent.
  // Loaded lazily: requestValidator pulls in utils/env, which cache/index
  // (and its importers) should not load at module init.
  if (!embedder.local) {
    const { isSafeUpstreamURL } = await import('../requestValidator');
    if (!embedder.url || !isSafeUpstreamURL(embedder.url, c))
      return bypass('embedder_unsafe_url');
  }

  let vector: Float32Array | null;
  try {
    vector = normalize(await embedder.embed(split.text));
  } catch (err) {
    console.warn('[PS Cache] semantic embedding failed (fail-open):', err);
    return bypass('embedding_error');
  }
  if (!vector) return bypass('embedding_error');

  const partition = await partitionKey(
    c,
    input,
    split.exact,
    sens.level,
    sens.policy,
    embedder,
    cfg
  );
  const ttl = Math.min(
    input.maxAge && input.maxAge > 0 ? input.maxAge : cfg.ttlSeconds,
    cfg.ttlSeconds
  );
  const pending: Pending = {
    partition,
    vector,
    expiresAt: Date.now() + ttl * 1000,
  };
  const remember = () => {
    const all = ctxGet<Record<string, Pending>>(c, PENDING_KEY) ?? {};
    c.set(PENDING_KEY as any, { ...all, [String(input.index)]: pending });
  };

  if (input.forceRefresh) {
    remember();
    return { status: SEMANTIC_STATUS.REFRESH, key: partition };
  }
  const match = getStore().lookup(partition, vector, cfg.threshold);
  updateGauges();
  if (match) {
    return {
      status: SEMANTIC_STATUS.HIT,
      responseBody: match.responseBody,
      key: partition,
      similarity: match.similarity,
    };
  }
  remember();
  return { status: SEMANTIC_STATUS.MISS, key: partition };
}

/**
 * Stores a response read by semanticLookup for target `index`. The caller
 * has already checked the response is a complete, successful, non-refusal
 * JSON body. Returns the write outcome for metrics.
 */
export function semanticStore(
  c: Context,
  index: number | string | undefined,
  responseBody: unknown
): 'stored' | 'too_large' | 'no_lookup' {
  const pending = ctxGet<Record<string, Pending>>(c, PENDING_KEY)?.[
    String(index)
  ];
  if (!pending) return 'no_lookup';
  const ok = getStore().insert(
    pending.partition,
    pending.vector,
    JSON.stringify(responseBody),
    pending.expiresAt
  );
  updateGauges();
  return ok ? 'stored' : 'too_large';
}
