/**
 * Node startup for rate limits (kept out of ./runtime.ts so edge bundles
 * never import `fs` or ioredis): reads PS_RATE_LIMITS or
 * PS_RATE_LIMITS_FILE and, with PS_RATELIMIT_REDIS_URL (else
 * REDIS_CONNECTION_STRING), sets up the shared Redis store. The client is
 * created on the first call a rule applies to, so a gateway with no rules
 * never talks to Redis for rate limits.
 */
import { RateLimitConfig, rateLimitConfigFromText } from './config';
import { leaseMsOf, setLocalRateLimits, setRateLimitStores } from './runtime';
import { RedisRateLimitStore } from './store';

export const MAX_RATE_LIMITS_FILE_BYTES = 1024 * 1024;

export async function loadRateLimitsFromEnv(
  env: Record<string, string | undefined> = process.env
): Promise<{ config?: RateLimitConfig; store: 'redis' | 'memory' }> {
  let text = env.PS_RATE_LIMITS;
  if (!text && env.PS_RATE_LIMITS_FILE) {
    const { readFile, stat } = await import('fs/promises');
    const path = env.PS_RATE_LIMITS_FILE;
    try {
      const info = await stat(path);
      if (!info.isFile() || info.size > MAX_RATE_LIMITS_FILE_BYTES)
        throw new Error('too large or not a file');
      text = await readFile(path, 'utf-8');
    } catch (err) {
      throw new Error(
        `cannot read PS_RATE_LIMITS_FILE ${path}: ${(err as Error).message}`
      );
    }
  }
  const config = rateLimitConfigFromText(text);
  setLocalRateLimits(config ?? null);

  const url = env.PS_RATELIMIT_REDIS_URL || env.REDIS_CONNECTION_STRING;
  let factory: (() => RedisRateLimitStore) | undefined;
  if (url) {
    const { default: Redis } = await import('ioredis');
    const timeout = Number(env.PS_RATELIMIT_REDIS_TIMEOUT_MS) || 250;
    factory = () => {
      const client = new Redis(url, {
        // Fail fast: a rate-limit check must not hold the call while Redis
        // is away (on_store_error decides what happens then).
        commandTimeout: timeout,
        connectTimeout: Math.max(timeout, 1000),
        maxRetriesPerRequest: 1,
        enableOfflineQueue: true,
      });
      client.on('error', (err) =>
        console.warn('[PS RateLimit] Redis error:', err?.message)
      );
      return new RedisRateLimitStore(client);
    };
  }
  setRateLimitStores({ primaryFactory: factory, leaseMs: leaseMsOf(env) });
  return { config, store: factory ? 'redis' : 'memory' };
}
