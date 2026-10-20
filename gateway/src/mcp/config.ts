/**
 * PS fork (#63, MCP Phase 1): upstream MCP servers the gateway proxies to.
 *
 * Servers come from the `mcp_servers` key of a JSON file: `PS_MCP_CONFIG_FILE`,
 * or `./conf.json` when `FETCH_SETTINGS_FROM_FILE=true` (the gateway's
 * settings file). Node only; nothing is configured on edge runtimes, so the
 * MCP route answers 404 there.
 *
 *   "mcp_servers": {
 *     "github": {
 *       "url": "https://api.githubcopilot.com/mcp/",
 *       "auth": { "type": "bearer", "token_env": "GITHUB_MCP_TOKEN" },
 *       "tools": { "allow": ["search_issues", "get_issue"] },
 *       "pinned_tools": { "search_issues": "<sha256 hex>" },
 *       "on_description_change": "block",
 *       "input_guardrails": [{ "default.regexMatch": { "rule": "...", "not": true } }],
 *       "output_guardrails": [...]
 *     }
 *   }
 *
 * The whole file is rejected (the gateway does not start) when any server is
 * invalid: a half-applied policy is worse than none.
 */
import { plugins } from '../../plugins';
import { convertHooksShorthand } from '../handlers/handlerUtils';
import { HookObject, HookType } from '../middlewares/hooks/types';
import { isSafeUpstreamURL } from '../middlewares/requestValidator';
import { isValidToolName } from './policy';

export type DescriptionChangeAction = 'block' | 'flag';

export interface McpServerConfig {
  /** Config key; also the `/v1/mcp/:server` path segment and metric label. */
  id: string;
  url: string;
  /** Outbound credential header the gateway adds (never the client's). */
  authHeader?: { name: string; value: string };
  tools: {
    mode: 'allow' | 'deny';
    names: Set<string>;
    /** Lower-cased names, for the case-insensitive denylist match. */
    denyLower: Set<string>;
  };
  /** Tool name -> sha256 hex of its definition (see `toolDefinitionHash`). */
  pinnedTools: Map<string, string>;
  onDescriptionChange: DescriptionChangeAction;
  inputGuardrails: HookObject[];
  outputGuardrails: HookObject[];
  /** Upstream must answer with headers within this time. */
  timeoutMs: number;
  maxRequestBytes: number;
  /** Largest JSON response, or SSE event, the gateway inspects. */
  maxResponseBytes: number;
  maxDescriptionLength: number;
}

export const SERVER_ID_RE = /^[a-z0-9][a-z0-9_-]{0,62}$/;
const HEADER_NAME_RE = /^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$/;
// Headers the gateway itself sets on the upstream request.
const RESERVED_HEADERS = new Set([
  'host',
  'content-length',
  'content-type',
  'accept',
  'connection',
  'transfer-encoding',
  'mcp-protocol-version',
  'mcp-session-id',
  'mcp-method',
  'mcp-name',
  'last-event-id',
]);

export const DEFAULTS = {
  timeoutMs: 30_000,
  maxRequestBytes: 1024 * 1024,
  maxResponseBytes: 8 * 1024 * 1024,
  maxDescriptionLength: 2048,
};

export class McpConfigError extends Error {}

const fail = (server: string, message: string): never => {
  throw new McpConfigError(`mcp_servers.${server}: ${message}`);
};

const positiveInt = (
  server: string,
  key: string,
  raw: unknown,
  fallback: number
): number => {
  if (raw === undefined) return fallback;
  if (!Number.isInteger(raw) || (raw as number) <= 0)
    fail(server, `${key} must be a positive integer`);
  return raw as number;
};

const stringList = (server: string, key: string, raw: unknown): string[] => {
  if (!Array.isArray(raw) || raw.some((v) => typeof v !== 'string'))
    fail(server, `${key} must be an array of tool names`);
  for (const name of raw as string[])
    if (!isValidToolName(name))
      fail(server, `${key} has an invalid tool name: ${JSON.stringify(name)}`);
  return raw as string[];
};

function parseAuth(
  id: string,
  raw: any,
  env: Record<string, string | undefined>
): McpServerConfig['authHeader'] {
  if (!raw || typeof raw !== 'object')
    return fail(id, 'auth is required ({"type": "none"} for no credentials)');
  const fromEnv = (key: string) => {
    const name = raw[key];
    if (typeof name !== 'string' || !name)
      return fail(id, `auth.${key} must name an environment variable`);
    const value = env[name]?.trim();
    if (!value) return fail(id, `environment variable ${name} is not set`);
    if (/[\r\n\0]/.test(value))
      return fail(id, `environment variable ${name} is not a header value`);
    return value;
  };
  switch (raw.type) {
    case 'none':
      return undefined;
    case 'bearer':
      return {
        name: 'authorization',
        value: `Bearer ${fromEnv('token_env')}`,
      };
    case 'header': {
      const name = String(raw.header ?? '').toLowerCase();
      if (!HEADER_NAME_RE.test(name) || RESERVED_HEADERS.has(name))
        return fail(id, 'auth.header must be a header name the gateway allows');
      return { name, value: fromEnv('value_env') };
    }
    default:
      // Inline secrets are refused on purpose: conf.json is not a secret store.
      return fail(id, 'auth.type must be "none", "bearer" or "header"');
  }
}

function parseGuardrails(id: string, key: string, raw: unknown): HookObject[] {
  if (raw === undefined) return [];
  if (!Array.isArray(raw)) return fail(id, `${key} must be an array`);
  const hooks: HookObject[] = convertHooksShorthand(
    // convertHooksShorthand mutates its input.
    JSON.parse(JSON.stringify(raw)),
    key.startsWith('input') ? 'input' : 'output',
    HookType.GUARDRAIL
  );
  return hooks.map((hook) => {
    if (!hook.checks?.length) fail(id, `${key}: a guardrail has no checks`);
    for (const check of hook.checks!) {
      const [source, fn, ...rest] = String(check.id).split('.');
      if (rest.length || !(plugins as any)[source]?.[fn])
        fail(id, `${key}: unknown check ${JSON.stringify(check.id)}`);
    }
    return {
      ...hook,
      type: HookType.GUARDRAIL,
      // Fail closed: a failed check blocks the call unless the operator
      // explicitly asks for flag-only, and nothing runs in the background.
      deny: (hook as any).deny !== false,
      async: false,
      eventType: key.startsWith('input')
        ? 'beforeRequestHook'
        : 'afterRequestHook',
    };
  });
}

export function parseServer(
  id: string,
  raw: any,
  env: Record<string, string | undefined>
): McpServerConfig {
  if (!SERVER_ID_RE.test(id))
    fail(id, 'the server id must match [a-z0-9][a-z0-9_-]{0,62}');
  if (!raw || typeof raw !== 'object') fail(id, 'must be an object');

  const url = raw.url;
  if (typeof url !== 'string' || !isSafeUpstreamURL(url))
    fail(id, 'url is missing or not an allowed upstream URL');
  const parsed = new URL(url);
  if (parsed.protocol !== 'https:' && raw.allow_http !== true)
    fail(id, 'url must be https (set "allow_http": true for a local server)');
  if (parsed.hash) fail(id, 'url must not have a fragment');

  const tools = raw.tools;
  if (!tools || typeof tools !== 'object')
    fail(id, 'tools is required: {"allow": [...]} or {"deny": [...]}');
  if ((tools.allow === undefined) === (tools.deny === undefined))
    fail(id, 'tools needs exactly one of "allow" or "deny"');
  const mode = tools.allow !== undefined ? 'allow' : 'deny';
  const names = stringList(id, `tools.${mode}`, tools[mode]);

  const pinned = new Map<string, string>();
  if (raw.pinned_tools !== undefined) {
    if (typeof raw.pinned_tools !== 'object' || Array.isArray(raw.pinned_tools))
      fail(id, 'pinned_tools must map tool names to sha256 hex digests');
    for (const [name, hash] of Object.entries(raw.pinned_tools)) {
      if (!isValidToolName(name) || typeof hash !== 'string')
        fail(
          id,
          `pinned_tools has an invalid entry for ${JSON.stringify(name)}`
        );
      if (!/^[0-9a-f]{64}$/i.test(hash as string))
        fail(id, `pinned_tools.${name} is not a sha256 hex digest`);
      pinned.set(name, (hash as string).toLowerCase());
    }
  }
  const onChange = raw.on_description_change ?? 'block';
  if (onChange !== 'block' && onChange !== 'flag')
    fail(id, 'on_description_change must be "block" or "flag"');

  return {
    id,
    url,
    authHeader: parseAuth(id, raw.auth, env),
    tools: {
      mode,
      names: new Set(names),
      denyLower: new Set(
        mode === 'deny' ? names.map((n) => n.toLowerCase()) : []
      ),
    },
    pinnedTools: pinned,
    onDescriptionChange: onChange,
    inputGuardrails: parseGuardrails(
      id,
      'input_guardrails',
      raw.input_guardrails
    ),
    outputGuardrails: parseGuardrails(
      id,
      'output_guardrails',
      raw.output_guardrails
    ),
    timeoutMs: positiveInt(
      id,
      'timeout_ms',
      raw.timeout_ms,
      DEFAULTS.timeoutMs
    ),
    maxRequestBytes: positiveInt(
      id,
      'max_request_bytes',
      raw.max_request_bytes,
      DEFAULTS.maxRequestBytes
    ),
    maxResponseBytes: positiveInt(
      id,
      'max_response_bytes',
      raw.max_response_bytes,
      DEFAULTS.maxResponseBytes
    ),
    maxDescriptionLength: positiveInt(
      id,
      'max_description_length',
      raw.max_description_length,
      DEFAULTS.maxDescriptionLength
    ),
  };
}

/** Every server in an `mcp_servers` object, or a McpConfigError. */
export function parseMcpServers(
  raw: unknown,
  env: Record<string, string | undefined> = typeof process !== 'undefined'
    ? process.env
    : {}
): Map<string, McpServerConfig> {
  const servers = new Map<string, McpServerConfig>();
  if (raw === undefined || raw === null) return servers;
  if (typeof raw !== 'object' || Array.isArray(raw))
    throw new McpConfigError(
      'mcp_servers must be an object keyed by server id'
    );
  for (const [id, value] of Object.entries(raw as Record<string, unknown>))
    servers.set(id, parseServer(id, value, env));
  return servers;
}

let configured = new Map<string, McpServerConfig>();

export const getMcpServer = (id: string): McpServerConfig | undefined =>
  configured.get(id);

export const mcpServerIds = (): string[] => [...configured.keys()];

export function setMcpServers(servers: Map<string, McpServerConfig>) {
  configured = servers;
}
