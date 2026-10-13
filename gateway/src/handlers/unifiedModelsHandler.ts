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
 * Error responses and bodies we cannot recognise pass through untouched.
 * Prompt Shields addition; not part of upstream Portkey.
 */
import { Context } from 'hono';
import {
  constructConfigFromRequestHeaders,
  tryTargetsRecursively,
} from './handlerUtils';
import { RouterError } from '../errors/RouterError';

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
    if (!entry || typeof entry !== 'object') continue;
    const e = entry as Record<string, unknown>;
    const id =
      typeof e.id === 'string'
        ? e.id
        : typeof e.name === 'string'
          ? e.name
          : null;
    if (!id) continue;
    models.push({
      id,
      created: toUnixSeconds(e.created ?? e.created_at),
      ownedBy: typeof e.owned_by === 'string' ? e.owned_by : '',
      displayName:
        typeof e.display_name === 'string'
          ? e.display_name
          : typeof e.displayName === 'string'
            ? e.displayName
            : id,
    });
  }
  return models;
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

export function toOpenAIModelList(models: NormalisedModel[]): {
  object: 'list';
  data: OpenAIModel[];
} {
  return {
    object: 'list',
    data: models.map((m) => ({
      id: m.id,
      object: 'model',
      created: m.created,
      owned_by: m.ownedBy || 'system',
    })),
  };
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

  const data: AnthropicModel[] = slice.map((m) => ({
    type: 'model',
    id: m.id,
    display_name: m.displayName,
    created_at: new Date(m.created * 1000).toISOString(),
  }));
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

  const headers = new Headers(res.headers);
  headers.delete('content-length');
  headers.delete('content-encoding');
  headers.set('content-type', 'application/json');
  return new Response(JSON.stringify(out), { status: res.status, headers });
}

export async function unifiedModelsHandler(c: Context): Promise<Response> {
  let res: Response;
  try {
    // Same upstream call proxyHandler makes for a GET, but always with an
    // empty params object: proxyHandler leaves the body undefined when the
    // caller sends no content-type (as SDK GETs often do), which tryPost
    // cannot handle.
    const requestHeaders = Object.fromEntries(c.req.raw.headers);
    res = await tryTargetsRecursively(
      c,
      constructConfigFromRequestHeaders(requestHeaders),
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
  return normaliseModelsResponse(res, wantsAnthropicShape(c.req.raw.headers), {
    limit: c.req.query('limit'),
    after_id: c.req.query('after_id'),
    before_id: c.req.query('before_id'),
  });
}
