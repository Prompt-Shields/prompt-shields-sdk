/** snake_case -> camelCase for one key. */
export function camelKey(key: string): string {
  return key.replace(/_([a-z0-9])/g, (_, c: string) => c.toUpperCase());
}

/** camelCase -> snake_case for one key. */
export function snakeKey(key: string): string {
  return key.replace(/[A-Z]/g, (c) => `_${c.toLowerCase()}`);
}

/**
 * Response keys whose value is a map keyed by data, not by schema field names:
 * `asset.attestation` and the attestations `meta.current` are keyed by the
 * attested field (`asset`, `owner`, `data_classification`), and the usage
 * summary's `outcome_signal_counts` by outcome class (`max_tokens`, ...).
 * Their own keys are kept as sent (so they match `Attestation.field` and the
 * class names); their values are still camelCased. A learner projection's
 * `arm_samples` is keyed by model name.
 */
const MAP_KEYS = new Set(['attestation', 'current', 'outcome_signal_counts', 'arm_samples']);

/**
 * Response keys whose whole value is a document kept exactly as sent: a
 * learner snapshot's `snapshot` (the uploaded `ps-learner-snapshot.v1`).
 */
const RAW_KEYS = new Set(['snapshot']);

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** Recursively rename object keys to camelCase. Values are left alone. */
export function camelizeKeys<T = unknown>(value: unknown): T {
  if (Array.isArray(value)) return value.map((v) => camelizeKeys(v)) as unknown as T;
  if (isPlainObject(value)) {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) {
      if (RAW_KEYS.has(k) && isPlainObject(v)) {
        out[camelKey(k)] = v;
      } else if (MAP_KEYS.has(k) && isPlainObject(v)) {
        const map: Record<string, unknown> = {};
        for (const [mk, mv] of Object.entries(v)) map[mk] = camelizeKeys(mv);
        out[camelKey(k)] = map;
      } else {
        out[camelKey(k)] = camelizeKeys(v);
      }
    }
    return out as T;
  }
  return value as T;
}

/**
 * Recursively rename object keys to snake_case for a JSON request body.
 * `undefined` values are dropped; `null` is sent as `null`.
 */
export function snakeizeKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((v) => snakeizeKeys(v));
  if (value instanceof Date) return value.toISOString();
  if (isPlainObject(value)) {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) {
      if (v === undefined) continue;
      out[snakeKey(k)] = snakeizeKeys(v);
    }
    return out;
  }
  return value;
}
