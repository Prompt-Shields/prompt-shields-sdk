export interface ResolvedMode {
  mode: 'simple' | 'DISABLED';
  forceRefresh: boolean;
}

// Precedence: valid X-PS-Cache header > PS_CACHE_DEFAULT env > null (defer to providerOption.cache).
// Returns null when neither header nor env decides, so the caller keeps existing behavior.
export function resolveCacheMode(
  headers: Record<string, string | undefined>,
  env: { PS_CACHE_DEFAULT?: string }
): ResolvedMode | null {
  const raw = (headers['x-ps-cache'] || '').trim().toLowerCase();
  if (raw === 'on') return { mode: 'simple', forceRefresh: false };
  if (raw === 'off') return { mode: 'DISABLED', forceRefresh: false };
  if (raw === 'refresh') return { mode: 'simple', forceRefresh: true };
  // raw is empty or malformed -> fall through to env default
  const def = (env.PS_CACHE_DEFAULT || '').trim().toLowerCase();
  if (def === 'on') return { mode: 'simple', forceRefresh: false };
  if (def === 'off') return { mode: 'DISABLED', forceRefresh: false };
  return null;
}
