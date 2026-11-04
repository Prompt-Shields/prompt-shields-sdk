import { Context } from 'hono';
import { env } from 'hono/adapter';
import { getValueOrFileContents } from '../../utils/env';

/**
 * PS gateway OIDC/JWT authentication and group policy: configuration.
 *
 * Everything is read from the environment (Workers: bindings) on each
 * request and parsed once per distinct value. An invalid configuration does
 * not fall back to "open": protected routes answer 500 until it is fixed.
 */

/** Asymmetric JWS algorithms. HS* (shared secret) and `none` are never accepted. */
export const ASYMMETRIC_ALGORITHMS = [
  'RS256',
  'RS384',
  'RS512',
  'PS256',
  'PS384',
  'PS512',
  'ES256',
  'ES384',
  'ES512',
  'EdDSA',
] as const;

export const DEFAULT_ALGORITHMS = ASYMMETRIC_ALGORITHMS.filter(
  (a) => a !== 'EdDSA'
);
export const DEFAULT_CLOCK_SKEW_SECONDS = 60;
export const MAX_CLOCK_SKEW_SECONDS = 300;
export const DEFAULT_GROUPS_CLAIM = 'groups';

export type TelemetrySubjectMode = 'hash' | 'raw' | 'off';

export interface RateLimitRule {
  requestsPerMinute: number;
  /** `user`: each user in the group has the budget; `group`: shared by all. */
  per: 'user' | 'group';
}

export interface PolicyGroup {
  /** Label used in telemetry, events and logs. Not secret, not PII. */
  name: string;
  /** IdP group/role values (names or Entra object-id GUIDs) that select this entry. */
  match: string[];
  /** Allowed providers (lowercase). Undefined: any. */
  providers?: string[];
  /** Allowed models: exact names or `prefix*`. Undefined: any. */
  models?: string[];
  rateLimit?: RateLimitRule;
  /** MCP servers and tools (see McpGroupPolicy). Undefined: every server. */
  mcp?: McpGroupPolicy;
}

/**
 * Per-group MCP access, layered on each server's own tool policy: the group
 * reaches only the servers listed, and on each one only the tools that pass
 * both the server's policy and the group's `allow` / `deny` (deny wins).
 */
export interface McpGroupPolicy {
  /** Server id -> the group's tool rules on it. */
  servers: Map<string, McpGroupToolRule>;
}

export interface McpGroupToolRule {
  /** Exact tool names. Undefined: any tool the server allows. */
  allow?: Set<string>;
  /** Lower-cased tool names, matched regardless of case. */
  denyLower?: Set<string>;
}

// Same rules as the MCP config (src/mcp/config.ts, src/mcp/policy.ts).
const MCP_SERVER_ID_RE = /^[a-z0-9][a-z0-9_-]{0,62}$/;
const MCP_TOOL_NAME_RE = /^[A-Za-z0-9_.-]{1,128}$/;

export interface OidcPolicy {
  /** What happens to an authenticated user who matches no group. */
  defaultAction: 'deny' | 'allow';
  /** First match wins, in this order. */
  groups: PolicyGroup[];
}

export interface OidcConfig {
  issuer: string;
  audiences: string[];
  jwksUrl?: string;
  algorithms: string[];
  clockSkewSeconds: number;
  /** Dotted claim path, e.g. `groups`, `roles`, `realm_access.roles`. */
  groupsClaim: string;
  /** Undefined: authentication only, every valid token is allowed. */
  policy?: OidcPolicy;
  telemetrySubject: TelemetrySubjectMode;
  subjectSalt?: string;
  /** Provider credentials the gateway uses for authenticated users (lowercase provider -> key). */
  providerKeys: Record<string, string>;
}

export type OidcSettings =
  | { enabled: false }
  | { enabled: true; config: OidcConfig; error?: undefined }
  | { enabled: true; config?: undefined; error: string };

const ENV_KEYS = [
  'PS_OIDC_ENABLED',
  'PS_OIDC_ISSUER',
  'PS_OIDC_AUDIENCE',
  'PS_OIDC_JWKS_URL',
  'PS_OIDC_ALGORITHMS',
  'PS_OIDC_CLOCK_SKEW_SECONDS',
  'PS_OIDC_GROUPS_CLAIM',
  'PS_OIDC_POLICY',
  'PS_OIDC_TELEMETRY_SUBJECT',
  'PS_OIDC_SUBJECT_SALT',
  'PS_OIDC_PROVIDER_KEYS',
] as const;

type RawEnv = Partial<Record<(typeof ENV_KEYS)[number], string>>;

const isTrue = (v?: string) =>
  ['true', '1', 'yes'].includes((v ?? '').trim().toLowerCase());

const list = (v?: string) =>
  (v ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);

class ConfigError extends Error {}

const fail = (message: string): never => {
  throw new ConfigError(message);
};

/** Value of an env var, or the contents of the file it names (Node only). */
const valueOrFile = (v?: string): string | undefined => {
  const raw = v?.trim();
  if (!raw) return undefined;
  const value = getValueOrFileContents(raw);
  return typeof value === 'string' ? value.trim() : undefined;
};

const parseJson = (name: string, raw: string): any => {
  try {
    return JSON.parse(raw);
  } catch {
    return fail(
      `${name} is not valid JSON (or a readable file containing JSON)`
    );
  }
};

const stringList = (where: string, v: unknown): string[] => {
  if (
    !Array.isArray(v) ||
    !v.length ||
    !v.every((s) => typeof s === 'string' && s.trim())
  )
    fail(`${where} must be a non-empty array of strings`);
  return (v as string[]).map((s) => s.trim());
};

const httpsUrl = (name: string, v: string): string => {
  let url: URL;
  try {
    url = new URL(v);
  } catch {
    return fail(`${name} is not a URL`);
  }
  if (url.protocol !== 'https:') fail(`${name} must be an https:// URL`);
  return v;
};

const isObject = (v: unknown): v is Record<string, any> =>
  !!v && typeof v === 'object' && !Array.isArray(v);

function toolNames(where: string, v: unknown): string[] {
  if (!Array.isArray(v) || !v.every((s) => typeof s === 'string'))
    fail(`${where} must be an array of tool names`);
  for (const name of v as string[])
    if (!MCP_TOOL_NAME_RE.test(name))
      fail(`${where} has an invalid tool name: ${JSON.stringify(name)}`);
  return v as string[];
}

/**
 * `"mcp": {"servers": {"<server id>": {"tools": {"allow": [...], "deny":
 * [...]}}}}`. A server listed with `{}` is reachable with the server's own
 * tool policy only.
 */
export function parseMcpGroupPolicy(
  where: string,
  raw: unknown
): McpGroupPolicy {
  if (!isObject(raw)) fail(`${where}.mcp must be an object`);
  const m = raw as Record<string, any>;
  for (const k of Object.keys(m))
    if (k !== 'servers') fail(`${where}.mcp: unknown key "${k}"`);
  if (!isObject(m.servers))
    fail(`${where}.mcp.servers must be an object keyed by MCP server id`);
  const servers = new Map<string, McpGroupToolRule>();
  for (const [id, rule] of Object.entries(m.servers as object)) {
    const at = `${where}.mcp.servers.${id}`;
    if (!MCP_SERVER_ID_RE.test(id))
      fail(`${at}: the server id must match [a-z0-9][a-z0-9_-]{0,62}`);
    if (!isObject(rule)) fail(`${at} must be an object`);
    for (const k of Object.keys(rule))
      if (k !== 'tools') fail(`${at}: unknown key "${k}"`);
    const out: McpGroupToolRule = {};
    if (rule.tools !== undefined) {
      if (!isObject(rule.tools))
        fail(`${at}.tools must be {"allow": [...]} and/or {"deny": [...]}`);
      for (const k of Object.keys(rule.tools))
        if (k !== 'allow' && k !== 'deny')
          fail(`${at}.tools: unknown key "${k}"`);
      if (rule.tools.allow !== undefined)
        out.allow = new Set(toolNames(`${at}.tools.allow`, rule.tools.allow));
      if (rule.tools.deny !== undefined)
        out.denyLower = new Set(
          toolNames(`${at}.tools.deny`, rule.tools.deny).map((n) =>
            n.toLowerCase()
          )
        );
    }
    servers.set(id, out);
  }
  return { servers };
}

export function parsePolicy(raw: unknown): OidcPolicy {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw))
    fail('PS_OIDC_POLICY must be a JSON object');
  const p = raw as Record<string, any>;
  const known = new Set(['default', 'groups']);
  for (const k of Object.keys(p))
    if (!known.has(k)) fail(`PS_OIDC_POLICY: unknown key "${k}"`);
  const defaultAction = p.default ?? 'deny';
  if (defaultAction !== 'deny' && defaultAction !== 'allow')
    fail('PS_OIDC_POLICY.default must be "deny" or "allow"');
  if (!Array.isArray(p.groups)) fail('PS_OIDC_POLICY.groups must be an array');
  const names = new Set<string>();
  const groups = (p.groups as any[]).map((g, i): PolicyGroup => {
    const where = `PS_OIDC_POLICY.groups[${i}]`;
    if (!g || typeof g !== 'object' || Array.isArray(g))
      fail(`${where} must be an object`);
    const groupKeys = new Set([
      'name',
      'match',
      'providers',
      'models',
      'rate_limit',
      'mcp',
    ]);
    for (const k of Object.keys(g))
      if (!groupKeys.has(k)) fail(`${where}: unknown key "${k}"`);
    if (typeof g.name !== 'string' || !/^[A-Za-z0-9_.:-]{1,64}$/.test(g.name))
      fail(`${where}.name must be 1-64 characters of [A-Za-z0-9_.:-]`);
    if (names.has(g.name)) fail(`${where}.name "${g.name}" is used twice`);
    names.add(g.name);
    const group: PolicyGroup = {
      name: g.name,
      match: stringList(`${where}.match`, g.match),
    };
    if (g.providers !== undefined)
      group.providers = stringList(`${where}.providers`, g.providers).map((s) =>
        s.toLowerCase()
      );
    if (g.models !== undefined)
      group.models = stringList(`${where}.models`, g.models);
    if (g.rate_limit !== undefined) {
      const rl = g.rate_limit;
      const rpm = rl?.requests_per_minute;
      if (!Number.isInteger(rpm) || rpm < 1)
        fail(
          `${where}.rate_limit.requests_per_minute must be a positive integer`
        );
      const per = rl.per ?? 'user';
      if (per !== 'user' && per !== 'group')
        fail(`${where}.rate_limit.per must be "user" or "group"`);
      group.rateLimit = { requestsPerMinute: rpm, per };
    }
    if (g.mcp !== undefined) group.mcp = parseMcpGroupPolicy(where, g.mcp);
    return group;
  });
  return { defaultAction, groups };
}

export function parseOidcConfig(raw: RawEnv): OidcSettings {
  if (!isTrue(raw.PS_OIDC_ENABLED)) return { enabled: false };
  try {
    const issuer = raw.PS_OIDC_ISSUER?.trim();
    if (!issuer) fail('PS_OIDC_ISSUER is required');
    httpsUrl('PS_OIDC_ISSUER', issuer!);
    const audiences = list(raw.PS_OIDC_AUDIENCE);
    if (!audiences.length) fail('PS_OIDC_AUDIENCE is required');
    const jwksUrl = raw.PS_OIDC_JWKS_URL?.trim() || undefined;
    if (jwksUrl) httpsUrl('PS_OIDC_JWKS_URL', jwksUrl);

    const algorithms = raw.PS_OIDC_ALGORITHMS?.trim()
      ? list(raw.PS_OIDC_ALGORITHMS)
      : [...DEFAULT_ALGORITHMS];
    for (const alg of algorithms)
      if (!(ASYMMETRIC_ALGORITHMS as readonly string[]).includes(alg))
        fail(
          `PS_OIDC_ALGORITHMS: "${alg}" is not allowed (asymmetric algorithms only: ${ASYMMETRIC_ALGORITHMS.join(', ')})`
        );

    let clockSkewSeconds = DEFAULT_CLOCK_SKEW_SECONDS;
    if (raw.PS_OIDC_CLOCK_SKEW_SECONDS?.trim()) {
      clockSkewSeconds = Number(raw.PS_OIDC_CLOCK_SKEW_SECONDS);
      if (
        !Number.isInteger(clockSkewSeconds) ||
        clockSkewSeconds < 0 ||
        clockSkewSeconds > MAX_CLOCK_SKEW_SECONDS
      )
        fail(
          `PS_OIDC_CLOCK_SKEW_SECONDS must be an integer from 0 to ${MAX_CLOCK_SKEW_SECONDS}`
        );
    }

    const groupsClaim =
      raw.PS_OIDC_GROUPS_CLAIM?.trim() || DEFAULT_GROUPS_CLAIM;
    if (!/^[A-Za-z0-9_:\/-]+(\.[A-Za-z0-9_:\/-]+)*$/.test(groupsClaim))
      fail('PS_OIDC_GROUPS_CLAIM must be a claim name or dotted path');

    const policyRaw = valueOrFile(raw.PS_OIDC_POLICY);
    const policy = policyRaw
      ? parsePolicy(parseJson('PS_OIDC_POLICY', policyRaw))
      : undefined;

    const mode = (raw.PS_OIDC_TELEMETRY_SUBJECT?.trim().toLowerCase() ||
      'hash') as TelemetrySubjectMode;
    if (!['hash', 'raw', 'off'].includes(mode))
      fail('PS_OIDC_TELEMETRY_SUBJECT must be hash, raw or off');

    const providerKeys: Record<string, string> = {};
    const keysRaw = valueOrFile(raw.PS_OIDC_PROVIDER_KEYS);
    if (keysRaw) {
      const parsed = parseJson('PS_OIDC_PROVIDER_KEYS', keysRaw);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed))
        fail('PS_OIDC_PROVIDER_KEYS must be a JSON object of provider: key');
      for (const [provider, key] of Object.entries(parsed)) {
        if (typeof key !== 'string' || !key.trim())
          fail(`PS_OIDC_PROVIDER_KEYS.${provider} must be a non-empty string`);
        providerKeys[provider.toLowerCase()] = (key as string).trim();
      }
    }

    return {
      enabled: true,
      config: {
        issuer: issuer!,
        audiences,
        jwksUrl,
        algorithms,
        clockSkewSeconds,
        groupsClaim,
        policy,
        telemetrySubject: mode,
        subjectSalt: raw.PS_OIDC_SUBJECT_SALT?.trim() || undefined,
        providerKeys,
      },
    };
  } catch (e) {
    if (e instanceof ConfigError) return { enabled: true, error: e.message };
    throw e;
  }
}

let memo: { key: string; settings: OidcSettings } | undefined;

/** The OIDC settings for this request's environment (parsed once per value). */
export function oidcSettings(c: Context): OidcSettings {
  const vars = env<Record<string, string | undefined>>(c) ?? {};
  if (!isTrue(vars.PS_OIDC_ENABLED)) return { enabled: false };
  const raw: RawEnv = {};
  for (const k of ENV_KEYS)
    if (typeof vars[k] === 'string') raw[k] = vars[k] as string;
  const key = JSON.stringify(raw);
  if (memo?.key !== key) memo = { key, settings: parseOidcConfig(raw) };
  return memo.settings;
}

/** Test helper. */
export function _resetOidcConfigForTests() {
  memo = undefined;
}
