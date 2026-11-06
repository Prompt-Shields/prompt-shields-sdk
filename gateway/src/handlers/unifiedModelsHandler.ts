/**
 * Unified, provider-backed `GET /v1/models` (#79).
 *
 * The request is proxied to the configured provider's models endpoint (same
 * path as any other `/v1` proxy call), then the list is normalised:
 * - callers that send `anthropic-version` (Anthropic SDKs, Claude Code) get
 *   Anthropic's shape: `{ data: [{ type, id, display_name, created_at }],
 *   has_more, first_id, last_id }`, with `limit` / `after_id` / `before_id`
 *   applied locally when the provider does not paginate natively;
 * - everyone else gets OpenAI's shape: `{ object: 'list', data: [{ id,
 *   object, created, owned_by }] }`.
 *
 * `GET /v1/models/{id}` gets the same treatment for a single model object.
 *
 * Error responses and bodies we cannot recognise pass through untouched.
 * Prompt Shields addition; not part of upstream Portkey.
 */
import { Context } from 'hono';
import {
  constructConfigFromRequestHeaders,
  tryTargetsRecursively,
} from './handlerUtils';
import { RouterError } from '../errors/RouterError';
import {
  classifyProxyRequest,
  configHasHooks,
  proxyGuardrailsUnsupportedResponse,
} from './proxyGuardrails';

export interface OpenAIModel {
  id: string;
  object: 'model';
  created: number;
  owned_by: string;
}

export interface AnthropicModel {
  type: 'model';
  id: string;
  display_name: string;
  created_at: string;
}

export interface AnthropicModelList {
  data: AnthropicModel[];
  has_more: boolean;
  first_id: string | null;
  last_id: string | null;
}

export interface AnthropicPageParams {
  limit?: string | null;
  after_id?: string | null;
  before_id?: string | null;
}

const DEFAULT_LIMIT = 20;
const MAX_LIMIT = 1000;

interface NormalisedModel {
  id: string;
  created: number; // unix seconds, 0 when unknown
  ownedBy: string;
  displayName: string;
}

function toUnixSeconds(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) {
    // Treat millisecond timestamps as such.
    return Math.floor(value > 1e12 ? value / 1000 : value);
  }
  if (typeof value === 'string') {
    const ms = Date.parse(value);
    if (!Number.isNaN(ms)) return Math.floor(ms / 1000);
  }
  return 0;
}

/**
 * Extracts a provider-neutral model list from a provider response, or `null`
 * when the body is not a recognisable models list.
 */
export function extractModels(body: unknown): NormalisedModel[] | null {
  if (!body || typeof body !== 'object') return null;
  const b = body as Record<string, unknown>;
  const raw = Array.isArray(b.data)
    ? b.data
    : Array.isArray(b.models)
      ? b.models
      : null;
  if (!raw) return null;

  const models: NormalisedModel[] = [];
  for (const entry of raw) {
    const model = extractModel(entry);
    if (model) models.push(model);
  }
  return models;
}

/**
 * Extracts one provider-neutral model from a single model object (OpenAI,
 * Anthropic or Gemini style), or `null` when it has no usable id.
 */
export function extractModel(entry: unknown): NormalisedModel | null {
  if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return null;
  const e = entry as Record<string, unknown>;
  const id =
    typeof e.id === 'string'
      ? e.id
      : typeof e.name === 'string'
        ? e.name
        : null;
  if (!id) return null;
  return {
    id,
    created: toUnixSeconds(e.created ?? e.created_at),
    ownedBy: typeof e.owned_by === 'string' ? e.owned_by : '',
    displayName:
      typeof e.display_name === 'string'
        ? e.display_name
        : typeof e.displayName === 'string'
          ? e.displayName
          : id,
  };
}

export function isAnthropicModelList(body: unknown): boolean {
  if (!body || typeof body !== 'object') return false;
  const b = body as Record<string, unknown>;
  return (
    Array.isArray(b.data) &&
    'has_more' in b &&
    b.data.every((m: any) => m && typeof m === 'object' && m.type === 'model')
  );
}

export function toOpenAIModel(m: NormalisedModel): OpenAIModel {
  return {
    id: m.id,
    object: 'model',
    created: m.created,
    owned_by: m.ownedBy || 'system',
  };
}

export function toAnthropicModel(m: NormalisedModel): AnthropicModel {
  return {
    type: 'model',
    id: m.id,
    display_name: m.displayName,
    created_at: new Date(m.created * 1000).toISOString(),
  };
}

export function toOpenAIModelList(models: NormalisedModel[]): {
  object: 'list';
  data: OpenAIModel[];
} {
  return { object: 'list', data: models.map(toOpenAIModel) };
}

function parseLimit(limit?: string | null): number {
  const n = Number(limit);
  if (!limit || !Number.isInteger(n) || n < 1) return DEFAULT_LIMIT;
  return Math.min(n, MAX_LIMIT);
}

export function toAnthropicModelList(
  models: NormalisedModel[],
  page: AnthropicPageParams = {}
): AnthropicModelList {
  const limit = parseLimit(page.limit);
  let start = 0;
  let end = models.length;
  if (page.after_id) {
    const i = models.findIndex((m) => m.id === page.after_id);
    start = i === -1 ? models.length : i + 1;
  } else if (page.before_id) {
    const i = models.findIndex((m) => m.id === page.before_id);
    end = i === -1 ? 0 : i;
    start = Math.max(0, end - limit);
  }
  const slice = page.before_id
    ? models.slice(start, end)
    : models.slice(start, start + limit);
  const hasMore = page.before_id ? start > 0 : start + limit < models.length;

  const data: AnthropicModel[] = slice.map(toAnthropicModel);
  return {
    data,
    has_more: hasMore,
    first_id: data.length ? data[0].id : null,
    last_id: data.length ? data[data.length - 1].id : null,
  };
}

export function wantsAnthropicShape(headers: Headers): boolean {
  return !!headers.get('anthropic-version');
}

/**
 * Rewrites a provider models response into the shape the caller expects.
 * Non-2xx responses and unrecognised bodies are returned unchanged.
 */
export async function normaliseModelsResponse(
  res: Response,
  anthropic: boolean,
  page: AnthropicPageParams
): Promise<Response> {
  if (!res.ok) return res;
  let body: unknown;
  try {
    body = await res.clone().json();
  } catch {
    return res;
  }

  let out: unknown;
  if (anthropic) {
    // Provider already paginated in Anthropic's shape (e.g. Anthropic itself).
    if (isAnthropicModelList(body)) return res;
    const models = extractModels(body);
    if (!models) return res;
    out = toAnthropicModelList(models, page);
  } else {
    const models = extractModels(body);
    if (!models) return res;
    out = toOpenAIModelList(models);
  }

  return jsonLike(res, out);
}

/**
 * Rewrites a provider's single-model response (`GET /v1/models/{id}`) into
 * the shape the caller expects: Anthropic's model object for
 * `anthropic-version` callers, OpenAI's otherwise. Non-2xx responses and
 * unrecognised bodies are returned unchanged.
 */
export async function normaliseModelResponse(
  res: Response,
  anthropic: boolean
): Promise<Response> {
  if (!res.ok) return res;
  let body: unknown;
  try {
    body = await res.clone().json();
  } catch {
    return res;
  }
  const b = body as Record<string, unknown> | null;
  // Already in the caller's shape (e.g. Anthropic's own model object).
  if (anthropic && b?.type === 'model') return res;
  if (!anthropic && b?.object === 'model') return res;
  const model = extractModel(body);
  if (!model) return res;
  return jsonLike(
    res,
    anthropic ? toAnthropicModel(model) : toOpenAIModel(model)
  );
}

/** A JSON response with `res`'s status and headers and a new body. */
function jsonLike(res: Response, out: unknown): Response {
  const headers = new Headers(res.headers);
  headers.delete('content-length');
  headers.delete('content-encoding');
  headers.set('content-type', 'application/json');
  return new Response(JSON.stringify(out), { status: res.status, headers });
}

/**
 * Proxies the request to the configured provider's models endpoint. Routing
 * errors become a JSON 400, anything else a JSON 500.
 */
async function proxyModelsRequest(c: Context): Promise<Response> {
  try {
    // Same upstream call proxyHandler makes for a GET, but always with an
    // empty params object: proxyHandler leaves the body undefined when the
    // caller sends no content-type (as SDK GETs often do), which tryPost
    // cannot handle.
    const requestHeaders = Object.fromEntries(c.req.raw.headers);
    const config = constructConfigFromRequestHeaders(requestHeaders);
    // Same up-front guardrail gate as proxyHandler (the /v1 catch-all): a
    // lookup the hooks cannot classify (e.g. an id with slashes) is refused
    // before any target, so a fallback order cannot reach an upstream first.
    if (configHasHooks(config)) {
      const decision = classifyProxyRequest(c, 'GET', {}, requestHeaders);
      if (decision.kind === 'unsupported') {
        return proxyGuardrailsUnsupportedResponse(decision.reason, c);
      }
    }
    return await tryTargetsRecursively(
      c,
      config,
      {},
      requestHeaders,
      'proxy',
      'GET',
      'config'
    );
  } catch (err: any) {
    const isRouterError = err instanceof RouterError;
    return new Response(
      JSON.stringify({
        status: 'failure',
        message: isRouterError ? err.message : `Proxy error: ${err.message}`,
      }),
      {
        status: isRouterError ? 400 : 500,
        headers: { 'content-type': 'application/json' },
      }
    );
  }
}

export async function unifiedModelsHandler(c: Context): Promise<Response> {
  const res = await proxyModelsRequest(c);
  return normaliseModelsResponse(res, wantsAnthropicShape(c.req.raw.headers), {
    limit: c.req.query('limit'),
    after_id: c.req.query('after_id'),
    before_id: c.req.query('before_id'),
  });
}

/** `GET /v1/models/{id}`: one model, in the caller's shape. */
export async function unifiedModelHandler(c: Context): Promise<Response> {
  const res = await proxyModelsRequest(c);
  return normaliseModelResponse(res, wantsAnthropicShape(c.req.raw.headers));
}
