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
 * A section the central policy does not have falls back to the local
 * setting. Team and project entries are validated (so a bad one never
 * becomes active later) but not applied yet: the gateway does not know a
 * caller's team.
 */
import { parsePolicy, OidcPolicy } from '../psOidc/config';
import { loadedRoutePolicy, parseRoutePolicyDoc } from '../router/policyDoc';
import { RoutePolicy } from '../router/types';

export const BUNDLE_SCHEMA = 'ps-policy-bundle.v1';

/** Section name -> versions this gateway can enforce. */
export const ENFORCED_SECTIONS: Record<string, number[]> = {
  route: [1],
  access: [1],
};

export class PolicyBundleError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'PolicyBundleError';
  }
}

export interface CentralPolicy {
  /** The tenant-wide effective policy's version (`default@3`), if any. */
  version?: string;
  /** The bundle's ETag, sent back as If-None-Match. */
  etag?: string;
  route?: RoutePolicy;
  access?: OidcPolicy;
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
  ignored: string[];
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
  for (const [key, label] of [
    ['teams', 'team'],
    ['projects', 'project'],
  ] as const) {
    const entries = raw[key] ?? {};
    if (!isObject(entries))
      throw new PolicyBundleError(`$.${key} must be an object`);
    for (const [id, eff] of Object.entries(entries)) {
      parseEffective(`$.${key}.${id}`, eff);
      if (label === 'team') teams++;
      else projects++;
    }
  }
  return {
    version: raw.tenant.version ?? undefined,
    etag,
    route: tenant.route,
    access: tenant.access,
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
