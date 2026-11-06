/**
 * Chat Completions → Responses API Response Transform
 *
 * Converts Chat Completions responses to Responses API format.
 *
 * Note: The Responses API natively supports `reasoning` output items as part
 * of its standard spec, so we always include them if thinking data is present.
 * This is different from Chat Completions where thinking requires
 * strictOpenAiCompliance=false since it's a non-standard extension.
 */

import {
  OpenAIResponse,
  ResponseOutputItem,
  ResponseOutputMessage,
  ResponseOutputText,
  ResponseFunctionToolCall,
  ResponseOutputItemReasoning,
} from '../../types/modelResponses';
import {
  ChatCompletionResponse,
  ErrorResponse,
  FINISH_REASON,
} from '../../providers/types';
import {
  normalizeFinishReason,
  normalizeUsage,
  plainReasoningText,
  toResponsesUsage,
} from '../shared';
import { randomUUID } from 'crypto';
import { encodeThinkingCarry } from './thinkingCarry';
import {
  UrlCitation,
  anthropicWebSearchSegments,
  googleGroundingOutput,
  hasAnthropicWebSearch,
  wantsWebSearchSources,
} from './webSearchOutput';
import { outputTextLogprobs } from './requestFields';

type ResponseStatus = 'completed' | 'incomplete' | 'in_progress';

/**
 * PS fork (#73): map a (possibly provider-native) finish reason to a
 * Responses status and incomplete reason. Truncation and content filtering
 * both make the response `incomplete`, as in the Responses API.
 */
export function toResponsesStatus(finishReason: string | null | undefined): {
  status: ResponseStatus;
  reason: 'max_output_tokens' | 'content_filter' | null;
} {
  switch (normalizeFinishReason(finishReason)) {
    case FINISH_REASON.length:
      return { status: 'incomplete', reason: 'max_output_tokens' };
    case FINISH_REASON.content_filter:
      return { status: 'incomplete', reason: 'content_filter' };
    default:
      return { status: 'completed', reason: null };
  }
}

/**
 * Default values for Responses API fields
 * These are used when the original request doesn't specify a value
 */
const RESPONSE_DEFAULTS = {
  truncation: 'disabled' as const,
  parallel_tool_calls: true,
  tool_choice: 'auto' as const,
  temperature: 1,
  top_p: 1,
  presence_penalty: 0,
  frequency_penalty: 0,
  top_logprobs: 0,
  store: false,
  background: false,
  service_tier: 'default',
};

/**
 * Transform Chat Completions response to Responses API format
 *
 * Handles:
 * - Standard text content → message output items
 * - Tool calls → function_call output items
 * - Thinking/reasoning from content_blocks → reasoning output items (always, since it's native to Responses API)
 *
 * @param response - The Chat Completions response from the provider
 * @param responseStatus - HTTP status code
 * @param provider - Provider name
 * @param originalRequest - Original Responses API request (optional, for echoing back config values)
 */
export function transformChatCompletionsToResponses(
  response: ChatCompletionResponse | ErrorResponse,
  responseStatus: number,
  provider: string,
  originalRequest?: any
): OpenAIResponse | ErrorResponse {
  // PS fork (#73): a body that is not an object is a failed request
  const body = isRecord(response) ? response : null;
  // Pass through errors
  if (body && 'error' in body) return response as ErrorResponse;
  if (responseStatus !== 200 || !body) {
    return {
      error: {
        message: 'Request failed',
        type: 'api_error',
        param: null,
        code: null,
      },
      provider,
    } as ErrorResponse;
  }

  const reasoningItems: ResponseOutputItem[] = [];
  const messageItems: ResponseOutputItem[] = [];
  const functionCallItems: ResponseOutputItem[] = [];
  // PS fork (#73): Google grounding searches go last, where the streamed
  // response puts them (grounding arrives with the last chunk)
  const groundingCallItems: ResponseOutputItem[] = [];
  let overallStatus: ResponseStatus = 'completed';
  let incompleteReason: string | null = null;
  const includeSources = wantsWebSearchSources(originalRequest);

  // Process choices. PS fork (#73): the provider's arrays may hold null or
  // badly typed entries; those are skipped, never dereferenced.
  const choices: unknown[] = Array.isArray(body.choices) ? body.choices : [];
  for (const choice of choices) {
    if (!isRecord(choice)) continue;
    const msg: Record<string, any> = isRecord(choice.message)
      ? choice.message
      : {};
    const blocks: Record<string, any>[] = Array.isArray(msg.content_blocks)
      ? msg.content_blocks.filter(isRecord)
      : [];
    const { status, reason } = toResponsesStatus(choice.finish_reason);
    if (status === 'incomplete') {
      overallStatus = 'incomplete';
      incompleteReason = reason;
    }

    // Handle reasoning/thinking from content_blocks
    // Providers like Anthropic, Google, OpenRouter expose thinking via content_blocks
    // The Responses API natively supports reasoning, so we always include it
    for (const block of blocks) {
      // Handle thinking blocks (Anthropic, OpenRouter format)
      // PS fork (#73): signed and redacted thinking travel in
      // encrypted_content so the next turn can send them back
      const encrypted = encodeThinkingCarry(provider, [block]);
      if (stringOr(block.thinking) || block.type === 'thinking') {
        const thinkingText = stringOr(block.thinking) || stringOr(block.text);
        reasoningItems.push({
          id: `rs_${randomUUID()}`,
          type: 'reasoning',
          summary: [
            {
              type: 'summary_text',
              text: thinkingText,
            },
          ],
          ...(encrypted ? { encrypted_content: encrypted } : {}),
        } as ResponseOutputItemReasoning);
      } else if (block.type === 'redacted_thinking' && encrypted) {
        reasoningItems.push({
          id: `rs_${randomUUID()}`,
          type: 'reasoning',
          summary: [],
          encrypted_content: encrypted,
        } as ResponseOutputItemReasoning);
      }
    }

    // PS fork (#73): OpenAI-compatible reasoning fields (reasoning_content)
    if (!reasoningItems.length) {
      const reasoning = plainReasoningText(msg);
      if (reasoning) {
        reasoningItems.push({
          id: `rs_${randomUUID()}`,
          type: 'reasoning',
          summary: [{ type: 'summary_text', text: reasoning }],
        } as ResponseOutputItemReasoning);
      }
    }

    // Handle tool calls
    if (Array.isArray(msg.tool_calls)) {
      for (const tc of msg.tool_calls) {
        if (!isRecord(tc) || !isRecord(tc.function)) continue;
        if (tc.type === 'function' || !tc.type) {
          functionCallItems.push({
            id: `fc_${randomUUID()}`,
            type: 'function_call',
            call_id: stringOr(tc.id) || `call_${randomUUID()}`,
            name: stringOr(tc.function.name),
            arguments: toolArguments(tc.function.arguments),
            status: 'completed',
          } as ResponseFunctionToolCall);
        }
      }
    }

    // Handle text content
    const content = msg.content;
    const text =
      typeof content === 'string'
        ? content
        : Array.isArray(content)
          ? content
              .map((c: unknown) => (isRecord(c) ? stringOr(c.text) : ''))
              .join('')
          : typeof content === 'number' || typeof content === 'boolean'
            ? String(content)
            : '';

    // PS fork (#73): Anthropic web search blocks and citations become
    // web_search_call items and url_citation annotations, in block order
    // (webSearchOutput.ts). The text is the same, split at each search.
    const segments = hasAnthropicWebSearch(blocks)
      ? anthropicWebSearchSegments(blocks, includeSources)
      : null;
    const segmentText = segments
      ?.map((s) => (s.kind === 'text' ? s.text : ''))
      .join('');
    if (segments && segmentText === text) {
      for (const segment of segments) {
        if (segment.kind === 'web_search_call') {
          messageItems.push(segment.item as any);
        } else if (segment.text) {
          messageItems.push(
            outputMessage(segment.text, status, segment.annotations)
          );
        }
      }
      continue;
    }

    // PS fork (#73): Google Search grounding: annotations on the message,
    // then one web_search_call per search query
    const grounding = googleGroundingOutput(
      choice.groundingMetadata,
      text,
      includeSources
    );
    // Only add message if there's actual text content
    if (text) {
      messageItems.push(
        outputMessage(
          text,
          status,
          grounding.annotations,
          // PS fork (#73): when include lists them (requestFields.ts)
          outputTextLogprobs(choice, originalRequest)
        )
      );
    }
    groundingCallItems.push(...(grounding.calls as any[]));
  }

  // PS fork (#73): reasoning, then message, then function calls, then
  // Google grounding searches (the streamed order)
  const output: ResponseOutputItem[] = [
    ...reasoningItems,
    ...messageItems,
    ...functionCallItems,
    ...groundingCallItems,
  ];

  // PS fork (#73): cached tokens also read from Anthropic-style usage
  const usage = toResponsesUsage(normalizeUsage(body.usage));

  const createdAt =
    typeof body.created === 'number' &&
    Number.isFinite(body.created) &&
    body.created > 0
      ? body.created
      : Math.floor(Date.now() / 1000);

  return {
    id: stringOr(body.id) || `resp_${randomUUID()}`,
    object: 'response',
    created_at: createdAt,
    completed_at:
      overallStatus === 'completed' ? Math.floor(Date.now() / 1000) : null,
    status: overallStatus,
    incomplete_details:
      overallStatus === 'incomplete' ? { reason: incompleteReason } : null,
    // PS fork (#73): the requested model when the provider reports none
    model: stringOr(body.model) || responseModel(originalRequest),
    output,
    error: null,
    usage,
    // PS fork (#73): shared with the streamed snapshots
    ...echoRequestFields(originalRequest),
  } as OpenAIResponse;
}

/** PS fork (#73): a plain (non-null, non-array) object. */
function isRecord(value: unknown): value is Record<string, any> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** PS fork (#73): `value` if it is a string, else ''. */
function stringOr(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

/** PS fork (#73): function call arguments as JSON text. */
function toolArguments(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value !== null && typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return '';
    }
  }
  return '';
}

/** PS fork (#73): an assistant message output item with one output_text. */
function outputMessage(
  text: string,
  status: ResponseStatus,
  annotations: UrlCitation[] = [],
  logprobs: any[] = []
): ResponseOutputMessage {
  return {
    id: `msg_${randomUUID()}`,
    type: 'message',
    role: 'assistant',
    status,
    content: [
      {
        type: 'output_text',
        text,
        annotations,
        logprobs,
      } as ResponseOutputText,
    ],
  } as ResponseOutputMessage;
}

/** PS fork (#73): the model named in the request, or ''. */
export function responseModel(originalRequest?: any): string {
  return typeof originalRequest?.model === 'string'
    ? originalRequest.model
    : '';
}

/**
 * PS fork (#73): the Response fields that echo the request, with the
 * Responses API defaults for fields the request leaves out. Used by the
 * JSON response and by every streamed snapshot (`response.created`,
 * `response.in_progress` and the terminal event), so a client reads the
 * same request config back from either, as from the native API.
 */
const TOOL_CREDENTIAL_KEYS = new Set(['headers', 'authorization']);

function withoutToolCredentials(tool: any) {
  if (!tool || typeof tool !== 'object') return tool;
  // fromEntries defines own properties: a `__proto__` key stays a key
  return Object.fromEntries(
    Object.entries(tool).filter(
      ([key]) => !TOOL_CREDENTIAL_KEYS.has(key.toLowerCase())
    )
  );
}

export function echoRequestFields(originalRequest?: any) {
  const req =
    originalRequest && typeof originalRequest === 'object'
      ? originalRequest
      : {};
  // Function tools take the Response object's full shape; other tool types
  // (web_search, mcp, custom, ...) are echoed as sent, minus credentials
  // (an mcp tool's `headers` / `authorization`), which must not reach the
  // response or anything that logs it.
  const tools = (Array.isArray(req.tools) ? req.tools : []).map((t: any) =>
    !t?.type || t.type === 'function'
      ? {
          type: 'function',
          name: t?.name,
          description: t?.description ?? null,
          parameters: t?.parameters ?? null,
          strict: t?.strict ?? null,
        }
      : withoutToolCredentials(t)
  );
  // `text` keeps any other config sent with it (e.g. verbosity)
  const text = {
    ...(req.text && typeof req.text === 'object' ? req.text : {}),
    format: req.text?.format ?? { type: 'text' as const },
  };
  return {
    previous_response_id: req.previous_response_id ?? null,
    instructions: req.instructions ?? null,
    tools,
    tool_choice: req.tool_choice ?? RESPONSE_DEFAULTS.tool_choice,
    truncation: req.truncation ?? RESPONSE_DEFAULTS.truncation,
    parallel_tool_calls:
      req.parallel_tool_calls ?? RESPONSE_DEFAULTS.parallel_tool_calls,
    text,
    top_p: req.top_p ?? RESPONSE_DEFAULTS.top_p,
    presence_penalty:
      req.presence_penalty ?? RESPONSE_DEFAULTS.presence_penalty,
    frequency_penalty:
      req.frequency_penalty ?? RESPONSE_DEFAULTS.frequency_penalty,
    top_logprobs: req.top_logprobs ?? RESPONSE_DEFAULTS.top_logprobs,
    temperature: req.temperature ?? RESPONSE_DEFAULTS.temperature,
    reasoning: req.reasoning ?? null,
    max_output_tokens: req.max_output_tokens ?? null,
    max_tool_calls: req.max_tool_calls ?? null,
    store: req.store ?? RESPONSE_DEFAULTS.store,
    background: req.background ?? RESPONSE_DEFAULTS.background,
    service_tier: req.service_tier ?? RESPONSE_DEFAULTS.service_tier,
    metadata: req.metadata ?? {},
    safety_identifier: req.safety_identifier ?? null,
    prompt_cache_key: req.prompt_cache_key ?? null,
  };
}

/**
 * Create a response transformer function for a specific provider
 */
export function createResponsesAdapterTransformer(
  provider: string,
  originalRequest?: any
) {
  return (response: any, responseStatus: number) =>
    transformChatCompletionsToResponses(
      response,
      responseStatus,
      provider,
      originalRequest
    );
}

export default transformChatCompletionsToResponses;
