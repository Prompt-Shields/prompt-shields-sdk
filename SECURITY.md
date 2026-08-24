# Security policy

## Reporting a vulnerability

Report suspected vulnerabilities privately to **security@promptshields.com**. Please do not open a public issue, and do not disclose the finding publicly until a fix is available.

Include as much of the following as you can: the affected component and version or commit, a description of the impact, and the steps or proof of concept needed to reproduce it.

We aim to acknowledge a report within three working days and to give an initial assessment, including whether we consider the finding in scope, within ten working days.

## Scope

In scope: the Python SDK (`packages/sdk`), the telemetry collector and registry API (`packages/collector`), the database layer (`packages/db`), and the Prompt Shields extensions to the vendored gateway (`gateway/src/middlewares`).

Out of scope: vulnerabilities in upstream [Portkey AI Gateway](https://github.com/Portkey-AI/gateway) code, which should be reported to that project; and the hosted Prompt Shields Cloud service, which is covered by its own disclosure process.

## Known limitations, deliberately not treated as vulnerabilities

The following are documented characteristics of the current code rather than findings. They are listed in the README under "What this does not do", and reports covering them will be closed as known.

- Telemetry is fail-open and lossy. Events are buffered in memory and dropped on overflow or process exit, and the register is not an audit log.
- The SDK does not block, filter, or redact prompts or responses, and provides no prompt-injection or jailbreak defence.
- PII detection is pattern-based and produces both false positives and false negatives. It is not a data-loss-prevention control.
- Collector authentication currently compares the bearer token against a plaintext value stored on the tenant record. This is a known Phase 1 shortcut, flagged in `packages/collector/collector/auth.py`, and must not be deployed to a production or pilot environment.
- `rate_limit_per_minute` is present in configuration but not enforced.
- The collector ships without TLS termination, role-based access control, encryption at rest, retention or deletion policies, or audit logging of registry reads.
- Tenant isolation is enforced by query predicate rather than database row-level security.

A report that demonstrates a way to bypass tenant isolation, to authenticate as another tenant, or to extract data across tenants is in scope and welcome, even though the limitations above are not.

## Deployment guidance

This repository is intended for self-hosted evaluation and development. Before any deployment handling real data, terminate TLS in front of the collector, replace the plaintext API key lookup with a hashed comparison, place the collector on a trusted network, and set a retention policy for the registry. Leave `OPENAI_API_KEY` unset on the collector unless you accept that asset metadata is sent to OpenAI's embeddings API.
