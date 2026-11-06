export interface ResolvedMode {
  mode: 'simple' | 'semantic' | 'DISABLED';
  forceRefresh: boolean;
}

// Precedence: valid X-PS-Cache header > PS_CACHE_DEFAULT env > null (defer to providerOption.cache).
// Returns null when neither header nor env decides, so the caller keeps existing behavior.
// `semantic` (#33) selects the semantic cache; it reads and writes nothing
// until an embedder is configured (see semantic.ts).
export function resolveCacheMode(
  headers: Record<string, string | undefined>,
  env: { PS_CACHE_DEFAULT?: string }
): ResolvedMode | null {
  const raw = (headers['x-ps-cache'] || '').trim().toLowerCase();
  const def = (env.PS_CACHE_DEFAULT || '').trim().toLowerCase();
  if (raw === 'on') return { mode: 'simple', forceRefresh: false };
  if (raw === 'semantic') return { mode: 'semantic', forceRefresh: false };
  if (raw === 'off') return { mode: 'DISABLED', forceRefresh: false };
  if (raw === 'refresh') {
    // Refresh keeps the deployment's default mode when it is semantic.
    return {
      mode: def === 'semantic' ? 'semantic' : 'simple',
      forceRefresh: true,
    };
  }
  // raw is empty or malformed -> fall through to env default
  if (def === 'on') return { mode: 'simple', forceRefresh: false };
  if (def === 'semantic') return { mode: 'semantic', forceRefresh: false };
  if (def === 'off') return { mode: 'DISABLED', forceRefresh: false };
  return null;
}
