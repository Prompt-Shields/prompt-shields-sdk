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
| JSON payload-size limit (#62) | `src/middlewares/payloadSizeValidator.ts` | Limit read per request from `MAX_JSON_PAYLOAD_SIZE_IN_MB`; matches `application/json; charset=...`; Prometheus bookkeeping dropped |
| `/v1/rerank` (#58) | `src/handlers/rerankHandler.ts`, `src/types/rerankRequestBody.ts`, `src/utils/idGenerator.ts`, `src/providers/{cohere,jina,voyage,bedrock}/rerank.ts` | Handler reads the body from the Hono request (our style); Bedrock routes rerank to `bedrock-agent-runtime` |
| Databricks, Latitude, Pinecone providers (#57) | `src/providers/{databricks,latitude,pinecone}/` | Databricks workspace header plumbed through `constructConfigFromRequestHeaders` |
| Responses/Messages adapters (#59) | `src/adapters/`, `src/handlers/adapterUtils.ts` | `tryPost` wraps the unchanged pipeline (`tryPostToProvider`); native Responses list matches this tree; `normalizeTokenLimitForProvider` renames `max_completion_tokens` for chat configs that only map `max_tokens`; upstream tests updated for the `max_completion_tokens` change |
| `/v1/health` (#61) | route in `src/index.ts` | Same response shape; used by `deployment.yaml` probes |
| Provider fixes (#60) | `openai`, `azure-openai`, `azure-ai-inference`, `z-ai`, `cohere`, `ollama`, `ai21` | Stand-alone fixes only; per-provider `pricing` configs dropped. Google/Vertex, Bedrock, Anthropic, Fireworks and batch-output fixes need 2.0.0's core: #108 |

Not ported:
- `/v1/gateway/tokenize` (#62). It is a wrapper over upstream's control-plane-coupled pricing engine (`services/winky`), so token counting will be built on our catalog instead (#71).
- Prometheus via `prom-client` and `addMetricsMiddleware` (#61): Node-only and tied to control-plane context keys. `/metrics` is our own dependency-free implementation (`src/middlewares/psMetrics/`).
- OTel trace ingest and `customLogHandler` (#61): wrappers over Portkey's log store. Span ingest belongs in the collector (#92).

### Upstream files modified in this fork

Every file that exists at the fork base `28ed85e` and differs here (regenerate with the loop in *How to sync upstream*). Files added by this fork are not listed.

- `README.md`
- `conf.example.json`
- `deployment.yaml`
- `src/globals.ts`
- `src/handlers/handlerUtils.ts`
- `src/handlers/retryHandler.ts`
- `src/handlers/services/cacheService.ts`
- `src/handlers/services/requestContext.ts`
- `src/index.ts`
- `src/middlewares/cache/index.ts`
- `src/middlewares/log/index.ts`
- `src/middlewares/requestValidator/index.ts`
- `src/middlewares/requestValidator/schema/config.ts`
- `src/providers/ai21/api.ts`
- `src/providers/ai21/embed.ts`
- `src/providers/azure-ai-inference/chatComplete.ts`
- `src/providers/azure-ai-inference/messages.ts`
- `src/providers/azure-openai/chatComplete.ts`
- `src/providers/azure-openai/imageGenerate.ts`
- `src/providers/azure-openai/index.ts`
- `src/providers/bedrock/api.ts`
- `src/providers/bedrock/index.ts`
- `src/providers/bedrock/types.ts`
- `src/providers/bytez/api.ts`
- `src/providers/cohere/api.ts`
- `src/providers/cohere/chatComplete.ts`
- `src/providers/cohere/complete.ts`
- `src/providers/cohere/index.ts`
- `src/providers/cohere/types.ts`
- `src/providers/index.ts`
- `src/providers/jina/api.ts`
- `src/providers/jina/index.ts`
- `src/providers/ollama/chatComplete.ts`
- `src/providers/ollama/embed.ts`
- `src/providers/open-ai-base/index.ts`
- `src/providers/openai/chatComplete.ts`
- `src/providers/openai/imageGenerate.ts`
- `src/providers/types.ts`
- `src/providers/voyage/api.ts`
- `src/providers/voyage/index.ts`
- `src/providers/z-ai/api.ts`
- `src/providers/z-ai/index.ts`
- `src/public/index.html`
- `src/shared/services/cache/index.ts`
- `src/shared/utils/logger.ts`
- `src/start-server.ts`
- `src/types/modelResponses.ts`
- `src/types/requestBody.ts`

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

## Original License

The original Portkey AI Gateway is licensed under the MIT License. See [`LICENSE`](./LICENSE).

The `ps-telemetry.ts` middleware and the PS-specific configuration glue are wholly original code by Prompt Shields, also released under MIT.
