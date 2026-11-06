/**
 * Adapter utilities for Messages and Responses API.
 *
 * When a provider doesn't natively support the requested API (Messages or Responses),
 * these functions handle the request/response transformation via ChatCompletions
 * as an intermediate format. The decision is made per-provider inside tryPost,
 * so mixed configs (e.g., Anthropic + OpenAI in a loadbalancer) correctly use
 * native passthrough for native providers and the adapter for non-native ones.
 */

import { Context } from 'hono';
import { endpointStrings } from '../providers/types';
import { Params } from '../types/requestBody';
import { logger } from '../apm';
import Providers from '../providers';
import {
  transformMessagesToChatCompletions,
  transformChatCompletionsToMessages,
  transformStreamChunk as messagesTransformStreamChunk,
  createStreamState as messagesCreateStreamState,
  supportsMessagesApiNatively,
} from '../adapters/messages';
import {
  transformResponsesToChatCompletions,
  transformChatCompletionsToResponses,
  transformStreamChunk as responsesTransformStreamChunk,
  createStreamState as responsesCreateStreamState,
  supportsResponsesApiNatively,
} from '../adapters/responses';
import {
  routesMessagesViaResponses,
  transformMessagesToResponses,
  MessagesViaResponsesRequestError,
  transformResponsesToMessages,
  transformResponsesStreamChunk,
  createResponsesToMessagesStreamState,
} from '../adapters/messagesViaResponses';
import { estimateMessagesInputTokens } from '../adapters/messages/estimateTokens';
import { toMessagesError } from '../adapters/shared';
import { GOOGLE, GOOGLE_VERTEX_AI, OPEN_AI } from '../globals';
import { getModelAndProvider } from '../providers/google-vertex-ai/utils';

// ── Types ───────────────────────────────────────────────────────────────────

export interface AdapterContext {
  /** Whether the adapter is active for this request */
  isActive: boolean;
  /** The original endpoint before switching to chatComplete */
  originalFn: endpointStrings;
  /** Raw request body (without overrides) for Responses API response echoing */
  originalRequest: any;
  /** The resolved provider name */
  provider: string;
  /**
   * PS fork (#73): the API the provider was called with. Unset means Chat
   * Completions; 'responses' is Messages routed via the Responses API.
   */
  via?: 'responses';
}

export interface AdapterRequestResult {
  adapterCtx: AdapterContext;
  params: Params;
  requestBody: Params | FormData | ArrayBuffer | ReadableStream;
  fn: endpointStrings;
}

/**
 * PS fork (#73): Responses operations that pass through only to providers
 * whose upstream API implements them (enterprise 2.25.1 behaviour).
 */
export const RESPONSES_PROVIDER_OPERATIONS: Partial<
  Record<endpointStrings, { route: string; providers: string[] }>
> = {
  cancelModelResponse: {
    route: '/v1/responses/{id}/cancel',
    providers: ['openai', 'azure-openai'],
  },
  countResponseInputTokens: {
    route: '/v1/responses/input_tokens',
    providers: ['openai'],
  },
};

// ── Request Transform ───────────────────────────────────────────────────────

/**
 * Detect if an adapter is needed and transform the request accordingly.
 *
 * @returns `null` if no adapter is needed (native provider or non-adapter endpoint).
 * @returns A `Response` if the request should be rejected (e.g., GET/DELETE on non-native).
 * @returns An `AdapterRequestResult` with the transformed params, fn, and adapter context.
 */
export function applyAdapterRequestTransform(
  fn: endpointStrings,
  provider: string,
  params: Params,
  requestBody: Params | FormData | ArrayBuffer | ReadableStream,
  isStreamingMode: boolean,
  method: string,
  messagesViaResponses?: boolean
): AdapterRequestResult | Response | null {
  // PS fork (#73): models that only the Responses API serves (e.g. codex)
  // are reached via Responses instead of Chat Completions. The target's
  // `messages_via_responses` / the x-portkey-messages-via-responses header
  // force (true) or disable (false) this regardless of the model name.
  if (
    fn === 'messages' &&
    provider &&
    routesMessagesViaResponses(
      provider,
      params.model || '',
      messagesViaResponses
    )
  ) {
    let transformedParams: Params;
    try {
      transformedParams = transformMessagesToResponses(params, provider);
    } catch (err) {
      if (!(err instanceof MessagesViaResponsesRequestError)) throw err;
      return new Response(
        JSON.stringify({
          type: 'error',
          error: { type: 'invalid_request_error', message: err.message },
        }),
        { status: 400, headers: { 'content-type': 'application/json' } }
      );
    }
    return {
      adapterCtx: {
        isActive: true,
        originalFn: fn,
        originalRequest: {},
        provider,
        via: 'responses',
      },
      params: transformedParams,
      requestBody: transformedParams as any,
      fn: 'createModelResponse',
    };
  }

  if (
    fn === 'messages' &&
    provider &&
    !supportsMessagesApiNatively(provider, params.model || '')
  ) {
    const originalRequest =
      requestBody instanceof ReadableStream || requestBody instanceof FormData
        ? {}
        : { ...(requestBody as Params) };
    const transformedParams = normalizeTokenLimitForProvider(
      transformMessagesToChatCompletions(params),
      provider
    );

    return {
      adapterCtx: {
        isActive: true,
        originalFn: fn,
        originalRequest,
        provider,
      },
      params: transformedParams,
      requestBody: transformedParams as any,
      fn: 'chatComplete',
    };
  }

  if (
    fn === 'createModelResponse' &&
    provider &&
    !supportsResponsesApiNatively(provider)
  ) {
    const originalRequest =
      requestBody instanceof ReadableStream || requestBody instanceof FormData
        ? {}
        : { ...(requestBody as Params) };
    const transformedParams = normalizeTokenLimitForProvider(
      transformResponsesToChatCompletions(params),
      provider
    );

    return {
      adapterCtx: {
        isActive: true,
        originalFn: fn,
        originalRequest,
        provider,
      },
      params: transformedParams,
      requestBody: transformedParams as any,
      fn: 'chatComplete',
    };
  }

  if (
    [
      'getModelResponse',
      'deleteModelResponse',
      'listResponseInputItems',
    ].includes(fn) &&
    provider &&
    !supportsResponsesApiNatively(provider)
  ) {
    return new Response(
      JSON.stringify({
        error: {
          message: `${method} /v1/responses is only supported for native Responses API providers`,
          type: 'invalid_request_error',
        },
      }),
      { status: 400, headers: { 'content-type': 'application/json' } }
    );
  }

  // PS fork (#73): cancel and input-token counting have no Chat Completions
  // equivalent, so they reach only providers whose own Responses API has them.
  const supportedBy = RESPONSES_PROVIDER_OPERATIONS[fn];
  if (supportedBy && provider && !supportedBy.providers.includes(provider)) {
    return new Response(
      JSON.stringify({
        error: {
          message: `${method} ${supportedBy.route} is not supported by provider ${provider}`,
          type: 'invalid_request_error',
          code: 'unsupported_operation',
        },
      }),
      { status: 400, headers: { 'content-type': 'application/json' } }
    );
  }

  return null;
}

// ── PS fork (#73 follow-up): Messages count_tokens ─────────────────────────

/** Response header set on count_tokens answers estimated in the gateway. */
export const TOKEN_COUNT_SOURCE_HEADER = 'x-ps-token-count';

/** The provider's config for `fn` (resolving getConfig), or undefined. */
function providerFnConfig(
  provider: string,
  fn: endpointStrings,
  params: Params
): unknown {
  const providerConfig: any = Object.hasOwn(Providers, provider)
    ? Providers[provider]
    : undefined;
  if (!providerConfig) return undefined;
  if (providerConfig[fn]) return providerConfig[fn];
  if (!providerConfig.getConfig) return undefined;
  try {
    return providerConfig.getConfig({
      params,
      providerOptions: { provider },
    })?.[fn];
  } catch {
    return undefined;
  }
}

/** An Anthropic count_tokens answer estimated in the gateway. */
export function estimatedCountTokensResponse(params: Params): Response {
  return new Response(
    JSON.stringify({ input_tokens: estimateMessagesInputTokens(params) }),
    {
      status: 200,
      headers: {
        'content-type': 'application/json',
        [TOKEN_COUNT_SOURCE_HEADER]: 'estimated',
      },
    }
  );
}

/**
 * Decide how `POST /v1/messages/count_tokens` reaches one target:
 *
 * 1. `null`: the provider has its own Messages count_tokens config
 *    (anthropic, bedrock, claude-platform-aws, Vertex AI for Claude, and
 *    Gemini on google / vertex-ai via countTokens): call it as is.
 * 2. `openai` without a custom host: the request goes through the Messages
 *    → Responses translation to `POST /v1/responses/input_tokens`, and the
 *    answer is reduced to `{ input_tokens }`.
 * 3. Any other provider the Messages endpoint can reach (it has a
 *    chatComplete or messages config): a local estimate, marked with
 *    `x-ps-token-count: estimated`; nothing is sent upstream.
 * 4. Anything else: a 400 in Anthropic's error shape.
 *
 * Guardrails and mutators do not run on count requests, so a target with
 * hooks is never sent its content on the routes added here (Gemini, OpenAI):
 * it gets the local estimate, as /v1/messages would redact or deny first.
 */
export function messagesCountTokensRoute(
  provider: string,
  params: Params,
  customHost?: string,
  targetHasHooks = false
): AdapterRequestResult | Response | null {
  const gemini =
    provider === GOOGLE ||
    (provider === GOOGLE_VERTEX_AI &&
      getModelAndProvider(String(params.model ?? '')).provider === 'google');
  if (targetHasHooks && (gemini || provider === OPEN_AI)) {
    return estimatedCountTokensResponse(params);
  }
  if (providerFnConfig(provider, 'messagesCountTokens', params)) return null;
  if (provider === OPEN_AI && !customHost) {
    let responsesParams: Params;
    try {
      responsesParams = transformMessagesToResponses(params, provider);
    } catch (err) {
      if (!(err instanceof MessagesViaResponsesRequestError)) throw err;
      // Content the Responses route cannot carry: count it locally.
      return estimatedCountTokensResponse(params);
    }
    // Generation-only fields have no place in an input-token count.
    const {
      store: _store,
      stream: _stream,
      max_output_tokens: _max,
      ...countParams
    } = responsesParams as any;
    return {
      adapterCtx: {
        isActive: true,
        originalFn: 'messagesCountTokens',
        originalRequest: {},
        provider,
        via: 'responses',
      },
      params: countParams,
      requestBody: countParams,
      fn: 'countResponseInputTokens',
    };
  }
  if (
    providerFnConfig(provider, 'chatComplete', params) ||
    providerFnConfig(provider, 'messages', params)
  ) {
    return estimatedCountTokensResponse(params);
  }
  return new Response(
    JSON.stringify({
      type: 'error',
      error: {
        type: 'invalid_request_error',
        message: `POST /v1/messages/count_tokens is not supported for provider ${provider || '(none)'}`,
      },
    }),
    { status: 400, headers: { 'content-type': 'application/json' } }
  );
}

// ── PS fork: provider-aware token limit ─────────────────────────────────────

/**
 * The adapters emit `max_completion_tokens`, but 16 chat configs in this
 * fork (e.g. cohere, groq, zhipu) only map `max_tokens`; for them the limit
 * would be silently dropped. Use whichever key the target's chatComplete
 * config understands.
 */
export function normalizeTokenLimitForProvider(
  params: Params,
  provider: string
): Params {
  if (params.max_completion_tokens === undefined) return params;
  const providerConfig: any = Providers[provider];
  if (!providerConfig) return params;
  let chatConfig = providerConfig.chatComplete;
  if (!chatConfig && providerConfig.getConfig) {
    try {
      chatConfig = providerConfig.getConfig({
        params,
        providerOptions: { provider },
      })?.chatComplete;
    } catch {
      return params;
    }
  }
  if (!chatConfig || 'max_completion_tokens' in chatConfig) return params;
  if (!('max_tokens' in chatConfig)) return params;
  const { max_completion_tokens, ...rest } = params as any;
  return { ...rest, max_tokens: max_completion_tokens };
}

// ── Response Transform ──────────────────────────────────────────────────────

/**
 * If the adapter is active, transform the chatComplete-format response
 * back to the original API format (Messages or Responses).
 * For non-adapter requests this is a no-op passthrough.
 */
export async function adaptResponse(
  response: Response,
  adapterCtx: AdapterContext,
  c: Context
): Promise<Response> {
  if (!adapterCtx.isActive) return response;

  const contentType = response.headers.get('content-type') || '';

  if (contentType.includes('text/event-stream')) {
    return adaptStreamingResponse(response, adapterCtx, c);
  }

  return adaptNonStreamingResponse(response, adapterCtx, c);
}

/**
 * Transform a non-streaming chatComplete JSON response to
 * Messages or Responses API format.
 */
async function adaptNonStreamingResponse(
  response: Response,
  adapterCtx: AdapterContext,
  c: Context
): Promise<Response> {
  let json: any;
  try {
    json = await response.json();
  } catch (err) {
    logger.error({
      message:
        'Adapter response JSON parse failed, returning original response',
      originalFn: adapterCtx.originalFn,
      provider: adapterCtx.provider,
      status: response.status,
      error: err instanceof Error ? err.message : String(err),
    });
    return new Response(
      JSON.stringify({
        error: { message: 'Internal error', type: 'server_error' },
      }),
      { status: 500, headers: { 'content-type': 'application/json' } }
    );
  }

  // PS fork (#73): 246 is a successful response whose non-deny guardrails
  // failed; translate it as one. Hook results (246, 446) are kept.
  const status = response.status === 246 ? 200 : response.status;
  let transformedJson: any;
  let outStatus = response.status;
  if (adapterCtx.originalFn === 'messagesCountTokens') {
    // PS fork (#73 follow-up): Responses input_tokens → Messages count; a
    // success without a count is an invalid upstream answer (502).
    if (status === 200 && typeof json?.input_tokens === 'number') {
      transformedJson = { input_tokens: json.input_tokens };
    } else {
      if (status === 200) outStatus = 502;
      transformedJson = toMessagesError(json, outStatus, adapterCtx.provider);
    }
  } else if (adapterCtx.via === 'responses') {
    transformedJson = transformResponsesToMessages(
      json,
      status,
      adapterCtx.provider
    );
  } else if (adapterCtx.originalFn === 'messages') {
    transformedJson = transformChatCompletionsToMessages(
      json,
      status,
      adapterCtx.provider
    );
  } else {
    transformedJson = transformChatCompletionsToResponses(
      json,
      status,
      adapterCtx.provider,
      adapterCtx.originalRequest
    );
  }
  if (json?.hook_results) {
    transformedJson = { ...transformedJson, hook_results: json.hook_results };
  }

  const headers = new Headers(response.headers);
  headers.delete('content-length');
  headers.set('content-type', 'application/json');

  const newResponse = new Response(JSON.stringify(transformedJson), {
    status: outStatus,
    headers,
  });

  // Update requestOptions so logging captures the adapted format
  const reqOptions = c.get('requestOptions');
  if (reqOptions?.length) {
    const lastOption = reqOptions[reqOptions.length - 1];
    lastOption.response = newResponse;
    lastOption.providerOptions.rubeusURL = adapterCtx.originalFn;
  }

  return newResponse;
}

// ── Streaming Transform ─────────────────────────────────────────────────────

/**
 * Transform a streaming chatComplete response to Messages or Responses API SSE format.
 * Reads the upstream body directly (the log copy of a stream keeps only status
 * and headers), and cancels it when the client disconnects so the provider
 * stops generating.
 */
function adaptStreamingResponse(
  response: Response,
  adapterCtx: AdapterContext,
  c: Context
): Response {
  if (!response.body) return response;
  const originalFn = adapterCtx.originalFn;

  const { readable, writable } = new TransformStream();
  const writer = writable.getWriter();
  const reader = response.body.getReader();
  const encoder = new TextEncoder();

  // Same as handleStreamingMode: a client cancel errors the writable side,
  // so cancel the upstream body then. desiredSize is null once errored.
  const cancelUpstream = (reason?: unknown) => {
    reader.cancel(reason).catch(() => {});
  };
  writer.closed.catch(cancelUpstream);
  const isClientGone = () => writer.desiredSize === null;
  const onError = (err: unknown) => {
    cancelUpstream(err);
    if (!isClientGone()) logger.error('Adapter stream transform error:', err);
  };
  const close = () => {
    if (!isClientGone()) writer.close().catch(() => {});
  };

  if (adapterCtx.via === 'responses') {
    // PS fork (#73): Responses SSE → Messages SSE
    const state = createResponsesToMessagesStreamState(adapterCtx.provider);

    (async () => {
      try {
        for await (const chunk of readSSEStream(reader)) {
          const transformed = transformResponsesStreamChunk(chunk, state);
          if (transformed) {
            await writer.write(encoder.encode(transformed));
          }
        }
        const finalEvents = transformResponsesStreamChunk(null, state);
        if (finalEvents) {
          await writer.write(encoder.encode(finalEvents));
        }
      } catch (err) {
        onError(err);
      } finally {
        close();
      }
    })();
  } else if (originalFn === 'messages') {
    const state = messagesCreateStreamState(adapterCtx.originalRequest?.model);

    (async () => {
      try {
        for await (const chunk of readSSEStream(reader)) {
          const transformed = messagesTransformStreamChunk(chunk, state);
          if (transformed) {
            await writer.write(encoder.encode(transformed));
          }
        }
        // Flush completion events for providers whose streams end without
        // a `data: [DONE]` message (e.g. Google/Gemini).
        const finalEvents = messagesTransformStreamChunk('data: [DONE]', state);
        if (finalEvents) {
          await writer.write(encoder.encode(finalEvents));
        }
      } catch (err) {
        onError(err);
      } finally {
        close();
      }
    })();
  } else {
    // Responses API adapter
    // PS fork (#73): snapshots echo the client's request fields
    const state = responsesCreateStreamState(adapterCtx.originalRequest);

    (async () => {
      try {
        for await (const chunk of readSSEStream(reader)) {
          const transformed = responsesTransformStreamChunk(chunk, state);
          if (transformed) {
            await writer.write(encoder.encode(transformed));
          }
        }
        // Flush completion events for providers whose streams end without
        // a `data: [DONE]` message (e.g. Google/Gemini). The idempotency
        // guard in transformStreamChunk ensures this is a no-op when
        // [DONE] was already processed.
        const finalEvents = responsesTransformStreamChunk(
          'data: [DONE]',
          state
        );
        if (finalEvents) {
          await writer.write(encoder.encode(finalEvents));
        }
      } catch (err) {
        onError(err);
      } finally {
        close();
      }
    })();
  }

  // Carry forward upstream headers (trace-id, cache-status, provider, etc.)
  // but override entity/transport headers for the new SSE body.
  const headers = new Headers(response.headers);
  headers.set('content-type', 'text/event-stream');
  headers.delete('content-length');
  headers.delete('content-encoding');
  headers.delete('transfer-encoding');

  const adaptedResponse = new Response(readable, {
    status: 200,
    headers,
  });

  // Synchronously update requestOptions so logging sees the adapted status
  // and headers. Streams are not read back from logs, and a clone would tee
  // the body and keep the upstream alive after the client disconnects.
  const requestOptions = c.get('requestOptions');
  if (requestOptions?.length) {
    const lastOption = requestOptions[requestOptions.length - 1];
    lastOption.response = new Response(null, { status: 200, headers });
    lastOption.providerOptions.rubeusURL = originalFn;
  }

  return adaptedResponse;
}

// ── SSE Stream Reader ───────────────────────────────────────────────────────

/**
 * Async generator to read SSE chunks from a ReadableStream.
 * Yields complete SSE messages (delimited by double newlines).
 */
async function* readSSEStream(
  reader: ReadableStreamDefaultReader<Uint8Array>
): AsyncGenerator<string> {
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      if (buffer.trim()) yield buffer;
      break;
    }

    buffer += decoder.decode(value, { stream: true });

    while (buffer.includes('\n\n')) {
      const idx = buffer.indexOf('\n\n');
      const chunk = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      if (chunk.trim()) yield chunk;
    }
  }
}
