# Semantic cache

The gateway can answer a chat request from its cache when the final user
message means nearly the same as one it answered before, and everything else
about the request is identical. It is off unless an operator configures an
embedder and a request (or the deployment default) asks for it.

```bash
PS_SEMANTIC_CACHE_EMBEDDER=local    # local | http (unset: semantic mode reads and writes nothing)
PS_CACHE_DEFAULT=semantic           # or per request: X-PS-Cache: semantic
```

A config can also ask for it per target: `"cache": {"mode": "semantic", "max_age": 600}`
(`max_age` in seconds, capped at `PS_SEMANTIC_CACHE_TTL_SECONDS`).

Responses carry `x-portkey-cache-status`: `SEMANTIC HIT`, `SEMANTIC MISS`,
`SEMANTIC BYPASS` (the request was not eligible, see below) or `REFRESH`.

## What is compared, and what must match exactly

Only the **text of the final user message** is embedded and compared by cosine
similarity against `PS_SEMANTIC_CACHE_THRESHOLD` (default `0.95`). Everything
else is part of an exact **partition key**, a SHA-256 over:

| Part | Source |
|---|---|
| Caller scope | provider, the caller's credential and identity fields (API key, virtual key, service account, role ARN, ...), headers forwarded upstream, upstream URL (the same `computeCacheScope` as the exact cache) |
| Tenant | a digest of this deployment's `PS_API_KEY` |
| Principal | the OIDC subject hash when `PS_OIDC_ENABLED` (unless `PS_SEMANTIC_CACHE_SHARE_PRINCIPALS=true`) |
| Use case | `X-PS-Use-Case` |
| Model | the request model after routing and `override_params` |
| Policy version | the route policy's `policy_version`, else a digest of the active policy, else `none` |
| Sensitivity | the call's effective level (data policy gate, else router decision, else `X-PS-Data-Classification` escalated by the policy's PII rules) |
| Endpoint, embedder | `chatComplete` or `messages`; the embedder id (URL and model, or local dimensions) |
| Request | every request parameter (temperature, tools, response format, ...), the whole conversation history including system messages, and the final message apart from its text |

A lookup only scans entries of its own partition, so two tenants, two
credentials or two principals can never be served each other's responses,
however similar the prompts. The whole-app test
`src/tests/regression/semanticCache.test.ts` checks this.

## When the cache is not used (`SEMANTIC BYPASS`)

The request neither reads nor writes the cache, and goes upstream, when:

- no embedder is configured, or the endpoint is not chat completions or messages;
- the request streams (`stream: true`);
- the final message is not a user message of plain text (images, audio and files are never compared), is empty, or is longer than `PS_SEMANTIC_CACHE_MAX_INPUT_CHARS` (default 4000; never truncated, since two long prompts sharing a prefix must not collide);
- **sensitivity**: the call's level is above `PS_SEMANTIC_CACHE_MAX_SENSITIVITY` (default: the level below `confidential`, i.e. `internal` with the default levels), or is a level the policy does not define. Under a policy with `unclassified_default: confidential`, unclassified calls are therefore off too. Raising the maximum is an explicit operator decision;
- **PII**: the gateway's PII scanner finds anything in the request (unless `PS_SEMANTIC_CACHE_ALLOW_PII=true`). Without a route policy there is no escalation, so this is the only PII guard;
- **data policy**: the call falls under a `data_policy` rule and the embedder does not meet it (see below). The prompt is then never sent to the embedder;
- the embedding call fails or times out (fail open).

`ps_gateway_semantic_cache_bypass_total{reason}` counts each reason.

## What is never written

Only complete, successful JSON responses are stored: never an error status, a
246 (failed hooks), a streamed body, an unfinished Responses object, or a
**refusal** (`message.refusal`, `finish_reason: content_filter`,
`stop_reason: refusal`, a Responses `refusal` part). The refusal rule also
applies to the exact-match cache.

## Embedders

| `PS_SEMANTIC_CACHE_EMBEDDER` | What it is | Data policy |
|---|---|---|
| `local` | Built in. Feature-hashed words and character trigrams (`PS_SEMANTIC_CACHE_LOCAL_DIMS`, default 512). **Lexical, not semantic**: it matches the same question with different case, punctuation, spacing or a word or two changed, not real paraphrases. Use a threshold of 0.9 or higher. | Always allowed: no text leaves the gateway process |
| `http` | An OpenAI-compatible `/v1/embeddings` endpoint: `PS_SEMANTIC_CACHE_EMBEDDING_URL`, `PS_SEMANTIC_CACHE_EMBEDDING_MODEL`, optional `PS_SEMANTIC_CACHE_EMBEDDING_API_KEY` (sent as a bearer token; keep it in a Secret) and `PS_SEMANTIC_CACHE_EMBEDDING_TIMEOUT_MS` (default 2000). Point it at a self-hosted model (e.g. text-embeddings-inference, Ollama, vLLM) to keep text in your network. Like other operator-configured egress (OTel, telemetry), the URL must pass the gateway's upstream check: metadata, private and loopback hosts are refused unless listed in `TRUSTED_CUSTOM_HOSTS` (bypass reason `embedder_unsafe_url`), and redirects are never followed. | Checked per call, see below |

Under a route policy with a `data_policy` section, the prompt text is sent to
an `http` embedder only when the embedder meets the rule for the call's
level. Its facts are declared by the operator, and missing facts fail closed
exactly as for a model tier:

```bash
PS_SEMANTIC_CACHE_EMBEDDING_PROVIDER=mistral-eu
PS_SEMANTIC_CACHE_EMBEDDING_JURISDICTION=FR     # ISO code, EEA, or any
PS_SEMANTIC_CACHE_EMBEDDING_ZDR=true
PS_SEMANTIC_CACHE_EMBEDDING_US_CLOUD_ACT_EXPOSURE=false
```

Declared facts decide alone. Only when none of them is set does a policy
tier that lists the embedding model by name count. Without a
data policy, any configured embedder is used for calls the sensitivity rule
allows. The embedding call is not metered in telemetry.

## Store, TTL and limits

The store is in memory and per instance (not shared across replicas):

| Setting | Default | |
|---|---|---|
| `PS_SEMANTIC_CACHE_TTL_SECONDS` | 3600 | Entry lifetime, and the cap on a config's `max_age` |
| `PS_SEMANTIC_CACHE_MAX_ENTRIES` | 1000 | Total entries (least recently used evicted first) |
| `PS_SEMANTIC_CACHE_MAX_MB` | 64 | Approximate total size of responses and vectors |
| `PS_SEMANTIC_CACHE_MAX_PER_PARTITION` | 256 | Entries per partition; also bounds the scan a lookup does |
| `PS_SEMANTIC_CACHE_THRESHOLD` | 0.95 | Minimum cosine similarity, in (0, 1] |

## Telemetry and metrics

A response served from either cache is sent to the collector with `cost: 0`,
`cost_source: "cache"`, `cache_status` and `cache_saved_cost` (the cached
response's usage priced as a normal call: what the hit avoided). Routed hits
keep their `counterfactual_cost`, so savings reports count cache hits as
savings against the baseline.

Prometheus (`PS_METRICS_ENABLED=true`):

- `ps_gateway_cache_responses_total{status}`: `hit`, `miss`, `semantic_hit`, `semantic_miss`, `semantic_bypass`, `refresh`
- `ps_gateway_cache_writes_total{mode,outcome}`: `stored`, `refusal`, `incomplete`, `too_large`, `error`
- `ps_gateway_semantic_cache_bypass_total{reason}`
- `ps_gateway_cache_saved_cost_usd_total{provider,model,status}`
- `ps_gateway_semantic_cache_entries`, `ps_gateway_semantic_cache_bytes`

## Limits

- Per-instance memory only; there is no Redis-backed semantic store yet.
- A semantic hit returns a response written for a different (similar) prompt. Tune the threshold per embedder and keep the mode off for use cases where small wording changes matter.
- Callers in one partition share entries. Without OIDC, everyone using the same credential (a shared team key or virtual key) is one partition, so one of them can seed an answer another gets for a similar prompt; with a remote semantic embedder a long prompt with a small appended instruction can still score above the threshold. Enable OIDC (principals are isolated by default) or give callers separate keys where that matters.
- Only the final user turn is compared; multi-turn conversations hit only when their whole history is identical.
