/**
 * PS fork (#63, MCP Phase 1): enforcing MCP proxy, `/v1/mcp/:server`.
 *
 * A Streamable HTTP pass-through to one configured upstream MCP server
 * (`./config.ts`), for protocol revisions 2026-07-28 (stateless, `Mcp-Method`
 * / `Mcp-Name` headers) and 2025-11-25 (`initialize`, `Mcp-Session-Id`, GET
 * stream and DELETE, passed through unchanged).
 *
 * On every POST:
 * 1. the client authenticates with the gateway's MCP token (`./auth.ts`);
 * 2. the body must be one JSON-RPC message; the mirrored headers must match
 *    it (`HeaderMismatch`, -32020, as the spec requires of servers);
 * 3. `tools/call`: the tool must pass the server's allow/deny list and
 *    description pin, then the input guardrails on its arguments;
 * 4. the request goes to the configured URL only, with the server's own
 *    credentials (the client's Authorization header is never forwarded), the
 *    URL re-checked by `isSafeUpstreamURL`, and redirects refused;
 * 5. `tools/list` results are filtered and sanitised; `tools/call` results go
 *    through the output guardrails. SSE responses stream: only the event that
 *    carries the response is held while it is checked.
 *
 * The body forwarded upstream is the JSON the gateway checked, re-serialised,
 * so the upstream cannot parse a different message (duplicate keys).
 */
import { Context } from 'hono';
import { isSafeUpstreamURL } from '../middlewares/requestValidator';
import { upstreamFetch } from '../utils/fetch';
import { checkMcpClient } from './auth';
import { getMcpServer, McpServerConfig } from './config';
import { mcpGuardrailsBlock, toolResultText } from './guardrails';
import {
  displayToolName,
  filterToolsList,
  headerMismatch,
  toolCallDecision,
} from './policy';
import { inspectSseResponse, isResponseTo } from './sse';
import {
  McpOutcome,
  mcpToolDefinitionChangesTotal,
  mcpToolsRemovedTotal,
  recordRequest,
  recordToolCall,
} from './telemetry';
import { isMetricsEnabled } from '../middlewares/psMetrics/labels';

/** JSON-RPC error codes. -32000..-32019 are implementation-defined. */
export const MCP_ERRORS = {
  PARSE_ERROR: -32700,
  INVALID_REQUEST: -32600,
  TOOL_DENIED: -32010,
  GUARDRAIL_BLOCKED: -32011,
  TOOL_DEFINITION_CHANGED: -32012,
  UPSTREAM_ERROR: -32013,
  UNAUTHORIZED: -32014,
  UNKNOWN_SERVER: -32015,
  HEADER_MISMATCH: -32020,
} as const;

const JSON_TYPE = 'application/json';
const SSE_TYPE = 'text/event-stream';

// Client headers forwarded upstream; everything else (Authorization,
// cookies, x-portkey-*, x-ps-*, Origin, ...) stays at the gateway.
const FORWARDED_REQUEST_HEADERS = [
  'accept',
  'mcp-protocol-version',
  'mcp-session-id',
  'mcp-method',
  'mcp-name',
  'last-event-id',
];
const MCP_PARAM_HEADER = /^mcp-param-[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$/i;
const FORWARDED_RESPONSE_HEADERS = [
  'content-type',
  'mcp-session-id',
  'retry-after',
];
const SAFE_HEADER_VALUE = /^[\x21-\x7E]{1,256}$/;

type RpcId = string | number | null;

export function rpcError(
  status: number,
  id: RpcId,
  code: number,
  message: string
): Response {
  return new Response(
    JSON.stringify({ jsonrpc: '2.0', id, error: { code, message } }),
    { status, headers: { 'content-type': JSON_TYPE } }
  );
}

const errorMessage = (id: RpcId, code: number, message: string) => ({
  jsonrpc: '2.0',
  id,
  error: { code, message },
});

const isRpcId = (v: unknown): v is string | number =>
  typeof v === 'string' || (typeof v === 'number' && Number.isFinite(v));

function upstreamHeaders(
  c: Context,
  server: McpServerConfig,
  hasBody: boolean
): Headers {
  const out = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = c.req.raw.headers.get(name);
    if (value !== null) out.set(name, value);
  }
  c.req.raw.headers.forEach((value, name) => {
    if (MCP_PARAM_HEADER.test(name)) out.set(name, value);
  });
  if (!out.has('accept')) out.set('accept', `${JSON_TYPE}, ${SSE_TYPE}`);
  if (hasBody) out.set('content-type', JSON_TYPE);
  if (server.authHeader)
    out.set(server.authHeader.name, server.authHeader.value);
  return out;
}

function clientHeaders(upstream: Response, streaming: boolean): Headers {
  const out = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value === null) continue;
    if (name !== 'content-type' && !SAFE_HEADER_VALUE.test(value)) continue;
    out.set(name, value);
  }
  if (streaming) {
    out.set('cache-control', 'no-cache');
    out.set('x-accel-buffering', 'no');
  }
  return out;
}

const contentTypeOf = (r: Response) =>
  (r.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();

/** Body text, or undefined once it is larger than `max` bytes. */
async function readLimited(
  body: ReadableStream<Uint8Array> | null,
  max: number
): Promise<string | undefined> {
  if (!body) return '';
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > max) {
      reader.cancel().catch(() => {});
      return undefined;
    }
    chunks.push(value);
  }
  const all = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    all.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(all);
}

/** The request body, or undefined once it is larger than `max` bytes. */
async function readRequestBody(c: Context, max: number) {
  // An earlier middleware may already have read (and cached) the body.
  const cached = Object.keys((c.req as any).bodyCache ?? {}).length > 0;
  if (!cached && !c.req.raw.bodyUsed) return readLimited(c.req.raw.body, max);
  const text = await c.req.text();
  return new TextEncoder().encode(text).byteLength > max ? undefined : text;
}

async function callUpstream(
  c: Context,
  server: McpServerConfig,
  method: string,
  body: string | undefined
): Promise<Response | { error: 'unsafe' | 'timeout' | 'unreachable' }> {
  // Checked again per request: TRUSTED_CUSTOM_HOSTS can differ per runtime.
  if (!isSafeUpstreamURL(server.url, c)) return { error: 'unsafe' };
  const controller = new AbortController();
  const onClientAbort = () => controller.abort();
  c.req.raw.signal?.addEventListener?.('abort', onClientAbort);
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, server.timeoutMs);
  try {
    // upstreamFetch never follows a redirect (a 3xx becomes a 502).
    return await upstreamFetch(server.url, {
      method,
      headers: upstreamHeaders(c, server, body !== undefined),
      body,
      signal: controller.signal,
    });
  } catch {
    return { error: timedOut ? 'timeout' : 'unreachable' };
  } finally {
    // Only the wait for response headers is bounded; a stream may run on.
    clearTimeout(timer);
  }
}

function upstreamFailure(
  id: RpcId,
  error: 'unsafe' | 'timeout' | 'unreachable'
): Response {
  if (error === 'timeout')
    return rpcError(
      504,
      id,
      MCP_ERRORS.UPSTREAM_ERROR,
      'The MCP server did not answer in time'
    );
  return rpcError(
    502,
    id,
    MCP_ERRORS.UPSTREAM_ERROR,
    error === 'unsafe'
      ? 'The MCP server URL is not an allowed upstream'
      : 'The MCP server could not be reached'
  );
}

/** Pass the upstream response through, streaming, with filtered headers. */
function passThrough(upstream: Response): Response {
  return new Response(upstream.body, {
    status: upstream.status,
    headers: clientHeaders(upstream, contentTypeOf(upstream) === SSE_TYPE),
  });
}

export async function mcpHandler(c: Context): Promise<Response> {
  const started = Date.now();
  const serverId = c.req.param('server') ?? '';
  const auth = checkMcpClient(c);
  if (!auth.ok) {
    recordRequest(c, 'unknown', undefined, 'invalid');
    return rpcError(auth.status, null, MCP_ERRORS.UNAUTHORIZED, auth.message);
  }
  const server = getMcpServer(serverId);
  if (!server) {
    recordRequest(c, 'unknown', undefined, 'invalid');
    return rpcError(404, null, MCP_ERRORS.UNKNOWN_SERVER, 'Unknown MCP server');
  }

  // 2025-11-25: GET opens the server's SSE stream, DELETE ends a session.
  if (c.req.method !== 'POST') {
    const upstream = await callUpstream(c, server, c.req.method, undefined);
    if ('error' in upstream) {
      recordRequest(c, server.id, c.req.method, 'upstream_error');
      return upstreamFailure(null, upstream.error);
    }
    recordRequest(c, server.id, c.req.method, upstream.ok ? 'ok' : 'error');
    return credentialRejection(upstream, null) ?? passThrough(upstream);
  }

  // ---- Parse and validate the message ----
  const contentType = (c.req.header('content-type') || '')
    .split(';')[0]
    .trim()
    .toLowerCase();
  if (contentType !== JSON_TYPE) {
    recordRequest(c, server.id, undefined, 'invalid');
    return rpcError(
      415,
      null,
      MCP_ERRORS.INVALID_REQUEST,
      'Content-Type must be application/json'
    );
  }
  const declared = Number(c.req.header('content-length'));
  if (Number.isFinite(declared) && declared > server.maxRequestBytes) {
    recordRequest(c, server.id, undefined, 'invalid');
    return rpcError(
      413,
      null,
      MCP_ERRORS.INVALID_REQUEST,
      'The request body is too large'
    );
  }
  const text = await readRequestBody(c, server.maxRequestBytes);
  if (text === undefined) {
    recordRequest(c, server.id, undefined, 'invalid');
    return rpcError(
      413,
      null,
      MCP_ERRORS.INVALID_REQUEST,
      'The request body is too large'
    );
  }
  let message: any;
  try {
    message = JSON.parse(text);
  } catch {
    recordRequest(c, server.id, undefined, 'invalid');
    return rpcError(
      400,
      null,
      MCP_ERRORS.PARSE_ERROR,
      'The body is not valid JSON'
    );
  }
  if (
    !message ||
    typeof message !== 'object' ||
    Array.isArray(message) ||
    message.jsonrpc !== '2.0'
  ) {
    recordRequest(c, server.id, undefined, 'invalid');
    return rpcError(
      400,
      null,
      MCP_ERRORS.INVALID_REQUEST,
      Array.isArray(message)
        ? 'JSON-RPC batches are not supported'
        : 'The body must be one JSON-RPC 2.0 message'
    );
  }
  const method: unknown = message.method;
  const id: RpcId = isRpcId(message.id) ? message.id : null;
  if (method !== undefined) {
    const isNotification = !('id' in message);
    if (
      typeof method !== 'string' ||
      (isNotification && !method.startsWith('notifications/')) ||
      (!isNotification && !isRpcId(message.id)) ||
      (message.params !== undefined &&
        (typeof message.params !== 'object' || message.params === null))
    ) {
      recordRequest(c, server.id, method, 'invalid');
      return rpcError(
        400,
        id,
        MCP_ERRORS.INVALID_REQUEST,
        'Invalid JSON-RPC request'
      );
    }
    const mismatch = headerMismatch(c.req.raw.headers, message);
    if (mismatch) {
      recordRequest(c, server.id, method, 'invalid');
      return rpcError(
        400,
        id,
        MCP_ERRORS.HEADER_MISMATCH,
        `Header mismatch: ${mismatch}`
      );
    }
  } else if (!('result' in message) && !('error' in message)) {
    recordRequest(c, server.id, undefined, 'invalid');
    return rpcError(
      400,
      id,
      MCP_ERRORS.INVALID_REQUEST,
      'Invalid JSON-RPC message'
    );
  }

  // ---- tools/call: policy and input guardrails ----
  const isToolCall = method === 'tools/call';
  const toolName: string = isToolCall ? message.params?.name : '';
  if (isToolCall) {
    const decision = toolCallDecision(server, toolName);
    if (!decision.allowed) {
      recordRequest(c, server.id, method, 'denied');
      return rpcError(
        200,
        id,
        decision.reason === 'pin'
          ? MCP_ERRORS.TOOL_DEFINITION_CHANGED
          : MCP_ERRORS.TOOL_DENIED,
        decision.reason === 'pin'
          ? `Tool ${displayToolName(toolName)} changed since it was approved`
          : `Tool ${displayToolName(toolName)} is not allowed on this server`
      );
    }
    const args = message.params?.arguments;
    if (
      await mcpGuardrailsBlock(
        c,
        server.inputGuardrails,
        'input',
        JSON.stringify(args === undefined ? {} : args)
      )
    ) {
      recordRequest(c, server.id, method, 'blocked');
      recordToolCall(c, {
        server: server.id,
        tool: toolName,
        outcome: 'blocked',
        latencyMs: Date.now() - started,
      });
      return rpcError(
        200,
        id,
        MCP_ERRORS.GUARDRAIL_BLOCKED,
        'The tool call was blocked by a guardrail'
      );
    }
  }

  // ---- Forward ----
  const upstream = await callUpstream(
    c,
    server,
    'POST',
    JSON.stringify(message)
  );
  const finish = (outcome: McpOutcome) => {
    recordRequest(c, server.id, method, outcome);
    if (isToolCall && outcome !== 'denied' && outcome !== 'invalid')
      recordToolCall(c, {
        server: server.id,
        tool: toolName,
        outcome,
        latencyMs: Date.now() - started,
      });
  };
  if ('error' in upstream) {
    finish('upstream_error');
    return upstreamFailure(id, upstream.error);
  }
  const rejected = credentialRejection(upstream, id);
  if (rejected) {
    finish('upstream_error');
    return rejected;
  }

  const inspect = method === 'tools/list' || isToolCall;
  if (!inspect || !upstream.ok || id === null || upstream.status === 202) {
    // An HTTP error, or a 202 where a tools/call needed a result.
    finish(upstream.ok && !isToolCall ? 'ok' : 'error');
    return passThrough(upstream);
  }

  // The response to tools/list or tools/call, checked before it is released.
  const onResponse = async (response: any): Promise<any | undefined> => {
    if (method === 'tools/list') {
      if (response.error) {
        finish('error');
        return undefined;
      }
      const filtered = await filterToolsList(server, response.result);
      reportFiltered(c, server, filtered);
      finish('ok');
      return { ...response, result: filtered.result };
    }
    if (
      await mcpGuardrailsBlock(
        c,
        server.outputGuardrails,
        'output',
        toolResultText(response)
      )
    ) {
      finish('blocked');
      return errorMessage(
        id,
        MCP_ERRORS.GUARDRAIL_BLOCKED,
        'The tool result was blocked by a guardrail'
      );
    }
    finish(
      response.error || response.result?.isError === true ? 'error' : 'ok'
    );
    return undefined;
  };

  const type = contentTypeOf(upstream);
  if (type === SSE_TYPE && upstream.body) {
    let answered = false;
    const body = inspectSseResponse(upstream.body, {
      requestId: id,
      maxEventBytes: server.maxResponseBytes,
      onResponse: async (response) => {
        answered = true;
        return onResponse(response);
      },
      tooLarge: () => {
        answered = true;
        finish('upstream_error');
        return errorMessage(
          id,
          MCP_ERRORS.UPSTREAM_ERROR,
          'The MCP server response is too large'
        );
      },
      onEnd: () => {
        if (!answered) finish('upstream_error');
      },
    });
    return new Response(body, {
      status: upstream.status,
      headers: clientHeaders(upstream, true),
    });
  }
  if (type === JSON_TYPE) {
    const raw = await readLimited(upstream.body, server.maxResponseBytes);
    let response: any;
    try {
      response = raw === undefined ? undefined : JSON.parse(raw);
    } catch {
      response = undefined;
    }
    if (!isResponseTo(response, id)) {
      finish('upstream_error');
      return rpcError(
        502,
        id,
        MCP_ERRORS.UPSTREAM_ERROR,
        raw === undefined
          ? 'The MCP server response is too large'
          : 'The MCP server sent an invalid response'
      );
    }
    const replacement = await onResponse(response);
    return new Response(JSON.stringify(replacement ?? response), {
      status: upstream.status,
      headers: clientHeaders(upstream, false),
    });
  }
  // Fail closed: a result the gateway cannot read is not released.
  upstream.body?.cancel().catch(() => {});
  finish('upstream_error');
  return rpcError(
    502,
    id,
    MCP_ERRORS.UPSTREAM_ERROR,
    'The MCP server sent an unsupported response type'
  );
}

/**
 * The upstream refused the gateway's own credentials. Its WWW-Authenticate
 * (OAuth discovery for the upstream) is not passed on: the client cannot fix
 * the gateway's credentials, and must not be sent to the upstream's IdP.
 */
function credentialRejection(
  upstream: Response,
  id: RpcId
): Response | undefined {
  if (upstream.status !== 401 && upstream.status !== 403) return undefined;
  upstream.body?.cancel().catch(() => {});
  return rpcError(
    502,
    id,
    MCP_ERRORS.UPSTREAM_ERROR,
    'The MCP server rejected the gateway credentials'
  );
}

function reportFiltered(
  c: Context,
  server: McpServerConfig,
  filtered: Awaited<ReturnType<typeof filterToolsList>>
) {
  const metrics = isMetricsEnabled(c);
  for (const { name, reason, hash } of filtered.removed) {
    if (metrics) mcpToolsRemovedTotal.inc({ server: server.id, reason });
    if (reason === 'pin') {
      if (metrics)
        mcpToolDefinitionChangesTotal.inc({
          server: server.id,
          action: 'block',
        });
      console.warn(
        `[PS MCP] ${server.id}: tool ${name} no longer matches its pinned definition (now sha256 ${hash}); removed and blocked`
      );
    }
  }
  for (const { name, hash } of filtered.flagged) {
    if (metrics)
      mcpToolDefinitionChangesTotal.inc({ server: server.id, action: 'flag' });
    console.warn(
      `[PS MCP] ${server.id}: tool ${name} no longer matches its pinned definition (now sha256 ${hash}); flagged`
    );
  }
}
