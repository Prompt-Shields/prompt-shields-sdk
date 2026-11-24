/**
 * PS fork (security, #377): `upstreamFetch` for operator-configured egress
 * (OTLP exporters, OIDC discovery and JWKS, telemetry) with the host
 * resolved and checked (`withEgressGuard`, policy `metadata`): a name that
 * resolves to a link-local or cloud metadata address is refused, and on
 * Node the connection is pinned to the checked addresses. Hosts listed in
 * TRUSTED_CUSTOM_HOSTS keep their existing behaviour (not guarded).
 */
import { Context } from 'hono';
import { isTrustedUpstreamHost } from '../middlewares/requestValidator';
import { upstreamFetch } from './fetch';
import { EgressPolicy, withEgressGuard } from './egressGuard';

export async function guardedUpstreamFetch(
  url: string,
  options: RequestInit = {},
  policy: EgressPolicy = 'metadata',
  c?: Context
): Promise<Response> {
  if (isTrustedUpstreamHost(url, c)) return upstreamFetch(url, options);
  return upstreamFetch(url, await withEgressGuard(url, options, policy));
}
