/** The MCP proxy route (`/v1/mcp/:server`, #63): not an LLM call. */
export const isMcpProxyPath = (path: string): boolean =>
  path.startsWith('/v1/mcp/');
