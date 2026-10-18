/**
 * PS fork (#73): Anthropic Messages request → OpenAI Responses request.
 *
 * - `system` becomes `instructions`; messages become input items in order:
 *   user/assistant text as `message` items, `tool_use` as `function_call`,
 *   `tool_result` as `function_call_output` (text, or input parts when the
 *   result holds images or documents).
 * - Thinking blocks go back as `reasoning` items only when their signature
 *   carries encrypted reasoning this adapter issued for the same provider
 *   (see reasoningSignature.ts); Anthropic-signed thinking is dropped, as the
 *   Responses API cannot verify it.
 * - The `web_search_*` server tool becomes the built-in `web_search` tool;
 *   other server tools and Anthropic-defined client tools are dropped, and a
 *   `tool_choice` naming a dropped tool is rejected (400).
 * - `temperature`, `top_p` and `stop_sequences` are dropped: the models this
 *   route serves are reasoning models, which reject sampling parameters,
 *   and the Responses API has no stop sequences.
 * - `store: false`, so the provider keeps no copy of the conversation (the
 *   Messages client resends the full history each turn anyway).
 */

import { Params } from '../../types/requestBody';
import { decodeReasoningSignature } from './reasoningSignature';

const EFFORTS = new Set(['minimal', 'low', 'medium', 'high', 'xhigh']);
const MIN_OUTPUT_TOKENS = 16;
const WEB_SEARCH_TOOL = /^web_search_\d{8}$/;

/** A request this route cannot serve faithfully: answered with a 400. */
export class MessagesViaResponsesRequestError extends Error {}

export function transformMessagesToResponses(
  req: any,
  provider: string = ''
): Params {
  const result: Record<string, any> = {
    model: req.model,
    input: transformMessages(req.messages || [], provider),
    store: false,
  };

  const instructions = systemText(req.system);
  if (instructions) result.instructions = instructions;
  // The Responses API rejects max_output_tokens below 16 (Messages allows
  // 1, e.g. Claude Code's probes), so small limits are raised to 16.
  if (typeof req.max_tokens === 'number') {
    result.max_output_tokens = Math.max(MIN_OUTPUT_TOKENS, req.max_tokens);
  }
  if (req.stream != null) result.stream = req.stream;

  const functionNames = new Set<string>();
  let webSearchName: string | undefined;
  const tools: any[] = [];
  for (const t of req.tools || []) {
    if (!t) continue;
    if ((!t.type || t.type === 'custom') && t.name) {
      functionNames.add(t.name);
      tools.push({
        type: 'function',
        name: t.name,
        description: t.description || '',
        parameters: t.input_schema || { type: 'object', properties: {} },
        strict: false,
      });
    } else if (typeof t.type === 'string' && WEB_SEARCH_TOOL.test(t.type)) {
      webSearchName = t.name || 'web_search';
      tools.push(webSearchTool(t));
    }
    // Other server tools (code execution, web fetch, ...) and
    // Anthropic-defined client tools (bash, text editor, computer) have no
    // Responses equivalent here and are dropped.
  }
  if (tools.length) result.tools = tools;

  const choice = req.tool_choice;
  if (choice?.type === 'tool') {
    if (choice.name && functionNames.has(choice.name)) {
      result.tool_choice = { type: 'function', name: choice.name };
    } else if (choice.name && choice.name === webSearchName) {
      result.tool_choice = {
        type: 'allowed_tools',
        mode: 'required',
        tools: [{ type: 'web_search' }],
      };
    } else {
      throw new MessagesViaResponsesRequestError(
        `tool_choice names tool '${choice.name ?? ''}', which is not a tool ` +
          'this model can call through the Responses API (only custom tools ' +
          'and web_search are supported)'
      );
    }
  } else if (choice && tools.length) {
    if (choice.type === 'auto') result.tool_choice = 'auto';
    else if (choice.type === 'any') result.tool_choice = 'required';
    else if (choice.type === 'none') result.tool_choice = 'none';
  }
  if (choice?.disable_parallel_tool_use === true && tools.length) {
    result.parallel_tool_calls = false;
  }

  // Extended thinking asks for reasoning summaries, streamed back as
  // thinking blocks, and for the encrypted reasoning, carried in their
  // signatures so the next turn can send it back. Effort comes from
  // output_config.effort when set.
  const include: string[] = [];
  const reasoning: Record<string, string> = {};
  const effort = req.output_config?.effort;
  if (typeof effort === 'string' && EFFORTS.has(effort)) {
    reasoning.effort = effort;
  } else if (effort === 'max') {
    reasoning.effort = 'high';
  }
  if (req.thinking?.type === 'enabled' || req.thinking?.type === 'adaptive') {
    reasoning.summary = 'auto';
    include.push('reasoning.encrypted_content');
  }
  if (Object.keys(reasoning).length) result.reasoning = reasoning;
  // Search sources fill the web_search_tool_result blocks.
  if (webSearchName) include.push('web_search_call.action.sources');
  if (include.length) result.include = include;

  const format = req.output_config?.format;
  if (format?.type === 'json_schema' && format.schema) {
    result.text = {
      format: {
        type: 'json_schema',
        name: format.name || 'response',
        schema: format.schema,
        strict: format.strict ?? false,
      },
    };
  }

  return result as Params;
}

/**
 * Anthropic `web_search_*` → Responses `web_search`. `allowed_domains` and
 * `user_location` map directly. `max_uses` has no equivalent and is dropped
 * (the model decides how often to search). `blocked_domains` cannot be
 * honoured, and silently searching them would break the caller's
 * restriction, so it is rejected.
 */
function webSearchTool(t: any): Record<string, any> {
  if (Array.isArray(t.blocked_domains) && t.blocked_domains.length) {
    throw new MessagesViaResponsesRequestError(
      `${t.name || 'web_search'}: blocked_domains is not supported by the ` +
        'Responses API web_search tool'
    );
  }
  const out: Record<string, any> = { type: 'web_search' };
  if (Array.isArray(t.allowed_domains) && t.allowed_domains.length) {
    out.filters = { allowed_domains: t.allowed_domains };
  }
  const loc = t.user_location;
  if (loc && typeof loc === 'object') {
    const location: Record<string, string> = { type: 'approximate' };
    for (const key of ['city', 'region', 'country', 'timezone']) {
      if (typeof loc[key] === 'string' && loc[key]) location[key] = loc[key];
    }
    out.user_location = location;
  }
  return out;
}

function systemText(system: any): string {
  if (!system) return '';
  if (typeof system === 'string') return system;
  if (Array.isArray(system)) {
    return system
      .map((b: any) => (typeof b?.text === 'string' ? b.text : ''))
      .filter(Boolean)
      .join('\n');
  }
  return '';
}

function transformMessages(messages: any[], provider: string): any[] {
  const items: any[] = [];
  for (const msg of messages) {
    const role = msg?.role === 'assistant' ? 'assistant' : 'user';
    if (typeof msg?.content === 'string') {
      items.push(messageItem(role, [textPart(role, msg.content)]));
      continue;
    }
    if (!Array.isArray(msg?.content)) continue;

    // Consecutive text/image blocks share one message item; tool calls and
    // tool results are their own items, so block order is kept.
    let parts: any[] = [];
    const flush = () => {
      if (parts.length) items.push(messageItem(role, parts));
      parts = [];
    };
    // Tool results answer the previous assistant turn, so they go first.
    for (const block of msg.content) {
      if (block?.type === 'tool_result') {
        items.push({
          type: 'function_call_output',
          call_id: block.tool_use_id || '',
          output: toolResultOutput(block),
        });
      }
    }
    for (const block of msg.content) {
      switch (block?.type) {
        case 'text':
          if (block.text) parts.push(textPart(role, block.text));
          break;
        case 'image': {
          const url = imageUrl(block.source);
          if (url && role === 'user') {
            parts.push({ type: 'input_image', image_url: url, detail: 'auto' });
          }
          break;
        }
        case 'document': {
          const part = role === 'user' ? documentPart(block) : undefined;
          if (part) parts.push(part);
          break;
        }
        case 'thinking':
        case 'redacted_thinking': {
          if (role !== 'assistant') break;
          const encrypted = decodeReasoningSignature(
            block.type === 'thinking' ? block.signature : block.data,
            provider
          );
          if (!encrypted) break;
          flush();
          items.push({
            type: 'reasoning',
            summary:
              block.type === 'thinking' && block.thinking
                ? [{ type: 'summary_text', text: block.thinking }]
                : [],
            encrypted_content: encrypted,
          });
          break;
        }
        case 'tool_use':
          flush();
          items.push({
            type: 'function_call',
            call_id: block.id || '',
            name: block.name || '',
            arguments: JSON.stringify(block.input ?? {}),
          });
          break;
        // tool_result handled above. server_tool_use and
        // web_search_tool_result are dropped: with store:false the provider
        // cannot resolve past searches, and their findings are already in
        // the assistant text that follows them.
      }
    }
    flush();
  }
  return items;
}

function messageItem(role: string, content: any[]) {
  return { type: 'message', role, content };
}

function textPart(role: string, text: string) {
  return { type: role === 'assistant' ? 'output_text' : 'input_text', text };
}

function documentPart(block: any): any {
  const s = block?.source;
  if (s?.type === 'base64' && s.data) {
    return {
      type: 'input_file',
      filename: block.title || 'document.pdf',
      file_data: `data:${s.media_type || 'application/pdf'};base64,${s.data}`,
    };
  }
  if (s?.type === 'text' && s.data) return textPart('user', s.data);
  return undefined;
}

function imageUrl(source: any): string | undefined {
  if (!source) return undefined;
  if (source.type === 'url' && source.url) return source.url;
  if (source.type === 'base64' && source.data) {
    const mediaType = source.media_type || sniffImageType(source.data);
    return `data:${mediaType};base64,${source.data}`;
  }
  return undefined;
}

/**
 * Media type from the base64 magic bytes, for a source without
 * `media_type`; PNG when unrecognised (a data URL needs a type).
 */
function sniffImageType(data: string): string {
  if (data.startsWith('/9j/')) return 'image/jpeg';
  if (data.startsWith('R0lGOD')) return 'image/gif';
  if (data.startsWith('UklGR')) return 'image/webp';
  return 'image/png';
}

/**
 * tool_result content for `function_call_output`. Text-only results are a
 * string (same rules as the chat adapter); results with images or
 * documents are a list of input parts, which the Responses API accepts as
 * function output, so the model sees e.g. a screenshot tool's image.
 */
function toolResultOutput(block: any): string | any[] {
  const c = block.content;
  if (
    Array.isArray(c) &&
    c.some((b: any) => b?.type === 'image' || b?.type === 'document')
  ) {
    const parts: any[] = block.is_error
      ? [{ type: 'input_text', text: 'Error:' }]
      : [];
    for (const b of c) {
      if (b?.type === 'text') {
        if (b.text) parts.push({ type: 'input_text', text: b.text });
      } else if (b?.type === 'image') {
        const url = imageUrl(b.source);
        if (url) {
          parts.push({ type: 'input_image', image_url: url, detail: 'auto' });
        }
      } else if (b?.type === 'document') {
        const part = documentPart(b);
        if (part) parts.push(part);
      } else if (b) {
        parts.push({ type: 'input_text', text: JSON.stringify(b) });
      }
    }
    return parts;
  }
  let text: string;
  if (c == null) text = '';
  else if (typeof c === 'string') text = c;
  else if (Array.isArray(c) && c.every((b: any) => b?.type === 'text')) {
    text = c.map((b: any) => b.text || '').join('\n');
  } else text = JSON.stringify(c);
  return block.is_error ? `Error: ${text}` : text;
}
