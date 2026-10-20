import { Context } from 'hono';
import {
  CONTENT_TYPES,
  HEADER_KEYS,
  POWERED_BY,
  VALID_PROVIDERS,
} from '../../globals';
import { configSchema } from './schema/config';
import { Environment } from '../../utils/env';

// Regex patterns for validation (defined once for reusability)
const VALIDATION_PATTERNS = {
  CONTROL_CHARS: /[\x00-\x1F\x7F]/,
  SUSPICIOUS_CHARS: /[\s<>{}|\\^`]/,
  DIGITS_1_3: /^\d{1,3}$/,
  DIGITS_1_10: /^\d{1,10}$/,
  DIGITS_ONLY: /^\d+$/,
  HEX_IP: /^0x[0-9a-f]{1,8}$/i,
  ALTERNATIVE_IP_PART: /^0[0-9a-fx]/i, // Starts with 0 followed by digits or x (octal or hex)
  IPV6_MAPPED_IPV4: /::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/i,
  IPV6_EMBEDDED_IPV4: /::(\d{1,3}(?:\.\d{1,3}){3})$/i,
  HOMOGRAPH_ATTACK: /^[a-z0-9.-]+$/,
};

// Disallowed URL schemes
const DISALLOWED_SCHEMES = ['file://', 'data:', 'gopher:', 'ftp://', 'ftps://'];

// Blocked hosts (cloud metadata endpoints and internal IPs)
const BLOCKED_HOSTS = [
  '0.0.0.0',
  '169.254.169.254', // AWS, Azure, GCP metadata (IPv4)
  'metadata.google.internal', // GCP metadata
  'metadata', // Kubernetes metadata
  'metadata.azure.com', // Azure instance metadata
  'instance-data', // AWS instance metadata alt
];

// Blocked TLDs for SSRF protection
const BLOCKED_TLDS = [
  '.local',
  '.localdomain',
  '.internal',
  '.intranet',
  '.lan',
  '.home',
  '.corp',
  '.test',
  '.invalid',
  '.onion',
  '.localhost',
];

// Parse allowed custom hosts from environment variable
// Format: comma-separated list of domains/IPs (e.g., "localhost,127.0.0.1,example.com")
const TRUSTED_CUSTOM_HOSTS = (c?: Context) => {
  const envVar = Environment(c)?.TRUSTED_CUSTOM_HOSTS;
  if (!envVar) {
    // Default allowed hosts for local development
    return new Set(['localhost', '127.0.0.1', '::1', 'host.docker.internal']);
  }
  return configuredTrustedCustomHosts(c);
};

/**
 * PS fork: only the hosts the operator listed in TRUSTED_CUSTOM_HOSTS, with
 * no development defaults (an unset variable trusts nothing). The entry
 * `none` is dropped, so `TRUSTED_CUSTOM_HOSTS=none` trusts no host anywhere.
 * Guardrail plugin egress uses this set, since its URLs come from the
 * caller's guardrail config.
 */
export function configuredTrustedCustomHosts(c?: Context): Set<string> {
  const envVar = Environment(c)?.TRUSTED_CUSTOM_HOSTS;
  if (!envVar || typeof envVar !== 'string') return new Set();
  return new Set(
    envVar
      .split(',')
      .map((h: string) => h.trim().toLowerCase())
      .filter((h: string) => h.length > 0 && h !== 'none')
  );
}

// Pre-computed IPv4 range boundaries for performance optimization
const IPV4_RANGES = {
  PRIVATE: [
    { start: ipv4ToInt('10.0.0.0'), end: ipv4ToInt('10.255.255.255') }, // 10/8
    { start: ipv4ToInt('172.16.0.0'), end: ipv4ToInt('172.31.255.255') }, // 172.16/12
    { start: ipv4ToInt('192.168.0.0'), end: ipv4ToInt('192.168.255.255') }, // 192.168/16
  ],
  RESERVED: [
    { start: ipv4ToInt('127.0.0.0'), end: ipv4ToInt('127.255.255.255') }, // loopback
    { start: ipv4ToInt('169.254.0.0'), end: ipv4ToInt('169.254.255.255') }, // link-local
    { start: ipv4ToInt('100.64.0.0'), end: ipv4ToInt('100.127.255.255') }, // CGNAT
    { start: ipv4ToInt('0.0.0.0'), end: ipv4ToInt('0.255.255.255') }, // "this" network
    { start: ipv4ToInt('224.0.0.0'), end: ipv4ToInt('255.255.255.255') }, // multicast/reserved/broadcast
  ],
};

export const requestValidator = (c: Context, next: any) => {
  const requestHeaders = Object.fromEntries(c.req.raw.headers);

  const contentType = requestHeaders['content-type'];
  if (
    !!contentType &&
    ![
      CONTENT_TYPES.APPLICATION_JSON,
      CONTENT_TYPES.MULTIPART_FORM_DATA,
    ].includes(requestHeaders['content-type'].split(';')[0]) &&
    !contentType.split(';')[0]?.startsWith(CONTENT_TYPES.GENERIC_AUDIO_PATTERN)
  ) {
    return new Response(
      JSON.stringify({
        status: 'failure',
        message: `Invalid content type passed`,
      }),
      {
        status: 400,
        headers: {
          'content-type': 'application/json',
        },
      }
    );
  }

  if (
    !(
      requestHeaders[`x-${POWERED_BY}-config`] ||
      requestHeaders[`x-${POWERED_BY}-provider`]
    )
  ) {
    return new Response(
      JSON.stringify({
        status: 'failure',
        message: `Either x-${POWERED_BY}-config or x-${POWERED_BY}-provider header is required`,
      }),
      {
        status: 400,
        headers: {
          'content-type': 'application/json',
        },
      }
    );
  }
  if (
    requestHeaders[`x-${POWERED_BY}-provider`] &&
    !VALID_PROVIDERS.includes(requestHeaders[`x-${POWERED_BY}-provider`])
  ) {
    return new Response(
      JSON.stringify({
        status: 'failure',
        message: `Invalid provider passed`,
      }),
      {
        status: 400,
        headers: {
          'content-type': 'application/json',
        },
      }
    );
  }

  const customHostHeader = requestHeaders[`x-${POWERED_BY}-custom-host`];
  if (customHostHeader && !isValidCustomHost(customHostHeader, c)) {
    return new Response(
      JSON.stringify({
        status: 'failure',
        message: `Invalid custom host`,
      }),
      {
        status: 400,
        headers: {
          'content-type': 'application/json',
        },
      }
    );
  }

  if (requestHeaders[`x-${POWERED_BY}-config`]) {
    try {
      const parsedConfig = JSON.parse(requestHeaders[`x-${POWERED_BY}-config`]);
      if (
        !requestHeaders[`x-${POWERED_BY}-provider`] &&
        !(parsedConfig.provider || parsedConfig.targets)
      ) {
        return new Response(
          JSON.stringify({
            status: 'failure',
            message: `Either x-${POWERED_BY}-provider needs to be passed. Or the x-${POWERED_BY}-config header should have a valid config with provider details in it.`,
          }),
          {
            status: 400,
            headers: {
              'content-type': 'application/json',
            },
          }
        );
      }

      const validatedConfig = configSchema.safeParse(parsedConfig);

      if (!validatedConfig.success && validatedConfig.error?.issues?.length) {
        return new Response(
          JSON.stringify({
            status: 'failure',
            message: `Invalid config passed`,
            errors: validatedConfig.error.issues.map(
              (e: any) => `path: ${e.path}, message: ${e.message}`
            ),
          }),
          {
            status: 400,
            headers: {
              'content-type': 'application/json',
            },
          }
        );
      }

      const fanOutError = checkConfigFanOut(parsedConfig, c);
      if (fanOutError) {
        return new Response(
          JSON.stringify({ status: 'failure', message: fanOutError }),
          {
            status: 400,
            headers: {
              'content-type': 'application/json',
            },
          }
        );
      }

      if (parsedConfig.options) {
        return new Response(
          JSON.stringify({
            status: 'failure',
            message: `This version of config is not supported in this route. Please migrate to the latest version`,
          }),
          {
            status: 400,
            headers: {
              'content-type': 'application/json',
            },
          }
        );
      }
    } catch (e) {
      return new Response(
        JSON.stringify({
          status: 'failure',
          message: `Invalid config passed. You need to pass a valid json`,
        }),
        {
          status: 400,
          headers: {
            'content-type': 'application/json',
          },
        }
      );
    }
  }

  if (requestHeaders[HEADER_KEYS.FORWARD_HEADERS]) {
    const forwardHeaders: string[] =
      requestHeaders[HEADER_KEYS.FORWARD_HEADERS].split(',');
    if (
      forwardHeaders.some(
        (h: string) => h.trim().toLowerCase() === HEADER_KEYS.FORWARD_HEADERS
      )
    ) {
      return new Response(
        JSON.stringify({
          status: 'failure',
          message: `forward_headers must not contain the '${HEADER_KEYS.FORWARD_HEADERS}' header`,
        }),
        {
          status: 400,
          headers: {
            'content-type': 'application/json',
          },
        }
      );
    }
  }
  return next();
};

// PS fork: cap config target fan-out so one request cannot multiply into
// thousands of upstream calls (egress amplification). Mirrors the defaults
// Portkey's enterprise gateway introduced in 2.14; override with env vars.
const DEFAULT_MAX_ROOT_CONFIG_TARGETS = 100;
const DEFAULT_MAX_CONDITIONAL_ROOT_CONFIG_TARGETS = 1000;
const DEFAULT_MAX_NESTED_CONFIG_TARGETS = 50;

const positiveIntFromEnv = (raw: unknown, fallback: number): number => {
  const n = Number(raw);
  return Number.isInteger(n) && n > 0 ? n : fallback;
};

const countNestedTargets = (target: any, budget: number): number => {
  let count = 0;
  const stack: any[] = Array.isArray(target?.targets)
    ? [...target.targets]
    : [];
  while (stack.length) {
    const t = stack.pop();
    count++;
    // Stop walking once over budget; the exact total does not matter.
    if (count > budget) return count;
    if (Array.isArray(t?.targets)) stack.push(...t.targets);
  }
  return count;
};

export function checkConfigFanOut(config: any, c?: Context): string | null {
  if (!config || !Array.isArray(config.targets)) return null;
  const envVars = Environment(c) ?? {};
  const isConditional = config.strategy?.mode === 'conditional';
  const maxRoot = isConditional
    ? positiveIntFromEnv(
        envVars.MAX_CONDITIONAL_ROOT_CONFIG_TARGETS,
        DEFAULT_MAX_CONDITIONAL_ROOT_CONFIG_TARGETS
      )
    : positiveIntFromEnv(
        envVars.MAX_ROOT_CONFIG_TARGETS,
        DEFAULT_MAX_ROOT_CONFIG_TARGETS
      );
  const maxNested = positiveIntFromEnv(
    envVars.MAX_NESTED_CONFIG_TARGETS,
    DEFAULT_MAX_NESTED_CONFIG_TARGETS
  );

  if (config.targets.length > maxRoot) {
    return `Config has ${config.targets.length} root targets; the limit is ${maxRoot}`;
  }
  for (let i = 0; i < config.targets.length; i++) {
    if (countNestedTargets(config.targets[i], maxNested) > maxNested) {
      return `Config target ${i} has more than ${maxNested} nested targets`;
    }
  }
  return null;
}

/**
 * PS fork: sanity check on the final upstream URL a provider config builds.
 * Provider base URLs interpolate request-controlled values (Azure resource
 * name, Vertex region and project, AWS region, account ids). A value such as
 * `169.254.169.254/latest#` would otherwise turn the gateway into an SSRF
 * proxy without ever touching the custom-host check. This is deliberately
 * looser than isValidCustomHost: it does not block special TLDs, because
 * real provider hosts are built here, only internal and metadata targets.
 */
export function isSafeUpstreamURL(rawUrl: string, c?: Context): boolean {
  return isSafeURLForTrustedHosts(rawUrl, TRUSTED_CUSTOM_HOSTS(c));
}

/**
 * PS fork: the check for guardrail plugin egress (webhook and log URLs,
 * vendor base-URL overrides, hosts built from caller credentials). Like
 * isSafeUpstreamURL, but only hosts listed in TRUSTED_CUSTOM_HOSTS are
 * trusted (never localhost by default), and the internal special-use TLDs
 * refused for custom hosts (`.internal`, `.local`, ...) are refused too.
 * Hostname-only: a public name that resolves to a private address is not
 * caught, so production deployments should also restrict network egress.
 */
export function isSafePluginEgressURL(rawUrl: string, c?: Context): boolean {
  const trustedHosts = configuredTrustedCustomHosts(c);
  if (!isSafeURLForTrustedHosts(rawUrl, trustedHosts)) return false;
  try {
    const host = new URL(rawUrl).hostname.toLowerCase().replace(/^\[|\]$/g, '');
    if (
      trustedHosts.has(host) ||
      (trustedHosts.has('localhost') && host.endsWith('.localhost'))
    ) {
      return true;
    }
    // Single-label names (`redis`, `prompt-shields-collector`) and `*.svc`
    // resolve to in-cluster services through the resolver's search domains;
    // a public guardrail endpoint always has a dotted, public name.
    if (!host.includes('.') || host.endsWith('.svc')) return false;
    return !BLOCKED_TLDS.some((tld) => host.endsWith(tld));
  } catch {
    return false;
  }
}

function isSafeURLForTrustedHosts(
  rawUrl: string,
  trustedHosts: Set<string>
): boolean {
  try {
    if (VALIDATION_PATTERNS.CONTROL_CHARS.test(rawUrl)) return false;
    const url = new URL(rawUrl);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return false;
    if (url.username || url.password) return false;

    const host = url.hostname.toLowerCase();
    if (!host || host.includes('%') || host.endsWith('.')) return false;
    const bareHost = host.replace(/^\[|\]$/g, '');
    if (!/^[a-z0-9.:-]+$/.test(bareHost)) return false;
    if (url.port && !isValidPort(url.port)) return false;

    if (
      trustedHosts.has(bareHost) ||
      (trustedHosts.has('localhost') && bareHost.endsWith('.localhost'))
    ) {
      return true;
    }

    if (BLOCKED_HOSTS.includes(bareHost)) return false;
    if (bareHost === 'localhost' || bareHost.endsWith('.localhost'))
      return false;
    if (
      bareHost.startsWith('169.254.169.') ||
      bareHost.startsWith('fd00:ec2::')
    )
      return false;

    const hostParts = bareHost.split('.');
    if (
      isIPv4(hostParts) &&
      (isPrivateIPv4(bareHost) || isReservedIPv4(bareHost))
    ) {
      return false;
    }
    if (isAlternativeIPRepresentation(bareHost, hostParts)) return false;
    // No provider builds an IPv6-literal host, and the URL parser rewrites
    // embedded IPv4 (::ffff:169.254.169.254 -> ::ffff:a9fe:a9fe, also
    // 64:ff9b::/96 and 2002::/16), which dotted-form checks cannot see.
    // Reject every IPv6 literal here; list one in TRUSTED_CUSTOM_HOSTS if needed.
    if (bareHost.includes(':')) return false;
    return true;
  } catch {
    return false;
  }
}

export function isValidCustomHost(customHost: string, c?: Context) {
  try {
    const value = customHost.trim().toLowerCase();

    // Block empty or whitespace-only hosts
    if (!value) return false;

    // Block URLs with control characters or excessive whitespace
    if (VALIDATION_PATTERNS.CONTROL_CHARS.test(customHost)) return false;

    // Project-specific and obvious disallowed schemes/hosts
    if (value.indexOf('api.portkey') > -1) return false;
    if (DISALLOWED_SCHEMES.some((scheme) => value.startsWith(scheme)))
      return false;

    const url = new URL(customHost);
    const protocol = url.protocol;

    // Allow only HTTP(S)
    if (protocol !== 'http:' && protocol !== 'https:') return false;

    // Disallow credentials and obfuscation
    if (url.username || url.password) return false;
    if (customHost.includes('@')) return false;

    const host = url.hostname;

    // Block empty hostname
    if (!host) return false;

    // Block URLs with encoded characters in hostname (potential bypass attempt)
    if (host.includes('%')) return false;

    // Block suspicious characters that might indicate injection attempts
    if (VALIDATION_PATTERNS.SUSPICIOUS_CHARS.test(host)) return false;

    // Block non-ASCII characters in hostname (homograph attack protection)
    // Prevents Unicode lookalike characters from spoofing legitimate domains
    if (!VALIDATION_PATTERNS.HOMOGRAPH_ATTACK.test(host)) return false;

    // Block trailing dots in hostname (can cause DNS rebinding issues)
    if (host.endsWith('.')) return false;

    // Split hostname once for reuse in multiple checks
    const hostParts = host.split('.');

    // Block excessive subdomain depth (potential DNS rebinding attack)
    // Limits the number of labels to prevent abuse
    if (hostParts.length > 10) return false;

    const trustedHosts = TRUSTED_CUSTOM_HOSTS(c);
    // Check against configurable allowed hosts (for local development or trusted domains)
    const isTrustedHost =
      trustedHosts.has(host) ||
      // Allow subdomains of .localhost
      (trustedHosts.has('localhost') && host.endsWith('.localhost'));

    if (isTrustedHost) {
      // Still validate port range if provided
      if (url.port && !isValidPort(url.port)) return false;
      return true;
    }

    // Block obvious internal/unsafe hosts and cloud metadata endpoints
    if (BLOCKED_HOSTS.includes(host as any)) return false;

    // Block AWS IMDSv2 endpoint variations
    if (host.startsWith('169.254.169.') || host.startsWith('fd00:ec2::')) {
      return false;
    }

    // Block internal/special-use TLDs often used in SSRF attempts
    if (
      BLOCKED_TLDS.some((tld) => host.endsWith(tld) && host !== 'localhost')
    ) {
      return false;
    }

    // Block private/reserved IPs (IPv4)
    if (isIPv4(hostParts) && (isPrivateIPv4(host) || isReservedIPv4(host))) {
      return false;
    }

    // Check for alternative IP representations (decimal, hex, octal)
    if (isAlternativeIPRepresentation(host, hostParts)) return false;

    // Block private/reserved IPv6 and IPv4-mapped IPv6
    if (host.includes(':')) {
      if (isLocalOrPrivateIPv6(host)) return false;

      // Check both IPv6-mapped and embedded IPv4 patterns
      const ipv4Match =
        host.match(VALIDATION_PATTERNS.IPV6_MAPPED_IPV4) ||
        host.match(VALIDATION_PATTERNS.IPV6_EMBEDDED_IPV4);

      if (ipv4Match) {
        const ip4 = ipv4Match[1];
        if (isPrivateIPv4(ip4) || isReservedIPv4(ip4)) return false;
      }
    }

    // Validate port if present
    if (url.port && !isValidPort(url.port)) return false;

    return true;
  } catch {
    return false;
  }
}

// Helper function to convert integer to IPv4 dotted decimal notation
function intToIPv4(num: number): string {
  const a = (num >>> 24) & 0xff;
  const b = (num >>> 16) & 0xff;
  const c = (num >>> 8) & 0xff;
  const d = num & 0xff;
  return `${a}.${b}.${c}.${d}`;
}

// Helper function to convert IPv4 dotted decimal to integer
function ipv4ToInt(ip: string): number {
  const [a, b, c, d] = ip.split('.').map((n) => Number(n));
  return ((a << 24) >>> 0) + (b << 16) + (c << 8) + d;
}

// Helper function to validate port numbers
function isValidPort(port: string): boolean {
  const p = parseInt(port, 10);
  return p > 0 && p <= 65535;
}

function isIPv4(parts: string[]): boolean {
  if (parts.length !== 4) return false;
  return parts.every((part) => {
    // Must be 1-3 digits
    if (!VALIDATION_PATTERNS.DIGITS_1_3.test(part)) return false;

    const num = Number(part);

    // Must be in range 0-255
    if (num < 0 || num > 255) return false;

    // Reject leading zeros (except for "0" itself)
    // This prevents octal interpretation ambiguity
    if (part.length > 1 && part.startsWith('0')) return false;

    return true;
  });
}

function isPrivateIPv4(ip: string): boolean {
  const ipInt = ipv4ToInt(ip);
  return IPV4_RANGES.PRIVATE.some(
    (range) => ipInt >= range.start && ipInt <= range.end
  );
}

function isReservedIPv4(ip: string): boolean {
  const ipInt = ipv4ToInt(ip);
  return IPV4_RANGES.RESERVED.some(
    (range) => ipInt >= range.start && ipInt <= range.end
  );
}

function isLocalOrPrivateIPv6(host: string): boolean {
  const h = host.toLowerCase();
  if (h === '::1' || h === '::') return true; // loopback/unspecified
  if (h.startsWith('fc') || h.startsWith('fd')) return true; // fc00::/7 (ULA)
  if (h.startsWith('fe80')) return true; // fe80::/10 (link-local)
  if (h.startsWith('fec0')) return true; // fec0::/10 (site-local, deprecated)
  return false;
}

function isAlternativeIPRepresentation(host: string, parts: string[]): boolean {
  // Check for decimal IP (e.g., 2130706433 for 127.0.0.1)
  // Valid range: 0 to 4294967295 (2^32 - 1)
  if (VALIDATION_PATTERNS.DIGITS_1_10.test(host)) {
    const num = parseInt(host, 10);
    if (num >= 0 && num <= 0xffffffff) {
      // Convert to dotted decimal and check if it's private/reserved
      const ip = intToIPv4(num);
      // Block if it resolves to a private or reserved IP
      if (isPrivateIPv4(ip) || isReservedIPv4(ip)) return true;
      // Also block public IPs in decimal format to prevent confusion
      return true;
    }
  }

  // Check for hex IP (e.g., 0x7f000001 for 127.0.0.1)
  if (VALIDATION_PATTERNS.HEX_IP.test(host)) {
    const num = parseInt(host, 16);
    if (num >= 0 && num <= 0xffffffff) {
      return true; // Block all hex IPs (no need to convert)
    }
  }

  // Check for octal or hex notation in any part (e.g., 0177.0.0.1 or 0x7f.0.0.1)
  if (
    parts.length === 4 &&
    parts.some((p) => VALIDATION_PATTERNS.ALTERNATIVE_IP_PART.test(p))
  ) {
    // Has octal or hex notation - block it
    return true;
  }

  // Check for shortened IP formats (e.g., 127.1 -> 127.0.0.1)
  if (parts.length >= 2 && parts.length < 4) {
    if (
      parts.every(
        (p) => VALIDATION_PATTERNS.DIGITS_ONLY.test(p) && Number(p) <= 255
      )
    ) {
      // Looks like a shortened IP format - block it
      return true;
    }
  }

  return false;
}
