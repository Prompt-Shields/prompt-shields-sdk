/**
 * Suites that call live vendor APIs. They need network access and, for most,
 * a gitignored `.creds.json` (or vendor env vars) next to the test, so they
 * cannot pass offline. The default `npx jest` run skips them; run them with
 * `npm run test:integration` (jest.integration.config.js) when the
 * credentials are available. Paths are matched against the test file path.
 */
export const integrationTests = [
  // Gateway end to end against real providers (needs a running gateway too).
  '/tests/integration/.+\\.test\\.ts$',
  // Guardrail plugins that import `.creds.json` / `.parameters.json`.
  '/plugins/acuvity/scan\\.test\\.ts$',
  '/plugins/aporia/aporia\\.test\\.ts$',
  '/plugins/azure/azure\\.test\\.ts$',
  '/plugins/bedrock/bedrock\\.test\\.ts$',
  '/plugins/crowdstrike-aidr/aidr\\.test\\.ts$',
  '/plugins/exa/exa\\.test\\.ts$',
  '/plugins/f5-guardrails/scan\\.test\\.ts$',
  '/plugins/lasso/lasso\\.test\\.ts$',
  '/plugins/mistral/mistral\\.test\\.ts$',
  '/plugins/pangea/pangea\\.test\\.ts$',
  '/plugins/patronus/patronus\\.test\\.ts$',
  '/plugins/pillar/pillar\\.test\\.ts$',
  '/plugins/portkey/portkey\\.test\\.ts$',
  '/plugins/sydelabs/sydelabs\\.test\\.ts$',
  '/plugins/walledai/walledai\\.test\\.ts$',
  // Guardrail plugins that call the vendor API without credentials files
  // (promptfoo's public API; Prompt Security via PROMPT_SECURITY_* env vars).
  '/plugins/promptfoo/promptfoo\\.test\\.ts$',
  '/plugins/promptsecurity/promptsecurity\\.test\\.ts$',
];
