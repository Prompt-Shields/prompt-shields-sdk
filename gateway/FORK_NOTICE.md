# Fork Notice

This directory is a fork of the [Portkey AI Gateway](https://github.com/Portkey-AI/gateway), MIT licensed. Upstream `LICENSE` is preserved verbatim alongside this notice.

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

## Original License

The original Portkey AI Gateway is licensed under the MIT License. See [`LICENSE`](./LICENSE).

The `ps-telemetry.ts` middleware and the PS-specific configuration glue are wholly original code by Prompt Shields, also released under MIT.
