/**
 * PS fork (#76): a stable `api_key_fingerprint` for gateway telemetry.
 *
 * The SDK reports sha256(provider api key)[:16]. With keyless upstream auth
 * the bearer the provider sees is a short-lived token, so fingerprinting it
 * would give a new value every hour. Here the fingerprint is taken from the
 * identity the credential belongs to, never from a minted token:
 *
 * - OAuth client credentials (`openai`, AWS `oauth`): token URL + client id
 * - AWS `assumedRole`: role ARN; static AWS keys: access key id
 * - Azure Entra client secret: tenant + client id; Azure managed/workload
 *   identity: the client id, when one was given
 * - Vertex service account JSON: the account's `client_email`
 * - OpenAI workload identity federation: the vendor service account id;
 *   Anthropic: organization id + service account id (operator settings,
 *   ./vendorWorkloadIdentity.ts)
 * - a static API key: sha256(key)[:16], exactly as the SDK computes it, so
 *   gateway events corroborate with registered key links (#93)
 *
 * The Python SDK computes the same values (`prompt_shields.credentials`), so
 * SDK and gateway events for one identity carry one fingerprint.
 *
 * Nothing is reported for short-lived bearers (Azure AD tokens, an API key
 * that is a JWT) or for the gateway host's own identity (AWS serviceRole,
 * Vertex workload, Azure managed identity without a client id).
 * Identity fingerprints are sha256("<kind>\n<parts>")[:16], so they never
 * collide with a key fingerprint. Off unless
 * PS_TELEMETRY_CREDENTIAL_FINGERPRINT=true.
 */
import { Options } from '../types/requestBody';

export const CREDENTIAL_FINGERPRINT_KEY = 'psCredentialFingerprint';

const JWT_RE = /^eyJ[\w-]*\.[\w-]+\.[\w-]*$/;

async function sha256Hex16(input: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(input)
  );
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
    .slice(0, 16);
}

const identity = (kind: string, ...parts: string[]) =>
  sha256Hex16([kind, ...parts].join('\n'));

/** `env` is the runtime env record (`env(c)`: process.env on Node). */
export function credentialFingerprintEnabled(
  env?: Record<string, any>
): boolean {
  return (
    String(env?.PS_TELEMETRY_CREDENTIAL_FINGERPRINT).toLowerCase() === 'true'
  );
}

/** The fingerprint of the credential identity behind providerOptions. */
export async function credentialFingerprint(
  o: Partial<Options>,
  env?: Record<string, any>
): Promise<string | undefined> {
  // Same precedence as the providers: workload auth wins when selected.
  if (o.openaiAuthType === 'workload' || o.anthropicAuthType === 'workload') {
    // The runtime env record (`env(c)`: process.env on Node).
    const e: Record<string, any> = env ?? {};
    const value = (k: string) =>
      typeof e[k] === 'string' && e[k].trim() ? String(e[k]).trim() : '';
    if (o.openaiAuthType === 'workload') {
      const sa = value('OPENAI_SERVICE_ACCOUNT_ID');
      return sa ? identity('openai-workload', sa) : undefined;
    }
    const org = value('ANTHROPIC_ORGANIZATION_ID');
    const sa = value('ANTHROPIC_SERVICE_ACCOUNT_ID');
    return org && sa ? identity('anthropic-workload', org, sa) : undefined;
  }
  // Same precedence as the providers (hasOAuthClientCredentials): any OAuth
  // field means OAuth is used and the static key is ignored.
  if (o.oauthTokenUrl || o.oauthClientId || o.oauthClientSecret) {
    if (!o.oauthTokenUrl || !o.oauthClientId) return undefined;
    return identity('oauth-client', o.oauthTokenUrl, o.oauthClientId);
  }
  switch (o.awsAuthType) {
    case 'assumedRole':
      return o.awsRoleArn ? identity('aws-role', o.awsRoleArn) : undefined;
    case 'serviceRole':
    case 'oauth':
      return undefined;
  }
  switch (o.azureAuthMode) {
    case 'entra':
      return o.azureEntraTenantId && o.azureEntraClientId
        ? identity('azure-entra', o.azureEntraTenantId, o.azureEntraClientId)
        : undefined;
    case 'managed':
      return o.azureManagedClientId
        ? identity('azure-managed', o.azureManagedClientId)
        : undefined;
    case 'workload':
      return o.azureWorkloadClientId
        ? identity('azure-workload', o.azureWorkloadClientId)
        : undefined;
  }
  if (o.azureAdToken) return undefined;
  if (o.vertexAuthType === 'workload') return undefined;
  const email = o.vertexServiceAccountJson?.client_email;
  if (typeof email === 'string' && email) return identity('gcp-sa', email);
  if (o.awsAccessKeyId) return identity('aws-key', o.awsAccessKeyId);
  const key = typeof o.apiKey === 'string' ? o.apiKey.trim() : '';
  if (!key || JWT_RE.test(key)) return undefined;
  return sha256Hex16(key);
}

/**
 * Records the fingerprint of the target that was actually called on the
 * request context for ps-telemetry. Last call wins (fallbacks, retries).
 * Fail-open: never affects the request.
 */
export async function recordCredentialFingerprint(
  c: { set: (key: string, value: unknown) => void } | undefined,
  providerOptions: Partial<Options>,
  env?: Record<string, any>
): Promise<void> {
  try {
    if (!c || !credentialFingerprintEnabled(env)) return;
    const fp = await credentialFingerprint(providerOptions, env);
    c.set(CREDENTIAL_FINGERPRINT_KEY, fp);
  } catch {
    /* fail-open */
  }
}
