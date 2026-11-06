/**
 * PS fork (#73): Responses request fields that have no direct Chat
 * Completions counterpart, for targets reached through the adapter.
 *
 * - `tools`: function tools map as before. Web search tools map to the
 *   provider's own search tool where it has one (Anthropic's web_search
 *   server tool; Gemini's Google Search grounding). Every other built-in
 *   tool type (file_search, custom, mcp, code_interpreter, computer use,
 *   image_generation, shell, ...) is refused with a 400: dropping a tool the
 *   caller relies on changes what the model can do.
 * - `tool_choice`: `allowed_tools` is applied by sending only the allowed
 *   tools with `auto` / `required`; a choice naming a built-in tool is
 *   refused.
 * - `safety_identifier`: sent as the Chat Completions `user` (Anthropic
 *   `metadata.user_id`), in place of the deprecated `user`.
 * - `text.verbosity`, `prompt_cache_key` and `service_tier` are advisory and
 *   ignored: these providers have no equivalent (Anthropic caching uses
 *   explicit `cache_control` breakpoints, which pass through as before).
 * - `max_tool_calls` (applyMaxToolCalls): limits built-in tool calls, as in
 *   the Responses API (function calls are not counted). The only built-in
 *   tool these targets get is web search: Anthropic's web_search tool takes
 *   the limit as `max_uses`; Google Search grounding has no limit, so that
 *   combination is refused. With no built-in tool it limits nothing.
 * - `include` (transformResponsesInclude): values the adapter can honour are
 *   accepted; any other value is refused, naming only that value.
 *
 * Error messages name the field and tool type (or the refused include value)
 * only, never other request values (an mcp tool carries credentials).
 */

export class ResponsesAdapterRequestError extends Error {
  constructor(
    message: string,
    public readonly param: string
  ) {
    super(message);
  }
}

/** Providers whose chat config sends Anthropic server tools. */
const ANTHROPIC_SERVER_TOOL_PROVIDERS = new Set([
  'anthropic',
  'claude-platform-aws',
]);

/** Anthropic's web search server tool version. */
export const ANTHROPIC_WEB_SEARCH_TOOL = 'web_search_20250305';

const WEB_SEARCH_TYPE = /^web_search(_preview)?(_\d{4}_\d{2}_\d{2})?$/;

export function isWebSearchType(type: unknown): boolean {
  return typeof type === 'string' && WEB_SEARCH_TYPE.test(type);
}

/** Vertex AI's publisher for a model id (as getModelAndProvider reads it). */
function vertexPublisher(model: string): string {
  const parts = (model || '').split('.');
  return parts.length > 1 &&
    ['google', 'anthropic', 'meta', 'endpoints', 'mistralai'].includes(parts[0])
    ? parts[0]
    : 'google';
}

type SearchFamily = 'anthropic' | 'gemini' | null;

/** Marks the Google Search grounding tool built from a web_search tool. */
const GOOGLE_SEARCH_MARK = Symbol('psGoogleSearch');

function searchFamily(provider = '', model = ''): SearchFamily {
  const p = provider.toLowerCase();
  if (ANTHROPIC_SERVER_TOOL_PROVIDERS.has(p)) return 'anthropic';
  if (p === 'google') return 'gemini';
  if (p === 'vertex-ai' && vertexPublisher(model) === 'google') return 'gemini';
  return null;
}

function unsupported(param: string, what: string, provider?: string): never {
  throw new ResponsesAdapterRequestError(
    `${what} is not supported for provider '${provider || 'unknown'}' ` +
      'on /v1/responses (served through the Chat Completions adapter)',
    param
  );
}

function webSearchTool(
  t: any,
  i: number,
  family: SearchFamily,
  provider?: string
): any {
  const param = `tools[${i}]`;
  if (family === 'anthropic') {
    const options: Record<string, any> = { name: ANTHROPIC_WEB_SEARCH_TOOL };
    const domains = t.filters?.allowed_domains;
    if (Array.isArray(domains) && domains.length)
      options.allowed_domains = domains;
    const loc = t.user_location;
    if (loc && typeof loc === 'object') {
      const userLocation: Record<string, any> = { type: 'approximate' };
      for (const k of ['city', 'region', 'country', 'timezone']) {
        if (typeof loc[k] === 'string' && loc[k]) userLocation[k] = loc[k];
      }
      options.user_location = userLocation;
    }
    const tool: any = { type: 'web_search', web_search: options };
    if (t.cache_control) tool.cache_control = t.cache_control;
    return tool;
  }
  if (family === 'gemini') {
    // Google Search grounding has no allow-list; dropping it would widen
    // the search. user_location and search_context_size are advisory.
    const domains = t.filters?.allowed_domains;
    if (Array.isArray(domains) && domains.length)
      unsupported(
        `${param}.filters.allowed_domains`,
        'filters.allowed_domains on a web_search tool',
        provider
      );
    const tool = { type: 'function', function: { name: 'google_search' } };
    // Non-enumerable, so it never reaches the provider (applyMaxToolCalls)
    Object.defineProperty(tool, GOOGLE_SEARCH_MARK, { value: true });
    return tool;
  }
  return unsupported(`${param}.type`, `Tool type '${t.type}'`, provider);
}

/**
 * Responses `tools` / `tool_choice` as Chat Completions `tools` /
 * `tool_choice` for `provider`. Throws ResponsesAdapterRequestError for
 * anything the target cannot honour.
 */
export function transformResponsesTools(
  req: any,
  provider?: string
): { tools?: any[]; tool_choice?: any } {
  const family = searchFamily(provider, req.model);
  const out: { tools?: any[]; tool_choice?: any } = {};
  // Source tool for each Chat Completions tool, for allowed_tools
  const sources: any[] = [];
  const tools: any[] = [];

  if (Array.isArray(req.tools)) {
    req.tools.forEach((t: any, i: number) => {
      if (!t || typeof t !== 'object') return;
      if (t.type === 'function') {
        const tool: any = {
          type: 'function' as const,
          function: {
            name: t.name,
            description: t.description || '',
            parameters: t.parameters,
          },
        };
        // Preserve cache_control for prompt caching (Anthropic, etc.)
        if (t.cache_control) tool.cache_control = t.cache_control;
        tools.push(tool);
      } else if (isWebSearchType(t.type)) {
        tools.push(webSearchTool(t, i, family, provider));
      } else {
        unsupported(
          `tools[${i}].type`,
          typeof t.type === 'string'
            ? `Tool type '${t.type}'`
            : 'A tool without a type',
          provider
        );
      }
      sources.push(t);
    });
  }

  const choice = req.tool_choice;
  if (typeof choice === 'string') {
    out.tool_choice = choice;
  } else if (choice && typeof choice === 'object') {
    if (choice.type === 'function') {
      if (typeof choice.name !== 'string' || !choice.name) {
        throw new ResponsesAdapterRequestError(
          'tool_choice.name is required for a function tool_choice',
          'tool_choice.name'
        );
      }
      out.tool_choice = { type: 'function', function: { name: choice.name } };
    } else if (choice.type === 'allowed_tools') {
      const allowed = choice.tools;
      if (!Array.isArray(allowed) || !allowed.length) {
        throw new ResponsesAdapterRequestError(
          'tool_choice.tools must list at least one tool',
          'tool_choice.tools'
        );
      }
      if (
        choice.mode !== undefined &&
        choice.mode !== 'auto' &&
        choice.mode !== 'required'
      ) {
        throw new ResponsesAdapterRequestError(
          "tool_choice.mode must be 'auto' or 'required'",
          'tool_choice.mode'
        );
      }
      const keep = new Set<number>();
      allowed.forEach((a: any, j: number) => {
        const idx = sources.findIndex((s) =>
          a?.type === 'function'
            ? s.type === 'function' && s.name === a.name
            : isWebSearchType(a?.type) && isWebSearchType(s.type)
        );
        if (idx < 0) {
          throw new ResponsesAdapterRequestError(
            `tool_choice.tools[${j}] does not match any tool in tools`,
            `tool_choice.tools[${j}]`
          );
        }
        keep.add(idx);
      });
      // Only the allowed tools are sent, so the model can call no others
      const filtered = tools.filter((_, i) => keep.has(i));
      tools.length = 0;
      tools.push(...filtered);
      out.tool_choice = choice.mode || 'auto';
    } else {
      unsupported(
        'tool_choice',
        typeof choice.type === 'string'
          ? `tool_choice type '${choice.type}'`
          : 'This tool_choice',
        provider
      );
    }
  }

  if (tools.length) out.tools = tools;
  return out;
}

/** Providers that never return token logprobs. */
const NO_LOGPROBS_PROVIDERS = new Set([
  'anthropic',
  'claude-platform-aws',
  'bedrock',
]);

function returnsNoLogprobs(provider = '', model = ''): boolean {
  const p = provider.toLowerCase();
  if (NO_LOGPROBS_PROVIDERS.has(p)) return true;
  return p === 'vertex-ai' && vertexPublisher(model) === 'anthropic';
}

export const INCLUDE_OUTPUT_TEXT_LOGPROBS = 'message.output_text.logprobs';

/**
 * `include` values accepted on adapted targets:
 * - `reasoning.encrypted_content`: reasoning items carry `encrypted_content`
 *   whenever the target's thinking can be sent back (signed thinking, see
 *   thinkingCarry.ts), whether or not it is listed; other targets return no
 *   thinking that could be carried, so there is nothing to add.
 * - `message.output_text.logprobs`: asks the target for logprobs and returns
 *   them in `output_text.logprobs` (JSON responses; refused with
 *   `stream: true`, and for targets that never return logprobs).
 * - `web_search_call.action.sources`: sources of `web_search_call` output
 *   items, where the adapter produces those items.
 */
const ADAPTED_INCLUDE_VALUES = new Set([
  'reasoning.encrypted_content',
  INCLUDE_OUTPUT_TEXT_LOGPROBS,
  'web_search_call.action.sources',
]);

/** A request value for an error message: quoted and length-capped. */
function quoted(value: string): string {
  return `'${value.length > 64 ? `${value.slice(0, 64)}...` : value}'`;
}

/** Whether the request lists `value` in `include`. */
export function requestIncludes(req: any, value: string): boolean {
  return Array.isArray(req?.include) && req.include.includes(value);
}

/**
 * Responses `include` for `provider`: the Chat Completions fields it needs
 * (`logprobs`). Throws ResponsesAdapterRequestError for a value the adapter
 * cannot honour.
 */
export function transformResponsesInclude(
  req: any,
  provider?: string
): { logprobs?: true } {
  const include = req.include;
  if (include == null) return {};
  if (!Array.isArray(include)) {
    throw new ResponsesAdapterRequestError(
      'include must be an array of strings',
      'include'
    );
  }
  const out: { logprobs?: true } = {};
  include.forEach((value: unknown, i: number) => {
    const param = `include[${i}]`;
    if (typeof value !== 'string') {
      throw new ResponsesAdapterRequestError(
        `${param} must be a string`,
        param
      );
    }
    const what = `include value ${quoted(value)}`;
    if (!ADAPTED_INCLUDE_VALUES.has(value)) unsupported(param, what, provider);
    if (value === INCLUDE_OUTPUT_TEXT_LOGPROBS) {
      if (returnsNoLogprobs(provider, req.model))
        unsupported(param, what, provider);
      if (req.stream === true) {
        throw new ResponsesAdapterRequestError(
          `${what} is not supported with stream: true on /v1/responses ` +
            '(served through the Chat Completions adapter)',
          param
        );
      }
      out.logprobs = true;
    }
  });
  return out;
}

/**
 * Chat Completions `choice.logprobs.content` as Responses
 * `output_text.logprobs` when the request includes them, else [].
 */
export function outputTextLogprobs(choice: any, originalRequest?: any): any[] {
  if (!requestIncludes(originalRequest, INCLUDE_OUTPUT_TEXT_LOGPROBS))
    return [];
  const content = choice?.logprobs?.content;
  if (!Array.isArray(content)) return [];
  const entry = (t: any) => ({
    token: typeof t?.token === 'string' ? t.token : '',
    logprob: typeof t?.logprob === 'number' ? t.logprob : 0,
    bytes: Array.isArray(t?.bytes) ? t.bytes : [],
  });
  return content.map((t: any) => ({
    ...entry(t),
    top_logprobs: Array.isArray(t?.top_logprobs)
      ? t.top_logprobs.map(entry)
      : [],
  }));
}

/**
 * Apply `max_tool_calls` to the Chat Completions `tools` built for
 * `provider` by transformResponsesTools (in place). Throws
 * ResponsesAdapterRequestError when the limit cannot be enforced.
 */
export function applyMaxToolCalls(
  req: any,
  tools: any[] | undefined,
  provider?: string
): void {
  const max = req.max_tool_calls;
  if (max == null) return;
  if (!Number.isInteger(max) || max < 0) {
    throw new ResponsesAdapterRequestError(
      'max_tool_calls must be a non-negative integer',
      'max_tool_calls'
    );
  }
  for (const tool of tools ?? []) {
    if (tool?.type === 'web_search' && tool.web_search) {
      // Anthropic's max_uses is per tool and at least 1. Anthropic accepts
      // one web_search tool per request, so this caps all built-in calls.
      if (max < 1) {
        unsupported(
          'max_tool_calls',
          'max_tool_calls 0 with a web_search tool (remove the tool instead)',
          provider
        );
      }
      tool.web_search.max_uses = max;
    } else if (tool?.[GOOGLE_SEARCH_MARK]) {
      unsupported(
        'max_tool_calls',
        'max_tool_calls with a web_search tool',
        provider
      );
    }
  }
}
