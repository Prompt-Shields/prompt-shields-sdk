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
        ? provider
          ? `provider "${provider}" is not allowed for your group`
          : 'your group has a provider allowlist and the request names no provider'
        : model
          ? `model "${model}" is not allowed for your group`
          : 'your group has a model allowlist and the request names no model';
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
 * Policy check for one upstream target (called from tryPost and, for
 * realtime, from psOidc). Every name in `models` must be allowed (see
 * targetModels). With `requireModel`, a group with a model allowlist is
 * denied when there is no model at all, because the provider transform would
 * otherwise fill in its default model. Returns the 403 to send instead of
 * calling the target, or undefined to proceed.
 */
export function oidcTargetDenied(
  c: Context,
  provider: string | undefined,
  models: string | undefined | (string | undefined)[],
  requireModel = false
): Response | undefined {
  const identity = oidcIdentityOf(c);
  if (!identity?.group) return undefined;
  const names = (Array.isArray(models) ? models : [models]).filter(
    (m): m is string => typeof m === 'string' && m !== ''
  );
  let model = names[0];
  let reason = checkTarget(identity.group, provider, model);
  for (const m of names.slice(1)) {
    if (reason) break;
    model = m;
    reason = checkTarget(identity.group, provider, m);
  }
  if (!reason && requireModel && identity.group.models && !names.length)
    reason = 'model_not_allowed';
  return reason
    ? policyDeniedResponse(c, identity, reason, provider, model)
    : undefined;
}

/**
 * Endpoints whose provider transform may fill in a default model when the
 * request has none (e.g. Anthropic chat), so a missing model is denied when
 * the group restricts models. Files, batches, proxied calls and the like
 * have no model and are checked on the provider alone.
 */
export const MODEL_ENDPOINTS = new Set<string>([
  'complete',
  'chatComplete',
  'stream-complete',
  'stream-chatComplete',
  'stream-messages',
  'messages',
  'messagesCountTokens',
  'createModelResponse',
  'countResponseInputTokens',
  'embed',
  'rerank',
  'moderate',
  'imageGenerate',
  'imageEdit',
  'createSpeech',
  'createTranscription',
  'createTranslation',
  'ocr',
]);

/**
 * The model names that decide what a target runs: `override_params.model`
 * or else the body's model, plus the deployment for Azure, which runs the
 * deployment named in the URL whatever the body says.
 */
export function targetModels(
  providerOption: Record<string, any> | undefined,
  requestBody: unknown
): string[] {
  const override = providerOption?.overrideParams?.model;
  const models = [
    typeof override === 'string' && override
      ? override
      : requestModel(requestBody),
  ];
  if (providerOption?.provider === 'azure-openai')
    models.push(providerOption.deploymentId);
  if (providerOption?.provider === 'azure-ai')
    models.push(providerOption.azureDeploymentName);
  return models.filter(
    (m): m is string => typeof m === 'string' && m.trim() !== ''
  );
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
