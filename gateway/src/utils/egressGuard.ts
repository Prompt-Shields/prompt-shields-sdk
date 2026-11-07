/**
 * PS fork (security, #377): resolve-and-check for gateway egress.
 *
 * The URL checks in `requestValidator` look at the host *name* only, so a
 * public name that resolves to the cloud metadata service (169.254.169.254)
 * or another link-local address passed them. On Node, `withEgressGuard`
 * gives a request a dispatcher whose DNS lookup resolves the host, refuses
 * it when any address is refused, and hands the connection exactly the
 * addresses it checked: there is no second lookup, so DNS rebinding between
 * the check and the connection cannot redirect it. TLS still verifies the
 * certificate for the host name.
 *
 * Policies:
 *  - `metadata` (operator-configured URLs: OTLP exporters, OIDC discovery /
 *    JWKS, telemetry): refuse link-local (169.254.0.0/16, fe80::/10), cloud
 *    metadata (fd00:ec2::254, 100.100.100.200), "this network" 0.0.0.0/8,
 *    unspecified, multicast, broadcast and reserved addresses, and IPv6 forms
 *    embedding a refused IPv4 address (IPv4-mapped, IPv4-compatible, SIIT,
 *    NAT64 64:ff9b::/96, 6to4, Teredo) and the local-use NAT64 prefix
 *    64:ff9b:1::/48.
 *  - `local` (caller-controlled URLs: custom provider hosts and guardrail
 *    plugin egress, whose name checks already refuse loopback): the above
 *    plus loopback (127.0.0.0/8, ::1).
 * Private ranges are never refused here, so private-network deployments are
 * unaffected. Hosts the operator trusts (TRUSTED_CUSTOM_HOSTS, checked by the
 * caller) are not guarded.
 *
 * Lookups are cached per host for DNS_CACHE_TTL_MS and bounded by
 * DNS_TIMEOUT_MS. On other runtimes (Workers) there is no resolver and the
 * request is sent unchanged. If the process uses a non-default global
 * dispatcher (an outbound proxy), the host is resolved and checked but the
 * connection is not pinned (the proxy connects).
 */

export type EgressPolicy = 'metadata' | 'local';

export const DNS_CACHE_TTL_MS = 30_000;
export const DNS_TIMEOUT_MS = 5_000;
const DNS_CACHE_MAX = 1000;

export class EgressRefusedError extends Error {
  readonly code = 'PS_EGRESS_REFUSED';
  constructor(host: string) {
    super(
      `Egress to ${host} refused: it resolves to a link-local, metadata or other non-routable address`
    );
    this.name = 'EgressRefusedError';
  }
}

// ── address classification ───────────────────────────────────────────

function parseIPv4(ip: string): number | null {
  const parts = ip.split('.');
  if (parts.length !== 4) return null;
  let n = 0;
  for (const p of parts) {
    if (!/^\d{1,3}$/.test(p)) return null;
    const v = Number(p);
    if (v > 255) return null;
    n = n * 256 + v;
  }
  return n;
}

/** Eight 16-bit groups, or null when `ip` is not an IPv6 address. */
function parseIPv6(ip: string): number[] | null {
  let s = ip.toLowerCase().replace(/^\[|\]$/g, '');
  const zone = s.indexOf('%');
  if (zone >= 0) s = s.slice(0, zone);
  if (!s.includes(':')) return null;
  // A trailing dotted IPv4 part becomes two groups.
  const lastColon = s.lastIndexOf(':');
  const tail = s.slice(lastColon + 1);
  if (tail.includes('.')) {
    const v4 = parseIPv4(tail);
    if (v4 === null) return null;
    s =
      s.slice(0, lastColon + 1) +
      ((v4 >>> 16) & 0xffff).toString(16) +
      ':' +
      (v4 & 0xffff).toString(16);
  }
  const halves = s.split('::');
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(':') : [];
  const rest = halves.length === 2 && halves[1] ? halves[1].split(':') : [];
  const fill = 8 - head.length - rest.length;
  if (halves.length === 1 ? head.length !== 8 : fill < 1) return null;
  const groups = [
    ...head,
    ...Array(halves.length === 2 ? fill : 0).fill('0'),
    ...rest,
  ];
  const out: number[] = [];
  for (const g of groups) {
    if (!/^[0-9a-f]{1,4}$/.test(g)) return null;
    out.push(parseInt(g, 16));
  }
  return out.length === 8 ? out : null;
}

const v4 = (a: number, b: number, c: number, d: number) =>
  ((a << 24) >>> 0) + (b << 16) + (c << 8) + d;
const inV4 = (n: number, base: number, bits: number) =>
  bits === 0 || n >>> (32 - bits) === base >>> (32 - bits);

function refusedIPv4(n: number, policy: EgressPolicy): boolean {
  if (inV4(n, v4(0, 0, 0, 0), 8)) return true; // "this network", 0.0.0.0
  if (inV4(n, v4(169, 254, 0, 0), 16)) return true; // link-local, metadata
  if (inV4(n, v4(224, 0, 0, 0), 3)) return true; // multicast, reserved, broadcast
  if (n === v4(100, 100, 100, 200)) return true; // Alibaba Cloud metadata
  if (policy === 'local' && inV4(n, v4(127, 0, 0, 0), 8)) return true;
  return false;
}

const low32 = (g: number[]) => ((g[6] << 16) >>> 0) + g[7];

function refusedIPv6(g: number[], policy: EgressPolicy): boolean {
  const zeroUpTo = (k: number) => g.slice(0, k).every((x) => x === 0);
  if (g.every((x) => x === 0)) return true; // ::
  if (zeroUpTo(7) && g[7] === 1) return policy === 'local'; // ::1
  if ((g[0] & 0xffc0) === 0xfe80) return true; // fe80::/10 link-local
  if ((g[0] & 0xff00) === 0xff00) return true; // multicast
  // fd00:ec2::254, the AWS IPv6 metadata endpoint.
  if (
    g[0] === 0xfd00 &&
    g[1] === 0x0ec2 &&
    g[2] === 0 &&
    g[3] === 0 &&
    g[4] === 0 &&
    g[5] === 0 &&
    g[6] === 0 &&
    g[7] === 0x254
  )
    return true;
  // Forms that embed (or reach through a translator) an IPv4 address.
  let embedded: number | null = null;
  if (zeroUpTo(5) && g[5] === 0xffff)
    embedded = low32(g); // ::ffff:a.b.c.d
  else if (zeroUpTo(6))
    embedded = low32(g); // ::a.b.c.d (compatible)
  else if (zeroUpTo(4) && g[4] === 0xffff && g[5] === 0)
    embedded = low32(g); // ::ffff:0:a.b.c.d (SIIT)
  else if (
    g[0] === 0x64 &&
    g[1] === 0xff9b &&
    g.slice(2, 6).every((x) => x === 0)
  )
    embedded = low32(g); // 64:ff9b::/96 NAT64
  else if (g[0] === 0x64 && g[1] === 0xff9b && g[2] === 1)
    return true; // 64:ff9b:1::/48 local-use NAT64: any prefix length, refused
  else if (g[0] === 0x2002)
    embedded = ((g[1] << 16) >>> 0) + g[2]; // 6to4
  else if (g[0] === 0x2001 && g[1] === 0) embedded = ~low32(g) >>> 0; // Teredo client address
  return embedded !== null && refusedIPv4(embedded, policy);
}

/** True when `address` (an IP literal) must not be connected to. */
export function isRefusedAddress(
  address: string,
  policy: EgressPolicy
): boolean {
  const n = parseIPv4(address);
  if (n !== null) return refusedIPv4(n, policy);
  const g = parseIPv6(address);
  if (g !== null) return refusedIPv6(g, policy);
  return true; // not an IP address
}

function isIPLiteral(host: string): boolean {
  return parseIPv4(host) !== null || parseIPv6(host) !== null;
}

// ── resolver with a bounded, cached lookup ───────────────────────────

export interface ResolvedAddress {
  address: string;
  family: number;
}
export type Resolver = (host: string) => Promise<ResolvedAddress[]>;

async function systemResolver(host: string): Promise<ResolvedAddress[]> {
  const dns = await import('dns');
  return dns.promises.lookup(host, { all: true, verbatim: true });
}

let resolver: Resolver = systemResolver;
const cache = new Map<
  string,
  { expires: number; addresses: Promise<ResolvedAddress[]> }
>();

/** Tests only: replace the resolver (and clear the cache). */
export function setEgressResolverForTests(r: Resolver | null): void {
  resolver = r ?? systemResolver;
  cache.clear();
}

function withTimeout<T>(p: Promise<T>, ms: number, host: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout>;
  const t = new Promise<never>((_, reject) => {
    timer = setTimeout(() => {
      const err: any = new Error(`DNS lookup for ${host} timed out`);
      err.code = 'ETIMEOUT';
      reject(err);
    }, ms);
  });
  return Promise.race([p, t]).finally(() => clearTimeout(timer));
}

/** All addresses `host` resolves to, cached for DNS_CACHE_TTL_MS. Failed
 * lookups are not cached. */
export function resolveCached(host: string): Promise<ResolvedAddress[]> {
  const key = host.toLowerCase();
  const now = Date.now();
  const hit = cache.get(key);
  if (hit && hit.expires > now) return hit.addresses;
  const addresses = withTimeout(resolver(key), DNS_TIMEOUT_MS, key);
  if (cache.size >= DNS_CACHE_MAX) {
    for (const [k, v] of cache) if (v.expires <= now) cache.delete(k);
    if (cache.size >= DNS_CACHE_MAX) cache.delete(cache.keys().next().value!);
  }
  cache.set(key, { expires: now + DNS_CACHE_TTL_MS, addresses });
  addresses.catch(() => {
    if (cache.get(key)?.addresses === addresses) cache.delete(key);
  });
  return addresses;
}

/** Resolve `host` and return its addresses; throws EgressRefusedError when
 * any of them is refused under `policy`. */
export async function resolveChecked(
  host: string,
  policy: EgressPolicy
): Promise<ResolvedAddress[]> {
  const addresses = await resolveCached(host);
  if (
    addresses.length === 0 ||
    addresses.some((a) => isRefusedAddress(a.address, policy))
  ) {
    throw new EgressRefusedError(host);
  }
  return addresses;
}

/** A `net.connect` / `tls.connect` lookup that only yields checked addresses. */
export function checkedLookup(policy: EgressPolicy) {
  return (
    hostname: string,
    options: { family?: number | string; all?: boolean },
    callback: (...args: any[]) => void
  ): void => {
    resolveChecked(hostname, policy).then(
      (all) => {
        const fam = Number(
          options?.family === 'IPv6'
            ? 6
            : options?.family === 'IPv4'
              ? 4
              : options?.family || 0
        );
        const list = fam ? all.filter((a) => a.family === fam) : all;
        if (list.length === 0) {
          const err: any = new Error(`No address for ${hostname}`);
          err.code = 'ENOTFOUND';
          return callback(err);
        }
        if (options?.all) return callback(null, list);
        callback(null, list[0].address, list[0].family);
      },
      (err) => callback(err)
    );
  };
}

// ── dispatcher (Node's bundled fetch) ────────────────────────────────

const GLOBAL_DISPATCHER = Symbol.for('undici.globalDispatcher.1');
const dispatchers = new Map<EgressPolicy, unknown>();

function isNode(): boolean {
  return (
    typeof process !== 'undefined' &&
    !!(process as any).versions?.node &&
    typeof (globalThis as any).WebSocketPair === 'undefined'
  );
}

/** The guarded dispatcher for `policy`, or undefined when fetch's default
 * dispatcher is not the plain undici Agent (not yet created, or replaced by
 * a proxy agent). */
function dispatcherFor(policy: EgressPolicy): unknown {
  const cached = dispatchers.get(policy);
  if (cached) return cached;
  const global = (globalThis as any)[GLOBAL_DISPATCHER];
  const Agent = global?.constructor;
  if (typeof Agent !== 'function' || Agent.name !== 'Agent') return undefined;
  try {
    const agent = new Agent({ connect: { lookup: checkedLookup(policy) } });
    dispatchers.set(policy, agent);
    return agent;
  } catch {
    return undefined;
  }
}

/**
 * Node only: make sure fetch's default dispatcher exists, so the first
 * guarded request is already pinned. Called once at server start.
 */
export async function initEgressGuard(): Promise<void> {
  if (!isNode() || (globalThis as any)[GLOBAL_DISPATCHER]) return;
  try {
    await fetch('data:,');
  } catch {
    /* best effort */
  }
}

/**
 * `init` for a request to `url`, guarded under `policy`. Throws
 * EgressRefusedError when the URL's host is an IP literal that is refused,
 * or (without a pinning dispatcher) when its name resolves to one. With a
 * dispatcher, the refusal surfaces as a fetch network error whose `cause`
 * is EgressRefusedError (see `isEgressRefused`).
 */
export async function withEgressGuard(
  url: string,
  init: RequestInit,
  policy: EgressPolicy
): Promise<RequestInit> {
  if (!isNode()) return init;
  let host: string;
  try {
    host = new URL(url).hostname.replace(/^\[|\]$/g, '');
  } catch {
    return init; // fetch rejects it
  }
  if (isIPLiteral(host)) {
    if (isRefusedAddress(host, policy)) throw new EgressRefusedError(host);
    return init;
  }
  const dispatcher = dispatcherFor(policy);
  if (dispatcher) return { ...init, dispatcher } as RequestInit;
  // No pinning possible: check what the name resolves to now. A failed
  // lookup is left to fetch (behind a proxy the name may not resolve here).
  try {
    await resolveChecked(host, policy);
  } catch (err) {
    if (err instanceof EgressRefusedError) throw err;
  }
  return init;
}

/** True when `err` (or its cause chain) is an egress refusal. */
export function isEgressRefused(err: unknown): boolean {
  let e: any = err;
  for (let i = 0; e && i < 5; i++) {
    if (e instanceof EgressRefusedError || e?.code === 'PS_EGRESS_REFUSED')
      return true;
    e = e.cause;
  }
  return false;
}
