/**
 * Responses API → Chat Completions Request Transform
 *
 * Converts OpenAI Responses API requests to Chat Completions format.
 * Optimized for minimal allocations and fast execution.
 */

import { Params, Message, ContentType } from '../../types/requestBody';
import { CarriedThinkingBlock, decodeThinkingCarry } from './thinkingCarry';
import {
  applyMaxToolCalls,
  transformResponsesInclude,
  transformResponsesTools,
} from './requestFields';

// Role mapping (inline for performance)
const ROLE_MAP: Record<string, 'system' | 'user' | 'assistant' | 'tool'> = {
  developer: 'system',
  system: 'system',
  user: 'user',
  assistant: 'assistant',
  tool: 'tool',
};

/**
 * Transform Responses API request to Chat Completions format. PS fork (#73):
 * `provider` is the target; reasoning items this adapter issued for it are
 * sent back as signed thinking (see thinkingCarry.ts).
 */
export function transformResponsesToChatCompletions(
  req: any,
  provider?: string
): Params {
  const messages: Message[] = [];

  // Add system message from instructions
  if (req.instructions) {
    messages.push({ role: 'system', content: req.instructions });
  }

  // Transform input to messages
  if (typeof req.input === 'string') {
    messages.push({ role: 'user', content: req.input });
  } else if (Array.isArray(req.input)) {
    // PS fork (#73): signed thinking waiting for its assistant message
    let thinking: CarriedThinkingBlock[] = [];
    for (const item of req.input) {
      if (item?.type === 'reasoning') {
        const blocks = decodeThinkingCarry(item.encrypted_content, provider);
        if (blocks) thinking.push(...blocks);
        continue;
      }
      const msg = transformInputItem(item);
      if (!msg) continue;
      if (msg.role !== 'assistant') {
        // Reasoning not followed by an assistant item is dropped
        thinking = [];
      } else if (thinking.length) {
        const last = messages[messages.length - 1];
        const merges =
          item.type === 'function_call' &&
          msg.tool_calls &&
          last?.role === 'assistant';
        // In content order: thinking before this item's content, or after
        // the content of the message this function call joins
        if (merges) {
          last.content_blocks = [
            ...assistantContentBlocks(last),
            ...thinking,
          ] as ContentType[];
        } else {
          msg.content_blocks = [
            ...thinking,
            ...assistantContentBlocks(msg),
          ] as ContentType[];
        }
        thinking = [];
      }
      // PS fork (#73): a turn's function calls (parallel calls, or calls
      // after the assistant's text) are one assistant message, so each call
      // is directly followed by its tool result as Chat Completions,
      // Anthropic and Gemini require.
      const last = messages[messages.length - 1];
      if (
        item.type === 'function_call' &&
        msg.tool_calls &&
        last?.role === 'assistant'
      ) {
        last.tool_calls = [...(last.tool_calls ?? []), ...msg.tool_calls];
        continue;
      }
      messages.push(msg);
    }
  }

  // Build result with only defined values (avoid undefined properties)
  const result: Params = { model: req.model, messages };

  // Direct property mapping (only set if defined and not null)
  if (req.temperature != null) result.temperature = req.temperature;
  if (req.top_p != null) result.top_p = req.top_p;
  // Only set max_tokens if explicitly provided - provider configs handle defaults (e.g., Anthropic: 64000)
  if (req.max_output_tokens != null)
    result.max_completion_tokens = req.max_output_tokens;
  if (req.stream != null) result.stream = req.stream;
  // PS fork (#73): safety_identifier replaces the deprecated `user`; both
  // reach the provider as Chat Completions `user` (Anthropic metadata.user_id).
  // text.verbosity, prompt_cache_key and service_tier are advisory and not
  // sent (see requestFields.ts).
  const user = req.safety_identifier || req.user;
  if (typeof user === 'string' && user) result.user = user;
  if (req.parallel_tool_calls != null)
    result.parallel_tool_calls = req.parallel_tool_calls;
  if (req.metadata) result.metadata = req.metadata;

  // PS fork (#73): tools and tool_choice, mapped for the target provider
  // or refused (requestFields.ts)
  const { tools, tool_choice } = transformResponsesTools(req, provider);
  // PS fork (#73): max_tool_calls and include (requestFields.ts)
  applyMaxToolCalls(req, tools, provider);
  const { logprobs } = transformResponsesInclude(req, provider);
  // (Params types logprobs as a number; Chat Completions takes a boolean)
  if (logprobs) (result as any).logprobs = logprobs;
  if (tools) result.tools = tools;
  if (tool_choice !== undefined) result.tool_choice = tool_choice;

  // Transform response format (text.format)
  if (req.text?.format) {
    const fmt = req.text.format;
    if (fmt.type === 'json_schema') {
      result.response_format = {
        type: 'json_schema',
        json_schema: {
          name: fmt.name || 'response',
          schema: fmt.schema,
          strict: fmt.strict,
        },
      };
    } else if (fmt.type === 'json_object') {
      result.response_format = { type: 'json_object' };
    }
  }

  // Reasoning effort - maps to reasoning_effort which providers handle:
  // - OpenAI/Azure: passthrough as reasoning_effort (o-series models)
  // - Anthropic: mapped to output_config.effort (Opus 4.5+)
  // - Google/Vertex: mapped to thinking_config/thinkingConfig
  if (req.reasoning?.effort) {
    result.reasoning_effort = req.reasoning.effort;
  }

  // Pass through provider-specific thinking parameter (Anthropic extended thinking)
  // This allows users to enable extended thinking on Claude models
  if (req.thinking) {
    (result as any).thinking = req.thinking;
  }

  // Logprobs settings
  if (req.top_logprobs != null) result.top_logprobs = req.top_logprobs;

  return result;
}

/**
 * Transform a single input item to a Message
 */
function transformInputItem(item: any): Message | null {
  // Simple message format { role, content }
  if (
    item.role &&
    item.content !== undefined &&
    item.type !== 'function_call'
  ) {
    return {
      role: ROLE_MAP[item.role] || 'user',
      content: transformContent(item.content),
    };
  }

  // Typed items
  switch (item.type) {
    case 'message':
      if (item.role && item.content !== undefined) {
        return {
          role: ROLE_MAP[item.role] || 'user',
          content: transformContent(item.content),
        };
      }
      break;

    case 'function_call':
      if (item.name && item.arguments !== undefined) {
        return {
          role: 'assistant',
          content: '',
          tool_calls: [
            {
              id: item.call_id || item.id,
              type: 'function',
              function: { name: item.name, arguments: item.arguments },
            },
          ],
        };
      }
      break;

    case 'function_call_output':
      if (item.call_id && item.output !== undefined) {
        return {
          role: 'tool',
          tool_call_id: item.call_id,
          content: functionCallOutputText(item.output),
        };
      }
      break;
  }

  return null;
}

/**
 * PS fork (#73): an assistant message's content as content_blocks, to
 * combine with carried thinking. Existing blocks (thinking carried by an
 * earlier reasoning item of the same turn) are kept as they are.
 */
function assistantContentBlocks(msg: Message): ContentType[] {
  if (msg.content_blocks?.length) return msg.content_blocks;
  const c = msg.content;
  if (typeof c === 'string') return c ? [{ type: 'text', text: c }] : [];
  return Array.isArray(c) ? c : [];
}

/**
 * PS fork (#73): `function_call_output.output` may be a string or a list of
 * `input_text` / `input_image` / `input_file` parts. Chat Completions tool
 * messages carry text, so text parts are joined; other parts fall back to
 * JSON rather than reaching the provider as unknown part types.
 */
function functionCallOutputText(output: any): string {
  if (typeof output === 'string') return output;
  if (
    Array.isArray(output) &&
    output.every((p: any) => p?.type === 'input_text' || p?.type === 'text')
  ) {
    return output.map((p: any) => p.text || '').join('\n');
  }
  return JSON.stringify(output);
}

/**
 * Transform message content (string or array of parts)
 */
function transformContent(content: any): string | ContentType[] {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';

  // Check if we can return a simple string
  if (content.length === 1) {
    const p = content[0];
    if (p.type === 'input_text' || p.type === 'output_text')
      return p.text || '';
    if (p.type === 'text') return p.text || '';
  }

  // Build content array
  const parts: ContentType[] = [];
  for (const p of content) {
    if (
      p.type === 'input_text' ||
      p.type === 'output_text' ||
      p.type === 'text'
    ) {
      const part: ContentType = { type: 'text', text: p.text || '' };
      // Preserve cache_control for prompt caching (Anthropic, etc.)
      if (p.cache_control) part.cache_control = p.cache_control;
      parts.push(part);
    } else if (p.type === 'input_image' && p.image_url) {
      const part: ContentType = {
        type: 'image_url',
        image_url: { url: p.image_url, detail: p.detail },
      };
      if (p.cache_control) part.cache_control = p.cache_control;
      parts.push(part);
    } else if (p.type === 'input_file') {
      // Transform input_file to Chat Completions file format
      // file_data should be a data URL (e.g., "data:application/pdf;base64,...")
      const part: any = {
        type: 'file',
        file: {
          filename: p.filename,
          file_data: p.file_data,
          file_id: p.file_id,
        },
      };
      if (p.cache_control) part.cache_control = p.cache_control;
      parts.push(part);
    }
  }

  return parts.length ? parts : '';
}

export default transformResponsesToChatCompletions;
