/**
 * PS fork (#343): settings and the collector client for virtual keys.
 *
 * With PS_VIRTUAL_KEYS_ENABLED=true the gateway resolves `ps-vk-...` keys
 * with `POST ${PS_COLLECTOR_URL}/api/v1/virtual-keys/resolve`, authenticated
 * with an ingest key holding `virtual-keys:resolve`
 * (PS_VIRTUAL_KEYS_API_KEY, else PS_API_KEY). The answer carries provider
 * keys, so it is only fetched over https, or plain http to loopback or an
 * in-cluster name (as the policy pull, ../policyPull/puller.ts);
 * PS_VIRTUAL_KEYS_ALLOW_HTTP=true lifts that for other hosts.
 *
 * Answers, active or not, are cached in memory per replica for
 * PS_VIRTUAL_KEYS_CACHE_SECONDS (default and maximum 30, or less when the
 * collector says so), keyed by the SHA-256 of the virtual key (never the
 * key itself), so a revocation reaches every replica within 30 s.
 * Concurrent lookups of one key share one request. A failed lookup is not
 * cached: the call gets 503 and the next call asks again.
 */
import { isPlainHttpHostAllowed } from '../policyPull/puller';
import { upstreamFetch } from '../../utils/fetch';
import type { PoolCredential } from './pool';

export const KEY_PREFIX = 'ps-vk-';
export const MAX_CACHE_SECONDS = 30;
export const REQUEST_TIMEOUT_MS = 5_000;
export const MAX_CACHE_ENTRIES = 10_000;
const MAX_KEY_LENGTH = 512;

export interface VirtualKeyInfo {
  id: string;
  name: string;
  team_id: string | null;
  project_id: string | null;
  principal_id: string | null;
  user_id: string | null;
  allowed_models: string[] | null;
  strategy: string;
  expires_at: string | null;
}

export type Resolution =
  | { active: false; reason: string }
  | {
      active: true;
      virtualKey: VirtualKeyInfo;
      credentials: PoolCredential[];
    };

export interface VirtualKeySettings {
  enabled: boolean;
  error?: string;
  url?: string;
  apiKey?: string;
  cacheMs?: number;
}

const isTrue = (v?: string) =>
  ['true', '1', 'yes'].includes((v ?? '').trim().toLowerCase());

/** Read per request from the runtime env (process.env on Node). */
export function virtualKeySettings(
  env: Record<string, any> = {}
): VirtualKeySettings {
  if (!isTrue(env.PS_VIRTUAL_KEYS_ENABLED)) return { enabled: false };
  const apiKey = String(
    env.PS_VIRTUAL_KEYS_API_KEY || env.PS_API_KEY || ''
  ).trim();
  if (!apiKey)
    return {
      enabled: true,
      error:
        'PS_VIRTUAL_KEYS_ENABLED needs an ingest key with virtual-keys:resolve in PS_VIRTUAL_KEYS_API_KEY or PS_API_KEY',
    };
  const base = String(env.PS_COLLECTOR_URL || 'http://localhost:8000').replace(
    /\/+$/,
    ''
  );
  let url: URL;
  try {
    url = new URL(base);
  } catch {
    return { enabled: true, error: 'PS_COLLECTOR_URL is not a valid URL' };
  }
  if (
    url.protocol !== 'https:' &&
    !(
      url.protocol === 'http:' &&
      (isTrue(env.PS_VIRTUAL_KEYS_ALLOW_HTTP) ||
        isPlainHttpHostAllowed(url.hostname))
    )
  )
    return {
      enabled: true,
      error: `virtual key resolution needs https for ${url.hostname} (plain http is only used for loopback and in-cluster names); use an https PS_COLLECTOR_URL or set PS_VIRTUAL_KEYS_ALLOW_HTTP=true`,
    };
  let seconds = MAX_CACHE_SECONDS;
  const raw = String(env.PS_VIRTUAL_KEYS_CACHE_SECONDS ?? '').trim();
  if (raw) {
    seconds = Number(raw);
    if (!Number.isFinite(seconds) || seconds < 0 || seconds > MAX_CACHE_SECONDS)
      return {
        enabled: true,
        error: `PS_VIRTUAL_KEYS_CACHE_SECONDS must be 0-${MAX_CACHE_SECONDS}, got "${raw}"`,
      };
  }
  return {
    enabled: true,
    url: `${base}/api/v1/virtual-keys/resolve`,
    apiKey,
    cacheMs: Math.round(seconds * 1000),
  };
}

/** The virtual key a request carries, if any, and where it was found. */
export function virtualKeyOf(
  headers: Headers
): { key: string; header: string } | undefined {
  const explicit = headers.get('x-ps-virtual-key')?.trim();
  if (explicit) return { key: explicit, header: 'x-ps-virtual-key' };
  const m = /^Bearer\s+(\S+)\s*$/i.exec(headers.get('authorization') ?? '');
  if (m?.[1]?.startsWith(KEY_PREFIX))
    return { key: m[1], header: 'authorization' };
  const apiKey = headers.get('x-api-key')?.trim();
  if (apiKey?.startsWith(KEY_PREFIX))
    return { key: apiKey, header: 'x-api-key' };
  return undefined;
}

export class ResolutionError extends Error {}

type Fetch = (url: string, init: RequestInit) => Promise<Response>;

async function sha256Hex(input: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(input)
  );
  return Array.from(new Uint8Array(digest), (b) =>
    b.toString(16).padStart(2, '0')
  ).join('');
}

const str = (v: unknown): string | null =>
  typeof v === 'string' && v ? v : null;

/** The collector's answer, checked; throws ResolutionError when malformed. */
export function parseResolution(body: any): {
  resolution: Resolution;
  ttlSeconds?: number;
} {
  if (!body || typeof body !== 'object' || typeof body.active !== 'boolean')
    throw new ResolutionError('malformed resolution');
  const ttl =
    typeof body.cache_ttl_seconds === 'number' && body.cache_ttl_seconds >= 0
      ? body.cache_ttl_seconds
      : undefined;
  if (!body.active)
    return {
      resolution: { active: false, reason: str(body.reason) ?? 'not_found' },
      ttlSeconds: ttl,
    };
  const vk = body.virtual_key;
  if (
    !vk ||
    typeof vk !== 'object' ||
    !str(vk.id) ||
    !Array.isArray(body.credentials)
  )
    throw new ResolutionError('malformed resolution');
  const credentials: PoolCredential[] = [];
  for (const c of body.credentials) {
    if (
      !c ||
      !str(c.id) ||
      !str(c.provider) ||
      !str(c.api_key) ||
      typeof c.key_fingerprint !== 'string'
    )
      throw new ResolutionError('malformed resolution');
    credentials.push({
      id: c.id,
      provider: String(c.provider).toLowerCase(),
      label: str(c.label) ?? undefined,
      key_fingerprint: c.key_fingerprint,
      api_key: c.api_key,
    });
  }
  const allowed =
    vk.allowed_models === null || vk.allowed_models === undefined
      ? null
      : Array.isArray(vk.allowed_models) &&
          vk.allowed_models.every((m: unknown) => typeof m === 'string')
        ? (vk.allowed_models as string[])
        : undefined;
  if (allowed === undefined) throw new ResolutionError('malformed resolution');
  return {
    resolution: {
      active: true,
      virtualKey: {
        id: vk.id,
        name: str(vk.name) ?? vk.id,
        team_id: str(vk.team_id),
        project_id: str(vk.project_id),
        principal_id: str(vk.principal_id),
        user_id: str(vk.user_id),
        allowed_models: allowed,
        strategy: str(vk.strategy) ?? 'round_robin',
        expires_at: str(vk.expires_at),
      },
      credentials,
    },
    ttlSeconds: ttl,
  };
}

export class VirtualKeyResolver {
  private cache = new Map<string, { until: number; resolution: Resolution }>();
  private inflight = new Map<string, Promise<Resolution>>();

  constructor(
    private readonly fetchFn: Fetch = upstreamFetch,
    private readonly now: () => number = Date.now
  ) {}

  async resolve(
    key: string,
    settings: VirtualKeySettings
  ): Promise<Resolution> {
    if (!key.startsWith(KEY_PREFIX) || key.length > MAX_KEY_LENGTH)
      return { active: false, reason: 'not_found' };
    // In-flight lookups are keyed by the key itself (process memory, only
    // while the request runs) so concurrent calls join before any await;
    // the cache, which outlives the request, by its SHA-256.
    const flightKey = `${settings.url}\n${key}`;
    const pending = this.inflight.get(flightKey);
    if (pending) return pending;
    const p = this.lookup(key, settings).finally(() =>
      this.inflight.delete(flightKey)
    );
    this.inflight.set(flightKey, p);
    return p;
  }

  private async lookup(
    key: string,
    settings: VirtualKeySettings
  ): Promise<Resolution> {
    const id = await sha256Hex(`${settings.url}\n${key}`);
    const hit = this.cache.get(id);
    if (hit && hit.until > this.now()) return hit.resolution;
    if (hit) this.cache.delete(id);
    return this.fetchResolution(key, settings, id);
  }

  private async fetchResolution(
    key: string,
    settings: VirtualKeySettings,
    id: string
  ): Promise<Resolution> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    let resp: Response;
    try {
      resp = await this.fetchFn(settings.url!, {
        method: 'POST',
        headers: {
          authorization: `Bearer ${settings.apiKey}`,
          'content-type': 'application/json',
          accept: 'application/json',
        },
        body: JSON.stringify({ key }),
        signal: controller.signal,
      });
    } catch (e: any) {
      throw new ResolutionError(
        e?.name === 'AbortError' ? 'timeout' : 'unreachable'
      );
    } finally {
      clearTimeout(timer);
    }
    if (!resp.ok) {
      await resp.body?.cancel().catch(() => undefined);
      throw new ResolutionError(`collector answered ${resp.status}`);
    }
    let body: unknown;
    try {
      body = await resp.json();
    } catch {
      throw new ResolutionError('malformed resolution');
    }
    const { resolution, ttlSeconds } = parseResolution(body);
    const ms = Math.min(
      settings.cacheMs ?? MAX_CACHE_SECONDS * 1000,
      ttlSeconds === undefined ? Infinity : ttlSeconds * 1000
    );
    if (ms > 0) {
      this.cache.set(id, { until: this.now() + ms, resolution });
      if (this.cache.size > MAX_CACHE_ENTRIES) {
        const oldest = this.cache.keys().next().value;
        if (oldest !== undefined) this.cache.delete(oldest);
      }
    }
    return resolution;
  }

  clear(): void {
    this.cache.clear();
    this.inflight.clear();
  }
}

export const defaultResolver = new VirtualKeyResolver();
