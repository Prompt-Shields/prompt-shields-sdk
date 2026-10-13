/**
 * PS fork: credential masking for anything the gateway writes to its own logs.
 * Upstream Portkey hardened this in its enterprise gateway (2.6.1, 2.19, 2.27),
 * which is not open source, so this is our own implementation.
 */

export const REDACTED = '[REDACTED]';

const SENSITIVE_KEY =
  /(authorization|api[-_]?key|secret|token|password|passwd|credential|signature|cookie|session[-_]?id|private[-_]?key|access[-_]?key)/i;

const STRING_PATTERNS: [RegExp, string][] = [
  // user:password@ in URLs (redis://, postgres://, https://)
  [/([a-z][a-z0-9+.-]*:\/\/)[^/\s:@]*:[^/\s@]+@/gi, `$1${REDACTED}@`],
  [/\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+/gi, `$1 ${REDACTED}`],
  // OpenAI, Anthropic, Portkey-style and PS keys. Requiring a digit keeps
  // file names such as ps-telemetry.ts in stack traces readable.
  [/\b(sk|pk|rk|ps)-(?=[A-Za-z_-]*\d)[A-Za-z0-9_-]{8,}/g, REDACTED],
  // AWS access key ids
  [/\b(AKIA|ASIA)[0-9A-Z]{16}\b/g, REDACTED],
  // Google API keys
  [/\bAIza[0-9A-Za-z_-]{35}\b/g, REDACTED],
];

export function isSensitiveKey(key: string): boolean {
  return SENSITIVE_KEY.test(key);
}

export function redactString(value: string): string {
  let out = value;
  for (const [pattern, replacement] of STRING_PATTERNS) {
    out = out.replace(pattern, replacement);
  }
  return out;
}

/**
 * Returns a copy of `value` with credential-looking keys and substrings
 * masked. Never mutates the input. Depth-limited to stay cheap on large logs.
 */
export function redactSecrets<T>(value: T, depth = 0): T {
  if (typeof value === 'string') return redactString(value) as T;
  if (value === null || typeof value !== 'object' || depth > 8) return value;
  if (value instanceof Error) {
    const copy: Error & { code?: unknown } = new Error(
      redactString(value.message)
    );
    copy.name = value.name;
    const code = (value as Error & { code?: unknown }).code;
    if (code !== undefined) copy.code = code;
    return copy as T;
  }
  if (Array.isArray(value)) {
    return value.map((v) => redactSecrets(v, depth + 1)) as T;
  }
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    out[k] = isSensitiveKey(k) ? REDACTED : redactSecrets(v, depth + 1);
  }
  return out as T;
}

// A provider config with no key configured still maps e.g. "Bearer undefined";
// that placeholder must not displace a credential the client sent itself.
const isMeaningfulCredential = (value: unknown): boolean => {
  if (typeof value !== 'string') return false;
  const v = value.trim();
  if (!v || /^(bearer|basic)$/i.test(v)) return false;
  return !/(^|\s)(undefined|null)$/.test(v);
};

/**
 * The credential headers a provider config produced, so they can be applied
 * last and win over forwarded or proxied client headers.
 */
export function pickCredentialHeaders(
  headers: Record<string, string>
): Record<string, string> {
  return Object.fromEntries(
    Object.entries(headers).filter(
      ([h, v]) => isSensitiveKey(h) && isMeaningfulCredential(v)
    )
  );
}
