/**
 * PS fork (#73): signed thinking carried across turns on the Responses
 * adapter.
 *
 * Anthropic-style providers (Anthropic, Bedrock, Vertex AI) sign each
 * `thinking` block, and with extended thinking and tool use they require
 * the signed blocks of the current assistant turn back on the next request.
 * A Responses client keeps the history itself and returns `reasoning` items
 * as it received them, but a `reasoning` item has only a summary, so the
 * signature was lost and the second step of a tool loop failed.
 *
 * The adapter therefore puts the provider's thinking blocks in the
 * reasoning item's `encrypted_content`, the field the Responses API uses
 * for exactly this, as `ps-thk1:<provider>:<base64 JSON blocks>`. On the
 * next request a reasoning item with that payload becomes the assistant
 * message's leading `content_blocks`, which these providers' Chat
 * Completions transforms send as signed thinking.
 *
 * - Payloads are restored only for the provider that issued them; for any
 *   other target (a fallback, a load balance) the reasoning is dropped, as
 *   before, since signatures do not verify elsewhere.
 * - A native Responses target (OpenAI, Azure OpenAI, ...) never sees the
 *   payload: `stripIssuedThinking` removes such items before the request is
 *   passed through.
 * - Only the thinking text, its signature and redacted thinking data are
 *   encoded: nothing from the request headers.
 */

const PREFIX = 'ps-thk1:';

/** Providers whose Chat Completions transform sends thinking content_blocks. */
const CARRY_PROVIDERS = new Set(['anthropic', 'bedrock', 'vertex-ai']);

export type CarriedThinkingBlock =
  | { type: 'thinking'; thinking: string; signature: string }
  | { type: 'redacted_thinking'; data: string };

export function carriesThinking(provider: string | undefined): boolean {
  return CARRY_PROVIDERS.has((provider || '').toLowerCase());
}

function toBase64(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let bin = '';
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

function fromBase64(b64: string): string {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}

/** Keep only blocks a provider can verify: signed thinking, redacted data. */
export function verifiableBlocks(blocks: any[]): CarriedThinkingBlock[] {
  const out: CarriedThinkingBlock[] = [];
  for (const b of blocks || []) {
    if (
      b?.type === 'thinking' &&
      typeof b.signature === 'string' &&
      b.signature &&
      typeof b.thinking === 'string'
    ) {
      out.push({
        type: 'thinking',
        thinking: b.thinking,
        signature: b.signature,
      });
    } else if (
      b?.type === 'redacted_thinking' &&
      typeof b.data === 'string' &&
      b.data
    ) {
      out.push({ type: 'redacted_thinking', data: b.data });
    }
  }
  return out;
}

/**
 * The `encrypted_content` for these blocks, or undefined when the provider
 * does not carry thinking or no block is verifiable.
 */
export function encodeThinkingCarry(
  provider: string,
  blocks: any[]
): string | undefined {
  if (!carriesThinking(provider)) return undefined;
  const keep = verifiableBlocks(blocks);
  if (!keep.length) return undefined;
  return `${PREFIX}${provider.toLowerCase()}:${toBase64(JSON.stringify(keep))}`;
}

/** Whether `encryptedContent` is a payload this adapter issued. */
export function isIssuedThinking(encryptedContent: unknown): boolean {
  return (
    typeof encryptedContent === 'string' && encryptedContent.startsWith(PREFIX)
  );
}

/**
 * The thinking blocks in `encryptedContent` if this adapter issued it for
 * `provider`; undefined otherwise (native Responses reasoning included).
 */
export function decodeThinkingCarry(
  encryptedContent: unknown,
  provider: string | undefined
): CarriedThinkingBlock[] | undefined {
  if (!isIssuedThinking(encryptedContent) || !carriesThinking(provider)) {
    return undefined;
  }
  const rest = (encryptedContent as string).slice(PREFIX.length);
  const sep = rest.indexOf(':');
  if (sep <= 0 || rest.slice(0, sep) !== provider!.toLowerCase()) {
    return undefined;
  }
  try {
    const blocks = verifiableBlocks(
      JSON.parse(fromBase64(rest.slice(sep + 1)))
    );
    return blocks.length ? blocks : undefined;
  } catch {
    return undefined;
  }
}

/**
 * The Responses body without the reasoning items this adapter issued, for
 * targets that speak the Responses API natively. Returns the body unchanged
 * (same object) when there is nothing to strip; otherwise a copy, so other
 * targets of the same request keep the items.
 */
export function stripIssuedThinking<T>(body: T): T {
  const input = (body as any)?.input;
  if (!Array.isArray(input)) return body;
  const out = input.filter(
    (item: any) =>
      !(item?.type === 'reasoning' && isIssuedThinking(item.encrypted_content))
  );
  return out.length === input.length
    ? body
    : ({ ...(body as any), input: out } as T);
}
