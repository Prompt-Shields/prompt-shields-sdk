import { Context } from 'hono';
import { reportPolicyDenied } from '../psSecurityEvents';
import type { OidcConfig, PolicyGroup } from './config';
import { checkTarget, PolicyDenyReason } from './policy';

/**
 * The authenticated OIDC identity on a request, and the per-target policy
 * check tryPost calls. Kept apart from ./index.ts so request handling code
 * does not load jose (ES modules only) just to read the identity.
 */

export const OIDC_IDENTITY_KEY = 'psOidcIdentity';

export interface OidcIdentity {
  /** 32 hex characters; see subjectHash. */
  subjectHash: string;
  /** The raw `sub` claim. Only leaves the gateway with PS_OIDC_TELEMETRY_SUBJECT=raw. */
  subject: string;
  /** The policy entry that applied. */
  group?: PolicyGroup;
  config: OidcConfig;
}

export const oidcIdentityOf = (c: Context): OidcIdentity | undefined =>
  c.get(OIDC_IDENTITY_KEY as any) as OidcIdentity | undefined;

export const errorBody = (message: string, type: string, code: string) => ({
  error: { message, type, param: null, code },
});

/** The 403 for a policy denial, after recording a policy.denied event. */
export function policyDeniedResponse(
  c: Context,
  identity: OidcIdentity,
  reason: PolicyDenyReason,
  provider?: string,
  model?: string,
  groupsOverage = false
): Response {
  reportPolicyDenied(c, reason, {
    group: identity.group?.name,
    userHash:
      identity.config.telemetrySubject === 'off'
        ? undefined
        : identity.subjectHash,
    provider,
    model,
  });
  const what =
    reason === 'no_matching_group'
      ? groupsOverage
        ? 'your token lists no groups (Entra ID groups overage: use app roles or groups assigned to the application)'
        : 'your identity provider groups are not mapped to any gateway policy'
      : reason === 'provider_not_allowed'
        ? `provider "${provider}" is not allowed for your group`
        : `model "${model}" is not allowed for your group`;
  return new Response(
    JSON.stringify(
      errorBody(
        `Forbidden by gateway policy: ${what}`,
        'permission_error',
        'policy_denied'
      )
    ),
    { status: 403, headers: { 'content-type': 'application/json' } }
  );
}

/**
 * Policy check for one upstream target (called from tryPost). Returns the
 * 403 to send instead of calling the target, or undefined to proceed.
 */
export function oidcTargetDenied(
  c: Context,
  provider: string | undefined,
  model: string | undefined
): Response | undefined {
  const identity = oidcIdentityOf(c);
  if (!identity?.group) return undefined;
  const reason = checkTarget(identity.group, provider, model);
  return reason
    ? policyDeniedResponse(c, identity, reason, provider, model)
    : undefined;
}

/** The model of a request body, if it names one. */
export function requestModel(body: unknown): string | undefined {
  try {
    if (typeof FormData !== 'undefined' && body instanceof FormData) {
      const m = body.get('model');
      return typeof m === 'string' ? m : undefined;
    }
    const m = (body as any)?.model;
    return typeof m === 'string' ? m : undefined;
  } catch {
    return undefined;
  }
}
