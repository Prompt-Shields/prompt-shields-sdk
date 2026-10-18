/**
 * PS fork (#73): OpenAI Responses response → Anthropic Messages response.
 *
 * Output items map in order: `reasoning` summaries to `thinking` blocks
 * (signed with the encrypted reasoning when the request asked for it, see
 * reasoningSignature.ts; a reasoning item with no summary but encrypted
 * content becomes `redacted_thinking`), `message` text and refusals to
 * `text` blocks, `function_call` to `tool_use`, and a `web_search_call` to
 * a `server_tool_use` + `web_search_tool_result` pair. Usage keeps cached
 * tokens apart from `input_tokens`, as the Messages API reports them.
 */

import { randomUUID } from 'crypto';
import { logger } from '../../apm';
import {
  ANTHROPIC_STOP_REASON,
  ContentBlock,
  MessagesResponse,
} from '../../types/messagesResponse';
import { NormalizedUsage, toMessagesError, toMessagesUsage } from '../shared';
import { encodeReasoningSignature } from './reasoningSignature';

const pos = (v: unknown) =>
  typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : 0;

/** Responses usage: input_tokens includes cached tokens. */
export function normalizeResponsesUsage(u: any): NormalizedUsage {
  const promptTokens = pos(u?.input_tokens);
  const completionTokens = pos(u?.output_tokens);
  return {
    promptTokens,
    completionTokens,
    cacheReadTokens: pos(u?.input_tokens_details?.cached_tokens),
    cacheCreationTokens: 0,
    reasoningTokens: pos(u?.output_tokens_details?.reasoning_tokens),
    totalTokens: pos(u?.total_tokens) || promptTokens + completionTokens,
  };
}

/** Stop reason from a final Responses object. */
export function responsesStopReason(
  response: any,
  hasToolUse: boolean
): ANTHROPIC_STOP_REASON {
  if (response?.status === 'incomplete') {
    const reason = response?.incomplete_details?.reason;
    if (reason === 'max_output_tokens') return ANTHROPIC_STOP_REASON.max_tokens;
    if (reason === 'content_filter') return ANTHROPIC_STOP_REASON.refusal;
  }
  return hasToolUse
    ? ANTHROPIC_STOP_REASON.tool_use
    : ANTHROPIC_STOP_REASON.end_turn;
}

export function parseToolArguments(
  args: unknown,
  name: string,
  provider: string
): Record<string, unknown> {
  if (args && typeof args === 'object') return args as Record<string, unknown>;
  try {
    const parsed = JSON.parse((args as string) || '{}');
    return parsed !== null && typeof parsed === 'object' ? parsed : {};
  } catch (err) {
    logger.warn({
      message: 'Failed to parse tool arguments as JSON',
      provider,
      toolName: name,
      error: err instanceof Error ? err.message : String(err),
    });
    return {};
  }
}

export function reasoningSummaryText(item: any): string {
  return (item?.summary || [])
    .map((s: any) => (typeof s?.text === 'string' ? s.text : ''))
    .filter(Boolean)
    .join('\n\n');
}

/**
 * The thinking block for a reasoning item, or undefined when it carries
 * neither summary text nor encrypted reasoning.
 */
export function reasoningBlock(
  item: any,
  provider: string
): Record<string, any> | undefined {
  const thinking = reasoningSummaryText(item);
  const encrypted =
    typeof item?.encrypted_content === 'string' && item.encrypted_content
      ? encodeReasoningSignature(provider, item.encrypted_content)
      : '';
  if (thinking) return { type: 'thinking', thinking, signature: encrypted };
  if (encrypted) return { type: 'redacted_thinking', data: encrypted };
  return undefined;
}

/**
 * A `web_search_call` as Anthropic web search blocks. Only `search` actions
 * map (open_page / find have no Messages equivalent). Results list the
 * sources the provider returned; the Responses API gives no titles or
 * encrypted page content, so the URL stands in for the title.
 */
export function webSearchBlocks(item: any): Record<string, any>[] {
  const action = item?.action;
  if (action && action.type && action.type !== 'search') return [];
  const id = item?.id || `srvtoolu_${randomUUID()}`;
  const query = typeof action?.query === 'string' ? action.query : '';
  const sources: any[] = Array.isArray(action?.sources) ? action.sources : [];
  return [
    {
      type: 'server_tool_use',
      id,
      name: 'web_search',
      input: { query },
    },
    {
      type: 'web_search_tool_result',
      tool_use_id: id,
      content: sources
        .filter((s: any) => typeof s?.url === 'string' && s.url)
        .map((s: any) => ({
          type: 'web_search_result',
          url: s.url,
          title: s.title || s.url,
          encrypted_content: '',
          page_age: null,
        })),
    },
  ];
}

/** Messages usage, with the web search count when searches ran. */
export function messagesUsage(usage: any, webSearches: number) {
  const out: Record<string, any> = toMessagesUsage(
    normalizeResponsesUsage(usage)
  );
  if (webSearches > 0) {
    out.server_tool_use = { web_search_requests: webSearches };
  }
  return out;
}

export function transformResponsesToMessages(
  response: any,
  responseStatus: number,
  provider: string
): MessagesResponse | Record<string, any> {
  if (responseStatus !== 200 || response?.error) {
    return toMessagesError(response, responseStatus || 500, provider);
  }

  const content: ContentBlock[] = [];
  let hasToolUse = false;
  let webSearches = 0;
  for (const item of response?.output || []) {
    switch (item?.type) {
      case 'reasoning': {
        const block = reasoningBlock(item, provider);
        if (block) content.push(block as any);
        break;
      }
      case 'web_search_call': {
        const blocks = webSearchBlocks(item);
        if (blocks.length) webSearches++;
        content.push(...(blocks as any[]));
        break;
      }
      case 'message':
        for (const part of item.content || []) {
          const text =
            part?.type === 'output_text'
              ? part.text
              : part?.type === 'refusal'
                ? part.refusal
                : undefined;
          if (typeof text === 'string' && text) {
            content.push({ type: 'text', text } as any);
          }
        }
        break;
      case 'function_call':
        hasToolUse = true;
        content.push({
          type: 'tool_use',
          id: item.call_id || item.id || `toolu_${randomUUID()}`,
          name: item.name,
          input: parseToolArguments(item.arguments, item.name, provider),
        } as any);
        break;
    }
  }

  return {
    id: response?.id || `msg_${randomUUID()}`,
    type: 'message',
    role: 'assistant',
    content,
    model: response?.model,
    stop_reason: responsesStopReason(response, hasToolUse),
    stop_sequence: null,
    usage: messagesUsage(response?.usage, webSearches),
  } as MessagesResponse;
}
