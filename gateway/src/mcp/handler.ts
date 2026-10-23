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
 *    description pin, and the OIDC group's MCP policy (`./groupPolicy.ts`),
 *    its `Mcp-Param-*` headers must match its arguments, then the input
 *    guardrails run on its arguments and MRTR `inputResponses`;
 * 4. the request goes to the configured URL only, with the server's own
 *    credentials (the client's Authorization header is never forwarded), the
 *    URL re-checked by `isSafeUpstreamURL`, and redirects refused;
 * 5. `tools/list` results are filtered and sanitised; `tools/call` results go
 *    through the output guardrails. SSE responses stream: only the event that
 *    carries the response is held while it is checked.
 * 6. A response reaches the client only where it was checked: the response to
 *    the request itself (same `id`) on its own POST. Responses with another
 *    `id`, responses on the GET stream (stream resumption would replay them),
 *    HTTP error bodies other than a JSON-RPC error, and the bodies of
 *    notification acknowledgements are never released. `tasks/*` (results
 *    fetched later, by task id) is refused.
 *
 * The body forwarded upstream is the JSON the gateway checked, re-serialised,
 * so the upstream cannot parse a different message (duplicate keys).
 */
import { Context } from 'hono';
import { isSafeUpstreamURL } from '../middlewares/requestValidator';
import { upstreamFetch } from '../utils/fetch';
import { checkMcpClient } from './auth';
import { getMcpServer, McpServerConfig } from './config';
import {
  groupServerAccess,
  groupToolAllowed,
  reportMcpPolicyDenied,
} from './groupPolicy';
import {
  mcpGuardrailsBlock,
  toolCallInputText,
  toolResultText,
} from './guardrails';
import {
  displayToolName,
  filterToolsList,
  headerMismatch,
  paramHeaderMismatch,
  toolCallDecision,
} from './policy';
import { filterSseStream, isResponseTo } from './sse';
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
  /** A tool outside the policy, or a method that would bypass it (`tasks/*`). */
  TOOL_DENIED: -32010,
  GUARDRAIL_BLOCKED: -32011,
  TOOL_DEFINITION_CHANGED: -32012,
  UPSTREAM_ERROR: -32013,
  UNAUTHORIZED: -32014,
  UNKNOWN_SERVER: -32015,
  /** The caller's OIDC group may not use this server. */
  SERVER_DENIED: -32016,
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

/**
 * Body text, or undefined once it is larger than `max` bytes or, with
 * `timeoutMs`, once reading it takes longer than that.
 */
async function readLimited(
  body: ReadableStream<Uint8Array> | null,
  max: number,
  timeoutMs?: number
): Promise<string | undefined> {
  if (!body) return '';
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  let timedOut = false;
  const timer =
    timeoutMs === undefined
      ? undefined
      : setTimeout(() => {
          timedOut = true;
          reader.cancel().catch(() => {});
        }, timeoutMs);
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (timedOut) return undefined;
      if (done) break;
      size += value.byteLength;
      if (size > max) {
        reader.cancel().catch(() => {});
        return undefined;
      }
      chunks.push(value);
    }
  } catch {
    return undefined;
  } finally {
    clearTimeout(timer);
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

/** The upstream's status and headers, without its body. */
function withoutBody(upstream: Response): Response {
  upstream.body?.cancel().catch(() => {});
  const headers = clientHeaders(upstream, false);
  headers.delete('content-type');
  return new Response(null, { status: upstream.status, headers });
}

const isErrorObject = (e: any) =>
  !!e &&
  typeof e === 'object' &&
  Number.isInteger(e.code) &&
  typeof e.message === 'string';

/**
 * The JSON-RPC error in the body of an upstream HTTP error, re-serialised
 * with the request's id; undefined when the body is anything else.
 */
async function upstreamErrorMessage(
  upstream: Response,
  server: McpServerConfig,
  id: RpcId
): Promise<any | undefined> {
  if (contentTypeOf(upstream) !== JSON_TYPE) {
    upstream.body?.cancel().catch(() => {});
    return undefined;
  }
  const raw = await readLimited(
    upstream.body,
    server.maxResponseBytes,
    server.timeoutMs
  );
  let parsed: any;
  try {
    parsed = raw === undefined ? undefined : JSON.parse(raw);
  } catch {
    return undefined;
  }
  if (
    !parsed ||
    typeof parsed !== 'object' ||
    'result' in parsed ||
    !isErrorObject(parsed.error) ||
    (parsed.id !== id && parsed.id !== null)
  )
    return undefined;
  const { code, message, data } = parsed.error;
  return {
    jsonrpc: '2.0',
    id,
    error: { code, message, ...(data === undefined ? {} : { data }) },
  };
}

const jsonResponse = (message: unknown, status: number, upstream: Response) =>
  new Response(JSON.stringify(message), {
    status,
    headers: clientHeaders(upstream, false),
  });

export async function mcpHandler(c: Context): Promise<Response> {
  const started = Date.now();
  const serverId = c.req.param('server') ?? '';
  const auth = checkMcpClient(c);
  if (!auth.ok) {
    recordRequest(c, 'unknown', undefined, 'invalid');
    return rpcError(auth.status, null, MCP_ERRORS.UNAUTHORIZED, auth.message);
  }
  // Before the server lookup, so a group cannot probe which ids exist.
  const access = groupServerAccess(c, serverId);
  if (access.restricted && !access.rule) {
    recordRequest(c, 'unknown', undefined, 'denied');
    reportMcpPolicyDenied(c, 'mcp_server_not_allowed', serverId);
    return rpcError(
      403,
      null,
      MCP_ERRORS.SERVER_DENIED,
      'Forbidden by gateway policy: this MCP server is not allowed for your group'
    );
  }
  const groupRule = access.restricted ? access.rule : undefined;
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
    const rejected = credentialRejection(upstream, null);
    if (rejected) return rejected;
    if (
      c.req.method === 'GET' &&
      upstream.ok &&
      upstream.body &&
      contentTypeOf(upstream) === SSE_TYPE
    ) {
      // Requests and notifications only: no response is released here.
      return new Response(
        filterSseStream(upstream.body, {
          maxEventBytes: server.maxResponseBytes,
        }),
        { status: upstream.status, headers: clientHeaders(upstream, true) }
      );
    }
    return withoutBody(upstream);
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
    const mismatch =
      headerMismatch(c.req.raw.headers, message) ??
      (method === 'tools/call'
        ? paramHeaderMismatch(server, c.req.raw.headers, message)
        : undefined);
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

  // ---- Tasks: a result fetched later by task id is never checked ----
  const isToolCall = method === 'tools/call';
  if (
    (typeof method === 'string' && method.startsWith('tasks/')) ||
    (isToolCall && message.params?.task !== undefined)
  ) {
    recordRequest(c, server.id, method, 'denied');
    return rpcError(
      200,
      id,
      MCP_ERRORS.TOOL_DENIED,
      'Tasks are not supported by the MCP proxy'
    );
  }

  // ---- tools/call: policy and input guardrails ----
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
          : decision.reason === 'invalid_header'
            ? `Tool ${displayToolName(toolName)} has an invalid x-mcp-header annotation`
            : `Tool ${displayToolName(toolName)} is not allowed on this server`
      );
    }
    if (!groupToolAllowed(groupRule, toolName)) {
      recordRequest(c, server.id, method, 'denied');
      reportMcpPolicyDenied(c, 'mcp_tool_not_allowed', server.id, toolName);
      return rpcError(
        200,
        id,
        MCP_ERRORS.TOOL_DENIED,
        `Tool ${displayToolName(toolName)} is not allowed for your group`
      );
    }
    if (
      await mcpGuardrailsBlock(
        c,
        server.inputGuardrails,
        'input',
        toolCallInputText(message.params)
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

  // A notification or a client's response: the upstream only acknowledges it.
  // (A request's id was checked above, so `id === null` means no request.)
  if (method === undefined || id === null) {
    finish(upstream.ok ? 'ok' : 'error');
    return withoutBody(upstream);
  }
  if (upstream.status === 202) {
    // A request needs a response; a 202 has none.
    upstream.body?.cancel().catch(() => {});
    finish('upstream_error');
    return rpcError(
      502,
      id,
      MCP_ERRORS.UPSTREAM_ERROR,
      'The MCP server sent no response'
    );
  }

  const inspect = method === 'tools/list' || isToolCall;

  // The response to tools/list or tools/call, checked before it is released.
  const onResponse = async (response: any): Promise<any | undefined> => {
    if (method === 'tools/list') {
      if (response.error) {
        finish('error');
        return undefined;
      }
      const filtered = await filterToolsList(server, response.result, (name) =>
        groupToolAllowed(groupRule, name)
      );
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

  // An HTTP error: only a JSON-RPC error in its body reaches the client,
  // checked like a response. The status is kept: 404 ends a 2025-11-25
  // session, 400 carries version negotiation.
  if (!upstream.ok) {
    const error = await upstreamErrorMessage(upstream, server, id);
    if (!error) {
      finish('error');
      return rpcError(
        upstream.status,
        id,
        MCP_ERRORS.UPSTREAM_ERROR,
        `The MCP server answered with HTTP ${upstream.status}`
      );
    }
    const replacement = inspect ? await onResponse(error) : undefined;
    if (!inspect) finish('error');
    return jsonResponse(replacement ?? error, upstream.status, upstream);
  }

  const type = contentTypeOf(upstream);
  if (type === SSE_TYPE && upstream.body) {
    let answered = false;
    if (!inspect) finish('ok');
    const body = filterSseStream(upstream.body, {
      requestId: id,
      maxEventBytes: server.maxResponseBytes,
      onResponse: inspect
        ? async (response) => {
            answered = true;
            return onResponse(response);
          }
        : undefined,
      tooLarge: () => {
        if (inspect && !answered) finish('upstream_error');
        answered = true;
        return errorMessage(
          id,
          MCP_ERRORS.UPSTREAM_ERROR,
          'The MCP server response is too large'
        );
      },
      onEnd: () => {
        if (inspect && !answered) finish('upstream_error');
      },
    });
    return new Response(body, {
      status: upstream.status,
      headers: clientHeaders(upstream, true),
    });
  }
  if (type === JSON_TYPE) {
    const raw = await readLimited(
      upstream.body,
      server.maxResponseBytes,
      server.timeoutMs
    );
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
          ? 'The MCP server response is too large or too slow'
          : 'The MCP server sent an invalid response'
      );
    }
    const replacement = inspect ? await onResponse(response) : undefined;
    if (!inspect) finish(response.error ? 'error' : 'ok');
    return jsonResponse(replacement ?? response, upstream.status, upstream);
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
