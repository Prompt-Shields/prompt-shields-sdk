# MCP proxy (`/v1/mcp/{server}`)

Phase 1 (MVP) of the MCP plan in [`mcp-tool-call-discovery.md`](mcp-tool-call-discovery.md), #63. The gateway proxies MCP Streamable HTTP to the upstream MCP servers an operator configures, and enforces a tool policy, guardrails and audit on the way. Code: `src/mcp/`.

Point an MCP client (Claude Desktop, Cursor, VS Code, an agent framework) at `https://<gateway>/v1/mcp/<server>` instead of the server's own URL, with `Authorization: Bearer <PS_MCP_TOKEN>`.

## Protocol support

| Revision                 | What the proxy does                                                                                                                                                                                                                                                                      |
| ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `2026-07-28` (stateless) | `MCP-Protocol-Version`, `Mcp-Method` and, for `tools/call`, `prompts/get` and `resources/read`, `Mcp-Name` are required and must match the body (`=?base64?…?=` values are decoded). A mismatch is `400` with JSON-RPC `-32020` (`HeaderMismatch`). `Mcp-Param-*` headers are checked against the arguments (below) and forwarded. |
| `2025-11-25` (sessions)  | `initialize`, `Mcp-Session-Id` (both ways), the GET stream, DELETE and `Last-Event-ID` pass through, but the GET stream never carries a response (see below), so a resumed stream does not deliver the response it would replay. Mirrored headers are checked when a client sends them.  |

Every POST body must be one JSON-RPC 2.0 message: batches are refused, and a request method without an `id` (other than `notifications/*`) is refused, so a `tools/call` cannot be smuggled as a notification. The body sent upstream is the JSON the gateway checked, re-serialised, so the upstream cannot read a different message (duplicate keys, parser differences).

Responses: a client matches a response to its request by `id`, whatever stream it arrives on, so the gateway releases a response only where it checked it.

- JSON is read (up to `max_response_bytes`, within `timeout_ms`) and must be the response to the request (same `id`). SSE is never buffered as a whole: events are forwarded as they complete, and only the event that carries the response to the proxied request is held while it is checked. Responses with another `id` on a stream are dropped.
- The GET stream (2025-11-25) carries requests and notifications only; responses on it are dropped. The spec allows them there only when a stream is resumed with `Last-Event-ID`, and those replayed responses were never checked. A client whose stream broke before the response retries the request.
- Every message forwarded is the JSON the gateway parsed, re-serialised: SSE data that is not one JSON-RPC message (a batch, `NaN`, invalid JSON) is dropped, and duplicate keys cannot read differently for the client.
- An HTTP error keeps its status (`404` ends a 2025-11-25 session, `400` carries version negotiation), but its body reaches the client only when it is a JSON-RPC error, re-serialised and, for `tools/call`, checked by the output guardrails. Any other error body becomes a `-32013` error.
- A notification or a client's response is acknowledged with the upstream's status and no body.
- A response the gateway cannot read (too large, too slow, another content type, not a response to the request) is replaced by a JSON-RPC error.
- `tasks/*`, and `tools/call` with a `task`, are refused with `-32010`: a task's result is fetched later by task id, where the gateway cannot check it.

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
| `timeout_ms`                             | `30000`  | Time allowed for the upstream's response headers, and then for a JSON body. An SSE stream may run on                                                                                                                              |
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

### With OIDC (`PS_OIDC_ENABLED=true`)

The MCP route is a `/v1/*` route, so the gateway's OIDC middleware runs on it before the MCP handler: the client sends its identity-provider access token as `Authorization: Bearer <JWT>`, not `PS_MCP_TOKEN`. The middleware verifies the token, applies the group policy and removes the token from the request; the MCP handler then accepts the verified identity and does not check `PS_MCP_TOKEN` (which need not be set, and is refused as an invalid IdP token if a client sends it). In this mode:

- A missing or invalid token is the OIDC `401` (an OpenAI-style error body with `error.code` `missing_token` / `invalid_token`, not a JSON-RPC error) and an `auth.failure` event in realm `oidc`.
- A user whose groups match no policy entry under `default: deny` gets the OIDC `403` (`policy.denied`), and the group's rate limit applies (`429`).
- The group's provider and model allowlists do not apply: an MCP call names no provider or model.
- **Per-group MCP policy.** A policy entry's `mcp` key lists the servers its users may reach and, per server, tool `allow` / `deny` lists layered on the server's own tool policy: a tool must pass both, and the group's `deny` wins (matched regardless of case). See [the OIDC guide](../../docs/guides/gateway-oidc.mdx#group-policy).
  ```json
  { "name": "analysts", "match": ["quants"],
    "mcp": { "servers": { "github": { "tools": { "deny": ["create_issue"] } }, "docs": {} } } }
  ```
  A server the entry does not list is `403` with JSON-RPC `-32016` for every request (POST, GET, DELETE), whether or not it is configured, so a group cannot probe for server ids. A tool the entry does not allow is answered like the server's own policy (`-32010`, HTTP 200), and `tools/list` shows the user only the tools that pass both. Both refusals record a `policy.denied` event (`mcp_server_not_allowed` / `mcp_tool_not_allowed`, with `ps.mcp.server` and `ps.mcp.tool`). An entry without `mcp`, a user who matched no entry under `default: allow`, and OIDC off keep the previous behaviour: every configured server, within its tool policy.
- The IdP token never reaches the MCP server. When the request also carries `x-portkey-provider` with a key in `PS_OIDC_PROVIDER_KEYS`, the middleware sets that provider key as `Authorization`; the MCP handler never forwards `Authorization`, so it does not leave the gateway either.
- The `mcp_tool_call` telemetry event carries `user_id` (the subject hash the LLM telemetry uses; the hash even with `PS_OIDC_TELEMETRY_SUBJECT=raw`, nothing with `off`) and `user_group` (the policy entry's `name`). Never the raw subject or email.

With OIDC off, nothing changes: `PS_MCP_TOKEN` is required as described above.

## What is enforced

1. **Client auth.** Bearer `PS_MCP_TOKEN`, compared in constant time, the same static-token pattern as `/metrics` and the admin console. A failure is a `401` and an `auth.failure` security event (`ps.auth.realm: "mcp"`). With [OIDC](../../docs/guides/gateway-oidc.mdx) on, the IdP token replaces it (see below).
2. **No token passthrough.** Only `Accept`, `MCP-Protocol-Version`, `Mcp-Session-Id`, `Mcp-Method`, `Mcp-Name`, `Mcp-Param-*` and `Last-Event-ID` are forwarded. The server's configured credential is added. The client's `Authorization`, cookies, `x-portkey-*` and `x-ps-*` never leave the gateway. When the upstream refuses the gateway's credentials (401/403), the client gets a `502`, without the upstream's `WWW-Authenticate` (its OAuth metadata is for the gateway, not the client).
3. **Upstream URL.** Fixed per server; nothing in the request changes the scheme, host, port or path. `isSafeUpstreamURL` is checked at startup and on every request, and redirects are never followed (`upstreamFetch`, #209): a 3xx is a `502`.
4. **Tool policy.** The server's list, then the caller's OIDC group `mcp` rules (above). `tools/list` results keep only allowed tools (a result without a `tools` array lists none). Allowlists match exactly; denylists match regardless of case, so an upstream that resolves names case-insensitively cannot be reached through `Delete_File` when `delete_file` is denied. Tools whose name is not 1–128 of `A-Z a-z 0-9 _ - .`, and repeated names (two definitions under one name), are removed. A `tools/call` for any other tool is answered by the gateway with `-32010` and never reaches the upstream.
5. **Untrusted strings.** Descriptions and titles from the upstream, including `description` and `title` anywhere in the input and output schemas (parameter descriptions), are stripped of control, zero-width, bidi and Unicode tag characters (invisible text used to hide instructions), then capped. Tool names appear in logs, errors and metric labels only when they match the name rule.
6. **Definition pinning.** The pin is the sha256 (hex) of the canonical JSON (sorted keys) of the whole tool definition as received, before sanitising, except `_meta`: name, title, description and schemas, and also `annotations` (a changed `readOnlyHint` can change what a client approves), `execution`, `icons` and any later field. In `flag` mode the warning prints the hash now served, so a pin can be collected by running in `flag` mode first and reviewed before it is set. Drift state is per process: a replica that has not seen the changed list yet still allows calls, but no client receives the changed definition through any replica.
7. **`Mcp-Param-*` headers.** A tool can declare, with `x-mcp-header` in its `inputSchema`, arguments that clients mirror into `Mcp-Param-{Name}` headers (2026-07-28). The gateway learns the declarations from `tools/list` results (per server, per process) and, on a `tools/call` for such a tool, checks every declared header against the argument at its path, after decoding `=?base64?…?=`: strings exactly, booleans as `true`/`false`, integers numerically. A header that does not match, is malformed, is missing while the argument has a value (from 2026-07-28 on), or is sent for an argument that is absent or `null` is `400` with `-32020`, and the call never reaches the upstream. Headers the tool does not declare, and calls to a tool the gateway has not yet seen listed, are forwarded unchecked, as the spec asks of intermediaries. A tool whose annotations break the spec's rules (not a token, repeated, on a non-primitive type, or not reached from the schema root through `properties` alone) is removed from `tools/list` (reason `invalid_header`) and its calls are refused (`-32010`), since its headers cannot be checked.
8. **Guardrails, fail closed.** Input guardrails run on `tools/call` arguments, and on the MRTR `inputResponses` of a retried call (elicited input, sampled model output), before the call; output guardrails on the result (`content[].text`, embedded resource text, embedded resources with a text MIME type sent as base64 `blob`, the fields of `resource_link` and unknown item types, `structuredContent`, MRTR `inputRequests`, or the error message) before it is released. They run through the request's `HooksManager`, so the guardrail, PII and injection security events and the `ps_gateway_guardrail_*` metrics of #211 are recorded as for model calls. Content is presented to checks in the Completions shape (`prompt` / `choices[0].text`), which every text check supports. A guardrail denies unless it says `"deny": false`. A check that errors, a skipped hook or a hooks failure blocks the call. A blocked call is `-32011`; for SSE, the result event is replaced and the events before it are already delivered. Transformations (mutators, `regexReplace`) are not applied to MCP traffic.
9. **No content at rest.** Arguments and results are held in memory for the checks only. They are not logged, stored, put in metrics or sent as telemetry.

JSON-RPC errors from the gateway use the implementation-defined range: `-32010` tool not allowed, `-32011` guardrail, `-32012` changed pinned tool, `-32013` upstream error, `-32014` gateway auth, `-32015` unknown server, `-32016` server not allowed for the caller's OIDC group. A policy refusal of a request is HTTP 200 with a JSON-RPC error. Malformed requests are 4xx.

## Audit and metrics

- **Collector.** One `mcp_tool_call` event per `tools/call` that passed the tool policy, posted to `POST /ingest/mcp` (#214), with the same opt-in, retries and loss accounting as LLM telemetry (`PS_API_KEY`, `PS_COLLECTOR_URL`): `{source: "gateway", server_name: <server id>, tool_name, outcome, latency_ms, transport: "streamable-http", environment, calling_service, user_id, user_group}`. `environment` comes from `X-PS-Environment`, `calling_service` from `X-PS-Calling-Service`, `user_id` / `user_group` from the OIDC identity (above). `outcome` is `ok`, `error` or `blocked` (a guardrail refused the arguments or the result); the collector counts blocked calls apart from errors (`blocked_count`). Calls refused by the tool policy are counted in metrics only, so a client cannot fill the registry with made-up tool names. The LLM telemetry, OTel and router middlewares skip this route.
- **Prometheus** (`PS_METRICS_ENABLED`):
  - `ps_gateway_mcp_requests_total{server, method, outcome}`: `method` is one of the MCP methods, else `notification` or `other`; `outcome` is `ok`, `error`, `denied`, `blocked`, `invalid` or `upstream_error`.
  - `ps_gateway_mcp_tool_calls_total{server, tool, outcome}` and `ps_gateway_mcp_tool_call_duration_seconds{server, outcome}`: `tool` is a tool the policy allowed, capped at 200 names per process (then `other`).
  - `ps_gateway_mcp_tools_removed_total{server, reason}`: `reason` is `policy`, `invalid_name`, `duplicate`, `pin`, `invalid_header` or `group`.
  - `ps_gateway_mcp_tool_definition_changes_total{server, action}`.

## Not in this slice

- Inbound OAuth 2.1 resource server for MCP clients: RFC 9728 protected-resource metadata and a `WWW-Authenticate` that points MCP clients at the IdP. (JWT validation, identity and per-group MCP policy exist with OIDC on, see above.) Then per-tenant MCP policy, and RFC 8693 token exchange (Phase 1b).
- Per-consumer and per-tool rate limits.
- Shared drift state across replicas, and verifying a pinned tool's definition on `tools/call` when no `tools/list` has been seen.
- Policy and guardrails on `resources/*`, `prompts/*`, `initialize` instructions, server-to-client requests on 2025-11-25 streams (sampling, elicitation) and `notifications/message`. These pass through unchanged today.
- OTel MCP spans (`mcp.method.name`) from the gateway, and a collector-side MCP server registry that the gateway pulls its allowlist from (Phase 2).
- DNS-rebinding-proof upstream checks (the host check is by name; see FORK_NOTICE #6). The upstream URL is set by the operator, never by the client, so the default trust of `localhost` / `127.0.0.1` (for a server beside the gateway) is not reachable by a client; set `TRUSTED_CUSTOM_HOSTS` to narrow it.
- `Mcp-Param-*` headers of a tool the gateway has not yet seen in a `tools/list` (no declarations to check against; the upstream must check them, per the spec), and shared `x-mcp-header` state across replicas.
- Guardrails on MRTR `inputResponses` of `prompts/get` and `resources/read` retries (those methods have no guardrails yet).
- Resuming a broken 2025-11-25 stream: replayed responses are dropped, so the client's request times out and is retried.
- Workers support (configuration from an environment variable or KV).
