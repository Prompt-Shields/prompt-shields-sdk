/**
 * PS fork (#77): OpenTelemetry spans for MCP proxy requests.
 *
 * One CLIENT span per JSON-RPC request the proxy decided on (forwarded,
 * refused or blocked), named and attributed per the OTel MCP semantic
 * conventions (semantic-conventions-genai `docs/gen-ai/mcp.md`, status
 * Development): `{mcp.method.name} {target}`, `mcp.method.name`,
 * `gen_ai.operation.name` (`execute_tool` for `tools/call`),
 * `gen_ai.tool.name` / `gen_ai.prompt.name`, `jsonrpc.request.id`,
 * `mcp.protocol.version`, `network.transport`, `network.protocol.name`,
 * `server.address` / `server.port`, `error.type` and `rpc.status_code`
 * (the JSON-RPC error code of an error response from the MCP server; for
 * a refusal by the gateway itself, `error.type` is the outcome: `denied`,
 * `blocked`, `invalid`, `upstream_error`).
 *
 * Same opt-in and exporter as the LLM spans (`PS_OTEL_ENDPOINT`, see
 * `../middlewares/psOtel.ts`). Never any content: the spec's opt-in
 * `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` and
 * `gen_ai.prompt.variable` are not emitted whatever
 * `PS_OTEL_CAPTURE_CONTENT` says, and neither are `mcp.resource.uri` (a URI
 * can carry data) or `mcp.session.id` (a session handle). The caller is the
 * same pseudonym the `mcp_tool_call` event carries (`ps.user.hash`,
 * `ps.user.group`).
 */
import { Context } from 'hono';
import {
  OtlpJsonExporter,
  otelConfig,
  parseTraceparent,
  sharedOtlpExporter,
  toOtlpAttributes,
} from '../middlewares/psOtel';
import { getMcpServer } from './config';
import { isValidToolName } from './policy';

/** What the handler learned about one MCP request, for spans and metering. */
export interface McpCallState {
  startMs: number;
  /** The JSON-RPC method of a POST request; unset for GET/DELETE. */
  method?: string;
  id?: string | number | null;
  tool?: string;
  prompt?: string;
  /** Size of the request body, in bytes. */
  requestBytes?: number;
  /** Size of the response message the gateway checked, in bytes. */
  responseBytes?: number;
  /** JSON-RPC error code the gateway answered with, if any. */
  errorCode?: number;
  /** Set once the span is emitted; a request has one span. */
  emitted?: boolean;
}

const states = new WeakMap<Context, McpCallState>();

export function mcpCallState(c: Context, startMs = Date.now()): McpCallState {
  let state = states.get(c);
  if (!state) {
    state = { startMs };
    states.set(c, state);
  }
  return state;
}

export const byteLength = (text: string) =>
  new TextEncoder().encode(text).byteLength;

const METHOD_RE = /^[A-Za-z0-9_./-]{1,64}$/;
const PROTOCOL_VERSION_RE = /^\d{4}-\d{2}-\d{2}$/;

/** A name fit for a span name or attribute: the tool-name rule. */
const safeName = (v: unknown): string | undefined =>
  typeof v === 'string' && isValidToolName(v) ? v : undefined;

export interface McpSpanInput {
  serverId: string;
  serverUrl?: string;
  outcome: string;
  state: McpCallState;
  endMs: number;
  headers: Headers;
  user?: { user_id?: string; user_group?: string };
}

const hex = (bytes: number) => {
  const b = new Uint8Array(bytes);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
};
const nanos = (ms: number) => `${Math.floor(ms)}000000`;

/** One OTLP JSON span for an MCP request, or undefined when not a request. */
export function buildMcpSpan(input: McpSpanInput): any | undefined {
  const { state } = input;
  if (typeof state.method !== 'string' || !METHOD_RE.test(state.method))
    return undefined;
  const method = state.method;
  const tool = method === 'tools/call' ? safeName(state.tool) : undefined;
  const prompt = method === 'prompts/get' ? safeName(state.prompt) : undefined;
  let address: string | undefined;
  let port: number | undefined;
  if (input.serverUrl) {
    try {
      const url = new URL(input.serverUrl);
      address = url.hostname || undefined;
      port = url.port ? Number(url.port) : url.protocol === 'https:' ? 443 : 80;
    } catch {
      /* configured URLs are valid; nothing to report otherwise */
    }
  }
  const version = input.headers.get('mcp-protocol-version') ?? '';
  const failed = input.outcome !== 'ok';
  const errorType = !failed
    ? undefined
    : state.errorCode !== undefined
      ? String(state.errorCode)
      : input.outcome === 'error' && method === 'tools/call'
        ? 'tool_error'
        : input.outcome;
  const id = state.id;
  const attrs = {
    'mcp.method.name': method,
    'gen_ai.operation.name':
      method === 'tools/call' ? 'execute_tool' : undefined,
    'gen_ai.tool.name': tool,
    'gen_ai.prompt.name': prompt,
    'jsonrpc.request.id':
      id === null || id === undefined ? undefined : String(id).slice(0, 128),
    'mcp.protocol.version': PROTOCOL_VERSION_RE.test(version)
      ? version
      : undefined,
    'network.transport': 'tcp',
    'network.protocol.name': 'http',
    'server.address': address,
    'server.port': address ? port : undefined,
    'error.type': errorType,
    'rpc.status_code':
      state.errorCode !== undefined ? String(state.errorCode) : undefined,
    'ps.mcp.server': input.serverId,
    'ps.mcp.outcome': input.outcome,
    'ps.mcp.request.size': state.requestBytes,
    'ps.mcp.response.size': state.responseBytes,
    'ps.user.hash': input.user?.user_id,
    'ps.user.group': input.user?.user_group,
  };
  const parent = parseTraceparent(input.headers.get('traceparent') ?? '');
  // A name the policy refused is client-chosen: kept as an attribute, but
  // out of the span name so it cannot grow span-name cardinality.
  const target =
    input.outcome === 'denied' || input.outcome === 'invalid'
      ? undefined
      : tool ?? prompt;
  const span: any = {
    traceId: parent?.traceId ?? hex(16),
    spanId: hex(8),
    name: target ? `${method} ${target}` : method,
    kind: 3, // SPAN_KIND_CLIENT: the gateway calls the MCP server
    startTimeUnixNano: nanos(state.startMs),
    endTimeUnixNano: nanos(Math.max(input.endMs, state.startMs)),
    attributes: toOtlpAttributes(attrs),
    status: failed ? { code: 2, message: errorType } : { code: 0 },
  };
  if (parent) span.parentSpanId = parent.parentSpanId;
  return span;
}

let exporter: OtlpJsonExporter | null | undefined;

function mcpExporter(): OtlpJsonExporter | null {
  if (exporter === undefined) {
    const config = otelConfig(
      typeof process !== 'undefined' ? process.env : {}
    );
    exporter = config ? sharedOtlpExporter(config) : null;
  }
  return exporter;
}

/** null: export off; undefined: read the environment again. */
export function _setMcpOtelExporterForTests(
  e: OtlpJsonExporter | null | undefined
) {
  exporter = e;
}

/** Emits the request's span once, when OTel export is on. Never throws. */
export function emitMcpSpan(
  c: Context,
  serverId: string,
  outcome: string,
  user?: McpSpanInput['user']
): void {
  try {
    const target = mcpExporter();
    if (!target) return;
    const state = states.get(c);
    if (!state || state.emitted) return;
    const server = getMcpServer(serverId);
    if (!server) return;
    const span = buildMcpSpan({
      serverId,
      serverUrl: server.url,
      outcome,
      state,
      endMs: Date.now(),
      headers: c.req.raw.headers,
      user,
    });
    if (!span) return;
    state.emitted = true;
    let waitUntil: ((p: Promise<unknown>) => void) | undefined;
    try {
      const ctx = c.executionCtx;
      if (ctx?.waitUntil) waitUntil = (p) => ctx.waitUntil(p);
    } catch {
      /* no execution context on Node */
    }
    const environment =
      c.req.header('x-ps-environment')?.slice(0, 64) || undefined;
    target.enqueue({ span, environment }, waitUntil);
  } catch {
    /* telemetry must never affect the call */
  }
}
