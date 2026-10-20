/**
 * PS fork (#63, MCP Phase 1): client authentication for the MCP proxy.
 *
 * The gateway's own routes (admin console, /metrics) authenticate with a
 * static bearer token from the environment; the MCP route does the same with
 * `PS_MCP_TOKEN` (at least 16 characters). The proxy adds the gateway's own
 * upstream credentials, so it is never open by default: without a token it
 * refuses every request, unless `PS_MCP_ALLOW_UNAUTHENTICATED=true` says an
 * authenticating proxy (e.g. Kong) sits in front.
 *
 * The client's Authorization header is checked here and never forwarded.
 * Inbound OAuth 2.1 (RFC 9728 protected-resource metadata) is a follow-up.
 */
import { Context } from 'hono';
import { env } from 'hono/adapter';
import { reportAuthFailure } from '../middlewares/psSecurityEvents';

export const MCP_TOKEN_ENV = 'PS_MCP_TOKEN';
export const MCP_ALLOW_UNAUTHENTICATED_ENV = 'PS_MCP_ALLOW_UNAUTHENTICATED';
const MIN_TOKEN_LENGTH = 16;

// Constant-time comparison so the token cannot be recovered via timing.
const tokensMatch = (candidate: string, expected: string): boolean => {
  const a = new TextEncoder().encode(candidate);
  const b = new TextEncoder().encode(expected);
  let diff = a.length ^ b.length;
  for (let i = 0; i < b.length; i++) diff |= (a[i] ?? 0) ^ b[i];
  return diff === 0;
};

export type McpAuthResult =
  | { ok: true }
  | { ok: false; status: 401 | 503; message: string };

export function checkMcpClient(c: Context): McpAuthResult {
  const vars = env<Record<string, string | undefined>>(c);
  const token = vars[MCP_TOKEN_ENV]?.trim() ?? '';
  if (token.length < MIN_TOKEN_LENGTH) {
    if (vars[MCP_ALLOW_UNAUTHENTICATED_ENV] === 'true') return { ok: true };
    return {
      ok: false,
      status: 503,
      message: `The MCP proxy needs ${MCP_TOKEN_ENV} (at least ${MIN_TOKEN_LENGTH} characters).`,
    };
  }
  const header = c.req.header('authorization')?.trim() ?? '';
  const [scheme, value] = header.split(/\s+/, 2);
  if (scheme?.toLowerCase() === 'bearer' && value && tokensMatch(value, token))
    return { ok: true };
  reportAuthFailure(
    c,
    'mcp',
    header ? 'invalid_credentials' : 'missing_credentials'
  );
  return {
    ok: false,
    status: 401,
    message:
      'MCP proxy authentication required: Authorization: Bearer <token>.',
  };
}
