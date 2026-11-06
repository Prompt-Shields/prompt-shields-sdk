/**
 * The shared budget counter format (schemas/budget/README.md). The gateway
 * and the SDK BudgetStore write the same keys, so a budget counts spend from
 * both; schemas/budget/vectors/keys.json pins the format for both.
 *
 *   ps:budget:v1:{namespace}:{scope}:{subject}:{window}:{period}
 *
 * The key is a Redis string holding the subject's USD spend in the window,
 * changed only with INCRBYFLOAT; its TTL is set on the first write in the
 * window (seconds to the window's end, rounded up, plus a day) and never
 * extended.
 *
 * Gateway extension (not read by the SDK): in-flight reservations of
 * estimated cost live in a sorted set at `{<counter key>}:res`. The braces
 * make the counter key the Redis Cluster hash tag, so the set hashes to the
 * counter's slot (counter keys never contain braces: they are encoded).
 */

export type BudgetWindow = 'day' | 'week' | 'month';
export type BudgetScope = 'global' | 'business_unit' | 'use_case' | 'owner';

export const KEY_PREFIX = 'ps:budget:v1';
export const GRACE_SECONDS = 86_400;
/** Subject of a global budget. */
export const GLOBAL_SUBJECT = '_';

const pad = (n: number, width = 2) => String(n).padStart(width, '0');

/** Thursday-based ISO 8601 week of `d` (UTC): [isoYear, week]. */
function isoWeek(d: Date): [number, number] {
  const day = new Date(
    Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate())
  );
  const weekday = day.getUTCDay() || 7; // Monday 1 .. Sunday 7
  day.setUTCDate(day.getUTCDate() + 4 - weekday); // that week's Thursday
  const yearStart = Date.UTC(day.getUTCFullYear(), 0, 1);
  const week = Math.ceil(((day.getTime() - yearStart) / 86400000 + 1) / 7);
  return [day.getUTCFullYear(), week];
}

/** The period label of `window` at `nowMs`: 2026-11-06, 2026-W45 or 2026-11. */
export function periodOf(window: BudgetWindow, nowMs: number): string {
  const d = new Date(nowMs);
  const y = d.getUTCFullYear();
  if (window === 'day')
    return `${y}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
  if (window === 'month') return `${y}-${pad(d.getUTCMonth() + 1)}`;
  const [isoYear, week] = isoWeek(d);
  return `${isoYear}-W${pad(week)}`;
}

/** Epoch ms at which the period holding `nowMs` ends (exclusive). */
export function periodEnd(window: BudgetWindow, nowMs: number): number {
  const d = new Date(nowMs);
  const y = d.getUTCFullYear();
  const m = d.getUTCMonth();
  if (window === 'day') return Date.UTC(y, m, d.getUTCDate() + 1);
  if (window === 'month') return Date.UTC(y, m + 1, 1);
  const weekday = d.getUTCDay() || 7;
  return Date.UTC(y, m, d.getUTCDate() + 8 - weekday); // next Monday
}

/** TTL of a counter first written at `nowMs`, in seconds. */
export function counterTtl(window: BudgetWindow, nowMs: number): number {
  return Math.ceil((periodEnd(window, nowMs) - nowMs) / 1000) + GRACE_SECONDS;
}

/** Every UTF-8 byte outside [A-Za-z0-9._~-] becomes %XX (upper-case hex). */
export function encodeSegment(value: string): string {
  let out = '';
  for (const byte of new TextEncoder().encode(value)) {
    const ch = String.fromCharCode(byte);
    out += /[A-Za-z0-9._~-]/.test(ch)
      ? ch
      : '%' + byte.toString(16).toUpperCase().padStart(2, '0');
  }
  return out;
}

export function counterKey(
  namespace: string,
  scope: BudgetScope,
  subject: string,
  window: BudgetWindow,
  period: string
): string {
  return [
    KEY_PREFIX,
    encodeSegment(namespace),
    scope,
    encodeSegment(subject),
    window,
    period,
  ].join(':');
}

/** The gateway's reservation set for a counter (same Cluster slot). */
export const reservationsKey = (key: string) => `{${key}}:res`;
