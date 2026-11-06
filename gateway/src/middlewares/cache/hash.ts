/** Hashing helpers shared by the exact-match and semantic caches. */

export const sha256Hex = async (value: string): Promise<string> => {
  const digest = await crypto.subtle.digest(
    { name: 'SHA-256' },
    new TextEncoder().encode(value)
  );
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
};

/** JSON with object keys sorted, so equal values always serialise alike. */
export const canonicalJSON = (value: unknown): string => {
  if (Array.isArray(value)) return `[${value.map(canonicalJSON).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canonicalJSON(record[k])}`)
      .join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
};
