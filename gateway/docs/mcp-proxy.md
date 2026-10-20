# MCP proxy (`/v1/mcp/{server}`)

Phase 1 (MVP) of the MCP plan in [`mcp-tool-call-discovery.md`](mcp-tool-call-discovery.md), #63. The gateway proxies MCP Streamable HTTP to the upstream MCP servers an operator configures, and enforces a tool policy, guardrails and audit on the way. Code: `src/mcp/`.

Point an MCP client (Claude Desktop, Cursor, VS Code, an agent framework) at `https://<gateway>/v1/mcp/<server>` instead of the server's own URL, with `Authorization: Bearer <PS_MCP_TOKEN>`.

## Protocol support

| Revision                 | What the proxy does                                                                                                                                                                                                                                                                      |
| ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `2026-07-28` (stateless) | `MCP-Protocol-Version`, `Mcp-Method` and, for `tools/call`, `prompts/get` and `resources/read`, `Mcp-Name` are required and must match the body (`=?base64?…?=` values are decoded). A mismatch is `400` with JSON-RPC `-32020` (`HeaderMismatch`). `Mcp-Param-*` headers are forwarded. |
| `2025-11-25` (sessions)  | `initialize`, `Mcp-Session-Id` (both ways), the GET stream, DELETE and `Last-Event-ID` pass through. Mirrored headers are checked when a client sends them.                                                                                                                              |

Every POST body must be one JSON-RPC 2.0 message: batches are refused, and a request method without an `id` (other than `notifications/*`) is refused, so a `tools/call` cannot be smuggled as a notification. The body sent upstream is the JSON the gateway checked, re-serialised, so the upstream cannot read a different message (duplicate keys, parser differences).

Responses: JSON is read (up to `max_response_bytes`) when it has to be checked (`tools/list`, `tools/call`) and streamed otherwise. SSE is never buffered as a whole: events are forwarded as they complete, and only the event that carries the response to the proxied request is held while it is checked. A response the gateway cannot read (too large, another content type, not a response to the request) is replaced by a JSON-RPC error.

## Configuration

Servers are read at startup from the `mcp_servers` key of `PS_MCP_CONFIG_FILE`, or of `./conf.json` when `FETCH_SETTINGS_FROM_FILE=true`. Node only: on Workers no server is configured and the route answers 404. An invalid entry stops the gateway at startup: it never runs with part of its policy.

```json
{
  "mcp_servers": {
    "github": {
      "url": "https://api.githubcopilot.com/mcp/",
      "auth": { "type": "bearer", "token_env": "GITHUB_MCP_TOKEN" },
      "tools": { "allow": ["search_issues", "get_issue"] },
      "pinned_tools": { "search_issues": "<sha256 hex>" },
      "on_description_change": "block",
      "input_guardrails": [
        {
          "default.regexMatch": { "rule": "[Pp]assword|[Ss]ecret", "not": true }
        }
      ],
      "output_guardrails": []
    }
  }
}
```

| Key                                      | Default  | Meaning                                                                                                                                                                                                                           |
| ---------------------------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| server id                                | required | `[a-z0-9][a-z0-9_-]{0,62}`: the path segment and the `server` metric label                                                                                                                                                        |
| `url`                                    | required | Must pass `isSafeUpstreamURL` (no private, link-local or metadata addresses, no IP tricks, no credentials) and be `https`, unless `"allow_http": true` (local servers)                                                            |
| `auth`                                   | required | Outbound credentials: `{"type": "none"}`, `{"type": "bearer", "token_env": "VAR"}` or `{"type": "header", "header": "x-api-key", "value_env": "VAR"}`. Values come from the environment only, never inline                        |
| `tools`                                  | required | Exactly one of `{"allow": [...]}` or `{"deny": [...]}`. `{"deny": []}` allows every tool explicitly                                                                                                                               |
| `pinned_tools`                           | `{}`     | Tool name → sha256 of its definition (below)                                                                                                                                                                                      |
| `on_description_change`                  | `block`  | `block`: a pinned tool whose definition changed is removed from `tools/list` and its calls are refused (`-32012`) until the pinned definition is served again. `flag`: the tool is kept, and a warning plus a metric are recorded |
| `input_guardrails` / `output_guardrails` | `[]`     | Guardrails in the gateway's config shorthand (any installed check, e.g. `default.regexMatch`, `portkey.pii`, `azure.shieldPrompt`)                                                                                                |
| `timeout_ms`                             | `30000`  | Time allowed for the upstream's response headers. A stream may then run on                                                                                                                                                        |
| `max_request_bytes`                      | 1 MiB    | Request body limit                                                                                                                                                                                                                |
| `max_response_bytes`                     | 8 MiB    | Largest JSON response, or SSE event, the gateway inspects                                                                                                                                                                         |
| `max_description_length`                 | `2048`   | Tool descriptions are cut to this length                                                                                                                                                                                          |

Environment:

| Variable                       | Meaning                                                                                                                                                   |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PS_MCP_TOKEN`                 | Bearer token MCP clients send, at least 16 characters. Without it every request is refused with `503`                                                     |
| `PS_MCP_ALLOW_UNAUTHENTICATED` | `true` lets requests through without a token. Only when an authenticating proxy (e.g. Kong) sits in front                                                 |
| `PS_MCP_CONFIG_FILE`           | The file with `mcp_servers`                                                                                                                               |
| `TRUSTED_CUSTOM_HOSTS`         | As for custom provider hosts: hosts here are accepted by `isSafeUpstreamURL` (the default trusts `localhost`, `127.0.0.1`, `::1`, `host.docker.internal`) |

In the Helm chart, mount the file from a ConfigMap with `gateway.extraVolumes` / `gateway.extraVolumeMounts`, set `PS_MCP_CONFIG_FILE` in `gateway.config`, and put `PS_MCP_TOKEN` and the upstream credentials in the gateway Secret.

## What is enforced

1. **Client auth.** Bearer `PS_MCP_TOKEN`, compared in constant time, the same static-token pattern as `/metrics` and the admin console. A failure is a `401` and an `auth.failure` security event (`ps.auth.realm: "mcp"`).
2. **No token passthrough.** Only `Accept`, `MCP-Protocol-Version`, `Mcp-Session-Id`, `Mcp-Method`, `Mcp-Name`, `Mcp-Param-*` and `Last-Event-ID` are forwarded. The server's configured credential is added. The client's `Authorization`, cookies, `x-portkey-*` and `x-ps-*` never leave the gateway. When the upstream refuses the gateway's credentials (401/403), the client gets a `502`, without the upstream's `WWW-Authenticate` (its OAuth metadata is for the gateway, not the client).
3. **Upstream URL.** Fixed per server; nothing in the request changes the scheme, host, port or path. `isSafeUpstreamURL` is checked at startup and on every request, and redirects are never followed (`upstreamFetch`, #209): a 3xx is a `502`.
4. **Tool policy.** `tools/list` results keep only allowed tools. Tools whose name is not 1–128 of `A-Z a-z 0-9 _ - .`, and repeated names (two definitions under one name), are removed. A `tools/call` for any other tool is answered by the gateway with `-32010` and never reaches the upstream.
5. **Untrusted strings.** Descriptions and titles from the upstream are stripped of control, zero-width, bidi and Unicode tag characters (invisible text used to hide instructions), then capped. Tool names appear in logs, errors and metric labels only when they match the name rule.
6. **Definition pinning.** The pin is the sha256 (hex) of the canonical JSON (sorted keys) of `{name, title, description, inputSchema, outputSchema}` as received, before sanitising. In `flag` mode the warning prints the hash now served, so a pin can be collected by running in `flag` mode first and reviewed before it is set. Drift state is per process: a replica that has not seen the changed list yet still allows calls, but no client receives the changed definition through any replica.
7. **Guardrails, fail closed.** Input guardrails run on `tools/call` arguments before the call; output guardrails on the result (`content[].text`, embedded resource text, `structuredContent`, MRTR `inputRequests`, or the error message) before it is released. They run through the request's `HooksManager`, so the guardrail, PII and injection security events and the `ps_gateway_guardrail_*` metrics of #211 are recorded as for model calls. Content is presented to checks in the Completions shape (`prompt` / `choices[0].text`), which every text check supports. A guardrail denies unless it says `"deny": false`. A check that errors, a skipped hook or a hooks failure blocks the call. A blocked call is `-32011`; for SSE, the result event is replaced and the events before it are already delivered. Transformations (mutators, `regexReplace`) are not applied to MCP traffic.
8. **No content at rest.** Arguments and results are held in memory for the checks only. They are not logged, stored, put in metrics or sent as telemetry.

JSON-RPC errors from the gateway use the implementation-defined range: `-32010` tool not allowed, `-32011` guardrail, `-32012` changed pinned tool, `-32013` upstream error, `-32014` gateway auth, `-32015` unknown server. A policy refusal of a request is HTTP 200 with a JSON-RPC error. Malformed requests are 4xx.

## Audit and metrics

- **Collector.** One `mcp_tool_call` event per `tools/call` that passed the tool policy, posted to `POST /ingest/mcp` (#214), with the same opt-in, retries and loss accounting as LLM telemetry (`PS_API_KEY`, `PS_COLLECTOR_URL`): `{source: "gateway", server_name: <server id>, tool_name, outcome, latency_ms, transport: "streamable-http", environment, calling_service}`. `environment` comes from `X-PS-Environment` and `calling_service` from `X-PS-Calling-Service`. A blocked call is stored as `outcome: "error"`. Calls refused by the tool policy are counted in metrics only, so a client cannot fill the registry with made-up tool names. The LLM telemetry, OTel and router middlewares skip this route.
- **Prometheus** (`PS_METRICS_ENABLED`):
  - `ps_gateway_mcp_requests_total{server, method, outcome}`: `method` is one of the MCP methods, else `notification` or `other`; `outcome` is `ok`, `error`, `denied`, `blocked`, `invalid` or `upstream_error`.
  - `ps_gateway_mcp_tool_calls_total{server, tool, outcome}` and `ps_gateway_mcp_tool_call_duration_seconds{server, outcome}`: `tool` is a tool the policy allowed, capped at 200 names per process (then `other`).
  - `ps_gateway_mcp_tools_removed_total{server, reason}`.
  - `ps_gateway_mcp_tool_definition_changes_total{server, action}`.

## Not in this slice

- Inbound OAuth 2.1 resource server: RFC 9728 protected-resource metadata, JWKS validation, per-consumer identity. Then per-consumer and per-tenant policy, and RFC 8693 token exchange (Phase 1b).
- Per-consumer and per-tool rate limits.
- Shared drift state across replicas, and verifying a pinned tool's definition on `tools/call` when no `tools/list` has been seen.
- Policy and guardrails on `resources/*`, `prompts/*`, `initialize` instructions, server-to-client requests on 2025-11-25 streams (sampling, elicitation) and `notifications/message`. These pass through unchanged today.
- OTel MCP spans (`mcp.method.name`) from the gateway, a `blocked` outcome in the collector, and a collector-side MCP server registry that the gateway pulls its allowlist from (Phase 2).
- DNS-rebinding-proof upstream checks (the host check is by name; see FORK_NOTICE #6).
- Workers support (configuration from an environment variable or KV).
