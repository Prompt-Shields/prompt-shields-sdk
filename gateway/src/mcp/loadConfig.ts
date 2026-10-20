/**
 * PS fork (#63): Node-only loader for the MCP proxy's `mcp_servers` (kept out
 * of ./config.ts so edge bundles never import `fs`).
 */
import { McpConfigError, parseMcpServers, setMcpServers } from './config';

/**
 * Node startup: load `mcp_servers` from `PS_MCP_CONFIG_FILE`, else from
 * `./conf.json` when `FETCH_SETTINGS_FROM_FILE=true`. Throws on a bad file.
 */
export async function loadMcpServersFromEnv(
  env: Record<string, string | undefined> = process.env
): Promise<number> {
  const path =
    env.PS_MCP_CONFIG_FILE ||
    (env.FETCH_SETTINGS_FROM_FILE === 'true' ? './conf.json' : undefined);
  if (!path) return 0;
  const { readFile } = await import('fs/promises');
  let text: string;
  try {
    text = await readFile(path, 'utf-8');
  } catch (err) {
    // conf.json is optional for the LLM routes; an explicit MCP file is not.
    if (!env.PS_MCP_CONFIG_FILE && (err as any)?.code === 'ENOENT') return 0;
    throw new McpConfigError(`cannot read ${path}`);
  }
  let json: any;
  try {
    json = JSON.parse(text);
  } catch {
    throw new McpConfigError(`${path} is not valid JSON`);
  }
  const servers = parseMcpServers(json?.mcp_servers, env);
  setMcpServers(servers);
  return servers.size;
}
