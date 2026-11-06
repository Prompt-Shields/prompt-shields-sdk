# Upstream triage: Portkey changes since enterprise 2.28.0 (#208)

Triaged 2026-11-06 (2.29.0-2.33.0, then re-checked the same day through 2.33.1) against the triage points in [`FORK_NOTICE.md`](../FORK_NOTICE.md) (OSS `main` `669825c`, OSS `2.0.0` `8febc1d`, enterprise changelog 2.28.0).

Sources:

- The OSS repository `github.com/Portkey-AI/gateway` (MIT), fetched read-only on 2026-11-06.
- The enterprise changelog, through `scripts/upstream-watch.py --since-enterprise 2.28.0 --since-main 669825c --since-2-0 8febc1d`. The enterprise gateway is closed source. Every enterprise item ported below was re-implemented from the one-line changelog description. No Portkey code was copied.

## OSS repository

| Ref | Since triage point | Result |
|---|---|---|
| `main` | none: still `669825c` (2026-05-25) | Nothing to port |
| `2.0.0` | none: still `8febc1d` (2026-03-14) | Nothing to port |
| Tags and releases | none: latest is still `v1.15.2` | Nothing to port |
| Other branches | none updated after 2026-05-25. The newest are `fix/add-auth-validation-for-public-routes` (`55d653e`, 2026-05-19, already merged into `main`, so covered by the `main` triage point) and `fix/version-updates` (`0930486`, 2026-05-19, unmerged), which changes only `package-lock.json` | Dependency bumps are covered by our own audit below |

## Enterprise changelog 2.29.0 to 2.33.1 (91 entries)

Class is one of: **security**, **bug fix**, **provider**, **feature** or **irrelevant** (control plane, Portkey's log store, or a product we do not ship).

Status is one of:

- **Ported**: done in the first triage PR (#275), with a test; entries 12-14 and 19-21 in the F2 follow-up (#283); entries 60-62 and 78 in the second triage PR (`FORK_NOTICE.md` item 50).
- **In fork**: the fork already behaves this way. The column names where.
- **N/A**: the code involved is not in the fork.
- **Follow-up**: worth doing; listed below.
- **Skip**: out of scope under the *Upstream policy* in `FORK_NOTICE.md`.

| # | Release | Entry (summarised) | Class | Status | Notes |
|---|---|---|---|---|---|
| 1 | 2.29.0 | Updated dependencies (security) | security | Ported | We ran our own `npm audit` (2026-11-06) because Portkey does not say which packages. Runtime deps bumped: `hono` 4.9.7→4.13.13, `@hono/node-server` 1.13.5→1.19.17 (then 2.1.6, #293), `ws` 8.18.3→8.22.0; `zod` 4 (#284). `npm audit --omit=dev` now reports 0 vulnerabilities; the dev-only rest is F1 |
| 2 | 2.29.0 | OpenAI `/v1/decisions`, native and Jev schemas | feature | Follow-up | F5 |
| 3 | 2.29.0 | Workers AI Decisions API with pricing | feature | Follow-up | F5 |
| 4 | 2.29.0 | Azure OpenAI pricing falls back to `x-ms-served-model` | feature | Follow-up | F6 (Route cost accuracy) |
| 5 | 2.29.0 | Bedrock Mantle web-search tool calls priced | feature | Follow-up | F6 |
| 6 | 2.29.0 | MCP: SSE-only external servers | bug fix | Follow-up | PS MCP proxy (`src/mcp/`) speaks Streamable HTTP only; legacy HTTP+SSE transport is F8 |
| 7 | 2.29.0 | MCP: disabled tools/prompts/resources usable on servers exposing more than 100 | security | N/A | Upstream filters a paginated list. The PS proxy checks every `tools/call` against the allow/deny list by name, whatever the list size (`src/mcp/policy.ts`). It has no prompt or resource policies |
| 8 | 2.29.0 | MCP logs honour `traceparent` | feature | Follow-up | F8 |
| 9 | 2.29.0 | Messages API: non-native providers surface cache tokens | bug fix | In fork | `toMessagesUsage` in `src/adapters/shared.ts` (#73) |
| 10 | 2.29.0 | JWT plugin claim extraction | feature | Skip | Guardrail plugin |
| 11 | 2.29.0 | MCP: real status codes for blocks | irrelevant | N/A | Upstream MCP app |
| 12 | 2.29.1 | Trust store: emSign CA distrust | security | Ported | F2: the image is now `node:24.22.2-alpine3.24`, whose bundled root store no longer has the emSign ECC G3, C1 and C3 roots |
| 13 | 2.29.1 | System CA bundle without `emSign ECC Root CA - G3` | security | Ported | F2 |
| 14 | 2.29.1 | Node.js 24.22.1 | security | Ported | F2 (24.22.2) |
| 15 | 2.30.0 | NEAR AI provider | provider | Follow-up | F7 (#74 pattern) |
| 16 | 2.30.0 | Simple cache honours `max_age` | bug fix | Ported | The fork had a different bug in the same place. `max_age` (seconds) was added to `Date.now()` as milliseconds, so entries expired almost at once. Fixed by `cacheExpiresAt` in `src/middlewares/cache/index.ts` |
| 17 | 2.30.0 | Messages API: negative cost with large cached prefix | bug fix | In fork | `input_tokens` clamped at 0 in `toMessagesUsage` |
| 18 | 2.30.0 | Faster control-plane/S3 log writes | irrelevant | N/A | Portkey log store |
| 19 | 2.30.1 | Trust store: emSign CA distrust | security | Ported | F2 (same as 12) |
| 20 | 2.30.1 | System CA bundle update | security | Ported | F2 |
| 21 | 2.30.1 | Node.js 24.22.1 | security | Ported | F2 (24.22.2) |
| 22 | 2.31.0 | Hardened Fireworks AI upstream URL construction | security | Ported | Two changes in `src/providers/fireworks-ai/api.ts`. The account id is now a slug, and the image model path rejects `..`, empty segments and URL metacharacters. List query values are encoded |
| 23 | 2.31.0 | Hardened Vertex AI upstream URL construction | security | Ported | `assertVertexUrlParts` in `src/providers/google-vertex-ai/api.ts` checks the region (before, a region could rewrite the `<region>-aiplatform` host), the project id and the model. List query values are encoded |
| 24 | 2.31.0 | SSRF allowlist expanded | security | N/A | The fork has no allowlist to expand. It blocks private and metadata targets (`isSafeUpstreamURL`, `plugins/egress.ts`), and operators add hosts with `TRUSTED_CUSTOM_HOSTS` |
| 25 | 2.31.0 | Updated dependencies (security) | security | Ported | See 1 |
| 26 | 2.31.0 | OpenAI GPT Live endpoint | feature | Follow-up | F9 |
| 27-32 | 2.31.0 | Signals (beta): built-in and custom signals, logging, conditional routing on signals, rule signal, request classifier | feature | Skip | Routing is done by the PS router (`src/middlewares/router/`). Signals depend on Portkey's log store |
| 33 | 2.31.0 | Bedrock / Claude Platform on AWS: Messages `safeguards` | feature | Follow-up | F9 |
| 34 | 2.31.0 | Claude Platform on AWS: tiered pricing for long Haiku 5.5 prompts | feature | Follow-up | F6 |
| 35 | 2.31.0 | Gemini: distinct indexes for parallel tool calls when streaming | bug fix | In fork | `transformGeminiStreamParts` numbers tool calls per candidate (#72) |
| 36 | 2.31.0 | Responses: Codex custom tools become function tools for native providers | bug fix | Follow-up | F10 |
| 37 | 2.31.0 | OpenAI: WebSocket upgrade on `/v1/responses` | bug fix | Follow-up | F9 |
| 38 | 2.31.0 | MCP: per-integration upstream timeout | feature | In fork | `timeout_ms` per server (`src/mcp/config.ts`) |
| 39 | 2.31.0 | MCP log export metadata | irrelevant | N/A | Portkey log store |
| 40 | 2.31.0 | Messages API: `message_start.model` empty for non-native providers | bug fix | Ported | Falls back to the requested model (`src/adapters/messages/streamTransform.ts`, `src/handlers/adapterUtils.ts`) |
| 41 | 2.31.0 | Messages API: non-text/image `tool_result` blocks kept for Bedrock Converse | bug fix | Follow-up | F10 |
| 42 | 2.31.0 | `anthropic-beta` header reaches nested targets | bug fix | In fork | `anthropicBeta` is read from the request headers for each target in `constructConfigFromRequestHeaders` (`src/handlers/handlerUtils.ts`), not from the root config |
| 43 | 2.31.0 | Span id without a client trace header | irrelevant | N/A | Internal Portkey tracing. PS OTel spans are `src/middlewares/psOtel` |
| 44 | 2.31.0 | Remote pricing validated before caching | irrelevant | N/A | No remote pricing service. Prices come from the PS catalog |
| 45 | 2.31.0 | Gateway-local JWT auth: claim policies | feature | N/A | Not in the fork. PS OIDC (`src/middlewares/psOidc/`, item 29) is separate |
| 46 | 2.31.0 | Custom guardrail failure messages | feature | Skip | Guardrails |
| 47 | 2.31.0 | Gateway-local JWT: algorithms, audience, issuer, clock tolerance | security | N/A | As 45. PS OIDC already pins the issuer, audience and algorithms |
| 48 | 2.31.0 | MCP access policy recovers from fail-open | security | N/A | Upstream MCP app. The PS proxy fails closed |
| 49 | 2.31.0 | MCP guardrail cache sync | irrelevant | N/A | Control plane |
| 50 | 2.31.0 | PANW AIRS: block on error/rate limit | feature | Skip | Guardrails. Errored checks already honour `failOnError` (item 31) |
| 51 | 2.31.0 | PANW AIRS: Codex session ids | feature | Skip | Guardrails |
| 52 | 2.31.0 | Prometheus workspace label | irrelevant | N/A | `prom-client` not ported. PS `/metrics` is our own |
| 53 | 2.32.0 | Header Check / Header Transform guardrails log names only | security | N/A | Those guardrails are not in the fork (`plugins/default/` has neither) |
| 54 | 2.32.0 | Updated dependencies (security) | security | Ported | See 1 |
| 55 | 2.32.0 | MCP: OAuth client credentials for upstream servers | feature | Follow-up | F8 |
| 56 | 2.32.0 | Decisions API on Azure OpenAI / Foundry | feature | Follow-up | F5 |
| 57 | 2.32.0 | OTel complete logs: metadata and cost | irrelevant | N/A | Portkey log export |
| 58 | 2.32.0 | OpenAI: `moderation` passed through on chat completions | bug fix | Ported | `src/providers/openai/chatComplete.ts`. The config is shared by Azure OpenAI and other OpenAI-compatible providers |
| 59 | 2.32.0 | OpenAI: Codex `at-` tokens routed to the Codex backend | feature | Follow-up | F9 |
| 60 | 2.32.0 | Vertex: token cached for its real lifetime, one retry | bug fix | Ported | `getAccessToken` (`src/providers/google-vertex-ai/utils.ts`) caches for `expires_in` less 5 minutes, retries once on a network error, 429 or 5xx, and never caches a failure. The fork sets no gateway cache hooks, so a bounded in-process cache keyed by a SHA-256 of the whole credential is used |
| 61 | 2.32.0 | Vertex: blocked prompt returns a choice with the block reason | bug fix | Ported | `geminiBlockedPromptChoice` (`src/providers/google/chatComplete.ts`), for Vertex and the Gemini API, JSON and streams |
| 62 | 2.32.0 | Vertex: Model Armor / prohibited image gives `content_filter` | bug fix | Ported | `MODEL_ARMOR` and `IMAGE_PROHIBITED_CONTENT` in `src/providers/utils/finishReasonMap.ts` |
| 63 | 2.32.0 | Gemini/Vertex: tool result followed by a user message | bug fix | Follow-up | F10 |
| 64 | 2.32.0 | Gemini/Vertex: tool images in `/v1/messages` | bug fix | Follow-up | F10 |
| 65 | 2.32.0 | Claude Platform on AWS: OpenAI-format chat completions | bug fix | In fork | The PS-written provider uses the shared Anthropic chat transforms (`src/providers/claude-platform-aws/index.ts`) |
| 66 | 2.32.0 | Together AI: keep-alive messages break streams | bug fix | Ported | `getTogetherAIStreamData` skips SSE comments and empty events. `readStream` no longer yields an `undefined` final chunk |
| 67 | 2.32.0 | Messages API: stream without terminal event becomes an `error` event | bug fix | Follow-up | F10: needs care, because Gemini streams never send `[DONE]` |
| 68 | 2.32.0 | Circuit breaker: missing counts | bug fix | N/A | Upstream breaker not in the fork. The PS breaker (`src/middlewares/psFailover`) is separate |
| 69 | 2.32.0 | Agent Gateway: forward caller headers | irrelevant | N/A | Product not shipped |
| 70 | 2.32.0 | Guardrails: `soft_deny_200` | feature | Skip | Guardrails |
| 71 | 2.32.0 | Gateway-local JWT: default config, settings cache | irrelevant | N/A | As 45 |
| 72 | 2.32.0 | `PLUGIN_MAX_RESPONSE_BYTES` | security | In fork | Guardrail plugin responses are already size-capped by `plugins/egress.ts` (item 28) |
| 73 | 2.32.0 | Agent Gateway: in-cluster hostnames allowed | irrelevant | N/A | Product not shipped |
| 74 | 2.33.0 | Updated dependencies (security) | security | Ported | See 1 |
| 75 | 2.33.0 | MCP access policies GA | feature | N/A | Upstream MCP app. The PS proxy has its own policies |
| 76 | 2.33.0 | Auto caching for Anthropic models | feature | Follow-up | F9 (cost) |
| 77 | 2.33.0 | Bedrock: Portkey metadata as request metadata | feature | Skip | Portkey metadata model |
| 78 | 2.33.0 | Vertex: `excludeDomains`, `blockingConfidence` on search tools | bug fix | Ported | `transformGoogleTools` (`src/providers/google-vertex-ai/utils.ts`) forwards both on `googleSearch`. `enterpriseWebSearch` is not a supported tool in the fork |
| 79 | 2.33.0 | OpenAI/Azure: `prompt_cache_options` on `/v1/responses` | bug fix | Ported | `src/providers/open-ai-base/createModelResponse.ts` |
| 80 | 2.33.0 | Azure Responses: 404 without a default deployment | irrelevant | N/A | Portkey's multi-deployment integrations are not in the fork |
| 81 | 2.33.0 | Workers AI Decisions: Clef models, unified route | feature | Follow-up | F5 |
| 82 | 2.33.0 | Logging path proxy selection | irrelevant | N/A | Portkey log store |
| 83 | 2.33.0 | OTel complete logs: workspace, user agent | irrelevant | N/A | Portkey log export |
| 84 | 2.33.0 | Guardrail updates | feature | Skip | Guardrails |
| 85 | 2.33.0 | Bedrock guardrails `failOnError` | security | In fork | Every errored check honours `failOnError` (item 31) |
| 86 | 2.33.0 | PANW AIRS workspace key override | feature | Skip | Guardrails |
| 87 | 2.33.0 | Tripo3D v3 credits | irrelevant | N/A | No Tripo3D provider |
| 88 | 2.33.0 | Log masking of non-string hook credentials | security | In fork | `redactSecrets` masks a sensitive key's value whatever its type (`src/utils/redact.ts`) |
| 89 | 2.33.0 | OTel: status code on failed MCP/guardrail export | irrelevant | N/A | Portkey log export |
| 90 | 2.33.1 | Logging: unused Redis pub/sub events removed from the analytics queue | irrelevant | N/A | Portkey log store and analytics queue |
| 91 | 2.33.1 | Logging: `NODE_EXTRA_CA_CERTS` honoured again for log uploads | irrelevant | N/A | Portkey log uploads. The fork's own outbound calls run on Node, which reads `NODE_EXTRA_CA_CERTS` at start-up |

### Counts

| Class | Entries | Ported | In fork | N/A | Follow-up | Skip |
|---|---|---|---|---|---|---|
| security | 20 | 12 | 3 | 5 | 0 | 0 |
| bug fix | 22 | 9 | 5 | 1 | 7 | 0 |
| provider | 1 | 0 | 0 | 0 | 1 | 0 |
| feature | 30 | 0 | 1 | 2 | 13 | 14 |
| irrelevant | 18 | 0 | 0 | 18 | 0 | 0 |
| **Total** | **91** | **21** | **9** | **26** | **21** | **14** |

The four "Updated dependencies" entries (1, 25, 54, 74) are covered by a single dependency bump.

## Follow-ups (not implemented here)

- **F1 (security, deps).** The audit's remaining items:
  - `zod` 3 (GHSA-mrhx-c7xc-m3w5) is fixed only in zod 4. That is a major upgrade of the config validator. **Done** in #284 (zod 4); `@hono/node-server` 2.1.6 followed in #293. On 2026-11-06 `npm audit --omit=dev` reports 0 vulnerabilities. The remaining advisories are all in dev and build tooling (`wrangler`/`miniflare`/`undici`/`sharp`/`esbuild`, `@rollup/plugin-terser`/`serialize-javascript`, `jest` internals, `typescript-eslint`/`minimatch`, the `portkey-ai`/`openai` test client) and need major bumps.
  - Dev and build tooling: `rollup`, `@rollup/plugin-terser`/`serialize-javascript`, `wrangler`/`miniflare`/`undici`, `tsx`/`esbuild`, `jest` internals, `portkey-ai`/`openai` (test client), `form-data`.
  - `tmp` and `yaml`, pulled in by `patch-package`, which runs only at install.

  None is in the request path.
- **F2 (security, image).** The gateway image ran `node:20-alpine`. Node 20 reached end of life in April 2026, and its bundled root store still trusts the distrusted emSign CA. Move the image (and CI) to Node 24 LTS, then rebuild and run the image smoke test. **Done:** the image is `node:24.22.2-alpine3.24` (build and runtime stages) and the CI gateway job runs Node 24.
- **F5 (feature).** A Decisions API (`/v1/decisions`) for OpenAI, Workers AI and Azure (entries 2, 3, 56, 81), if the product wants it.
- **F6 (cost accuracy).** The catalog/pricing cases: Azure `x-ms-served-model` fallback, Bedrock Mantle web-search pricing, and tiered long-context pricing on Claude Platform on AWS (entries 4, 5, 34). Per the sync policy each should become its own issue.
- **F7 (provider).** NEAR AI (entry 15), using the #74 pattern from public docs.
- **F8 (MCP proxy).** Legacy HTTP+SSE upstream transport, `traceparent` on MCP telemetry, and OAuth client credentials for upstream servers (entries 6, 8, 55).
- **F9 (features).** GPT Live endpoint, the Messages `safeguards` parameter, WebSocket `/v1/responses`, Codex `at-` tokens, and Anthropic auto-caching (entries 26, 33, 37, 59, 76).
- **F10 (provider correctness, larger).** These touch the 2.0 transforms and each needs fixtures:
  - Gemini/Vertex: tool-result turn merging and tool images (entries 63, 64). Blocked prompt, Model Armor finish reason, token lifetime and retry, and search-tool parameters (60-62, 78) are **done** (`FORK_NOTICE.md` item 50).
  - Codex custom tools (36).
  - Bedrock Converse `tool_result` blocks (41).
  - Messages stream without a terminal event (67).
