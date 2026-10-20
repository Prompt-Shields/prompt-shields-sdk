# Fork Notice

This directory is a fork of the [Portkey AI Gateway](https://github.com/Portkey-AI/gateway), MIT licensed. Upstream `LICENSE` is preserved verbatim alongside this notice.

## Upstream base

| | Commit | Date | Notes |
|---|---|---|---|
| Fork base | [`28ed85e`](https://github.com/Portkey-AI/gateway/commit/28ed85e) | 2026-02-19 | v1.15.2. Forked 2026-03-24. Confirmed by diffing `src/` and `plugins/` against every upstream `main` commit (`plugins/` identical; `src/` differs only by our changes). |
| OSS `main` triaged to | [`669825c`](https://github.com/Portkey-AI/gateway/commit/669825c) | 2026-05-25 | No upstream commits since. |
| OSS `2.0.0` triaged to | [`8febc1d`](https://github.com/Portkey-AI/gateway/commit/8febc1d) | 2026-03-14 | Pre-release; branches from our exact base. |
| Enterprise changelog triaged to | 2.28.0 | 2026-10-08 | Closed source. Features are re-implemented from the public changelog, never copied. |

**Decision (#56, 2026-10-12): stay on `28ed85e` and port features from `2.0.0`; do not rebase.** Run standalone (`FETCH_SETTINGS_FROM_FILE=true`), `2.0.0` rejects every request with `401 Invalid API Key`: its `authNMiddleWare` always validates the caller's key against Portkey's control plane (`ALBUS_BASEPATH`). The branch also does not build on Linux without fixing two filename-case bugs. The features we want (rerank, Responses/Messages adapters, tokenizer, payload-size limit, OTel/metrics, new providers) do not depend on the control plane, so they are ported one at a time (milestone *U1*, #57–#62). Security fixes keep being cherry-picked from `main`.

### Cherry-picked from upstream `main`

| Upstream | Change | Our adaptation |
|---|---|---|
| [`de0e6c5`](https://github.com/Portkey-AI/gateway/commit/de0e6c5), [`3def659`](https://github.com/Portkey-AI/gateway/commit/3def659) | Reject `x-portkey-forward-headers` inside forward headers or config `forward_headers` (request loop) | None (#55) |
| [`4e43b19`](https://github.com/Portkey-AI/gateway/commit/4e43b19), [`9bcaee4`](https://github.com/Portkey-AI/gateway/commit/9bcaee4) | Admin auth on the `/public` console and `/log/stream`; log stream disabled without a token | Token comes from `PS_GATEWAY_ADMIN_TOKEN` (min. 16 chars) instead of a committed `conf.json`, which this fork gitignores; constant-time comparison (#54) |
| [`e457022`](https://github.com/Portkey-AI/gateway/commit/e457022) | Remove the default admin token | Applies to upstream's `conf.json` only; our `conf.example.json` ships no token (#54) |
| [`c9ee943`](https://github.com/Portkey-AI/gateway/commit/c9ee943), [`55d653e`](https://github.com/Portkey-AI/gateway/commit/55d653e) | Redact provider options and headers in the log stream | None (#54) |

Skipped from `main`: [`b341133`](https://github.com/Portkey-AI/gateway/commit/b341133) (CrowdStrike AIDR guardrail metadata; guardrails are out of scope).

### Ported from upstream `2.0.0` (U1)

| Feature | Upstream files | Our adaptation |
|---|---|---|
| JSON payload-size limit (#62) | `src/middlewares/payloadSizeValidator.ts` | Limit read per request from `MAX_JSON_PAYLOAD_SIZE_IN_MB`; matches `application/json; charset=...`; Prometheus bookkeeping dropped. PS addition (#75): `MAX_FILE_PAYLOAD_SIZE_IN_MB` caps `multipart/form-data` uploads (413), and while it is set a multipart body without a valid `content-length` gets 411 |
| `/v1/rerank` (#58) | `src/handlers/rerankHandler.ts`, `src/types/rerankRequestBody.ts`, `src/utils/idGenerator.ts`, `src/providers/{cohere,jina,voyage,bedrock}/rerank.ts` | Handler reads the body from the Hono request (our style); Bedrock routes rerank to `bedrock-agent-runtime` |
| Databricks, Latitude, Pinecone providers (#57) | `src/providers/{databricks,latitude,pinecone}/` | Databricks workspace header plumbed through `constructConfigFromRequestHeaders` |
| Responses/Messages adapters (#59) | `src/adapters/`, `src/handlers/adapterUtils.ts` | `tryPost` wraps the unchanged pipeline (`tryPostToProvider`); native Responses list matches this tree; `normalizeTokenLimitForProvider` renames `max_completion_tokens` for chat configs that only map `max_tokens`; upstream tests updated for the `max_completion_tokens` change; #73: shared `adapters/shared.ts` maps cached/reasoning usage, provider-native stop reasons, thinking blocks and Anthropic error shapes in both adapters; Messages for Responses-only models (codex) goes via the native Responses API (`adapters/messagesViaResponses/`, see 14 below) |
| `/v1/health` (#61) | route in `src/index.ts` | Same response shape; used by the Helm chart's probes (`charts/prompt-shields`) |
| Provider fixes (#60) | `openai`, `azure-openai`, `azure-ai-inference`, `z-ai`, `cohere`, `ollama`, `ai21` | Stand-alone fixes only; per-provider `pricing` configs dropped. Google/Vertex, Bedrock, Anthropic, Fireworks and batch-output fixes need 2.0.0's core: #108 |
| Gemini/Vertex chat (#108) | `src/providers/google/{chatComplete,types,embed}.ts`, `src/providers/google-vertex-ai/{chatComplete,types,transformGenerationConfig,utils}.ts`, upstream tests | Thought signatures, media resolution, tool-message content; `getAccessToken` kept from this fork (context cache, plain fetch); workload-identity and model-lookup helpers not ported |
| Anthropic (#108) | `src/providers/anthropic/{chatComplete,types,api,complete}.ts` | Cached tokens counted in `prompt_tokens`; structured outputs and effort via `output_config`; tool `strict` not forwarded (reverted upstream in enterprise 2.4.4); Vertex Claude uses the shared Anthropic transforms; #9: chat responses and the final stream chunk keep `usage.cache_creation` (cache writes by TTL) |
| Batch output, Fireworks, Bedrock fixes (#108) | `cohere`/`azure-ai-inference` `getBatchOutput.ts`, `fireworks-ai/*` | `src/utils/fetch.ts` is a plain-fetch shim of upstream's names; OpenAI batch fallback and two Bedrock chat fixes hand-ported; Fireworks keeps this fork's `index.ts` |
| Bedrock full port (#108) | `src/providers/bedrock/*`, `src/providers/anthropic-base/{messages,utils/streamGenerator}.ts`, `src/utils/awsAuth.ts`, `src/providers/sagemaker/api.ts` | `Environment()` accepts the runtime env record (2.0) or a Hono context (ours); `services/cache/cacheService.ts` is an in-process TTL shim of upstream's Redis/KV `requestCache`; `assumedRole`/`serviceRole` resolve through one helper (`providerAssumedRoleCredentials`); host credentials (env, shared files, IRSA, pod identity, ECS, IMDS) only with `AWS_AMBIENT_CREDENTIALS_ENABLED=true`, and are no longer read at module load; signing only for `https` hosts under `AWS_ENDPOINT_DOMAIN` (or a fixed domain the provider passes, `api.aws` for `claude-platform-aws`), region format checked, STS query encoded; `uploadFile` keeps this fork's web-stream implementation |
| Batch output streaming, Vertex batch upload (#108) | `src/providers/{openai,azure-openai,google-vertex-ai}/getBatchOutput.ts`, `src/providers/google-vertex-ai/{uploadFile,createBatch}.ts`, `createLineSplitter` in `src/handlers/streamHandlerUtils.ts` | Upstream switches to `node-fetch` plus Node streams (`nodeLineReader`) so its proxy agents apply; this fork keeps one web-stream path for Node and Workers on plain `fetch`, so `nodeLineReader` and `externalServiceFetchWithNodeFetch` are not ported. Ported: octet-stream pass-through, `createLineSplitter` emitting bytes and decoding with `stream: true`, Vertex output-file probe, row-id fix and error responses, resource-name models in `createBatch`. Upstream resolves custom-model base models via the Vertex API (`getModelProviderFromModelID`); not ported, upload and output use the name's last segment. PS hardening: provider-returned output locations are followed only to GCS (`gs://`) and Azure Blob Storage hosts |

End-to-end regression tests for these ports (whole app, offline, with ps-telemetry checks) are in `src/tests/regression/` (#69); transform-level tests sit next to each provider.

Not ported:
- `/v1/gateway/tokenize` (#62). It is a wrapper over upstream's control-plane-coupled pricing engine (`services/winky`), so token counting will be built on our catalog instead (#71).
- Prometheus via `prom-client` and `addMetricsMiddleware` (#61): Node-only and tied to control-plane context keys. `/metrics` is our own dependency-free implementation (`src/middlewares/psMetrics/`).
- OTel trace ingest and `customLogHandler` (#61): wrappers over Portkey's log store. Span ingest belongs in the collector (#92).
- MCP Gateway, upstream `src/mcp/` (#63): a separate proxy app whose auth needs the Portkey control plane, and whose `emitLog` records tool arguments and results. Not ported. Our `src/mcp/` is a PS-written enforcing proxy on the main app (`/v1/mcp/:server`, Phase 1 of [`docs/mcp-tool-call-discovery.md`](docs/mcp-tool-call-discovery.md); see [`docs/mcp-proxy.md`](docs/mcp-proxy.md)). It shares no code with upstream's and records names only.

### Upstream files modified in this fork

Every file that exists at the fork base `28ed85e` and differs here (regenerate with the loop in *How to sync upstream*). Files added by this fork are not listed.

- `.dockerignore`
- `Dockerfile`
- `README.md`
- `conf.example.json`
- `deployment.yaml` (deleted; replaced by the Helm chart in `charts/prompt-shields`)
- `jest.config.js`
- `package.json`
- `plugins/azure/shieldPrompt.ts`
- `plugins/azure/utils.ts`
- `plugins/types.ts`
- `plugins/utils.test.ts`
- `plugins/utils.ts`
- `src/globals.ts`
- `src/handlers/createTranscriptionHandler.ts`
- `src/handlers/createTranslationHandler.ts`
- `src/handlers/handlerUtils.ts`
- `src/handlers/imageEditsHandler.ts`
- `src/handlers/modelResponsesHandler.ts`
- `src/handlers/modelsHandler.ts`
- `src/handlers/proxyHandler.ts`
- `src/handlers/realtimeHandler.ts`
- `src/handlers/realtimeHandlerNode.ts`
- `src/handlers/retryHandler.ts`
- `src/handlers/services/cacheService.ts`
- `src/handlers/services/hooksService.ts`
- `src/handlers/services/logsService.ts`
- `src/handlers/services/providerContext.ts`
- `src/handlers/services/requestContext.ts`
- `src/handlers/streamHandler.ts`
- `src/handlers/streamHandlerUtils.ts`
- `src/handlers/websocketUtils.ts`
- `src/index.ts`
- `src/middlewares/cache/index.ts`
- `src/middlewares/hooks/index.ts`
- `src/middlewares/log/index.ts`
- `src/middlewares/requestValidator/index.ts`
- `src/middlewares/requestValidator/schema/config.ts`
- `src/providers/ai21/api.ts`
- `src/providers/ai21/embed.ts`
- `src/providers/anthropic-base/messages.ts`
- `src/providers/anthropic-base/utils/streamGenerator.ts`
- `src/providers/anthropic/api.ts`
- `src/providers/anthropic/chatComplete.ts`
- `src/providers/anthropic/complete.ts`
- `src/providers/anthropic/types.ts`
- `src/providers/azure-ai-inference/api.ts`
- `src/providers/azure-ai-inference/chatComplete.ts`
- `src/providers/azure-ai-inference/getBatchOutput.ts`
- `src/providers/azure-ai-inference/index.ts`
- `src/providers/azure-ai-inference/messages.ts`
- `src/providers/azure-openai/api.ts`
- `src/providers/azure-openai/chatComplete.ts`
- `src/providers/azure-openai/getBatchOutput.ts`
- `src/providers/azure-openai/imageGenerate.ts`
- `src/providers/azure-openai/index.ts`
- `src/providers/azure-openai/utils.ts`
- `src/providers/bedrock/api.ts`
- `src/providers/bedrock/chatComplete.ts`
- `src/providers/bedrock/complete.ts`
- `src/providers/bedrock/countTokens.ts`
- `src/providers/bedrock/createBatch.ts`
- `src/providers/bedrock/createFinetune.ts`
- `src/providers/bedrock/getBatchOutput.ts`
- `src/providers/bedrock/index.ts`
- `src/providers/bedrock/messages.ts`
- `src/providers/bedrock/retrieveFile.ts`
- `src/providers/bedrock/retrieveFileContent.ts`
- `src/providers/bedrock/types.ts`
- `src/providers/bedrock/uploadFile.ts`
- `src/providers/bedrock/uploadFileUtils.ts`
- `src/providers/bedrock/utils.ts`
- `src/providers/bedrock/utils/messagesUtils.ts`
- `src/providers/bytez/api.ts`
- `src/providers/cohere/api.ts`
- `src/providers/cohere/chatComplete.ts`
- `src/providers/cohere/complete.ts`
- `src/providers/cohere/getBatchOutput.ts`
- `src/providers/cohere/index.ts`
- `src/providers/cohere/types.ts`
- `src/providers/fireworks-ai/api.ts`
- `src/providers/fireworks-ai/cancelFinetune.ts`
- `src/providers/fireworks-ai/chatComplete.ts`
- `src/providers/fireworks-ai/listFiles.ts`
- `src/providers/fireworks-ai/retrieveFile.ts`
- `src/providers/fireworks-ai/uploadFile.ts`
- `src/providers/fireworks-ai/utils.ts`
- `src/providers/google-vertex-ai/api.ts`
- `src/providers/google-vertex-ai/chatComplete.ts`
- `src/providers/google-vertex-ai/createBatch.ts`
- `src/providers/google-vertex-ai/getBatchOutput.ts`
- `src/providers/google-vertex-ai/index.ts`
- `src/providers/google-vertex-ai/retrieveFile.ts`
- `src/providers/google-vertex-ai/transformGenerationConfig.ts`
- `src/providers/google-vertex-ai/types.ts`
- `src/providers/google-vertex-ai/uploadFile.ts`
- `src/providers/google-vertex-ai/utils.ts`
- `src/providers/google/chatComplete.ts`
- `src/providers/google/embed.ts`
- `src/providers/google/types.ts`
- `src/providers/index.ts`
- `src/providers/jina/api.ts`
- `src/providers/jina/index.ts`
- `src/providers/mistral-ai/api.ts`
- `src/providers/mistral-ai/index.ts`
- `src/providers/ollama/chatComplete.ts`
- `src/providers/ollama/embed.ts`
- `src/providers/open-ai-base/index.ts`
- `src/providers/openai/api.ts`
- `src/providers/openai/chatComplete.ts`
- `src/providers/openai/getBatchOutput.ts`
- `src/providers/openai/imageGenerate.ts`
- `src/providers/openai/index.ts`
- `src/providers/sagemaker/api.ts`
- `src/providers/types.ts`
- `src/providers/voyage/api.ts`
- `src/providers/voyage/index.ts`
- `src/providers/z-ai/api.ts`
- `src/providers/z-ai/index.ts`
- `src/public/index.html`
- `src/services/transformToProviderRequest.ts`
- `src/shared/services/cache/index.ts`
- `src/shared/utils/logger.ts`
- `src/start-server.ts`
- `src/types/MessagesRequest.ts`
- `src/types/MessagesStreamResponse.ts`
- `src/types/messagesResponse.ts`
- `src/types/modelResponses.ts`
- `src/types/requestBody.ts`
- `src/utils/env.ts`
- `tests/integration/src/handlers/tryPost.test.ts`
- `tests/unit/src/handlers/services/providerContext.test.ts`

### How to sync upstream

```bash
# 1. What changed upstream (OSS) and in the enterprise changelog since the triage points above
python3 scripts/upstream-watch.py --since-enterprise 2.28.0 --since-main 669825c --since-2-0 8febc1d

# 2. Re-confirm the base: an empty plugins/ diff and a src/ diff limited to PS files
git clone --filter=blob:none https://github.com/Portkey-AI/gateway /tmp/portkey
mkdir -p /tmp/portkey-base && git -C /tmp/portkey archive 28ed85e | tar -x -C /tmp/portkey-base
diff -rq /tmp/portkey-base gateway -x node_modules

# 3. Regenerate the modified-files list below
(cd gateway && git ls-files | while read f; do [ -f /tmp/portkey-base/$f ] && ! cmp -s /tmp/portkey-base/$f $f && echo "$f"; done)
```

File an issue for every `security` entry and for any `review` entry that serves discovery or Route cost accuracy, then update the triage points in the table above in the same PR. Autopilot runs step 1 weekly (see `.claude/skills/autopilot/SKILL.md`, phase 6b).

## Upstream policy

We track upstream Portkey at a known commit. New upstream changes are evaluated case-by-case:

**Merge selectively (relevant to discovery):**
- New provider adapters in `src/providers/*` — every new provider is one more LLM we can discover
- Streaming/SSE bug fixes
- Multi-modal handler fixes (vision, audio, image, realtime)
- MCP Gateway observability primitives
- OpenAI-API-compatibility fixes
- Security patches and dependency bumps

**Skip (out of scope for discovery):**
- New routing strategies (fallbacks, weighted load balancing, conditional routing)
- Caching layers (simple or semantic)
- Guardrails additions
- Virtual-key / secure-key-management features
- Cost-optimization routing
- RBAC and tenancy primitives — these live in the Atlas AI dashboard repo

## Modifications by Prompt Shields

1. **Added** `src/middlewares/ps-telemetry.ts` — AI discovery telemetry middleware
2. **Added** PS-specific environment variables: `PS_COLLECTOR_URL`, `PS_API_KEY`
3. **Added** `X-PS-*` HTTP header parsing for business-context annotations
4. **Disabled** routing/caching/guardrails defaults in `conf.example.json` and `initializeSettings.ts`
5. **Documented** the keep/strip policy in `README.md` for downstream maintainers
6. **Hardened** (re-implementations of Portkey enterprise fixes, #65): upstream URL SSRF check on provider-built URLs (`isSafeUpstreamURL`), credential masking in gateway logs (`src/utils/redact.ts`), config target fan-out cap (`MAX_ROOT_CONFIG_TARGETS`, `MAX_CONDITIONAL_ROOT_CONFIG_TARGETS`, `MAX_NESTED_CONFIG_TARGETS`), custom-host response size cap (`PS_MAX_CUSTOM_HOST_RESPONSE_BYTES`, default 50 MiB), and provider credential headers taking precedence over forwarded or proxied client headers.
   Limits: the upstream URL check is hostname-only (no DNS resolution, so no DNS-rebinding or redirect protection), rejects every IPv6 literal not listed in `TRUSTED_CUSTOM_HOSTS`, and does not cover providers with custom request handlers, which build their own URLs. The response size cap applies to custom-host upstreams only.
7. **Ported with changes** the upstream admin login (`src/middlewares/adminAuth`, the login modal in `src/public/index.html`): the token comes from `PS_GATEWAY_ADMIN_TOKEN` instead of `conf.json`, malformed session cookies are treated as absent, and expired sessions are pruned. The session cookie is `HttpOnly; SameSite=Strict` but not `Secure`, because the console is served over plain HTTP on localhost
8. **Hardened** AWS credential handling in the Bedrock/SageMaker port (#108): Portkey 2.0 lets a caller pick `awsAuthType=serviceRole`, or `assumedRole` with no configured keys, and then signs with the gateway host's own AWS identity. Here both need `AWS_AMBIENT_CREDENTIALS_ENABLED=true` (otherwise `serviceRole` is 403 and `assumedRole` must use `AWS_ASSUME_ROLE_ACCESS_KEY_ID`/`SECRET` or `AWS_ASSUME_ROLE_SOURCE_ARN`). `assumedRole` without a role ARN is a 400, so a caller can never get the operator's assume-role keys themselves. `AWS_ENDPOINT_DOMAIN` is read per request, so Workers bindings apply. Requests are only signed for `https://*.<AWS_ENDPOINT_DOMAIN>` hosts, so a crafted `x-portkey-aws-region` or S3 bucket cannot send signed headers (access key id, session token) to another host.
9. **Hardened** Azure host-identity auth: Portkey lets a caller send `x-portkey-azure-auth-mode: managed` (a token for the host's managed identity from IMDS) or `workload` (the host's federated token from `AZURE_FEDERATED_TOKEN_FILE`) and calls Azure OpenAI or Azure AI Inference as the gateway. Here both need `AZURE_AMBIENT_CREDENTIALS_ENABLED=true` and otherwise return 403 (`assertAzureAuthModeAllowed` in `src/providers/azure-openai/utils.ts`). The Azure guardrail plugins (`plugins/azure/`) refuse `managed` credentials the same way and fail closed (verdict `false`) instead of skipping the check; other token errors still fail open as upstream. `shieldPrompt` and `protectedMaterial` now pass the runtime env and cache options to `getAccessToken`, like `contentSafety` and `pii`. The host token is also kept on Azure (`assertAzureAmbientUpstreamIsAzure`, checked before minting): `managed`/`workload` refuse a custom host (config or header) with 400, require resource names to match `^[a-z0-9][a-z0-9-]{0,62}$`, and require the Foundry URL and the final upstream URL to be `https`/`wss` on a Microsoft Azure AI host suffix with no port or user info, and only accept a Cognitive Services `azureEntraScope`. Blob Storage (with a storage-audience token) is allowed only for the gateway's own batch-output download (`retrieveFileContent` with the storage scope set in `getBatchOutput.ts`). The guardrail plugins' `managed` mode refuses a `customHost` and resource names outside that pattern (fail closed) and asks IMDS for `https://cognitiveservices.azure.com/` rather than for `resourceName`.

10. **Added** (re-implemented from the Portkey enterprise changelog 2.18-2.23, #75): `POST /v1/ocr` in the Mistral OCR request shape for `mistral-ai`, Vertex-hosted Mistral (`mistralai.<model>`, `rawPredict`) and Azure AI Foundry (`<azureFoundryUrl>/ocr`); `elevenlabs` and `deepgram` providers for `/v1/audio/speech` and `/v1/audio/transcriptions`, on fixed vendor hosts (the ElevenLabs voice must match a voice-ID pattern before it becomes a path segment). Their transcription calls use provider request handlers, which ignore custom hosts. `ps-telemetry` emits `modality` and non-token `usage_units`/`usage_unit_type` (seconds, pages, characters). The transcription and translation handlers read the form with `c.req.formData()` (Hono's body cache) instead of `c.req.raw.formData()`, so `ps-telemetry` and `psOtel` take the `model` from the parsed form; for multipart bodies they read nothing else and never re-read the upload.
11. **Added** keyless upstream auth (re-implemented from the Portkey enterprise changelog 2.8, 2.19, 2.20, #76; first slice): OAuth 2.0 client credentials for the `openai` provider (including OpenAI-compatible `custom_host` upstreams) in `src/utils/oauthClientCredentials.ts`, and Vertex AI `vertexAuthType: workload` using the host's Google identity from the metadata server in `src/utils/gcpAmbientAuth.ts`. Hardening: the caller-supplied token URL must be `https`, pass `isSafeUpstreamURL`, and is refused for IP literals and loopback or `.internal`/`.local` names (or must match `OAUTH_TOKEN_URL_ALLOWED_HOSTS` when set); redirects are not followed; tokens are cached under a SHA-256 of (token URL, client id, secret hash, scope, audience); token-endpoint bodies never reach errors. `workload` returns 403 unless `GCP_AMBIENT_CREDENTIALS_ENABLED=true`, before any metadata request. `workload` is also refused (400) with a custom host or a `vertexRegion` outside `[a-z0-9-]`, so the host's token only goes to `*aiplatform.googleapis.com` / `storage.googleapis.com`. Precedence: when any of the OAuth token URL, client id or client secret is supplied, OAuth is used and the static `apiKey` is ignored; a partial set is a 400.
   Limits: the token URL check is hostname-only like the custom-host check (no DNS resolution, so no DNS-rebinding protection); operators who accept untrusted callers should set `OAUTH_TOKEN_URL_ALLOWED_HOSTS`.

11. **Added** (first slice of #74, written from the vendors' public OpenAI-compatible API docs, no enterprise code): `minimax`, `byteplus`, `lightning-ai` from the Portkey enterprise changelog 2.9-2.16, and the EU-hosted `berget`, `scaleway`, `regolo`, `ionos` and `stackit` for sovereignty routing. Each reuses the `open-ai-base` chat (and, where the vendor offers it, embeddings) transforms on a fixed vendor base URL.
    Second slice: `meta` (Meta Llama API via its OpenAI compatibility endpoint `https://api.llama.com/compat/v1`, enterprise 2.25.1, chat only) and `scx-ai` (SCX.ai, Australian sovereign inference, `https://api.scx.ai/v1`, enterprise 2.21, chat only), same pattern.
    Third slice: `claude-platform-aws` (Claude Platform on AWS, enterprise 2.10; written from Anthropic's and AWS's public docs since Portkey's code is closed). It is Anthropic's first-party API on `https://aws-external-anthropic.<region>.api.aws/v1` (region required and format-checked, no default), so the Anthropic chat, stream and Messages transforms are reused unchanged. Auth is SigV4 with service `aws-external-anthropic` through the Bedrock helpers (`generateAWSHeaders`, `providerAssumedRoleCredentials`, so `assumedRole`/`serviceRole` keep the #108 hardening), or an AWS-console API key as `x-api-key` (`aws_auth_type: apiKey`, or an `api_key` with no AWS keys). Every request carries `anthropic-version` and `anthropic-workspace-id` (`anthropic_aws_workspace_id`, `x-portkey-anthropic-aws-workspace-id` or `anthropic-workspace-id`); a request without one is a 400. Signing only happens for the exact regional host, so a `custom_host` never receives signed credentials. `GET /v1/models` goes to the provider's Models API. The `meta` provider also gets offline regression coverage for streaming and `GET /v1/models` (both already work through the compat endpoint); Meta's messages, responses and files are not on its documented compat API and stay unwired.
    Fourth slice (written from AWS's and fal's public docs plus the enterprise changelog; Portkey's code is closed and OSS `main` has neither): `bedrock-mantle` (Amazon Bedrock Mantle, enterprise 2.7) on `https://bedrock-mantle.<region>.api.aws` (region required and format-checked). Chat Completions and Responses (native, added to the Responses adapter's native list) use the `open-ai-base` transforms on `/v1`, or on `/openai/v1` for OpenAI's GPT models other than gpt-oss (per AWS's model cards; `bedrock_mantle_api_path` / `x-portkey-bedrock-mantle-api-path` picks one of the two explicitly). `/v1/messages` for Claude models is native on `/anthropic/v1/messages` (Anthropic Messages shape plus `safeguards`, `anthropic-version` always sent, the betas Mantle rejects — `advanced-tool-use`, `extended-cache-ttl`, `prompt-caching-scope`, `redact-thinking` — dropped from `anthropic-beta`, enterprise 2.27); other models go through the Messages adapter. Auth is SigV4 with service `bedrock-mantle` through the Bedrock helpers (static keys, `assumedRole`, `serviceRole` behind `AWS_AMBIENT_CREDENTIALS_ENABLED`) or a Bedrock API key as `Authorization: Bearer` (`aws_auth_type: apiKey`, or an `api_key` with no AWS keys); signing only happens for the exact regional host (https, no port or userinfo), so a `custom_host` never receives signed credentials. OAuth client credentials (enterprise 2.28) are not ported. `fal-ai` (fal.ai, enterprise 2.9): `/v1/images/generations` posts to `https://fal.run/<model id>` (the model id is validated as `owner/app[/path]`, no `.`/`..` segments) with `Authorization: Key <key>`; `n` → `num_images`, `WxH` sizes → `image_size`, `b64_json` → `sync_mode` with data URIs returned as `b64_json`. Video and audio models have no unified route and go through the proxy (`POST /v1/<model id>`); only the synchronous `fal.run` API is used, not the queue. `src/handlers/proxyHandler.ts` now reads the request body through `c.req` (Hono's body cache) instead of `c.req.raw`, because `psRouter` already consumes the raw body of every JSON POST and proxied JSON POSTs failed with `Body has already been read`. Security: a proxy path that does not start with `/` after the `/v1` or `/v1/proxy` prefix (`/v1/proxy.evil.com/x`) is refused with a 400; appended to a bare-host base URL such as `https://fal.run` it changed the host the provider credentials were sent to.
12. **Hardened** the response cache key (`src/middlewares/cache/index.ts`, `src/handlers/services/cacheService.ts`): upstream keys on request body + endpoint only, so callers with different credentials shared cached responses. The key now includes `computeCacheScope`, a SHA-256 of the provider, the caller's credential/identity provider options and incoming headers, the values of headers forwarded upstream (proxy and `forward_headers`, minus tracing headers), and the upstream URL. The write-back in `memoryCache` reuses the scope the read recorded per target (`psCacheScopes` on the Hono context) instead of recomputing it from the logged options; a read or write without a scope is skipped.
13. **Added** (re-implemented from the Portkey enterprise changelog 2.25.1, #73; not in OSS 2.0.0): `POST /v1/responses/{id}/cancel` (endpoint `cancelModelResponse`; `openai`, `azure-openai`) and `POST /v1/responses/input_tokens` (endpoint `countResponseInputTokens`; `openai`, body limited to the fields OpenAI counts). Other providers get a 400 `unsupported_operation` from `applyAdapterRequestTransform` before any upstream call (`RESPONSES_PROVIDER_OPERATIONS` in `src/handlers/adapterUtils.ts`). Cancel is not cached, and its returned usage is not counted again by `ps-telemetry`, `/metrics` or `psOtel` (`reportsOwnUsage`), since the create call already reported it.
14. **Added** (re-implemented from the Portkey enterprise changelog 2.16 "Messages API to Responses API routing" and 2.20 "Azure OpenAI codex via `/v1/messages`", #73; not in OSS 2.0.0): `/v1/messages` for `openai` and `azure-openai` models that only the Responses API serves (`codex` in the model id, `o1-pro`, `o3-pro`, `gpt-5[.x]-pro`, the deep-research models; `routesMessagesViaResponses` in `src/adapters/messagesViaResponses/`) is sent to the provider's native `/responses` instead of through the Chat Completions adapter, and the Responses JSON or SSE is translated back to Messages (thinking from reasoning summaries, text, `tool_use` from function calls, cached tokens split out of `input_tokens`). The request uses `store: false`; `temperature`, `top_p`, `stop_sequences`, `metadata.user_id`, thinking blocks and Anthropic server tools are not forwarded. `src/providers/openai/api.ts` and `src/providers/azure-openai/api.ts` map `createModelResponse` to `/responses` when the gateway path is not a Responses path. A stream that ends without `response.completed` ends with an Anthropic `error` event (no invented `message_stop`), so `ps-telemetry` still marks it truncated.
15. **Fixed** (re-implemented from the Portkey enterprise changelog 2.7, 2.20 and 2.26, #72; not in OSS `main` or 2.0.0): the `memoryCache` write-back (`src/middlewares/cache/index.ts`) only stores a complete result (`isCacheableResponse`: status 200 and `application/json`, so never an SSE or other streamed body, an error or the 246 hooks-failed status; `isCompleteResponseBody`: no `error`, no Anthropic `type: "error"`, no unfinished Responses `status`). It writes back the last attempt (the response the client got) rather than upstream's `requestOptions[0]`, which after a fallback is the failed attempt. Gemini on Google AI Studio and Vertex AI (`src/providers/google/chatComplete.ts`, `src/providers/google-vertex-ai/chatComplete.ts`): with strict OpenAI compliance a candidate that returned function calls finishes with `tool_calls` instead of `stop` (`transformGeminiFinishReason`), in JSON, batch output and streams, where `streamState` remembers per candidate that an earlier chunk carried the calls (`geminiStreamToolCalls`); Vertex streams read each candidate's own finish reason instead of the first candidate's. Anthropic keepalive `ping` events already pass through native `/v1/messages` streams unchanged (no stream transform on that path); a regression test pins it.
16. **Changed** (#73 follow-ups to 14, fork behaviour; not in OSS 2.0.0): (a) the routing can be forced or disabled per target with the config key `messages_via_responses` (boolean; validated in `src/middlewares/requestValidator/schema/config.ts`, inherited by nested targets in `src/handlers/handlerUtils.ts` like `strict_open_ai_compliance`, `messagesViaResponses` on `Options` in `src/types/requestBody.ts`) or the request header `x-portkey-messages-via-responses: true|false`; the target key wins over the header, and only `openai` / `azure-openai` targets are ever routed. (b) With `thinking` enabled the request asks for `reasoning.encrypted_content`; the payload is returned in the thinking `signature` (or `redacted_thinking.data`) as `ps-rsn1:<provider>:<payload>` and sent back as a `reasoning` item only to the provider that issued it; Anthropic signatures are never forwarded. (c) `web_search_*` maps to the Responses `web_search` tool (`allowed_domains`, `user_location`; `max_uses` dropped; `blocked_domains` → 400), and `web_search_call` output to `server_tool_use` + `web_search_tool_result`; a `tool_choice` naming a dropped or unknown tool is a 400 `invalid_request_error` before any upstream call. (d) `tool_result` content with images or documents is sent as `function_call_output` input parts.
17. **Fixed** (security, #73; ported from OSS 2.0.0 with additions): guardrails and mutators run on `createModelResponse` (`shouldSkipHook` in `src/middlewares/hooks/index.ts` skipped it, so `/v1/responses` and `/v1/messages` routed via Responses bypassed every hook and default guardrail). `HookSpan` extracts the Responses `input` / `output` text as 2.0.0 does; `plugins/utils.ts` (`getCurrentContentPart` / `setCurrentContentPart`, unchanged upstream) maps the string `input` or the last input item, and the output `output_text` / `refusal` parts, so text checks and redacting mutators work (`requestType` in `plugins/types.ts` gains `createModelResponse`). Fork behaviour: only checks verified on Responses requests (the allowlist `RESPONSES_SUPPORTED_CHECKS` in `src/middlewares/hooks/responsesSupport.ts`) may run there; a target with any other enabled check (`default.webhook`, custom ids, checks reading `messages` / `choices` directly) fails closed with a 400 in `tryPost` rather than the check being skipped; `adaptResponse` keeps `hook_results` and translates a 246 as a success; the Messages → Responses routing uses the target's `override_params.model` when set.
18. **Fixed** (re-implemented from the Portkey enterprise changelog 2.25.1, #72; not in OSS `main` or 2.0.0, whose `readAWSStream` is identical to the fork base): `readAWSStream` (`src/handlers/streamHandler.ts`) reads an event-stream message length only once 4 bytes are buffered and decodes a message only once it is complete, so Bedrock streams (extended thinking above all) are no longer cut off when a network read ends inside a length prefix; a length below the 16-byte minimum is an error instead of an endless loop. Gemini/Vertex streams (`transformGeminiStreamParts` in `src/providers/google/chatComplete.ts`, used by both stream transforms) build each candidate's delta from all of its parts, so text and function calls in one chunk are both kept, and number tool calls per candidate across the stream (`streamState.toolCallIndexes`); upstream branched on `parts[0]` and used the part's position in the chunk as `index`.
19. **Fixed** (#69): upstream's `describe.only` in `plugins/utils.test.ts` and `it.only` in `tests/integration/src/handlers/tryPost.test.ts` hid the rest of those files; removed, and CI fails on focused tests. The un-hidden tests showed `getCurrentContentPart` reading `choices[0]` while `setCurrentContentPart` wrote the last choice for `complete` (in place), so with `n > 1` a mutator could copy choice 0's text over the last choice; both now read and write the last choice for `chatComplete` and `complete`, on a copied choice. `azure.shieldPrompt` reads system prompts without assuming `messages` (Responses: `instructions`, then `system` / `developer` input messages) and the user text through the shared getters, so it is on the Responses allowlist.
20. **Fixed** (security; fork behaviour, upstream intentionally skips): hooks on the generic proxy routes. In the fork base, OSS `main` and 2.0.0, `shouldSkipHook` only accepts unified endpoint types, so `/v1/proxy/*` and the `/v1/*` catch-all (`fn === 'proxy'`) never run before/after hooks or default guardrails, and a guarded target's model API could be called through the proxy unchecked. Here (`src/handlers/proxyGuardrails.ts`): when any target in the config has hooks or guardrails, `proxyHandler` refuses a proxied request the hooks cannot inspect with a 400 `invalid_request_error` (`unsupported_guardrail`) before any target is called, and `tryPost` runs the target's hooks on a recognised request with the matching endpoint type (`RequestContext.hooksEndpoint`, used by `HooksService` for the span's `requestType`; the upstream call stays a passthrough). Recognised means a POST with a JSON object body whose proxied path ends in `chat/completions` (`messages` array), `completions` / `complete` (`prompt`), `messages` (`messages` array), `responses` (`input`; the #73 `RESPONSES_SUPPORTED_CHECKS` allowlist applies) or `embeddings` (`input`). Unknown paths, mismatched bodies, multipart, binary, and GET/DELETE are refused; only a body-less GET of `…/models[/<id>]` (model listing, used by `/v1/models`) is passed without hooks. The path is classified after the same `/v1/proxy` / `/v1` cut `ProviderContext.getProxyPath` applies (`stripProxyEndpointPath`, so a `/v1/proxy` spliced mid-path cannot change the endpoint the provider sees), `threads/{id}/messages` is not Messages, and input/output mutators count as hooks. Targets without hooks are proxied unchanged.
21. **Fixed** (security; upstream behaviour in the fork base, OSS `main` and 2.0.0): the request path chose the upstream host for Azure OpenAI proxy calls. `ProviderContext.getProxyPath` built the URL as `https:/${reqPath}` for any proxied path containing `.openai.azure.com`, so `/v1/proxy/evil.com/.openai.azure.com/openai/deployments/x/chat/completions` sent the target's `api-key` (or Entra token) to `evil.com`. The full-URL form is now kept only when the path's first segment is exactly the configured host (`<resource>.openai.azure.com`, or the custom host), compared case-insensitively on the whole `host[:port]`, so no suffix, `@`, port or encoded-slash variant matches; anything else is a 400 before headers are built. Every proxied URL is also checked centrally (`assertProxyURLOrigin`, called in `getFullURL` after any builder): it must have the same origin (scheme, host, port) as the provider's base URL or custom host and no user info, else 400 with no upstream call, so a future `getProxyEndpoint` that lets the path reach the authority fails closed. Base URLs with a path (`/v1`, `/openai`, a custom host path) and per-region hosts (Bedrock, Mantle) are unaffected. Non-proxy endpoints get the same check on `${baseURL}${endpointPath}` (400 `Invalid upstream URL`; skipped for provider request handlers, which build their own URLs), so a request field in an endpoint path can never reach the authority either. The upstream unit test that proxied an Azure full URL against an `api.openai.com` base now uses the matching resource base.

22. **Fixed** (security; upstream behaviour in the fork base, OSS `main` and 2.0.0): upstream redirects were followed, and realtime WebSocket URLs were unchecked. `constructRequest` set no `redirect` option, so fetch followed a 3xx; on Node 22 a cross-origin hop drops only `Authorization`, so a custom host (or a provider open redirect) could send the body and the `api-key` / `x-api-key` / `x-goog-api-key` header to any `Location`, including metadata and private hosts `isSafeUpstreamURL` refuses. Every upstream call now uses `redirect: 'manual'` (`constructRequest`, `retryRequest`, `externalServiceFetch` / `internalServiceFetch` in `src/utils/fetch.ts` and the new `upstreamFetch` used by the Azure Entra, Vertex token, Vertex file, Bedrock S3 upload, Deepgram, ElevenLabs and `/v1/models` control-plane calls, and the Workers realtime upgrade), and a 3xx answer becomes a 502 `upstream_redirect` error without the `Location`, which `retryRequest` does not retry (nor does the status-code retry in `tryPost`). Same-origin redirects are not followed either: no provider API the gateway calls redirects, and presigned storage URLs (Azure blob batch output, Cohere dataset parts, Fireworks uploads) come in response bodies, not through a 3xx. The realtime handlers (`realtimeHandlerNode.ts`, `realtimeHandler.ts` via `getURLForOutgoingConnection` in `websocketUtils.ts`) now await `getBaseURL` and refuse an upstream URL that fails `isSafeUpstreamURL` or `assertProxyURLOrigin` against the base (Workers: 400; Node: the upgrade fails), so a crafted Azure resource name or Foundry URL cannot open a WebSocket to an internal host with the provider key; Node maps `https:`/`http:` to `wss:`/`ws:` by URL scheme (anything else is refused) and passes `followRedirects: false` to `ws`. Cohere batch output (`cohere/getBatchOutput.ts`) sent the Cohere key with every dataset part URL from the Cohere response (presigned storage URLs); it now sends it only to the Cohere API origin, fetches other https hosts without it, and refuses parts that fail `isSafeUpstreamURL`.
23. **Added** (PS security events, schema v1; fork behaviour): `HooksManager` gets an optional `onHookResults` callback, called once per `executeHooks` run inside a try/catch so reporting can never change a verdict; the `hooks` middleware (`src/middlewares/hooks/index.ts`) sets it to `recordHookResults` (`src/middlewares/psSecurityEvents/`). `src/index.ts` mounts `psSecurityEvents()` (429s) before `/metrics`; `proxyHandler.ts` and `handlerUtils.ts` pass the context to `proxyGuardrailsUnsupportedResponse` so the refusal is recorded. No upstream behaviour changes.

24. **Fixed** (privacy; fork bug): `X-PS-*` business metadata (`x-ps-business-unit`, `x-ps-data-classification`, `x-ps-owner`, `x-ps-route`, `x-ps-max-cost`, ...) was sent to the upstream provider on the proxy routes (`/v1/proxy/*` and the `/v1/*` catch-all pass every client header except `x-portkey-*` through `constructRequestHeaders`), and on unified routes when a caller listed one in `forward_headers`. The `beforeRequest` strip in `ps-telemetry.ts` only cleaned a local copy and never reached the upstream call. `stripPSInternalHeaders` (`src/utils/psInternalHeaders.ts`) now removes every `x-ps-*` header, in any case, where upstream headers are finalised: the end of `constructRequestHeaders` in `src/handlers/handlerUtils.ts` (every proxy and unified route, Responses, files and batches that go through `tryPost`), and the realtime WebSocket headers in `src/handlers/websocketUtils.ts` (Workers) and `src/handlers/realtimeHandlerNode.ts` (Node). Bedrock proxy calls to another AWS service (`aws_service`) copy the client headers into the SigV4 request; `src/providers/bedrock/api.ts` drops `x-ps-*` there before signing, since removing a signed header afterwards would break the signature. The incoming request is not changed, so `ps-router`, `ps-telemetry`, `psMetrics`, `psOtel` and the cache still read the headers. Provider request handlers for files and batches build their headers from the provider config only and do not copy client headers. `x-portkey-*` headers are already dropped from proxied headers; on unified routes they reach the provider only when named in `forward_headers` (the operator's choice) or as `x-portkey-file-purpose` on `uploadFile` (upstream behaviour), both unchanged.

25. **Changed** (Kubernetes readiness): the Node server drains on SIGTERM/SIGINT (`src/psShutdown.ts`, wired in `src/start-server.ts`). Upstream calls `server.close()` without waiting and exits, cutting off in-flight and streamed completions; here it waits for them up to `PS_SHUTDOWN_TIMEOUT_MS` (default 25000), answers requests that still arrive on open keep-alive connections with 503 + `Connection: close`, then flushes telemetry and exits 0. The signal handlers are installed as soon as the server listens (upstream registered them after a one-second start-up animation, so a SIGTERM to PID 1 in that window was ignored). The `Dockerfile` runs `node build/start-server.js --headless` directly (not `npm`) as UID 10001, without npm/yarn in the runtime image, and `.dockerignore` keeps local `node_modules`, `build` and secrets out of the build context.
26. **Removed** `deployment.yaml`: upstream's Kubernetes manifest ran Portkey's `portkeyai/gateway` image as root with a NodePort Service. The Helm chart at `charts/prompt-shields` (repository root) deploys this gateway and the collector instead.

## Original License

The original Portkey AI Gateway is licensed under the MIT License. See [`LICENSE`](./LICENSE).

The `ps-telemetry.ts` middleware and the PS-specific configuration glue are wholly original code by Prompt Shields, also released under MIT.
