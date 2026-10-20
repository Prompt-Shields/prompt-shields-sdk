/**
 * PS fork (#63, MCP Phase 1): MCP proxy audit and metrics.
 *
 * - One `mcp_tool_call` event per `tools/call` that passed the tool policy,
 *   posted to the collector's `POST /ingest/mcp` (#214) with `source:
 *   "gateway"`. Names, outcome and latency only: never arguments, results,
 *   descriptions, URLs or headers. Same opt-in, delivery and loss accounting
 *   as the LLM telemetry (`PS_API_KEY`, `PS_COLLECTOR_URL`).
 * - Prometheus metrics (`PS_METRICS_ENABLED`). Labels are bounded: `server`
 *   is a configured server id, `method` one of a fixed list, `tool` a tool
 *   name the policy allowed (capped per process), the rest fixed sets.
 */
import { Context } from 'hono';
import { isMetricsEnabled, labelValue } from '../middlewares/psMetrics/labels';
import {
  Counter,
  Histogram,
  defaultRegistry,
} from '../middlewares/psMetrics/registry';
import { PSTelemetrySender } from '../middlewares/psTelemetrySender';

export type McpOutcome =
  /** Forwarded; the upstream answered with a result. */
  | 'ok'
  /** Forwarded; a JSON-RPC error, `isError: true`, or an HTTP error. */
  | 'error'
  /** Refused by the tool policy (allowlist, denylist, invalid name, pin). */
  | 'denied'
  /** Blocked by a guardrail, on the arguments or on the result. */
  | 'blocked'
  /** Refused before policy: malformed body or header mismatch. */
  | 'invalid'
  /** The upstream could not be reached or answered unusably. */
  | 'upstream_error';

const METHODS = new Set([
  'initialize',
  'notifications/initialized',
  'server/discover',
  'ping',
  'tools/list',
  'tools/call',
  'resources/list',
  'resources/read',
  'resources/templates/list',
  'prompts/list',
  'prompts/get',
  'completion/complete',
  'subscriptions/listen',
]);

export const methodLabel = (method: unknown): string =>
  typeof method === 'string' && METHODS.has(method)
    ? method
    : typeof method === 'string' && method.startsWith('notifications/')
      ? 'notification'
      : 'other';

export const MAX_TOOL_LABELS = 200;
const toolLabels = new Set<string>();
export const toolLabel = (name: string): string => {
  if (toolLabels.has(name)) return name;
  if (toolLabels.size >= MAX_TOOL_LABELS) return 'other';
  toolLabels.add(name);
  return name;
};

export const mcpRequestsTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_mcp_requests_total',
    'MCP proxy requests by server, JSON-RPC method (fixed list, else notification or other) and outcome.',
    ['server', 'method', 'outcome']
  )
);
export const mcpToolCallsTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_mcp_tool_calls_total',
    'MCP tools/call requests that passed the tool policy, by server, tool and outcome (ok, error, blocked, upstream_error).',
    ['server', 'tool', 'outcome']
  )
);
export const mcpToolCallDuration = defaultRegistry.register(
  new Histogram(
    'ps_gateway_mcp_tool_call_duration_seconds',
    'MCP tools/call duration through the proxy, guardrails included, by server and outcome.',
    ['server', 'outcome']
  )
);
export const mcpToolsRemovedTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_mcp_tools_removed_total',
    'Tools removed from tools/list results, by server and reason (policy, invalid_name, duplicate, pin).',
    ['server', 'reason']
  )
);
export const mcpToolDefinitionChangesTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_mcp_tool_definition_changes_total',
    'Pinned MCP tools seen with a changed definition, by server and action (block, flag).',
    ['server', 'action']
  )
);

/** Sender for /ingest/mcp: its own client id and loss counters. */
export const psMcpTelemetrySender = PSTelemetrySender.fromEnv(
  typeof process !== 'undefined' ? process.env : {},
  '/ingest/mcp'
);

export function _resetMcpTelemetryForTests() {
  toolLabels.clear();
}

export interface ToolCallRecord {
  server: string;
  tool: string;
  outcome: 'ok' | 'error' | 'blocked' | 'upstream_error';
  latencyMs: number;
}

/** Metrics plus the collector event for one tools/call. Never throws. */
export function recordToolCall(
  c: Context,
  record: ToolCallRecord,
  sender: PSTelemetrySender = psMcpTelemetrySender
): void {
  try {
    if (isMetricsEnabled(c)) {
      mcpToolCallsTotal.inc({
        server: record.server,
        tool: toolLabel(record.tool),
        outcome: record.outcome,
      });
      mcpToolCallDuration.observe(
        { server: record.server, outcome: record.outcome },
        record.latencyMs / 1000
      );
    }
    const environment = labelValue(c.req.header('x-ps-environment'), '');
    const callingService = c.req.header('x-ps-calling-service');
    const event = {
      source: 'gateway',
      server_name: record.server,
      tool_name: record.tool,
      // The collector stores ok/error; a blocked call is an error there.
      outcome: record.outcome === 'ok' ? 'ok' : 'error',
      latency_ms: Math.max(0, Math.round(record.latencyMs)),
      transport: 'streamable-http',
      ...(environment && environment !== 'other' ? { environment } : {}),
      ...(callingService
        ? { calling_service: callingService.slice(0, 200) }
        : {}),
    };
    const sent = sender.send(event).catch(() => {});
    try {
      c.executionCtx?.waitUntil?.(sent);
    } catch {
      /* Node: no execution context; the sender is flushed at shutdown */
    }
  } catch {
    /* telemetry must never affect the call */
  }
}

export function recordRequest(
  c: Context,
  server: string,
  method: unknown,
  outcome: McpOutcome
) {
  if (!isMetricsEnabled(c)) return;
  mcpRequestsTotal.inc({ server, method: methodLabel(method), outcome });
}
