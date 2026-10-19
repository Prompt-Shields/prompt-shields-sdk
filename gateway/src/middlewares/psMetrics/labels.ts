import { Context } from 'hono';
import { env } from 'hono/adapter';

/** Shared by psMetrics and psSecurityEvents (kept apart to avoid an import cycle). */

export const isMetricsEnabled = (c: Context) =>
  env<Record<string, string | undefined>>(c).PS_METRICS_ENABLED === 'true';

// Bounded, printable label values; anything else collapses to "other".
export const labelValue = (raw: unknown, fallback = 'none'): string => {
  if (typeof raw !== 'string' || !raw) return fallback;
  const v = raw.trim().slice(0, 64);
  return /^[A-Za-z0-9 _.:\/@-]+$/.test(v) ? v : 'other';
};

export const routeOf = (c: Context): string => {
  // The first concrete route that matched is the one that handled the
  // request (later catch-alls such as /v1/* also match). Using its pattern,
  // e.g. /v1/files/:id, keeps the label low-cardinality for paths with ids.
  const matched = (c.req as any).matchedRoutes as
    | { path: string; method: string }[]
    | undefined;
  const route = matched?.find(
    (r) => r.method !== 'ALL' && r.path !== '*' && r.path !== '/*'
  )?.path;
  return route ?? 'unmatched';
};
