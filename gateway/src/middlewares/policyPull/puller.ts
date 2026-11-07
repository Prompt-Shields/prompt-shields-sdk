/**
 * PS fork (#340): Node-only puller for the collector's central policy.
 *
 * With PS_POLICY_PULL_ENABLED=true the gateway polls
 * `${PS_COLLECTOR_URL}/api/v1/policy/effective` every
 * PS_POLICY_PULL_INTERVAL_SECONDS (default 30), authenticated with an
 * ingest key holding `policy:read` (PS_POLICY_PULL_API_KEY, else
 * PS_API_KEY), sending the last ETag as If-None-Match.
 *
 * Transport: the pulled policy can widen access (`access` replaces
 * PS_OIDC_POLICY) and change where data is routed, so it is only fetched
 * over https, or over plain http to loopback or an in-cluster name (a
 * single-label host such as the chart's `<release>-collector`, or one
 * ending in `.svc`, `.svc.cluster.local` or `.cluster.local`).
 * PS_POLICY_PULL_ALLOW_HTTP=true lifts that for other hosts.
 *
 * Fail-safe:
 *   - collector unreachable, timeout, non-2xx: the last good policy stays
 *     active (at start: none, so the local PS_ROUTE_POLICY_FILE /
 *     PS_OIDC_POLICY apply, as without the pull);
 *   - a malformed bundle (./state.ts parsePolicyBundle) is never applied,
 *     in part or whole: the last good policy stays active and the problem
 *     is logged once per distinct bundle.
 * A failure is logged when it starts and when it clears, not on every tick.
 */
import { isSafeUpstreamURL } from '../requestValidator';
import { upstreamFetch } from '../../utils/fetch';
import {
  CentralPolicy,
  centralPolicy,
  parsePolicyBundle,
  setCentralPolicy,
} from './state';

export const DEFAULT_INTERVAL_SECONDS = 30;
export const MIN_INTERVAL_SECONDS = 5;
/** setInterval treats delays above 2^31-1 ms as 1 ms; cap at a day. */
export const MAX_INTERVAL_SECONDS = 86_400;
export const REQUEST_TIMEOUT_MS = 10_000;
/** A bundle is a few KB per policy; refuse anything absurd. */
export const MAX_BUNDLE_BYTES = 8 * 1024 * 1024;

export class PolicyPullConfigError extends Error {}

export interface PolicyPullConfig {
  url: string;
  apiKey: string;
  intervalMs: number;
}

const isTrue = (v?: string) =>
  ['true', '1', 'yes'].includes((v ?? '').trim().toLowerCase());

const IN_CLUSTER_SUFFIXES = ['.svc', '.svc.cluster.local', '.cluster.local'];

/** Hosts plain http may reach: loopback and in-cluster service names. */
export function isPlainHttpHostAllowed(hostname: string): boolean {
  const host = hostname
    .toLowerCase()
    .replace(/^\[|\]$/g, '')
    .replace(/\.$/, '');
  if (
    host === 'localhost' ||
    host === '::1' ||
    /^127\.\d+\.\d+\.\d+$/.test(host)
  )
    return true;
  // An IP literal other than loopback is not an in-cluster name.
  if (/^[\d.]+$/.test(host) || host.includes(':')) return false;
  if (!host.includes('.')) return true;
  return IN_CLUSTER_SUFFIXES.some((s) => host.endsWith(s));
}

function checkTransport(base: string, allowHttp: boolean): void {
  let url: URL;
  try {
    url = new URL(base);
  } catch {
    throw new PolicyPullConfigError(
      `PS_COLLECTOR_URL is not a valid URL: "${base}"`
    );
  }
  if (url.protocol === 'https:') return;
  if (url.protocol !== 'http:')
    throw new PolicyPullConfigError(
      `PS_COLLECTOR_URL must be an http(s) URL for the policy pull, got "${url.protocol}"`
    );
  if (allowHttp || isPlainHttpHostAllowed(url.hostname)) return;
  throw new PolicyPullConfigError(
    `the policy pull needs https for ${url.hostname} (plain http is only used for loopback and in-cluster names); use an https PS_COLLECTOR_URL or set PS_POLICY_PULL_ALLOW_HTTP=true`
  );
}

/** The pull's settings, or undefined when PS_POLICY_PULL_ENABLED is off. */
export function policyPullConfig(
  env: Record<string, string | undefined> = process.env
): PolicyPullConfig | undefined {
  if (!isTrue(env.PS_POLICY_PULL_ENABLED)) return undefined;
  const apiKey = (env.PS_POLICY_PULL_API_KEY || env.PS_API_KEY || '').trim();
  if (!apiKey)
    throw new PolicyPullConfigError(
      'PS_POLICY_PULL_ENABLED needs an ingest key with policy:read in PS_POLICY_PULL_API_KEY or PS_API_KEY'
    );
  let seconds = DEFAULT_INTERVAL_SECONDS;
  const raw = env.PS_POLICY_PULL_INTERVAL_SECONDS?.trim();
  if (raw) {
    seconds = Number(raw);
    if (
      !Number.isFinite(seconds) ||
      seconds < MIN_INTERVAL_SECONDS ||
      seconds > MAX_INTERVAL_SECONDS
    )
      throw new PolicyPullConfigError(
        `PS_POLICY_PULL_INTERVAL_SECONDS must be ${MIN_INTERVAL_SECONDS}-${MAX_INTERVAL_SECONDS} seconds, got "${raw}"`
      );
  }
  const base = (env.PS_COLLECTOR_URL || 'http://localhost:8000').replace(
    /\/+$/,
    ''
  );
  checkTransport(base, isTrue(env.PS_POLICY_PULL_ALLOW_HTTP));
  return {
    url: `${base}/api/v1/policy/effective`,
    apiKey,
    intervalMs: Math.round(seconds * 1000),
  };
}

export type PullOutcome =
  | 'updated'
  | 'unchanged'
  | 'unreachable'
  | 'rejected'
  | 'refused';

export interface PolicyPuller {
  /** Pull now; resolves with what happened. Never throws. */
  pull(): Promise<PullOutcome>;
  stop(): void;
}

type Fetch = (url: string, init: RequestInit) => Promise<Response>;

function describe(policy: CentralPolicy): string {
  const parts = [
    policy.version ? `version ${policy.version}` : 'no tenant-wide policy',
  ];
  if (policy.route) parts.push('route policy');
  if (policy.access) parts.push('access policy');
  if (policy.teams || policy.projects)
    parts.push(
      `${policy.teams} team / ${policy.projects} project policies (validated, not applied)`
    );
  if (policy.ignored.length)
    parts.push(`not enforced here: ${policy.ignored.join(', ')}`);
  return parts.join(', ');
}

/**
 * Start polling. The first pull runs before this resolves, so the
 * gateway serves with the central policy from its first request when the
 * collector answers; a failure there is not fatal.
 */
export async function startPolicyPull(
  config: PolicyPullConfig,
  options: {
    fetch?: Fetch;
    log?: Pick<Console, 'log' | 'warn' | 'error'>;
    oidcEnabled?: boolean;
  } = {}
): Promise<PolicyPuller> {
  const doFetch: Fetch = options.fetch ?? upstreamFetch;
  const log = options.log ?? console;
  let failing: string | undefined;
  let rejectedEtag: string | undefined;
  let warnedAccess = false;

  const fail = (outcome: PullOutcome, reason: string): PullOutcome => {
    if (failing !== reason) {
      failing = reason;
      const keeping = centralPolicy()
        ? 'keeping the last good central policy'
        : 'local policy settings apply';
      log.error(`[PS Policy] pull failed (${reason}); ${keeping}`);
    }
    return outcome;
  };

  const pullOnce = async (): Promise<PullOutcome> => {
    if (!isSafeUpstreamURL(config.url))
      return fail(
        'refused',
        'the collector URL is not an allowed egress target; list private or loopback hosts in TRUSTED_CUSTOM_HOSTS'
      );
    const headers: Record<string, string> = {
      Authorization: `Bearer ${config.apiKey}`,
      Accept: 'application/json',
    };
    const etag = centralPolicy()?.etag;
    if (etag) headers['If-None-Match'] = etag;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    let resp: Response;
    let text: string;
    try {
      resp = await doFetch(config.url, {
        method: 'GET',
        headers,
        signal: controller.signal,
      });
      if (resp.status === 304) {
        clearTimeout(timer);
        if (failing) log.log('[PS Policy] pull recovered (unchanged)');
        failing = undefined;
        return 'unchanged';
      }
      if (!resp.ok) {
        clearTimeout(timer);
        return fail('unreachable', `HTTP ${resp.status}`);
      }
      text = await resp.text();
    } catch (err) {
      return fail(
        'unreachable',
        (err as Error).name === 'AbortError'
          ? 'timeout'
          : (err as Error).message || 'network error'
      );
    } finally {
      clearTimeout(timer);
    }
    const newEtag = resp.headers.get('etag') ?? undefined;
    if (newEtag && newEtag === rejectedEtag) return 'rejected';
    let next: CentralPolicy;
    try {
      if (text.length > MAX_BUNDLE_BYTES)
        throw new Error(`the bundle is larger than ${MAX_BUNDLE_BYTES} bytes`);
      next = parsePolicyBundle(JSON.parse(text), newEtag);
    } catch (err) {
      rejectedEtag = newEtag;
      failing = undefined;
      const keeping = centralPolicy()
        ? 'keeping the last good central policy'
        : 'local policy settings apply';
      log.error(
        `[PS Policy] refused a malformed central policy, ${keeping}: ${(err as Error).message}`
      );
      return 'rejected';
    }
    setCentralPolicy(next);
    failing = undefined;
    rejectedEtag = undefined;
    log.log(`[PS Policy] central policy applied: ${describe(next)}`);
    if (next.access && options.oidcEnabled === false && !warnedAccess) {
      warnedAccess = true;
      log.warn(
        '[PS Policy] the central policy has an access section, which needs PS_OIDC_ENABLED=true to be enforced'
      );
    }
    return 'updated';
  };

  let busy: Promise<PullOutcome> | undefined;
  const pull = () => {
    busy ??= pullOnce().finally(() => {
      busy = undefined;
    });
    return busy;
  };

  await pull();
  const timer = setInterval(() => void pull(), config.intervalMs);
  timer.unref?.();
  return {
    pull,
    stop() {
      clearInterval(timer);
    },
  };
}
