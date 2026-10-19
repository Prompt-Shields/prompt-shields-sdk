# Decision record: MCP tool-call discovery (#63)

|                    |                                                                                                                                                                             |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Status             | **Proposed.** Needs a human decision on scope (see _Decision needed_).                                                                                                      |
| Date               | 2026-10-15                                                                                                                                                                  |
| Issue              | #63, "[Upstream] Evaluate MCP Gateway for MCP tool-call discovery"                                                                                                          |
| Upstream evaluated | Portkey `2.0.0` at [`8febc1d`](https://github.com/Portkey-AI/gateway/commit/8febc1d), `src/mcp/`                                                                            |
| Recommendation     | Do **not** port the upstream MCP Gateway now. Discover MCP usage by observing it: the SDK, config scanning and the collector. The SDK part has started (see _First slice_). |

## Question

Should Prompt Shields discover **MCP tool usage**: which MCP servers and tools are called, by which service or user, and how often? If so, how? Porting upstream's MCP Gateway is one way to do it.

`FORK_NOTICE.md` allows "MCP Gateway observability primitives" from upstream. It does not allow the gateway itself.

## What we capture today

| Layer                                 | Tool-call data                                                                                                                                                                                                                                |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| SDK (`ShieldsClient`, `instrument()`) | `tool_calls_used`: the tools the **model chose** (`[{name, type}]`) from OpenAI `tool_calls` / Responses `function_call`, Anthropic `tool_use`, Gemini `function_call` and Bedrock `toolUse`. Names only.                                     |
| Gateway (`ps-telemetry.ts`)           | None. Events carry vendor, model, usage, latency and `X-PS-*` context. They do not include tool calls.                                                                                                                                        |
| Collector `/ingest/events`            | Stores `tool_calls_used` as JSONB on `ai_usage_events`. Nothing reads it yet.                                                                                                                                                                 |
| Collector `/ingest/otel/v1/traces`    | Drops `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` and `gen_ai.tool.definitions` (`CONTENT_ATTRIBUTE_PREFIXES`). A span without a `gen_ai.provider.name` / `gen_ai.system` vendor is dropped, so `execute_tool` spans are ignored. |

Gaps before this change:

1. Provider-hosted MCP calls were dropped. In these calls the LLM provider calls a remote MCP server for the app: OpenAI Responses `mcp` tool (`mcp_call` items) and Anthropic MCP connector (`mcp_tool_use` blocks, beta only). `instrument()` did not patch Anthropic `beta.messages`.
2. Client-side MCP calls are invisible. Here the app's own code calls a server, e.g. `mcp.ClientSession.call_tool`, or Claude Desktop, Cursor or VS Code do. No LLM response describes these calls, so there is no event to attach them to.
3. The collector has no MCP entity. Each event is keyed to an AI asset by `vendor`/`model` (`find_or_create_asset`). Sending an MCP call as a usage event would create an "AI model" asset for each server, and embed it too.

## What upstream 2.0.0 ships (`src/mcp/`)

About 19k lines: 10.7k of code, 7.5k of tests and test guides, 0.8k of docs. It runs as a **separate Hono app** (`mcp-index.ts`, its own port via `--mcp-node`), not as middleware on the LLM gateway.

| Group                | Files                                                                                                                                                                                                                                      | Lines | Depends on                                                                                                                                                             |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Proxy + session      | `handlers/mcpHandler.ts`, `services/{mcpSession,upstream,downstream,upstreamConnectionPool}.ts`, `utils/{headers,connectionErrors,mcp-utils}.ts`, `types/mcp.ts`, `constants/mcp.ts`, `middleware/hydrateContext.ts`                       | ~3.9k | `@modelcontextprotocol/sdk` (not a dependency here), `@hono/node-server` (Node-only, so Workers would lose it), Redis or the in-memory cache                           |
| Telemetry            | `utils/emitLog.ts`                                                                                                                                                                                                                         | 159   | `forwardMCPLogToWinky`, Portkey's log store. Builds an OTLP-like record per request.                                                                                   |
| Auth / control plane | `middleware/{controlPlane,oauth,jwt,userAccess}`, `routes/{oauth,wellknown,admin,sync}.ts`, `services/{oauthGateway,upstreamOAuth,mcpAccessService,mcpCacheService}.ts`, `utils/{userIdentity,oauthTokenRevocation,mcpCacheKeyTracker}.ts` | ~6.8k | `ALBUS_BASEPATH` (Portkey control plane) for token introspection, server configs and user access. `authNMiddleWare`, which #56 found rejects every standalone request. |

Coupling that matters:

- **OAuth is hard-wired on.** `mcp-index.ts` sets `OAUTH_REQUIRED = true`. Every `/:workspaceId/:serverId/mcp` request goes through `oauthMiddleware`, which introspects tokens through `OAuthGateway`, and that uses the control plane when `ALBUS_BASEPATH` is set. `mcpUserAccessMiddleware({ failOpen: false })` also asks the control plane. The issue proposes porting "proxy and session plus `emitLog`, skip OAuth and user access". That leaves an **unauthenticated** MCP proxy, so we would have to write our own auth layer.
- **`emitLog` logs content.** `mcpSession.ts` sets `mcp.tool.params` (the arguments) and `mcp.tool.result` (the full result) on every `tools/call`, along with user, org and workspace ids. That conflicts with our content policy: the collector drops tool arguments and results, and the SDK records names only. A port would keep only the light attributes: server id, method, tool name, transport, success and duration.
- **Server configs** come from `SERVERS_CONFIG_PATH` (`./data/servers.json`) or the control plane. Clients connect only to servers registered there. That is useful for **governance** (allowlisting). It is not **discovery**: a server nobody routed through the gateway stays invisible.

## Options

### A. Port the upstream MCP Gateway (proxy + session + `emitLog`)

Port the proxy/session group. Replace the control plane and OAuth with a static bearer token (`PS_GATEWAY_ADMIN_TOKEN`-style) or our own auth. Rewrite `emitLog` to post an `mcp_tool_call` event to the collector without content.

- **Covers:** every client that is reconfigured to use the gateway's URL, including Claude Desktop, Cursor and VS Code. This is the only option that also lets us _enforce_ a server allowlist.
- **Misses:** every server not routed through it, so it shows only what was already allowed. Stdio servers (most local MCP servers) cannot be proxied over HTTP.
- **Effort:** 3-4 weeks plus a security review. Most of it goes to our own auth layer, to removing the Redis and control-plane paths, to the Node-only runtime (this fork also builds for Workers), to a new dependency (`@modelcontextprotocol/sdk`), and to adapting the e2e suite. Each upstream release then adds sync cost on ~4k lines.
- **Security:** see below. This option puts us **in the tool-execution path** as a credential broker.

### B. Thin MCP proxy of our own

A small streamable-HTTP pass-through that parses JSON-RPC `tools/call` requests and responses, emits names, and forwards bytes unchanged. No sessions, no pooling, no OAuth brokering.

- **Covers / misses:** the same as A, at lower fidelity. It has no session or pool reuse, and OAuth discovery flows (`WWW-Authenticate: resource_metadata`) need careful pass-through.
- **Effort:** 1-2 weeks, plus the same security review as A. It is still in the execution path.

### C. SDK instrumentation of MCP clients (recommended)

Two parts, both observe-only:

1. **Provider-hosted MCP** (done in this PR): read `mcp_call` / `mcp_tool_use` from LLM responses the SDK already sees and add them to the event's `tool_calls_used`.
2. **Client-side MCP:** wrap the official `mcp` Python package's `ClientSession.call_tool` (and `list_tools` for inventory) in `instrument()`. Import it optionally, fail open, and record server name, tool name, outcome and latency only. The server name comes from the `initialize` result (`serverInfo.name`). The transport type (stdio, sse or streamable-http) is recorded without the URL or command line. The TypeScript SDK `@modelcontextprotocol/sdk` `Client.callTool` would follow the same pattern in the TS partner package.

- **Covers:** Python (and later TS) agents and services that use the official SDK, including stdio servers, which no proxy can see. It needs no reconfiguration.
- **Misses:** desktop IDE clients (Claude Desktop, Cursor, VS Code) and non-official MCP clients.
- **Effort:** part 1 took about a day. Part 2 needs 2-3 days in the SDK, plus the collector schema change below (2-3 days).

### D. Collector-only ingestion of MCP server logs / spans

Accept MCP spans on the existing `/ingest/otel/v1/traces`. The OTel semantic conventions define MCP attributes (`mcp.method.name`, `mcp.session.id`, with `gen_ai.tool.name` for `tools/call`). Optionally add a log-shipper format for servers that only write logs.

- **Covers:** MCP servers the customer runs and has instrumented, whatever client calls them. That includes desktop clients.
- **Misses:** third-party hosted servers, and servers without OTel.
- **Effort:** 2-4 days on top of the collector schema change. It adds no new execution surface, and the content-attribute drop list already strips arguments and results.

### E. (Additional) Inventory from MCP config files

`ps-scan` already scans repositories. It could also read MCP client configs (`.mcp.json`, `.cursor/mcp.json`, `.vscode/mcp.json`, `claude_desktop_config.json`) and report configured servers as `static_scan` discovery. It must never echo `env`/`headers` values, which hold tokens. This answers "which servers exist". It does not tell us how often they are called. Effort: 1-2 days.

## Security implications

MCP servers are tool-execution surfaces. Our role is to **observe, not execute**.

| Risk                                  | A / B (proxy)                                                                                                                                                                                                                                                                                                                                                                          | C (SDK)                                                                                                                                                                                | D (collector)                                |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------- |
| **In the execution path**             | Yes. If the proxy is down or slow, every tool call fails. The proxy can alter requests and responses, and it cannot fail open in the way telemetry does.                                                                                                                                                                                                                               | No. A wrapper around the client call. Telemetry errors are swallowed, the call's result or exception is returned unchanged, and nothing is called or listed that the app did not call. | No. Ingest only.                             |
| **Auth passthrough / token custody**  | The proxy receives client bearer tokens and either forwards them (the MCP authorization spec forbids passing tokens through to upstream servers, because of the confused-deputy risk) or brokers its own OAuth (upstream: `oauthGateway.ts`, 1.8k lines). Both make us a credential store. Upstream's `PROTECTED_HEADERS` and allowlist forwarding would have to be ported with it.    | None. Tokens stay in the app's own client. We never read transport headers, env or URLs.                                                                                               | None. Same as today's `PS_API_KEY` ingest.   |
| **SSRF**                              | Server URLs come from config. OAuth discovery fetches `resource_metadata` and authorization-server URLs that the **MCP server** supplies. Our `isSafeUpstreamURL` is hostname-only, with no DNS-rebinding or redirect protection (FORK_NOTICE #6), so every fetch would need the stricter rules from #76 (https only, no IP literals or internal names, host allowlist, no redirects). | None. No outbound requests besides telemetry.                                                                                                                                          | None.                                        |
| **Prompt injection via tool results** | Tool results pass through the proxy. Upstream logs them in full (`mcp.tool.result`). If stored, they could later reach a model through any feature that summarises usage.                                                                                                                                                                                                              | Results are never read.                                                                                                                                                                | Already dropped (`gen_ai.tool.call.result`). |
| **Untrusted names**                   | Tool names, descriptions and server names come from the MCP server's `tools/list` and `initialize` responses, so a malicious server controls them.                                                                                                                                                                                                                                     | Same. The slice caps names at 200 characters and records only strings. Consumers must escape them when rendering and must never put them in an LLM prompt.                             | Same. Cap and escape at ingest.              |
| **Data exposure**                     | User/org identity on every call (upstream attaches `user_id`, `organisation_id`).                                                                                                                                                                                                                                                                                                      | Server and tool names can reveal internal system names. Treat them like `calling_service`. Tenant retention and erasure apply.                                                         | Same.                                        |

Whichever option is chosen:

- Never store tool arguments, results, tool descriptions or server URLs and commands. Keep names, counts, outcome and latency.
- Treat every MCP-provided string as attacker-controlled data.
- Never connect to, list or call an MCP server on the customer's behalf.

## Collector schema change (needed by C part 2, D and A)

Do not overload AI assets. Add a separate aggregate, and keep `tool_calls_used` on usage events for model-chosen and provider-hosted calls.

- New table `mcp_tool_usage`: `tenant_id` FK, `server_name`, `tool_name`, `transport`, `calling_service`, `environment`, `source` (`sdk` / `otel` / `scan` / `gateway`), `call_count`, `error_count`, `first_seen`, `last_seen`. Unique on (tenant, server, tool, calling_service, source). It must be added to the erasure `ERASE_ORDER` and to the retention purge.
- Ingest: either `POST /ingest/mcp` with `{events: [{server_name, tool_name, transport, outcome, latency_ms, calling_service, ...}]}`, or an `event_type: "mcp_tool_call"` discriminator on `/ingest/events` that skips `find_or_create_asset`. Prefer the separate endpoint: today's `IngestEvent.vendor` is required and always creates an asset.
- Read side: a registry endpoint and a Partner API scope (`mcp:read`, added to `SCOPES` in `collector/partner_scopes.py`), plus AI-BOM `services` entries for MCP servers.

## Decision needed (human)

1. **Is MCP tool-usage discovery in product scope?** The recommendation assumes yes, as observation only.
2. **Is MCP enforcement (allowlisting servers and tools) in scope?** If yes, only options A/B can provide it, and porting the upstream gateway should be reconsidered then, with its auth layer replaced. If no, do not port it.

## Recommendation

- **No-go on porting `src/mcp/` now.** It covers only reconfigured clients, puts us in the execution path as a credential broker, and its telemetry logs content we must not store. About two thirds of the code is control-plane and OAuth coupling we would replace anyway. Keep "MCP Gateway observability primitives" in the FORK_NOTICE allowlist, narrowed to attribute names and event shape only (`emitLog`'s light attributes).
- **Go on observation:**
  1. Done here: provider-hosted MCP calls in `tool_calls_used`.
  2. Next: the `mcp_tool_usage` table and ingest endpoint, plus `mcp` `ClientSession.call_tool` instrumentation in `instrument()`. Optional import, fail open, names only.
  3. Then: MCP span mapping in the OTel ingest (D) and MCP config discovery in `ps-scan` (E).
- **Revisit the gateway** if customers need central routing or allowlisting for desktop IDE clients. That is a governance feature, not discovery.

If the gateway port is approved later, the minimum file set is the proxy + session group above plus `utils/emitLog.ts` rewritten to post to the collector. `middleware/{controlPlane,oauth,jwt,userAccess}`, `routes/*`, `services/{oauthGateway,upstreamOAuth,mcpAccessService}.ts` and `utils/{userIdentity,oauthTokenRevocation,mcpCacheKeyTracker}.ts` are not ported.

## Phase 0 implemented

The observe-only work (option C part 2, option D and the collector schema above) is implemented in the PR that adds `020_mcp_tool_usage.py`. It does not change the status of this record: the scope questions under _Decision needed_ are still open. What it adds:

- SDK: `instrument()` wraps `mcp.ClientSession.call_tool` (and `initialize`, only to remember `serverInfo.name` on mcp 1.x) and posts names, outcome and latency to `POST /ingest/mcp`. `list_tools` is not wrapped.
- Collector: `mcp_tool_usage`, as specified above, plus `latency_ms_total`/`latency_count` and `environment` in the key. `POST /ingest/mcp`, MCP `tools/call` spans on the OTLP receiver (`source: "otel"`), and a tenant read route `GET /api/v1/registry/mcp-tools`.
- Not yet: the Partner API scope (`mcp:read`), AI-BOM `services`, MCP config scanning in `ps-scan` (option E), and the TypeScript MCP client.

## First slice (this change)

Observe-only, no new dependency, no collector change:

- `prompt_shields.providers.mcp_tool_call()` maps an Anthropic `mcp_tool_use` block or an OpenAI `mcp_call` item to `{"name", "type": "mcp", "server"}`. Names are capped at 200 characters, and arguments, results and URLs are ignored.
- It is used by `AnthropicAdapter`, the Bedrock adapter (Anthropic bodies), the stream accumulator, and the Responses extractor and stream accumulator. For streamed Responses, an MCP call is recorded on `response.output_item.done`, once per call.
- `instrument()` also patches `anthropic.resources.beta.messages` `Messages`/`AsyncMessages.create`, because the MCP connector is beta-only.
- Not covered yet: `mcp_list_tools` and `mcp_approval_request` items (inventory and pending approval, not calls), and client-side MCP (option C part 2).
