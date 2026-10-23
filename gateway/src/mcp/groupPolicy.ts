/**
 * PS fork (#63, #77): per-group MCP access under OIDC.
 *
 * A policy group (`PS_OIDC_POLICY`) may carry an `mcp` entry: the MCP
 * servers its users may reach and, per server, tool `allow` / `deny` lists
 * applied on top of the server's own tool policy (`./policy.ts`). A tool
 * must pass both; a group `deny` wins over everything. A group without an
 * `mcp` entry, a user who matched no group (`default: allow`), and a
 * request without an OIDC identity keep the server's policy alone.
 *
 * A refusal records a `policy.denied` security event.
 */
import { Context } from 'hono';
import type { McpGroupToolRule } from '../middlewares/psOidc/config';
import { oidcIdentityOf } from '../middlewares/psOidc/identity';
import { reportPolicyDenied } from '../middlewares/psSecurityEvents';

export type GroupServerAccess =
  /** No group MCP policy applies: the server's policy alone. */
  | { restricted: false }
  /** The group may use this server, with these tool rules. */
  | { restricted: true; rule: McpGroupToolRule }
  /** The group may not use this server. */
  | { restricted: true; rule: undefined };

export function groupServerAccess(
  c: Context,
  serverId: string
): GroupServerAccess {
  const policy = oidcIdentityOf(c)?.group?.mcp;
  if (!policy) return { restricted: false };
  return { restricted: true, rule: policy.servers.get(serverId) };
}

/** Whether the group's tool rule lets `name` through (deny wins). */
export function groupToolAllowed(
  rule: McpGroupToolRule | undefined,
  name: string
): boolean {
  if (!rule) return true;
  if (rule.denyLower?.has(name.toLowerCase())) return false;
  if (rule.allow && !rule.allow.has(name)) return false;
  return true;
}

/** Records the `policy.denied` event for an MCP refusal. */
export function reportMcpPolicyDenied(
  c: Context,
  reason: 'mcp_server_not_allowed' | 'mcp_tool_not_allowed',
  serverId: string,
  tool?: string
): void {
  const identity = oidcIdentityOf(c);
  reportPolicyDenied(c, reason, {
    group: identity?.group?.name,
    userHash:
      !identity || identity.config.telemetrySubject === 'off'
        ? undefined
        : identity.subjectHash,
    mcpServer: serverId,
    mcpTool: tool,
    // A tool refusal is a JSON-RPC error on HTTP 200, like the server's own
    // tool policy; a server refusal is a 403.
    status: reason === 'mcp_tool_not_allowed' ? 200 : 403,
  });
}
