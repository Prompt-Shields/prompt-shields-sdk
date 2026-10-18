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

const isIssued = (v: unknown) => typeof v === 'string' && v.startsWith(PREFIX);

/**
 * The Messages body without the thinking blocks this adapter issued, for
 * targets that speak Messages natively (Anthropic, Bedrock, Vertex, ...).
 * Their signatures are not Anthropic's, so such a target would reject the
 * request when a conversation moves there from this route (e.g. a fallback
 * from OpenAI to Anthropic). An assistant turn left empty is dropped.
 * Returns the body unchanged (same object) when there is nothing to strip;
 * otherwise a copy, so other targets of the same request keep the blocks.
 */
export function stripIssuedReasoning<T>(body: T): T {
  const messages = (body as any)?.messages;
  if (!Array.isArray(messages)) return body;
  let changed = false;
  const out: any[] = [];
  for (const m of messages) {
    if (m?.role !== 'assistant' || !Array.isArray(m.content)) {
      out.push(m);
      continue;
    }
    const content = m.content.filter(
      (b: any) =>
        !(
          (b?.type === 'thinking' && isIssued(b.signature)) ||
          (b?.type === 'redacted_thinking' && isIssued(b.data))
        )
    );
    if (content.length === m.content.length) {
      out.push(m);
      continue;
    }
    changed = true;
    if (content.length) out.push({ ...m, content });
  }
  return changed ? ({ ...(body as any), messages: out } as T) : body;
}
