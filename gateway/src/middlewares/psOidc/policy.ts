import type { JWTPayload } from 'jose';
import type { OidcConfig, OidcPolicy, PolicyGroup } from './config';

/**
 * Group policy: IdP groups/roles -> provider/model allowlist and an
 * optional rate limit. Entries are checked in order; the first entry with a
 * `match` value in the user's groups claim applies.
 */

export type PolicyDenyReason =
  | 'no_matching_group'
  | 'provider_not_allowed'
  | 'model_not_allowed';

/** Values of the (dotted) groups claim: an array of strings or one string. */
export function groupsFromClaims(
  payload: JWTPayload,
  claimPath: string
): string[] {
  let value: any = payload;
  for (const part of claimPath.split('.')) {
    if (!value || typeof value !== 'object') return [];
    value = value[part];
  }
  if (typeof value === 'string') return [value];
  if (Array.isArray(value))
    return value.filter((v) => typeof v === 'string').slice(0, 1000);
  return [];
}

/**
 * Entra ID replaces `groups` with `_claim_names.groups` when a user is in
 * more groups than fit in a token ("groups overage"). The gateway does not
 * call Microsoft Graph, so such a user matches no group by groups.
 */
export function hasGroupsOverage(
  payload: JWTPayload,
  claimPath: string
): boolean {
  const names = (payload as any)._claim_names;
  return !!names && typeof names === 'object' && claimPath in names;
}

export function matchGroup(
  policy: OidcPolicy,
  groups: string[]
): PolicyGroup | undefined {
  const have = new Set(groups);
  return policy.groups.find((g) => g.match.some((m) => have.has(m)));
}

export const modelAllowed = (patterns: string[], model: string): boolean =>
  patterns.some((p) =>
    p.endsWith('*') ? model.startsWith(p.slice(0, -1)) : p === model
  );

/**
 * Whether `group` may call `provider` / `model`. A request with no model
 * (files, batches, proxied GETs) is checked on the provider alone.
 */
export function checkTarget(
  group: PolicyGroup | undefined,
  provider: string | undefined,
  model: string | undefined
): PolicyDenyReason | undefined {
  if (!group) return undefined;
  if (group.providers && provider !== undefined) {
    if (!group.providers.includes(provider.toLowerCase()))
      return 'provider_not_allowed';
  }
  if (group.models && model) {
    if (!modelAllowed(group.models, model)) return 'model_not_allowed';
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// Rate limits: fixed one-minute windows, in memory, per gateway replica.

const WINDOW_MS = 60_000;
const MAX_BUCKETS = 50_000;
const buckets = new Map<string, { windowStart: number; count: number }>();
let now = () => Date.now();

/** Seconds until the window resets when over the limit, else undefined. */
export function takeRateLimit(
  group: PolicyGroup,
  subjectHash: string
): number | undefined {
  const rule = group.rateLimit;
  if (!rule) return undefined;
  const key =
    rule.per === 'group'
      ? `g\n${group.name}`
      : `u\n${group.name}\n${subjectHash}`;
  const t = now();
  const windowStart = t - (t % WINDOW_MS);
  let bucket = buckets.get(key);
  if (!bucket || bucket.windowStart !== windowStart) {
    if (!bucket && buckets.size >= MAX_BUCKETS) {
      for (const [k, b] of buckets)
        if (b.windowStart !== windowStart) buckets.delete(k);
      // Still full: forget the oldest entries rather than grow without bound.
      if (buckets.size >= MAX_BUCKETS) {
        let n = Math.ceil(MAX_BUCKETS / 10);
        for (const k of buckets.keys()) {
          if (n-- <= 0) break;
          buckets.delete(k);
        }
      }
    }
    bucket = { windowStart, count: 0 };
    buckets.set(key, bucket);
  }
  if (bucket.count >= rule.requestsPerMinute)
    return Math.max(1, Math.ceil((windowStart + WINDOW_MS - t) / 1000));
  bucket.count++;
  return undefined;
}

// ---------------------------------------------------------------------------
// Subject pseudonym

const hex = (buf: ArrayBuffer) =>
  Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');

/**
 * 32 hex characters identifying `iss` + `sub`: HMAC-SHA-256 keyed with
 * PS_OIDC_SUBJECT_SALT when it is set, else SHA-256. The raw subject is
 * never stored or sent unless PS_OIDC_TELEMETRY_SUBJECT=raw.
 */
export async function subjectHash(
  config: OidcConfig,
  sub: string
): Promise<string> {
  const data = new TextEncoder().encode(`${config.issuer}\n${sub}`);
  if (config.subjectSalt) {
    const key = await crypto.subtle.importKey(
      'raw',
      new TextEncoder().encode(config.subjectSalt),
      { name: 'HMAC', hash: 'SHA-256' },
      false,
      ['sign']
    );
    return hex(await crypto.subtle.sign('HMAC', key, data)).slice(0, 32);
  }
  return hex(await crypto.subtle.digest('SHA-256', data)).slice(0, 32);
}

/** Test helpers. */
export function _resetRateLimitsForTests() {
  buckets.clear();
  now = () => Date.now();
}
export function _setRateLimitClockForTests(fn: () => number) {
  now = fn;
}
