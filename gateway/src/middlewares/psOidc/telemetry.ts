import type { OidcIdentity } from './identity';

/**
 * Collector telemetry fields for an OIDC-authenticated request.
 *
 * `user_id` follows the SDK's `ps_metadata.user_id` convention (an opaque
 * identifier, hashed before it leaves the host): the 32-hex subject hash by
 * default, the raw `sub` claim only with PS_OIDC_TELEMETRY_SUBJECT=raw, and
 * nothing with `off`. Email, name and other profile claims are never sent.
 * `user_group` is the operator-chosen name of the policy entry that applied,
 * not the IdP group value.
 */
export function oidcTelemetryFields(identity: OidcIdentity | undefined): {
  user_id?: string;
  user_group?: string;
} {
  if (!identity) return {};
  const out: { user_id?: string; user_group?: string } = {};
  const mode = identity.config.telemetrySubject;
  if (mode === 'hash') out.user_id = identity.subjectHash;
  else if (mode === 'raw') out.user_id = identity.subject;
  if (identity.group) out.user_group = identity.group.name;
  return out;
}
