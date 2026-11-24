/**
 * PS fork (#340): the central policy pulled from the collector.
 *
 * The collector compiles a tenant's governance policies into one
 * `ps-policy-bundle.v1` document (packages/collector/collector/policy_doc.py,
 * docs/guides/central-policy.mdx). The Node puller (./puller.ts) fetches it,
 * parses it here and makes it active; request handlers read it through the
 * accessors below, so edge bundles never import the puller.
 *
 * Parsing is all or nothing: a bundle with any section this gateway
 * enforces that does not validate (a bad route-policy.v1 document, a bad
 * access section, an unknown version of a known section) is refused as a
 * whole, and the puller keeps the last good policy. Sections this gateway
 * does not enforce (added for other components) are ignored.
 *
 * Enforced sections, from the bundle's tenant-wide effective policy:
 *   - `route` v1: the router's route policy; replaces PS_ROUTE_POLICY_FILE.
 *   - `access` v1: the OIDC group policy; replaces PS_OIDC_POLICY (needs
 *     PS_OIDC_ENABLED).
 *   - `rate_limits` v1 (#344): request and token rate limits; replaces
 *     PS_RATE_LIMITS / PS_RATE_LIMITS_FILE (../psRateLimit).
 *   - `guardrails` v1 (#349): checks the gateway runs on every model call
 *     (../psGuardrails).
 * A section the central policy does not have falls back to the local
 * setting. Team and project entries are validated (so a bad one never
 * becomes active later). Only their `rate_limits` and `guardrails`
 * sections are applied, to calls that name the team or project in
 * X-PS-Team / X-PS-Project, and only on top of the tenant's (a header can
 * add limits or guardrails, never remove one): the gateway does not
 * otherwise know a caller's team.
 */
import { parsePolicy, OidcPolicy } from '../psOidc/config';
import { loadedRoutePolicy, parseRoutePolicyDoc } from '../router/policyDoc';
import { RoutePolicy } from '../router/types';
import {
  combineSections,
  GuardrailsSection,
  parseGuardrailsSection,
} from '../psGuardrails/config';
import { parseRateLimitSection, RateLimitConfig } from '../psRateLimit/config';

export const BUNDLE_SCHEMA = 'ps-policy-bundle.v1';

/** Section name -> versions this gateway can enforce. */
export const ENFORCED_SECTIONS: Record<string, number[]> = {
  route: [1],
  access: [1],
  guardrails: [1],
  rate_limits: [1],
};

export class PolicyBundleError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'PolicyBundleError';
  }
}

/** A `rate_limits` section with its canonical JSON (to tell inherited ones apart). */
export interface RateLimitLayer {
  config: RateLimitConfig;
  canon: string;
}

export interface CentralRateLimits {
  tenant?: RateLimitLayer;
  /** Team id -> its name and effective `rate_limits` section. */
  teams: Record<string, { name?: string; layer?: RateLimitLayer }>;
  /** Project id -> its name, team and effective `rate_limits` section. */
  projects: Record<
    string,
    { name?: string; teamId?: string; layer?: RateLimitLayer }
  >;
}

export interface CentralPolicy {
  /** The tenant-wide effective policy's version (`default@3`), if any. */
  version?: string;
  /** The bundle's ETag, sent back as If-None-Match. */
  etag?: string;
  route?: RoutePolicy;
  access?: OidcPolicy;
  /** The tenant-wide guardrails (#349). */
  guardrails?: GuardrailsSection;
  /** Team guardrails (the tenant's included) by team id and by team name. */
  teamGuardrails?: Map<string, GuardrailsSection>;
  /** Project guardrails (its team's included) by project id and name. */
  projectGuardrails?: Map<string, GuardrailsSection>;
  /** `rate_limits` of the tenant, its teams and projects. */
  rateLimits?: CentralRateLimits;
  /** Sections present but not enforced by this gateway. */
  ignored: string[];
  teams: number;
  projects: number;
}

const isObject = (v: unknown): v is Record<string, any> =>
  !!v && typeof v === 'object' && !Array.isArray(v);

interface ParsedSections {
  route?: RoutePolicy;
  access?: OidcPolicy;
  guardrails?: GuardrailsSection;
  rateLimits?: RateLimitLayer;
  ignored: string[];
}

/** Key-sorted JSON, so equal sections compare equal. */
function canonical(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(canonical).join(',')}]`;
  if (isObject(v))
    return `{${Object.keys(v)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canonical(v[k])}`)
      .join(',')}}`;
  return JSON.stringify(v);
}

function parseEffective(at: string, raw: unknown): ParsedSections {
  if (!isObject(raw)) throw new PolicyBundleError(`${at} must be an object`);
  const version = raw.version;
  if (version !== null && version !== undefined && typeof version !== 'string')
    throw new PolicyBundleError(`${at}.version must be a string or null`);
  if (!isObject(raw.sections))
    throw new PolicyBundleError(`${at}.sections must be an object`);
  const out: ParsedSections = { ignored: [] };
  for (const [name, body] of Object.entries(raw.sections)) {
    const where = `${at}.sections.${name}`;
    const versions = ENFORCED_SECTIONS[name];
    if (!versions) {
      out.ignored.push(name);
      continue;
    }
    if (!isObject(body) || !versions.includes(body.version))
      throw new PolicyBundleError(
        `${where}: version ${JSON.stringify(isObject(body) ? body.version : body)} is not supported (supported: ${versions.join(', ')})`
      );
    try {
      if (name === 'route') {
        const route = parseRoutePolicyDoc(body.policy);
        // Every central change gets a new cache key and telemetry version.
        if (version) route.policyVersion = version;
        out.route = route;
      } else if (name === 'access') {
        const { version: _v, ...rest } = body;
        out.access = parsePolicy(rest);
      } else if (name === 'guardrails') {
        out.guardrails = parseGuardrailsSection(body, version ?? undefined);
      } else if (name === 'rate_limits') {
        out.rateLimits = {
          config: parseRateLimitSection(body),
          canon: canonical(body),
        };
      }
    } catch (err) {
      throw new PolicyBundleError(`${where}: ${(err as Error).message}`);
    }
  }
  return out;
}

/** A CentralPolicy from a parsed bundle. Throws PolicyBundleError. */
export function parsePolicyBundle(raw: unknown, etag?: string): CentralPolicy {
  if (!isObject(raw) || raw.schema !== BUNDLE_SCHEMA)
    throw new PolicyBundleError(`the bundle's schema must be ${BUNDLE_SCHEMA}`);
  const tenant = parseEffective('$.tenant', raw.tenant);
  let teams = 0;
  let projects = 0;
  const teamGuardrails = new Map<string, GuardrailsSection>();
  const projectGuardrails = new Map<string, GuardrailsSection>();
  const rateLimits: CentralRateLimits = {
    ...(tenant.rateLimits ? { tenant: tenant.rateLimits } : {}),
    teams: {},
    projects: {},
  };
  const str = (v: unknown) => (typeof v === 'string' && v ? v : undefined);
  for (const [key, label] of [
    ['teams', 'team'],
    ['projects', 'project'],
  ] as const) {
    const entries = raw[key] ?? {};
    if (!isObject(entries))
      throw new PolicyBundleError(`$.${key} must be an object`);
    for (const [id, eff] of Object.entries(entries)) {
      const parsed = parseEffective(`$.${key}.${id}`, eff);
      const layer = parsed.rateLimits ? { layer: parsed.rateLimits } : {};
      if (label === 'team') {
        teams++;
        rateLimits.teams[id] = { name: str((eff as any).name), ...layer };
      } else {
        projects++;
        rateLimits.projects[id] = {
          name: str((eff as any).name),
          teamId: str((eff as any).team_id),
          ...layer,
        };
      }
      if (parsed.guardrails) {
        const index = label === 'team' ? teamGuardrails : projectGuardrails;
        const name = (eff as any).name;
        for (const ref of typeof name === 'string' && name ? [id, name] : [id])
          index.set(
            ref,
            combineSections([
              index.get(ref),
              parsed.guardrails,
            ]) as GuardrailsSection
          );
      }
    }
  }
  return {
    version: raw.tenant.version ?? undefined,
    etag,
    route: tenant.route,
    access: tenant.access,
    rateLimits,
    guardrails: tenant.guardrails,
    teamGuardrails: teamGuardrails.size ? teamGuardrails : undefined,
    projectGuardrails: projectGuardrails.size ? projectGuardrails : undefined,
    ignored: tenant.ignored,
    teams,
    projects,
  };
}

let active: CentralPolicy | undefined;

/** Set (or, with undefined, clear) the active central policy. */
export function setCentralPolicy(policy: CentralPolicy | undefined): void {
  active = policy;
}

export function centralPolicy(): CentralPolicy | undefined {
  return active;
}

/** The OIDC group policy from the central `access` section, if any. */
export function centralAccessPolicy(): OidcPolicy | undefined {
  return active?.access;
}

/**
 * The guardrails for a call (#349): the tenant's, plus those of the team
 * and project it names (by id or exact name), added up. Undefined when
 * none apply.
 */
export function centralGuardrails(
  team?: string,
  project?: string
): GuardrailsSection | undefined {
  if (!active) return undefined;
  return combineSections([
    active.guardrails,
    team ? active.teamGuardrails?.get(team) : undefined,
    project ? active.projectGuardrails?.get(project) : undefined,
  ]);
}

/** Whether the active central policy has guardrails at any level. */
export function hasCentralGuardrails(): boolean {
  return !!(
    active &&
    (active.guardrails || active.teamGuardrails || active.projectGuardrails)
  );
}

/**
 * The route policy in force: the central `route` section, else the one
 * loaded from PS_ROUTE_POLICY_FILE.
 */
export function activeRoutePolicy(): RoutePolicy | undefined {
  return active?.route ?? loadedRoutePolicy();
}

/**
 * `policy_version` for telemetry: the central policy's version when one is
 * active, else the route policy file's `policy_version`.
 */
export function activePolicyVersion(): string | undefined {
  return active?.version ?? loadedRoutePolicy()?.policyVersion;
}

/** The central `rate_limits` sections, if the active policy has any. */
export function centralRateLimits(): CentralRateLimits | undefined {
  const rl = active?.rateLimits;
  if (!rl) return undefined;
  if (rl.tenant) return rl;
  for (const t of Object.values(rl.teams)) if (t.layer) return rl;
  for (const p of Object.values(rl.projects)) if (p.layer) return rl;
  return undefined;
}
