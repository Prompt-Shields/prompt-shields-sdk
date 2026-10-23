/**
 * PS fork (#63, MCP Phase 1): tool policy for the MCP proxy.
 *
 * - allowlist / denylist per server, applied to `tools/list` results and
 *   `tools/call` requests;
 * - `Mcp-Name` / `Mcp-Method` headers checked against the body;
 * - tool names, titles and descriptions from the upstream are attacker
 *   controlled: names outside the spec's character set are dropped,
 *   descriptions are stripped of invisible and control characters and capped;
 * - tool definitions pinned by hash: a changed definition is blocked or
 *   flagged, per server;
 * - `Mcp-Param-*` headers (2026-07-28, `x-mcp-header`) checked against the
 *   arguments of a `tools/call`, for tools whose definition the gateway has
 *   seen in a `tools/list`;
 * - an OIDC group's tool rules (`./groupPolicy.ts`) on top of the server's.
 */
import type { McpServerConfig } from './config';

/** The spec's SHOULD for tool names (server/tools, "Tool Names"). */
const TOOL_NAME_RE = /^[A-Za-z0-9_.-]{1,128}$/;
export const isValidToolName = (name: unknown): name is string =>
  typeof name === 'string' && TOOL_NAME_RE.test(name);

/** Shown in logs and errors: a valid tool name, or a fixed placeholder. */
export const displayToolName = (name: unknown): string =>
  isValidToolName(name) ? name : '<invalid tool name>';

// C0/C1 controls (except tab, LF, CR), zero-width and bidi controls, word
// joiners, BOM and Unicode tag characters (U+E0000-U+E007F, invisible text
// used to smuggle instructions into tool descriptions).
const INVISIBLE_RE =
  /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F​-‏‪-‮⁠-⁤⁦-⁩﻿]|\uDB40[\uDC00-\uDC7F]/g;

/** Strip invisible characters and cap at `max` UTF-16 units. */
export function sanitizeText(value: string, max: number): string {
  let text = value.replace(INVISIBLE_RE, '');
  if (text.length > max) {
    text = text.slice(0, max);
    // Do not leave half a surrogate pair.
    if (/[\uD800-\uDBFF]$/.test(text)) text = text.slice(0, -1);
    text += '…';
  }
  return text;
}

const MAX_TITLE_LENGTH = 256;

// ---------------------------------------------------------------------------
// Definition hashes

/** JSON with object keys sorted, so equal definitions hash equally. */
export function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    return JSON.stringify(value) ?? 'null';
  }
  if (Array.isArray(value))
    return `[${value.map((v) => canonicalJson(v)).join(',')}]`;
  const keys = Object.keys(value as object)
    .filter((k) => (value as any)[k] !== undefined)
    .sort();
  return `{${keys
    .map((k) => `${JSON.stringify(k)}:${canonicalJson((value as any)[k])}`)
    .join(',')}}`;
}

/**
 * sha256 hex of the whole tool definition as received from the upstream,
 * before sanitising, except `_meta`: what a model reads (name, title,
 * description, schemas with their parameter descriptions) and what a client
 * acts on (`annotations` such as `readOnlyHint`, `execution`, `icons`, and
 * any field a later revision adds).
 */
export async function toolDefinitionHash(tool: any): Promise<string> {
  const rest: any =
    tool && typeof tool === 'object' && !Array.isArray(tool) ? { ...tool } : {};
  delete rest._meta;
  const definition = canonicalJson(rest);
  const digest = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(definition)
  );
  return [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

/**
 * Pinned tools whose definition was last seen changed, per server. In
 * memory, per process: a call to such a tool is refused in `block` mode
 * until a `tools/list` shows the pinned definition again.
 */
const drifted = new Map<string, Set<string>>();

/**
 * The `x-mcp-header` annotations of each tool, per server, from the last
 * `tools/list` that showed the tool (in memory, per process); `invalid`
 * when the annotations broke the spec's rules.
 */
const paramHeaders = new Map<string, Map<string, ParamHeader[] | 'invalid'>>();

export function _resetPolicyStateForTests() {
  drifted.clear();
  paramHeaders.clear();
}

const driftedSet = (server: string) => {
  let set = drifted.get(server);
  if (!set) drifted.set(server, (set = new Set()));
  return set;
};

// ---------------------------------------------------------------------------
// tools/call

export type ToolDecision =
  | { allowed: true }
  | {
      allowed: false;
      reason: 'invalid_name' | 'policy' | 'pin' | 'invalid_header';
    };

export function toolCallDecision(
  server: McpServerConfig,
  name: unknown
): ToolDecision {
  if (!isValidToolName(name)) return { allowed: false, reason: 'invalid_name' };
  if (!toolAllowedByList(server, name))
    return { allowed: false, reason: 'policy' };
  if (
    server.onDescriptionChange === 'block' &&
    drifted.get(server.id)?.has(name)
  )
    return { allowed: false, reason: 'pin' };
  // Removed from tools/list for a broken x-mcp-header annotation, so not
  // callable either: its Mcp-Param-* headers cannot be checked.
  if (paramHeaders.get(server.id)?.get(name) === 'invalid')
    return { allowed: false, reason: 'invalid_header' };
  return { allowed: true };
}

// ---------------------------------------------------------------------------
// tools/list

export type FilterReason =
  | 'invalid_name'
  | 'policy'
  | 'pin'
  | 'duplicate'
  | 'invalid_header'
  | 'group';

export interface FilteredList {
  result: any;
  /** `hash`: the definition now served, for a tool removed by its pin. */
  removed: { name: string; reason: FilterReason; hash?: string }[];
  /** Pinned tools whose definition changed but were kept (`flag` mode). */
  flagged: { name: string; hash: string }[];
}

/**
 * The `tools/list` result with disallowed, invalid and (in `block` mode)
 * changed tools removed, and the rest sanitised. Never returns a tool the
 * policy does not allow. `groupAllows` is the caller's OIDC group tool rule
 * (`./groupPolicy.ts`); it is applied last, so the pin and `x-mcp-header`
 * state, which is per server, does not depend on who listed the tools.
 */
export async function filterToolsList(
  server: McpServerConfig,
  result: any,
  groupAllows: (name: string) => boolean = () => true
): Promise<FilteredList> {
  const removed: FilteredList['removed'] = [];
  const flagged: FilteredList['flagged'] = [];
  if (!result || typeof result !== 'object' || !Array.isArray(result.tools)) {
    // Fail closed: nothing the gateway could not read as a tool list.
    const base = result && typeof result === 'object' ? result : {};
    return { result: { ...base, tools: [] }, removed, flagged };
  }
  const seen = new Set<string>();
  const kept: any[] = [];
  const drift = driftedSet(server.id);
  let headers = paramHeaders.get(server.id);
  if (!headers) paramHeaders.set(server.id, (headers = new Map()));
  for (const tool of result.tools) {
    const name = tool?.name;
    if (!isValidToolName(name) || !tool || typeof tool !== 'object') {
      removed.push({ name: displayToolName(name), reason: 'invalid_name' });
      continue;
    }
    if (seen.has(name)) {
      // Two definitions under one name: which one a client uses is unknown.
      removed.push({ name, reason: 'duplicate' });
      continue;
    }
    seen.add(name);
    if (!toolAllowedByList(server, name)) {
      removed.push({ name, reason: 'policy' });
      continue;
    }
    const pin = server.pinnedTools.get(name);
    if (pin) {
      const hash = await toolDefinitionHash(tool);
      if (hash !== pin) {
        drift.add(name);
        if (server.onDescriptionChange === 'block') {
          removed.push({ name, reason: 'pin', hash });
          continue;
        }
        flagged.push({ name, hash });
      } else {
        drift.delete(name);
      }
    }
    const annotations = paramHeadersOf(tool.inputSchema);
    if (annotations === 'invalid') {
      headers.set(name, 'invalid');
      removed.push({ name, reason: 'invalid_header' });
      continue;
    }
    if (annotations.length) headers.set(name, annotations);
    else headers.delete(name);
    if (!groupAllows(name)) {
      removed.push({ name, reason: 'group' });
      continue;
    }
    const clean: any = { ...tool };
    if (typeof clean.description === 'string')
      clean.description = sanitizeText(
        clean.description,
        server.maxDescriptionLength
      );
    else delete clean.description;
    if (typeof clean.title === 'string')
      clean.title = sanitizeText(clean.title, MAX_TITLE_LENGTH);
    else delete clean.title;
    // Parameter descriptions are read by the model too.
    for (const key of ['inputSchema', 'outputSchema'])
      if (clean[key] !== undefined)
        clean[key] = sanitizeSchema(clean[key], server.maxDescriptionLength);
    kept.push(clean);
  }
  return { result: { ...result, tools: kept }, removed, flagged };
}

/**
 * `description` and `title` strings anywhere in a JSON Schema, sanitised like
 * the tool's own. Other keywords (enums, patterns, defaults) are unchanged.
 */
function sanitizeSchema(value: any, max: number, depth = 0): any {
  if (depth > 64) return value;
  if (Array.isArray(value))
    return value.map((v) => sanitizeSchema(v, max, depth + 1));
  if (!value || typeof value !== 'object') return value;
  // No prototype: a `__proto__` key stays an ordinary key.
  const out: any = Object.create(null);
  for (const [key, v] of Object.entries(value)) {
    out[key] =
      (key === 'description' || key === 'title') && typeof v === 'string'
        ? sanitizeText(v, max)
        : sanitizeSchema(v, max, depth + 1);
  }
  return out;
}

// ---------------------------------------------------------------------------
// x-mcp-header annotations (2026-07-28 Streamable HTTP, "Custom Headers from
// Tool Parameters")

export interface ParamHeader {
  /** The name part of `Mcp-Param-{name}`, lower-cased. */
  name: string;
  /** The chain of `properties` keys from the schema root. */
  path: string[];
}

// RFC 9110 token (tchar).
const TOKEN_RE = /^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$/;
const HEADER_TYPES = new Set(['string', 'integer', 'boolean']);
// Keywords whose values are data, not schemas.
const DATA_KEYWORDS = new Set([
  'const',
  'default',
  'enum',
  'examples',
  'example',
]);
const MAX_PARAM_HEADERS = 64;

const isPlainObject = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === 'object' && !Array.isArray(v);

/**
 * The `x-mcp-header` annotations in a tool's `inputSchema`, or `invalid` when
 * one breaks the spec's rules: a header name that is not a token or is used
 * twice (case-insensitively), a non-primitive parameter type, or an
 * annotation that is not reached from the root through `properties` keys
 * alone. Clients must drop such a tool from `tools/list`; so does the gateway.
 */
export function paramHeadersOf(schema: unknown): ParamHeader[] | 'invalid' {
  const found: ParamHeader[] = [];
  const seen = new Set<string>();
  let invalid = false;
  const visit = (
    node: unknown,
    path: string[],
    reachable: boolean,
    depth: number
  ) => {
    if (invalid) return;
    if (depth > 64) {
      invalid = true;
      return;
    }
    if (Array.isArray(node)) {
      for (const item of node) visit(item, path, false, depth + 1);
      return;
    }
    if (!isPlainObject(node)) return;
    if (Object.hasOwn(node, 'x-mcp-header')) {
      const name = node['x-mcp-header'];
      if (
        !reachable ||
        !path.length ||
        typeof name !== 'string' ||
        !TOKEN_RE.test(name) ||
        seen.has(name.toLowerCase()) ||
        typeof node.type !== 'string' ||
        !HEADER_TYPES.has(node.type) ||
        found.length >= MAX_PARAM_HEADERS
      ) {
        invalid = true;
        return;
      }
      seen.add(name.toLowerCase());
      found.push({ name: name.toLowerCase(), path });
    }
    for (const [key, value] of Object.entries(node)) {
      if (key === 'x-mcp-header' || DATA_KEYWORDS.has(key)) continue;
      if (key === 'properties' && isPlainObject(value)) {
        for (const [prop, sub] of Object.entries(value))
          visit(sub, [...path, prop], reachable, depth + 1);
      } else {
        visit(value, path, false, depth + 1);
      }
    }
  };
  visit(schema, [], true, 0);
  return invalid ? 'invalid' : found;
}

const INTEGER_TEXT_RE = /^[+-]?\d+(\.\d+)?([eE][+-]?\d+)?$/;

/** Whether a decoded header value stands for the body value. */
function headerMatchesValue(header: string, value: unknown): boolean {
  if (typeof value === 'string') return header === value;
  if (typeof value === 'boolean') return header === String(value);
  if (typeof value === 'number' && Number.isInteger(value))
    // Compared as numbers: `42.0` and `42` are equal.
    return INTEGER_TEXT_RE.test(header) && Number(header) === value;
  return false;
}

function valueAt(args: unknown, path: string[]): unknown {
  let value: unknown = args;
  for (const key of path) {
    if (!isPlainObject(value) || !Object.hasOwn(value, key)) return undefined;
    value = value[key];
  }
  return value;
}

/**
 * Why the `Mcp-Param-*` headers of a `tools/call` do not match its
 * arguments, or undefined when they do. Only headers the tool's definition
 * declares (seen in a `tools/list`) are recognised; others are forwarded
 * unchecked, as the spec asks of intermediaries. A recognised header must be
 * well formed and equal its argument after decoding; it must be absent when
 * the argument is absent or null, and present (from 2026-07-28 on) when the
 * argument has a value.
 */
export function paramHeaderMismatch(
  server: McpServerConfig,
  headers: Headers,
  message: any
): string | undefined {
  const declared = paramHeaders.get(server.id)?.get(message?.params?.name);
  if (!declared || declared === 'invalid') return undefined;
  const version = headers.get('mcp-protocol-version');
  const required = version !== null && version >= HEADERS_REQUIRED_FROM;
  for (const { name, path } of declared) {
    const label = `Mcp-Param-${name}`;
    const raw = headers.get(`mcp-param-${name}`);
    const value = valueAt(message.params?.arguments, path);
    if (value === undefined || value === null) {
      if (raw !== null) return `${label} header has no value in the body`;
      continue;
    }
    if (raw === null) {
      if (required) return `${label} header is missing`;
      continue;
    }
    const decoded = decodeHeaderValue(raw);
    if (decoded === undefined) return `${label} header is malformed`;
    if (!headerMatchesValue(decoded, value))
      return `${label} header does not match the body`;
  }
  return undefined;
}

function toolAllowedByList(server: McpServerConfig, name: string) {
  if (server.tools.mode === 'allow') return server.tools.names.has(name);
  // A denylist matches case-insensitively: an upstream that resolves tool
  // names without regard to case must not reach a denied tool as `Delete`.
  return !server.tools.denyLower.has(name.toLowerCase());
}

// ---------------------------------------------------------------------------
// Request metadata headers (2026-07-28 Streamable HTTP, "Request Metadata")

/** The first revision that requires Mcp-Method / Mcp-Name. */
export const HEADERS_REQUIRED_FROM = '2026-07-28';
export const PROTOCOL_VERSION_RE = /^\d{4}-\d{2}-\d{2}$/;
/** Methods whose `params.name` / `params.uri` is mirrored in Mcp-Name. */
const NAMED_METHODS: Record<string, 'name' | 'uri'> = {
  'tools/call': 'name',
  'prompts/get': 'name',
  'resources/read': 'uri',
};

const SENTINEL_RE = /^=\?base64\?([A-Za-z0-9+/]*={0,2})\?=$/;
// RFC 9110 field-value characters: visible ASCII, space and tab.
const HEADER_VALUE_RE = /^[\x20-\x7E\t]*$/;

/** A header value decoded from the `=?base64?...?=` sentinel, or undefined when invalid. */
export function decodeHeaderValue(raw: string): string | undefined {
  if (!HEADER_VALUE_RE.test(raw)) return undefined;
  const m = SENTINEL_RE.exec(raw);
  if (!m) {
    // Shaped like the sentinel but not valid base64: malformed, not literal.
    return raw.startsWith('=?base64?') && raw.endsWith('?=') ? undefined : raw;
  }
  try {
    const bytes = Uint8Array.from(atob(m[1]), (ch) => ch.charCodeAt(0));
    return new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(
      bytes
    );
  } catch {
    return undefined;
  }
}

/**
 * Why the mirrored headers do not match the body, or undefined when they do.
 * From 2026-07-28 on, the headers are required; for older revisions they are
 * checked when present (a client may send them early).
 */
export function headerMismatch(
  headers: Headers,
  message: any
): string | undefined {
  const version = headers.get('mcp-protocol-version');
  if (version !== null && !PROTOCOL_VERSION_RE.test(version))
    return 'MCP-Protocol-Version is malformed';
  const required = version !== null && version >= HEADERS_REQUIRED_FROM;
  const bodyVersion =
    message?.params?._meta?.['io.modelcontextprotocol/protocolVersion'];
  if (required && bodyVersion !== undefined && bodyVersion !== version)
    return 'MCP-Protocol-Version does not match the body';

  const method = headers.get('mcp-method');
  if (method === null) {
    if (required) return 'Mcp-Method header is missing';
  } else if (method !== message.method) {
    return 'Mcp-Method header does not match the body';
  }

  const field = NAMED_METHODS[message.method];
  const rawName = headers.get('mcp-name');
  if (!field) {
    return rawName !== null
      ? 'Mcp-Name header is not expected for this method'
      : undefined;
  }
  if (rawName === null)
    return required ? 'Mcp-Name header is missing' : undefined;
  const name = decodeHeaderValue(rawName);
  if (name === undefined) return 'Mcp-Name header is malformed';
  if (name !== message.params?.[field])
    return 'Mcp-Name header does not match the body';
  return undefined;
}
