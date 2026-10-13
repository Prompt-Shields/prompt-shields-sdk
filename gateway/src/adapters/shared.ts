/**
 * PS fork: helpers shared by the Messages and Responses adapters (#73).
 *
 * The adapters ask providers for non-strict Chat Completions output, so
 * finish reasons, usage and reasoning arrive in provider-specific shapes.
 * These helpers normalise them once for both adapters.
 */

import { FINISH_REASON, PROVIDER_FINISH_REASON } from '../providers/types';
import { finishReasonMap } from '../providers/utils/finishReasonMap';

/**
 * Map a provider finish reason (raw when strict compliance is off, e.g.
 * Gemini `STOP`/`MAX_TOKENS` or Anthropic `end_turn`) to the OpenAI value.
 */
export function normalizeFinishReason(
  reason: string | null | undefined
): FINISH_REASON | null {
  if (!reason) return null;
  if ((Object.values(FINISH_REASON) as string[]).includes(reason)) {
    return reason as FINISH_REASON;
  }
  return (
    finishReasonMap.get(reason as PROVIDER_FINISH_REASON) ?? FINISH_REASON.stop
  );
}

export interface NormalizedUsage {
  /** Prompt tokens including cache reads and cache writes. */
  promptTokens: number;
  completionTokens: number;
  cacheReadTokens: number;
  cacheCreationTokens: number;
  reasoningTokens: number;
  totalTokens: number;
}

/**
 * Read a Chat Completions usage object. Cached tokens come from
 * `prompt_tokens_details.cached_tokens` (OpenAI, Gemini, most providers) or
 * from Anthropic-style top-level `cache_read_input_tokens` /
 * `cache_creation_input_tokens`. In both shapes prompt_tokens already
 * includes the cached tokens.
 */
export function normalizeUsage(usage: any): NormalizedUsage {
  const u = usage && typeof usage === 'object' ? usage : {};
  const promptTokens = num(u.prompt_tokens);
  const completionTokens = num(u.completion_tokens);
  const cacheReadTokens = num(
    u.prompt_tokens_details?.cached_tokens ?? u.cache_read_input_tokens
  );
  const cacheCreationTokens = num(
    u.cache_creation_input_tokens ??
      u.prompt_tokens_details?.cache_creation_tokens
  );
  const reasoningTokens = num(u.completion_tokens_details?.reasoning_tokens);
  const totalTokens = num(u.total_tokens) || promptTokens + completionTokens;
  return {
    promptTokens,
    completionTokens,
    cacheReadTokens,
    cacheCreationTokens,
    reasoningTokens,
    totalTokens,
  };
}

function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : 0;
}

/**
 * Anthropic Messages usage: `input_tokens` excludes cache reads and cache
 * writes, which are reported separately.
 */
export function toMessagesUsage(u: NormalizedUsage) {
  const usage: {
    input_tokens: number;
    output_tokens: number;
    cache_creation_input_tokens?: number;
    cache_read_input_tokens?: number;
  } = {
    input_tokens: Math.max(
      0,
      u.promptTokens - u.cacheReadTokens - u.cacheCreationTokens
    ),
    output_tokens: u.completionTokens,
  };
  if (u.cacheCreationTokens || u.cacheReadTokens) {
    usage.cache_creation_input_tokens = u.cacheCreationTokens;
    usage.cache_read_input_tokens = u.cacheReadTokens;
  }
  return usage;
}

/**
 * Responses API usage: `input_tokens` includes cached tokens, which are
 * broken out in `input_tokens_details`.
 */
export function toResponsesUsage(u: NormalizedUsage) {
  return {
    input_tokens: u.promptTokens,
    input_tokens_details: { cached_tokens: u.cacheReadTokens },
    output_tokens: u.completionTokens,
    output_tokens_details: { reasoning_tokens: u.reasoningTokens },
    total_tokens: u.totalTokens,
  };
}

/**
 * Reasoning text an OpenAI-compatible provider passes through on a message
 * or delta (`reasoning_content` from DeepSeek-style APIs, `reasoning` from
 * OpenRouter-style APIs). Used only when no `content_blocks` thinking exists.
 */
export function plainReasoningText(msg: any): string {
  if (!msg) return '';
  if (typeof msg.reasoning_content === 'string') return msg.reasoning_content;
  if (typeof msg.reasoning === 'string') return msg.reasoning;
  return '';
}

const ANTHROPIC_ERROR_TYPE_BY_STATUS: Record<number, string> = {
  400: 'invalid_request_error',
  401: 'authentication_error',
  402: 'billing_error',
  403: 'permission_error',
  404: 'not_found_error',
  413: 'request_too_large',
  429: 'rate_limit_error',
  500: 'api_error',
  504: 'timeout_error',
  529: 'overloaded_error',
};

const ANTHROPIC_ERROR_TYPES = new Set(
  Object.values(ANTHROPIC_ERROR_TYPE_BY_STATUS)
);

/**
 * Anthropic Messages error envelope: `{ type: 'error', error: { type,
 * message } }`. Keeps `provider` so callers can tell where it came from.
 */
export function toMessagesError(
  body: any,
  status: number,
  provider: string
): Record<string, any> {
  const err = body?.error;
  const message =
    (typeof err === 'string' ? err : err?.message) ||
    body?.message ||
    'Request failed';
  const rawType = typeof err === 'object' ? err?.type : undefined;
  const type =
    rawType && ANTHROPIC_ERROR_TYPES.has(rawType)
      ? rawType
      : ANTHROPIC_ERROR_TYPE_BY_STATUS[status] ||
        (status >= 500 ? 'api_error' : 'invalid_request_error');
  return {
    type: 'error',
    error: { type, message },
    provider: body?.provider ?? provider,
  };
}
