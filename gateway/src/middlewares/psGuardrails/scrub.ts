/**
 * PS fork (#349): what the gateway keeps or exports of a response follows
 * the central policy's output guardrails too.
 *
 * psGuardrails rewrites the response on its way to the client, after the
 * middlewares inside it (cache write-back, OTel, the /log/stream entries)
 * have seen the provider's version. It leaves an OutputScrub on the
 * context for them: they pass their copy of the response through it
 * before storing or exporting it. No import from the guardrails code, so
 * any middleware can use it.
 */
import { Context } from 'hono';

/** Context key for the call's OutputScrub (unset: no output rules). */
export const GUARDRAIL_SCRUB_KEY = 'psGuardrailScrub';

/**
 * Redacts the model output in a response object in place with the call's
 * output rules; returns true when a `deny` rule matched, and the response
 * must then not be kept or exported. Records nothing (psGuardrails records
 * the findings once, for the response the client gets).
 */
export type OutputScrub = (obj: unknown) => boolean;

export type ScrubOutcome = 'none' | 'scrubbed' | 'denied';

/**
 * Passes `obj` (mutated in place) through the call's output guardrails:
 * 'none' when the call has none, 'denied' when the response must not be
 * kept or exported, else 'scrubbed'.
 */
export function scrubOutput(c: Context, obj: unknown): ScrubOutcome {
  let scrub: unknown;
  try {
    scrub = c.get(GUARDRAIL_SCRUB_KEY as any);
  } catch {
    return 'none';
  }
  if (typeof scrub !== 'function') return 'none';
  return (scrub as OutputScrub)(obj) ? 'denied' : 'scrubbed';
}
