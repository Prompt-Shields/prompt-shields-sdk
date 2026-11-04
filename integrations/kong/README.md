# kong-plugin-prompt-shields

A Kong Gateway plugin that adds Kong AI Gateway traffic to the Prompt Shields
AI asset registry. It is written in Lua and runs inside Kong.

For each request through `ai-proxy` (or another Kong AI plugin), it does three things:

1. **Access phase.** It reads the `X-PS-*` business headers into
   `kong.ctx.plugin`. Then it removes every `X-PS-*` header from the
   upstream request, so this business metadata never reaches the LLM provider.
   The removal does not depend on the read: if reading or stashing the
   headers fails, the headers are still removed. A request with more than
   Kong's 1,000-header limit for `kong.request.get_headers()` is read again in
   full, so an `X-PS-*` header cannot hide past it.
2. **Log phase.** It builds a small event with only the metadata the collector
   maps:
   - Kong's AI analytics: provider, requested and served model, tokens, cost
     and latency.
   - The consumer, route and service names.
   - The stashed `X-PS-*` values.
3. **Delivery.** It queues the event with `kong.tools.queue`. The queue
   posts batches to the collector's `POST /ingest/kong` route from a timer, not
   from the request, using `Authorization: Bearer <ingest key>`.

The event uses the same JSON shape as Kong's stock `http-log` entries, so
the collector treats it like any other Kong source (`source: kong`). The
plugin needs no collector change.

**What it never sends:**

- Request or response bodies, including AI payloads when `log_payloads` is on.
- Any header other than the five `X-PS-*` headers below.
- Auth headers, API keys and cookies.
- The query string.
- The client IP.
- Kong's plugin IDs, upstream targets, and workspace details.

The event is built from an allow-list. Kong is also asked not to copy request
or response headers when it serializes the log.

Compared with pointing Kong's stock `http-log` at the collector (see
[`docs/integrations/kong.mdx`](../../docs/integrations/kong.mdx)), this plugin:

- strips `X-PS-*` before the request goes upstream, but still reports the values;
- sends about 0.6 KB per AI call instead of the full log entry;
- skips non-AI requests;
- supports sampling;
- verifies TLS by default;
- drops batches the collector will never accept (`401`, `413`, ...) instead of
  retrying them for `max_retry_time`.

## Example event

```json
{
  "ai": {"proxy": {
    "meta": {"provider_name": "openai", "request_model": "gpt-4o",
             "response_model": "gpt-4o-2024-08-06", "llm_latency": 790},
    "usage": {"prompt_tokens": 120, "completion_tokens": 40, "cost": 0.0007}}},
  "consumer": {"username": "payments-api", "custom_id": "app-42"},
  "route": {"name": "hr-chat"},
  "service": {"name": "openai"},
  "latencies": {"proxy": 812},
  "request": {"headers": {"x-ps-business-unit": "Payments",
                          "x-ps-use-case": "fraud-explainer"}},
  "prompt_shields": {"plugin_version": "0.1.0"}
}
```

The plugin sends these as a JSON array, one element per AI call. Kong 3.14
reports embedding calls as `input_tokens` and `output_tokens`. The plugin
sends them as `prompt_tokens` and `completion_tokens`, the field names the
collector reads.

| Header | Registry field |
|--------|----------------|
| `X-PS-Business-Unit` | `business_unit` |
| `X-PS-Use-Case` | `use_case_name` |
| `X-PS-Owner` | `owner_email` |
| `X-PS-Data-Classification` | `data_classification` |
| `X-PS-Environment` | `environment` |

## Requirements

- **Kong Gateway versions.** These Kong Gateway images have been tested:
  - 3.10 LTS (`kong/kong-gateway:3.10`, 3.10.0.19);
  - 3.14 (`kong/kong-gateway:3.14`, 3.14.0.16).

  Both ran without an Enterprise license. The plugin only uses the Kong PDK,
  `kong.tools.queue` and `lua-resty-http`.
- **AI analytics.** On the AI plugins, set
  `config.logging.log_statistics: true`. Without it Kong records no AI
  analytics and the plugin has nothing to send. Since 3.8 the default is
  `false`.
- **Costs.** For costs, set `config.model.options.input_cost` and
  `output_cost` on the AI plugin.
- **Credentials and network.**
  - A Prompt Shields tenant API key (`ps-...`).
  - HTTPS access from the data plane to the collector.
  - Kong's `lua_ssl_trusted_certificate` must trust the collector's
    certificate. The default, `system`, does for a public CA.

## Configuration

| Field | Default | |
|-------|---------|---|
| `collector_url` | (required) | Base URL of the collector, such as `https://collector.example.com`. Events go to `<collector_url>/ingest/kong`. Must be `https` unless `allow_http` is set. No credentials, query or fragment. |
| `ingest_key` | (required) | Tenant API key. Encrypted at rest (Kong Enterprise keyring) and **referenceable**: use a vault reference such as `{vault://env/ps-ingest-key}`. |
| `allow_http` | `false` | Allow an `http://` collector URL. The key then travels unencrypted. Use it only for a collector on a trusted network. |
| `ssl_verify` | `true` | Verify the collector's TLS certificate. |
| `sample_rate` | `1` | Fraction of AI requests to report (0 to 1). |
| `strip_ps_headers` | `true` | Remove every `X-PS-*` request header before proxying. The values are reported either way. |
| `timeout` | `10000` | Timeout for each collector request, in ms. |
| `keepalive` | `60000` | Idle keepalive for collector connections, in ms. |
| `queue.*` | `max_batch_size: 200`, `max_coalescing_delay: 5` | Kong's standard queue settings (`max_entries`, `max_bytes`, `max_retry_time`, ...). `max_batch_size` is capped at 1000, the collector's limit per request. A batch larger than the collector's 4 MiB body limit is sent as several requests. |

**Delivery is fail-open.** The proxied request never waits on the collector,
and an error in the plugin is logged, never returned to the client. If the
collector cannot be reached, or returns `408`, `429` or `5xx`, the queue
backs off and retries until `queue.max_retry_time` (60 s by default). After
that, the batch is dropped and Kong logs an error.

If the collector returns `400`, `401`, `403`, `404`, `413`, `415` or `422`,
the batch is dropped at once. Kong logs the status, never the key. When the
queue holds `queue.max_entries` events, the oldest are dropped.

## Install

The plugin is two Lua files, `kong/plugins/prompt-shields/handler.lua` and
`schema.lua`. Kong must be able to load them, and the plugin must be enabled:

```bash
# Either install the rock (until it is published on LuaRocks, from a checkout):
cd integrations/kong && luarocks make kong-plugin-prompt-shields-0.1.0-1.rockspec

# or copy the files and add the directory to Kong's Lua path:
export KONG_LUA_PACKAGE_PATH="/opt/prompt-shields/?.lua;;"   # holds kong/plugins/prompt-shields/

# then, on every data plane node:
export KONG_PLUGINS=bundled,prompt-shields
export PS_INGEST_KEY=ps-your-tenant-key                       # read by {vault://env/ps-ingest-key}
```

### decK

```yaml
_format_version: "3.0"
plugins:
  - name: prompt-shields
    # Global here; scope it with `route:` or `service:` to cover fewer routes.
    config:
      collector_url: https://collector.promptshields.io
      ingest_key: "{vault://env/ps-ingest-key}"
      sample_rate: 1
      queue:
        max_batch_size: 200
        max_coalescing_delay: 5
  - name: ai-proxy
    route: hr-chat
    config:
      route_type: llm/v1/chat
      logging:
        log_statistics: true
        log_payloads: false
      model:
        provider: openai
        name: gpt-4o
        options: {input_cost: 2.5, output_cost: 10}
      auth:
        header_name: Authorization
        header_value: "{vault://env/openai-authorization}"
```

### Admin API

```bash
curl -sS -X POST http://localhost:8001/plugins \
  -H 'Content-Type: application/json' \
  --data '{
    "name": "prompt-shields",
    "config": {
      "collector_url": "https://collector.promptshields.io",
      "ingest_key": "{vault://env/ps-ingest-key}"
    }
  }'

# Validate a configuration without creating it:
curl -sS -X POST http://localhost:8001/schemas/plugins/validate \
  -H 'Content-Type: application/json' \
  --data '{"name": "prompt-shields", "config": {"collector_url": "https://collector.promptshields.io", "ingest_key": "ps-x"}}'
```

In a workspace, use `http://localhost:8001/<workspace>/plugins`.

### Konnect (custom plugin upload)

Konnect needs the plugin's schema before a control plane accepts its
configuration. The plugin's code lives in two files, so it fits Konnect's
**streamed** custom plugins. With streaming, Konnect sends the code to the
data planes for you.

1. In **Gateway Manager**, open the control plane and go to
   **Plugins → New plugin → Custom plugins → Create**.
2. Upload `kong/plugins/prompt-shields/schema.lua`, and, for streaming,
   `handler.lua`. Use the name `prompt-shields`.
3. Configure it like any other plugin, with decK
   (`deck gateway sync --konnect-control-plane-name ...`) or in the UI. Keep
   `ingest_key` as a vault reference, or use a Konnect Config Store vault
   (`{vault://<config-store-vault-prefix>/ps-ingest-key}`).

The same upload through the Konnect API:

```bash
jq -n --rawfile schema kong/plugins/prompt-shields/schema.lua \
      --rawfile handler kong/plugins/prompt-shields/handler.lua \
      '{name: "prompt-shields", schema: $schema, handler: $handler}' |
curl -sS -X POST "https://${KONNECT_REGION}.api.konghq.com/v2/control-planes/${CONTROL_PLANE_ID}/core-entities/custom-plugins" \
  -H "Authorization: Bearer ${KONNECT_TOKEN}" -H 'Content-Type: application/json' -d @-
```

If you do not stream the code, upload only the schema. Then install the files
on each data plane, as in [Install](#install).

### Helm (Kong's `kong/kong` chart)

Put the two files in a ConfigMap. The chart's `plugins.configMaps` setting mounts
them, adds the plugin to `KONG_PLUGINS`, and sets the Lua path:

```bash
kubectl create configmap kong-plugin-prompt-shields -n kong \
  --from-file=integrations/kong/kong/plugins/prompt-shields/handler.lua \
  --from-file=integrations/kong/kong/plugins/prompt-shields/schema.lua
kubectl create secret generic prompt-shields -n kong --from-literal=ingest-key=ps-your-tenant-key
```

```yaml
# values.yaml
plugins:
  configMaps:
    - pluginName: prompt-shields
      name: kong-plugin-prompt-shields
# Unprefixed environment for the env vault: {vault://env/ps-ingest-key}
customEnv:
  ps_ingest_key:
    valueFrom:
      secretKeyRef:
        name: prompt-shields
        key: ingest-key
```

With Kong Ingress Controller, enable the plugin with a `KongClusterPlugin`:

```yaml
apiVersion: configuration.konghq.com/v1
kind: KongClusterPlugin
metadata:
  name: prompt-shields
  annotations:
    kubernetes.io/ingress.class: kong
  labels:
    global: "true"
plugin: prompt-shields
config:
  collector_url: https://collector.promptshields.io
  ingest_key: "{vault://env/ps-ingest-key}"
```

The Helm and Konnect steps above follow Kong's documentation. They have not
been run against a live cluster or Konnect organization. See [Tests](#tests)
for what has been run.

## Tests

| What | How | Where |
|------|-----|-------|
| Lint | `luacheck .` | CI (`.github/workflows/kong-plugin.yml`) |
| Unit tests, with the Kong PDK, `kong.tools.queue` and `resty.http` mocked | `busted --run=unit` (LuaJIT, with `busted`, `luasocket` and `lua-cjson`) | CI |
| Integration tests in real Kong, with `ai-proxy` sending to a mock LLM and the plugin sending to a mock collector, on Postgres and DB-less | `pongo run`, with `KONG_VERSION=3.10.0.x` or `3.14.0.x` | CI |
| Collector side of the event contract | `packages/collector/tests/test_kong_plugin_contract.py` maps `unit/contract/event-3.14.json`, the file the Lua unit tests check the plugin builds | CI (`ci.yml`) |

Pongo needs Docker. It runs the Enterprise images in free mode, so no
license is needed.
