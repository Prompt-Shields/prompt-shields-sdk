/**
 * Implicit outcome signals (PRD 0001 §7.13, #44) for gateway telemetry.
 *
 * The SDK computes these classes in process
 * (`packages/sdk/prompt_shields/outcome_signals.py`). The gateway emits the
 * ones it can see from the request it proxied, with the same class names and
 * version, so the collector stores both alike:
 *
 *   fallback      an upstream attempt failed before the call was served
 *                 (non-empty `fallback_chain`)
 *   max_tokens    the answer was cut off at the output token limit
 *   refusal       the model refused or a content filter stopped it
 *   stream_error  a stream ended without its terminal event
 *   abandoned     the client disconnected before the stream ended
 *
 * The others (schema_invalid, tool_call_invalid, regenerate, retry) need the
 * caller's intent or session state and stay SDK-only. Only class names leave
 * the gateway; the response text is never read beyond stop reasons and the
 * presence of a refusal.
 */

/** Bumped with the SDK's OUTCOME_SIGNALS_VERSION. */
export const OUTCOME_SIGNALS_VERSION = 1;

// Provider stop/finish reasons, lower-cased (as the SDK lists them).
const TRUNCATED_REASONS = new Set([
  'length',
  'max_tokens',
  'max_output_tokens',
  'model_context_window_exceeded',
]);
const REFUSAL_REASONS = new Set([
  'content_filter',
  'refusal',
  'safety',
  'recitation',
  'prohibited_content',
  'blocklist',
  'spii',
  'image_safety',
  'guardrail_intervened',
  'content_filtered',
  'error_toxic',
]);

/** At most this many distinct stop reasons are kept from a stream. */
export const MAX_STOP_REASONS = 8;

/** A stop reason as a lower-case token, or undefined. */
export function normaliseReason(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value || value.length > 64) return;
  return value.split('.').pop()!.toLowerCase();
}

/**
 * The stop reasons a JSON body or one SSE event reports: Chat Completions
 * `choices[].finish_reason`, Messages `stop_reason` (and `delta.stop_reason`
 * on message_delta), Responses `incomplete_details.reason` (also nested in
 * `response` on stream events).
 */
export function stopReasonsOf(body: any): string[] {
  if (!body || typeof body !== 'object') return [];
  const raw: unknown[] = [];
  if (Array.isArray(body.choices)) {
    for (const ch of body.choices) raw.push(ch?.finish_reason);
  }
  raw.push(body.stop_reason, body.delta?.stop_reason);
  raw.push(body.incomplete_details?.reason);
  raw.push(body.response?.incomplete_details?.reason);
  const out: string[] = [];
  for (const r of raw) {
    const n = normaliseReason(r);
    if (n && !out.includes(n)) out.push(n);
  }
  return out;
}

/**
 * Whether a JSON body or SSE event carries an explicit refusal: a non-empty
 * Chat Completions `message.refusal` / `delta.refusal`, or a Responses
 * `refusal` content part.
 */
export function hasRefusal(body: any): boolean {
  if (!body || typeof body !== 'object') return false;
  const nonEmpty = (v: unknown) => typeof v === 'string' && v.length > 0;
  if (Array.isArray(body.choices)) {
    for (const ch of body.choices) {
      if (nonEmpty(ch?.message?.refusal) || nonEmpty(ch?.delta?.refusal))
        return true;
    }
  }
  const outputs = Array.isArray(body.output)
    ? body.output
    : Array.isArray(body.response?.output)
      ? body.response.output
      : [];
  for (const item of outputs) {
    if (!Array.isArray(item?.content)) continue;
    if (item.content.some((part: any) => part?.type === 'refusal')) return true;
  }
  const type = body.type;
  return (
    type === 'response.refusal.delta' ||
    type === 'response.refusal.done' ||
    body.part?.type === 'refusal' ||
    body.item?.content?.some?.((p: any) => p?.type === 'refusal') === true
  );
}

export interface OutcomeInputs {
  /** Stop reasons of the response (JSON body or stream). */
  stopReasons?: string[];
  /** The response carried an explicit refusal. */
  refusal?: boolean;
  fallbackChain?: string[];
  streamTruncated?: boolean;
  streamCancelled?: boolean;
}

/** The sorted outcome classes for one call. */
export function outcomeSignals(i: OutcomeInputs): string[] {
  const out = new Set<string>();
  if (i.fallbackChain && i.fallbackChain.length) out.add('fallback');
  for (const r of i.stopReasons ?? []) {
    if (TRUNCATED_REASONS.has(r)) out.add('max_tokens');
    if (REFUSAL_REASONS.has(r)) out.add('refusal');
  }
  if (i.refusal) out.add('refusal');
  if (i.streamCancelled) out.add('abandoned');
  else if (i.streamTruncated) out.add('stream_error');
  return [...out].sort();
}
