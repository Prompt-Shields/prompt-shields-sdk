/**
 * Operator settings for workloadIdentity.test.ts. Imported before the app:
 * `src/utils/env.ts` reads them once at load time on Node.
 */
import { mkdtempSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

export const TOKEN_FILE = join(
  mkdtempSync(join(tmpdir(), 'ps-wif-')),
  'identity-token'
);
export const SUBJECT_1 =
  'eyJhbGciOiJSUzI1NiIsImtpZCI6ImsxIn0.eyJzdWIiOiJwb2QtMSJ9.c3ViamVjdC0x';
export const SUBJECT_2 =
  'eyJhbGciOiJSUzI1NiIsImtpZCI6ImsxIn0.eyJzdWIiOiJwb2QtMiJ9.c3ViamVjdC0y';
writeFileSync(TOKEN_FILE, SUBJECT_1);

Object.assign(process.env, {
  OPENAI_AMBIENT_CREDENTIALS_ENABLED: 'true',
  OPENAI_IDENTITY_TOKEN_FILE: TOKEN_FILE,
  OPENAI_IDENTITY_PROVIDER_ID: 'idp_regression',
  OPENAI_SERVICE_ACCOUNT_ID: 'svc_regression',
  ANTHROPIC_AMBIENT_CREDENTIALS_ENABLED: 'true',
  ANTHROPIC_IDENTITY_TOKEN_FILE: TOKEN_FILE,
  ANTHROPIC_FEDERATION_RULE_ID: 'fdrl_regression',
  ANTHROPIC_ORGANIZATION_ID: '00000000-0000-0000-0000-00000000000a',
  ANTHROPIC_SERVICE_ACCOUNT_ID: 'svac_regression',
});
