# Fork Notice

This directory is a fork of the [Portkey AI Gateway](https://github.com/Portkey-AI/gateway), MIT licensed. Upstream `LICENSE` is preserved verbatim alongside this notice.

## Upstream base

| | Commit | Date | Notes |
|---|---|---|---|
| Fork base | [`28ed85e`](https://github.com/Portkey-AI/gateway/commit/28ed85e) | 2026-02-19 | v1.15.2. Forked 2026-03-24. Confirmed by diffing `src/` and `plugins/` against every upstream `main` commit (`plugins/` identical; `src/` differs only by our changes). |
| OSS `main` triaged to | [`669825c`](https://github.com/Portkey-AI/gateway/commit/669825c) | 2026-05-25 | No upstream commits since. |
| OSS `2.0.0` triaged to | [`8febc1d`](https://github.com/Portkey-AI/gateway/commit/8febc1d) | 2026-03-14 | Pre-release; branches from our exact base. |
| Enterprise changelog triaged to | 2.28.0 | 2026-10-08 | Closed source. Features are re-implemented from the public changelog, never copied. |

**Decision (#56, 2026-10-12): rebase onto `2.0.0`, do not wait for a GA tag.** OSS `main` has been frozen since 2026-05-25 and `2.0.0` branches from our base, so cherry-picking 909 files piece by piece costs more than one rebase. Until the rebase lands (milestone *U1*, #67), security fixes are cherry-picked from `main`.

### Cherry-picked from upstream `main`

| Upstream | Change | Our adaptation |
|---|---|---|
| [`de0e6c5`](https://github.com/Portkey-AI/gateway/commit/de0e6c5), [`3def659`](https://github.com/Portkey-AI/gateway/commit/3def659) | Reject `x-portkey-forward-headers` inside forward headers or config `forward_headers` (request loop) | None (#55) |
| [`4e43b19`](https://github.com/Portkey-AI/gateway/commit/4e43b19), [`9bcaee4`](https://github.com/Portkey-AI/gateway/commit/9bcaee4) | Admin auth on the `/public` console and `/log/stream`; log stream disabled without a token | Token comes from `PS_GATEWAY_ADMIN_TOKEN` (min. 16 chars) instead of a committed `conf.json`, which this fork gitignores; constant-time comparison (#54) |
| [`e457022`](https://github.com/Portkey-AI/gateway/commit/e457022) | Remove the default admin token | Applies to upstream's `conf.json` only; our `conf.example.json` ships no token (#54) |
| [`c9ee943`](https://github.com/Portkey-AI/gateway/commit/c9ee943), [`55d653e`](https://github.com/Portkey-AI/gateway/commit/55d653e) | Redact provider options and headers in the log stream | None (#54) |

Skipped from `main`: [`b341133`](https://github.com/Portkey-AI/gateway/commit/b341133) (CrowdStrike AIDR guardrail metadata; guardrails are out of scope).

### How to sync upstream

```bash
# 1. What changed upstream (OSS) and in the enterprise changelog since the triage points above
python3 scripts/upstream-watch.py --since-enterprise 2.28.0 --since-main 669825c --since-2-0 8febc1d

# 2. Re-confirm the base: an empty plugins/ diff and a src/ diff limited to PS files
git clone --filter=blob:none https://github.com/Portkey-AI/gateway /tmp/portkey
mkdir -p /tmp/portkey-base && git -C /tmp/portkey archive 28ed85e | tar -x -C /tmp/portkey-base
diff -rq /tmp/portkey-base gateway -x node_modules
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
6. **Hardened** (re-implementations of Portkey enterprise fixes, #65): upstream URL SSRF check on provider-built URLs (`isSafeUpstreamURL`), credential masking in gateway logs (`src/utils/redact.ts`), config target fan-out cap (`MAX_ROOT_CONFIG_TARGETS`, `MAX_CONDITIONAL_ROOT_CONFIG_TARGETS`, `MAX_NESTED_CONFIG_TARGETS`), custom-host response size cap (`PS_MAX_CUSTOM_HOST_RESPONSE_BYTES`, default 50 MiB), and provider credential headers taking precedence over forwarded or proxied client headers

## Original License

The original Portkey AI Gateway is licensed under the MIT License. See [`LICENSE`](./LICENSE).

The `ps-telemetry.ts` middleware and the PS-specific configuration glue are wholly original code by Prompt Shields, also released under MIT.
