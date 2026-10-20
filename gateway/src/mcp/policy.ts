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
 *   flagged, per server.
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
 * sha256 hex of what a model reads about a tool: name, title, description
 * and input/output schemas (parameter descriptions included), as received
 * from the upstream, before sanitising.
 */
export async function toolDefinitionHash(tool: any): Promise<string> {
  const definition = canonicalJson({
    name: tool?.name,
    title: tool?.title,
    description: tool?.description,
    inputSchema: tool?.inputSchema,
    outputSchema: tool?.outputSchema,
  });
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

export function _resetPolicyStateForTests() {
  drifted.clear();
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
  | { allowed: false; reason: 'invalid_name' | 'policy' | 'pin' };

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
  return { allowed: true };
}

// ---------------------------------------------------------------------------
// tools/list

export type FilterReason = 'invalid_name' | 'policy' | 'pin' | 'duplicate';

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
 * policy does not allow.
 */
export async function filterToolsList(
  server: McpServerConfig,
  result: any
): Promise<FilteredList> {
  const removed: FilteredList['removed'] = [];
  const flagged: FilteredList['flagged'] = [];
  if (!result || typeof result !== 'object' || !Array.isArray(result.tools)) {
    return { result, removed, flagged };
  }
  const seen = new Set<string>();
  const kept: any[] = [];
  const drift = driftedSet(server.id);
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
    kept.push(clean);
  }
  return { result: { ...result, tools: kept }, removed, flagged };
}

function toolAllowedByList(server: McpServerConfig, name: string) {
  const listed = server.tools.names.has(name);
  return server.tools.mode === 'allow' ? listed : !listed;
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
