/**
 * PS fork (#73): encrypted reasoning carried across turns in thinking blocks.
 *
 * With `store: false` the provider keeps no reasoning between turns, so a
 * reasoning model loses its chain of thought after every tool call unless
 * the client sends the reasoning back. The Responses API returns it as
 * `encrypted_content` (with `include: ["reasoning.encrypted_content"]`);
 * this adapter puts that payload in the Anthropic `signature` (or the
 * `data` of a `redacted_thinking` block), which Messages clients already
 * return verbatim on the next turn.
 *
 * The payload is marked `ps-rsn1:<provider>:` so that:
 * - only payloads this adapter produced are sent to the Responses API; an
 *   Anthropic signature (base64, which never contains `:`) is never sent;
 * - a payload is sent back only to the provider that issued it: OpenAI and
 *   Azure OpenAI cannot decrypt each other's reasoning, so in a fallback or
 *   load-balanced config the reasoning is dropped rather than failing the
 *   request.
 */

const PREFIX = 'ps-rsn1:';

export function encodeReasoningSignature(
  provider: string,
  encryptedContent: string
): string {
  return `${PREFIX}${(provider || '').toLowerCase()}:${encryptedContent}`;
}

/**
 * The encrypted reasoning in a thinking signature, if this adapter issued
 * it for `provider`; undefined otherwise (Anthropic signatures included).
 */
export function decodeReasoningSignature(
  signature: unknown,
  provider: string
): string | undefined {
  if (typeof signature !== 'string' || !signature.startsWith(PREFIX)) {
    return undefined;
  }
  const rest = signature.slice(PREFIX.length);
  const sep = rest.indexOf(':');
  if (sep <= 0) return undefined;
  if (rest.slice(0, sep) !== (provider || '').toLowerCase()) return undefined;
  const payload = rest.slice(sep + 1);
  return payload || undefined;
}
