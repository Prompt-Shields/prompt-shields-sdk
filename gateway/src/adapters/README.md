# API Adapters

Adapters enable **universal API compatibility** by translating between different API formats at the handler level. This allows any provider to support multiple API formats without per-provider modifications.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Incoming Request                            │
│                    (Responses API / Messages API)                   │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                           Adapter Layer                             │
│                                                                     │
│   ┌─────────────────┐              ┌─────────────────┐              │
│   │ responses/      │              │ messages/       │              │
│   │                 │              │                 │              │
│   │ Responses API   │              │ Anthropic       │              │
│   │ → Chat Complete │              │ Messages API    │              │
│   │                 │              │ → Chat Complete │              │
│   └─────────────────┘              └─────────────────┘              │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Chat Completions (Unified Format)                │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Provider Layer                               │
│     (Transforms Chat Completions → Provider-specific format)        │
│                                                                     │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐       │
│  │ OpenAI  │ │Anthropic│ │ Groq    │ │ Gemini  │ │ 70+more │       │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘       │
└─────────────────────────────────────────────────────────────────────┘
```

## How Adapters Work

### Request Flow

1. **Incoming**: Request arrives in a specific API format (e.g., Responses API)
2. **Adapter Transform**: Converts to Chat Completions (unified format)
3. **Provider Transform**: Existing provider logic converts to provider-specific format
4. **Provider Call**: Request sent to the actual provider

### Response Flow

1. **Provider Response**: Response received in provider-specific format
2. **Provider Transform**: Existing provider logic converts to Chat Completions
3. **Adapter Transform**: Converts back to the original API format
4. **Return**: Response sent to client in expected format

## Available Adapters

### `responses/` - OpenAI Responses API Adapter

Enables all 70+ providers to support OpenAI's newer Responses API.

**Files:**

- `requestTransform.ts` - Responses API → Chat Completions
- `responseTransform.ts` - Chat Completions → Responses API
- `streamTransform.ts` - Real-time SSE chunk transformation
- `index.ts` - Exports and native provider detection

**Native Providers** (bypass adapter): `openai`, `azure-openai`

### `messages/` - Anthropic Messages API Adapter

Enables all providers to accept Anthropic's Messages API format.

**Files:**

- `requestTransform.ts` - Messages API → Chat Completions
- `responseTransform.ts` - Chat Completions → Messages API
- `streamTransform.ts` - Real-time SSE chunk transformation
- `index.ts` - Exports and native provider detection

**Native Providers** (bypass adapter): `anthropic`, `bedrock`

### `messagesViaResponses/` - Messages API via the Responses API (PS fork, #73)

OpenAI and Azure OpenAI models that only the Responses API serves (codex,
`-pro` reasoning models, deep research) cannot go through Chat Completions.
For them a Messages request is translated straight to a Responses request,
sent to the provider's native `/responses`, and the Responses JSON or SSE is
translated back to Messages. `routesMessagesViaResponses` decides per target.

Routing is decided per target: by model name, unless the target sets
`messages_via_responses` (boolean, in the config) or the request sends
`x-portkey-messages-via-responses: true|false`. `true` forces the route,
e.g. for an Azure deployment named `prod-coder` that serves a codex model;
`false` keeps a target on the Chat Completions adapter. The config key wins
over the header; only `openai` and `azure-openai` targets are ever routed.

| Messages                                           | Responses                                                                                           |
| -------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `thinking` enabled                                 | `reasoning.summary: auto` + `include: ["reasoning.encrypted_content"]`                              |
| `thinking` / `redacted_thinking` from this adapter | `reasoning` item (`encrypted_content`), same provider only                                          |
| `thinking` signed by Anthropic                     | dropped (never sent to OpenAI)                                                                      |
| `web_search_*` tool                                | `web_search` tool (`allowed_domains`, `user_location`; `max_uses` dropped; `blocked_domains` → 400) |
| other server tools, Anthropic-defined client tools | dropped; `tool_choice` naming one → 400                                                             |
| `tool_result` with images / PDFs                   | `function_call_output` with `input_text` / `input_image` / `input_file` parts                       |

**Thinking round-trip.** With `store: false` the provider keeps no
reasoning between turns, so the encrypted reasoning travels in the thinking
block: `signature` (or `redacted_thinking.data` when there is no summary) is
`ps-rsn1:<provider>:<encrypted_content>`. Messages clients return it
verbatim, and the next request turns it back into a `reasoning` item. The
prefix contains `:`, which a base64 Anthropic signature never does, so an
Anthropic signature is never sent to OpenAI. The provider tag keeps OpenAI
reasoning away from Azure and back (neither can decrypt the other's), e.g.
in a fallback config; that reasoning is dropped instead. Two OpenAI targets
in different organisations share the tag, so a load balance across them can
still get a decryption error; keep such conversations on one organisation.
These signatures mean nothing to Anthropic: a conversation that moves from
this route to a native Anthropic target mid-way fails signature checks there,
as conversations through the Chat Completions adapter (empty signatures)
already do.

**Web search.** Results carry the source URLs the provider returned
(`include: ["web_search_call.action.sources"]`), with the URL as the title
and an empty `encrypted_content`; text citations are not mapped. Past
`server_tool_use` / `web_search_tool_result` blocks in the history are not
sent back (their findings are in the assistant text).

## Adding a New Adapter

1. Create a folder: `src/adapters/{api-name}/`
2. Implement transforms:
   - `requestTransform.ts` - Convert incoming format → Chat Completions
   - `responseTransform.ts` - Convert Chat Completions → outgoing format
   - `streamTransform.ts` (if streaming differs)
3. Export from `index.ts`
4. Integrate in the relevant handler

## Design Principles

- **Handler-level integration**: Adapters are applied once at the handler, not per-provider
- **Chat Completions as pivot**: All adapters translate to/from Chat Completions
- **Minimal overhead**: Optimized for low latency and memory usage
- **Native passthrough**: Providers with native support bypass the adapter entirely

## Key Design Decisions

### Reasoning/Thinking Support

**For Chat Completions**: Reasoning data requires `strictOpenAiCompliance: false` because
it's a non-standard Portkey extension (via `content_blocks`).

**For Responses API**: The `reasoning` output item is **part of the standard spec**, not
an extension. Therefore, we:

1. **Force** `strictOpenAiCompliance: false` for the underlying chatComplete call
2. **Always** include reasoning output items if thinking data is present

This means users calling the Responses API automatically get reasoning/thinking data
from any provider that supports it (Anthropic, Google, etc.) without setting any flags.

## Supported Features

### ✅ Fully Supported

- Text input/output
- Multi-turn conversations
- Function tool calls
- Token usage tracking
- Reasoning/thinking output (automatically included when provider returns it)
- Streaming responses (SSE events transformed to Responses API format)

### ❌ Not Supported (Responses API-only features)

- `previous_response_id` state management
- Built-in tools (web_search, file_search, computer_use)
- `input_file` content type

## Provider-Specific Parameters

### Reasoning/Thinking

Different providers have different mechanisms to enable reasoning. The gateway maps
`reasoning.effort` to each provider's native format automatically.

| Provider              | Parameter          | Mapping                                                                     |
| --------------------- | ------------------ | --------------------------------------------------------------------------- |
| OpenAI (o-series)     | `reasoning.effort` | Passthrough as `reasoning_effort`                                           |
| Anthropic (Opus 4.5+) | `reasoning.effort` | Mapped to `output_config.effort`                                            |
| Anthropic             | `thinking`         | Direct passthrough (supports `type: 'enabled'`, `'adaptive'`, `'disabled'`) |
| Google / Vertex AI    | `reasoning.effort` | Mapped to `thinking_config` / `thinkingConfig`                              |

**Note:** For Anthropic, you can use either `reasoning.effort` (mapped to `output_config.effort`)
or the native `thinking` parameter directly. For Claude 4.6+, adaptive thinking
(`thinking: { type: "adaptive" }`) combined with `reasoning.effort` is the recommended approach.
