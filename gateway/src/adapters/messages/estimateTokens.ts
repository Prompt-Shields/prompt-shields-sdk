/**
 * PS fork (#73 follow-up): local input-token estimate for an Anthropic
 * Messages request, used by `POST /v1/messages/count_tokens` when the target
 * provider has no token-count API the gateway can call.
 *
 * The gateway ships no tokenizer (upstream's `/v1/gateway/tokenize` is not
 * ported, see FORK_NOTICE), so this is a character heuristic in the same
 * spirit as the router's `estimateInputTokens`, extended to everything a
 * Messages request sends:
 *
 * - text (system, text and thinking blocks, tool results, tool definitions
 *   and tool_use inputs as JSON): 1 token per 4 characters, rounded up;
 * - each image or non-text document: a flat 1,600 tokens (Anthropic's
 *   figure for an image at its ~1.15 megapixel size cap);
 * - 3 tokens of framing per message and per tool definition.
 *
 * It is deterministic, but chars/4 is tuned for English prose: it can be
 * well off for code, non-Latin scripts and images of other sizes, and it is
 * not a substitute for the provider's own count. Responses built from it
 * carry `x-ps-token-count: estimated`.
 */

export const CHARS_PER_TOKEN = 4;
export const MEDIA_BLOCK_TOKENS = 1600;
export const FRAMING_TOKENS = 3;

function json(value: unknown): string {
  try {
    return JSON.stringify(value) ?? '';
  } catch {
    return '';
  }
}

interface Tally {
  chars: number;
  tokens: number;
}

function addContent(content: unknown, t: Tally): void {
  if (typeof content === 'string') {
    t.chars += content.length;
    return;
  }
  if (!Array.isArray(content)) return;
  for (const block of content) addBlock(block, t);
}

function addBlock(block: any, t: Tally): void {
  if (!block || typeof block !== 'object') return;
  switch (block.type) {
    case 'text':
      if (typeof block.text === 'string') t.chars += block.text.length;
      return;
    case 'thinking':
      if (typeof block.thinking === 'string') t.chars += block.thinking.length;
      return;
    case 'redacted_thinking':
      return;
    case 'image':
      t.tokens += MEDIA_BLOCK_TOKENS;
      return;
    case 'document': {
      const source = block.source;
      if (source?.type === 'text' && typeof source.data === 'string') {
        t.chars += source.data.length;
      } else if (source?.type === 'content') {
        addContent(source.content, t);
      } else {
        t.tokens += MEDIA_BLOCK_TOKENS;
      }
      return;
    }
    case 'tool_use':
    case 'server_tool_use':
      if (typeof block.name === 'string') t.chars += block.name.length;
      t.chars += json(block.input ?? {}).length;
      return;
    case 'tool_result':
    case 'web_search_tool_result':
      addContent(block.content, t);
      return;
    default:
      // Unknown block types: count their JSON so nothing is free.
      t.chars += json(block).length;
  }
}

/** Estimated input tokens of a Messages request body. */
export function estimateMessagesInputTokens(body: any): number {
  const t: Tally = { chars: 0, tokens: 0 };
  if (!body || typeof body !== 'object') return 0;
  addContent(body.system, t);
  const messages = Array.isArray(body.messages) ? body.messages : [];
  for (const m of messages) {
    t.tokens += FRAMING_TOKENS;
    addContent(m?.content, t);
  }
  const tools = Array.isArray(body.tools) ? body.tools : [];
  for (const tool of tools) {
    if (!tool || typeof tool !== 'object') continue;
    t.tokens += FRAMING_TOKENS;
    const { name, description, input_schema, ...rest } = tool;
    if (typeof name === 'string') t.chars += name.length;
    if (typeof description === 'string') t.chars += description.length;
    if (input_schema !== undefined) t.chars += json(input_schema).length;
    // Server and Anthropic-defined tools: their config (type, limits).
    if (Object.keys(rest).length) t.chars += json(rest).length;
  }
  return t.tokens + Math.ceil(t.chars / CHARS_PER_TOKEN);
}
