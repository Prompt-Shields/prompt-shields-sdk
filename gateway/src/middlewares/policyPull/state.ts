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
 *   - `pseudonymisation` v1 (#350): reversible pseudonymisation of model
 *     calls; replaces PS_PSEUDONYMISE* (psPseudonymise/config.ts).
 *   - `rate_limits` v1 (#344): request and token rate limits; replaces
 *     PS_RATE_LIMITS / PS_RATE_LIMITS_FILE (../psRateLimit).
 * A section the central policy does not have falls back to the local
 * setting. Team and project entries are validated (so a bad one never
 * becomes active later). Only their `rate_limits` sections are applied, to
 * calls that name the team or project in X-PS-Team / X-PS-Project, and only
 * on top of the tenant's (a header can add limits, never remove one): the
 * gateway does not otherwise know a caller's team.
 */
import { parsePolicy, OidcPolicy } from '../psOidc/config';
import { loadedRoutePolicy, parseRoutePolicyDoc } from '../router/policyDoc';
import { RoutePolicy } from '../router/types';
import {
  parsePseudonymisationSection,
  PseudonymisationSettings,
} from '../psPseudonymise/config';
import { parseRateLimitSection, RateLimitConfig } from '../psRateLimit/config';

export const BUNDLE_SCHEMA = 'ps-policy-bundle.v1';

/** Section name -> versions this gateway can enforce. */
export const ENFORCED_SECTIONS: Record<string, number[]> = {
  route: [1],
  access: [1],
  pseudonymisation: [1],
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
  pseudonymisation?: PseudonymisationSettings;
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
  pseudonymisation?: PseudonymisationSettings;
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
  // Sections the collector marks as required (#350): ones that must not be
  // silently ignored, such as an enabled `pseudonymisation`. A gateway that
  // does not enforce one refuses the whole bundle (the last good policy
  // stays) instead of forwarding what the section was meant to protect.
  if (raw.required !== undefined) {
    if (
      !Array.isArray(raw.required) ||
      raw.required.some((r: unknown) => typeof r !== 'string')
    )
      throw new PolicyBundleError(`${at}.required must be an array of names`);
    const unknown = (raw.required as string[]).filter(
      (r) => !ENFORCED_SECTIONS[r]
    );
    if (unknown.length)
      throw new PolicyBundleError(
        `${at}.required: this gateway does not enforce ${JSON.stringify(unknown)}; upgrade it`
      );
  }
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
      } else if (name === 'pseudonymisation') {
        out.pseudonymisation = parsePseudonymisationSection(body);
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
    }
  }
  return {
    version: raw.tenant.version ?? undefined,
    etag,
    route: tenant.route,
    access: tenant.access,
    pseudonymisation: tenant.pseudonymisation,
    rateLimits,
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

/** Pseudonymisation settings from the central `pseudonymisation` section. */
export function centralPseudonymisation():
  | PseudonymisationSettings
  | undefined {
  return active?.pseudonymisation;
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
