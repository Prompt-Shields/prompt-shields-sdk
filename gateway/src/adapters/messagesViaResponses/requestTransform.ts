/**
 * PS fork (#73): Anthropic Messages request → OpenAI Responses request.
 *
 * - `system` becomes `instructions`; messages become input items in order:
 *   user/assistant text as `message` items, `tool_use` as `function_call`,
 *   `tool_result` as `function_call_output`.
 * - Thinking blocks are dropped: the Responses API cannot verify Anthropic
 *   signatures, and summaries returned by this adapter carry no encrypted
 *   reasoning to send back.
 * - `temperature`, `top_p` and `stop_sequences` are dropped: the models this
 *   route serves are reasoning models, which reject sampling parameters,
 *   and the Responses API has no stop sequences.
 * - `store: false`, so the provider keeps no copy of the conversation (the
 *   Messages client resends the full history each turn anyway).
 */

import { Params } from '../../types/requestBody';

const EFFORTS = new Set(['minimal', 'low', 'medium', 'high', 'xhigh']);
const MIN_OUTPUT_TOKENS = 16;

export function transformMessagesToResponses(req: any): Params {
  const result: Record<string, any> = {
    model: req.model,
    input: transformMessages(req.messages || []),
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

  const tools = (req.tools || [])
    .filter((t: any) => t && (!t.type || t.type === 'custom') && t.name)
    .map((t: any) => ({
      type: 'function',
      name: t.name,
      description: t.description || '',
      parameters: t.input_schema || { type: 'object', properties: {} },
      strict: false,
    }));
  if (tools.length) result.tools = tools;

  const choice = req.tool_choice;
  if (choice && tools.length) {
    if (choice.type === 'auto') result.tool_choice = 'auto';
    else if (choice.type === 'any') result.tool_choice = 'required';
    else if (choice.type === 'none') result.tool_choice = 'none';
    else if (choice.type === 'tool' && choice.name) {
      result.tool_choice = { type: 'function', name: choice.name };
    }
    if (choice.disable_parallel_tool_use === true) {
      result.parallel_tool_calls = false;
    }
  }

  // Extended thinking asks for reasoning summaries, streamed back as
  // thinking blocks. Effort comes from output_config.effort when set.
  const reasoning: Record<string, string> = {};
  const effort = req.output_config?.effort;
  if (typeof effort === 'string' && EFFORTS.has(effort)) {
    reasoning.effort = effort;
  } else if (effort === 'max') {
    reasoning.effort = 'high';
  }
  if (req.thinking?.type === 'enabled' || req.thinking?.type === 'adaptive') {
    reasoning.summary = 'auto';
  }
  if (Object.keys(reasoning).length) result.reasoning = reasoning;

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

function transformMessages(messages: any[]): any[] {
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
          output: toolResultText(block),
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
          const s = block.source;
          if (role === 'user' && s?.type === 'base64' && s.data) {
            parts.push({
              type: 'input_file',
              filename: block.title || 'document.pdf',
              file_data: `data:${s.media_type || 'application/pdf'};base64,${s.data}`,
            });
          } else if (role === 'user' && s?.type === 'text' && s.data) {
            parts.push(textPart(role, s.data));
          }
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
        // tool_result handled above; thinking / redacted_thinking dropped.
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

/** tool_result content as text (same rules as the chat adapter). */
function toolResultText(block: any): string {
  const c = block.content;
  let text: string;
  if (c == null) text = '';
  else if (typeof c === 'string') text = c;
  else if (Array.isArray(c) && c.every((b: any) => b?.type === 'text')) {
    text = c.map((b: any) => b.text || '').join('\n');
  } else text = JSON.stringify(c);
  return block.is_error ? `Error: ${text}` : text;
}
