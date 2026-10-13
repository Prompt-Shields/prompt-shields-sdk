# AI Cost Tracker

One place to see what your cloud AI actually costs: **billed cost joined with token usage,
per model and per deployment**. It starts with Azure OpenAI / AI Foundry, because Microsoft
Cost Management makes this hard to see, and expands to GCP Vertex AI and AWS Bedrock.

> Status: **planning**. No code yet. Staged here until the `Prompt-Shields/ai-cost-tracker` repo is created. See [docs/ARCHITECTURE.md](ARCHITECTURE.md).

Internal tool first, run as a standalone SaaS web app that we host.

## What it will answer

- Cost and tokens per deployment and model, today (estimated) and historically (billed)
- Effective cost per 1M input / output tokens, and how it drifts over time
- Spend by team, app and cost centre (tags plus Prompt Shields gateway attribution)
- PTU utilisation and idle provisioned capacity
- Budget alerts and overnight spikes, sent to email, Slack or Teams

## Roadmap

0. Foundations: Entra ID login, RBAC, tenant-scoped schema, fake connector
1. Azure MVP (internal): our own tenant, token metrics, cost exports, per-deployment cost
2. Budgets, alerts, PTU reports, allocation, gateway attribution
2b. Open to external users: signup, connection wizard, Google login, SAML
3. GCP (Vertex AI)
4. AWS (Bedrock)
