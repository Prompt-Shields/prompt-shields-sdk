# @prompt-shields/partner

TypeScript client for the Prompt Shields Partner API. It has no runtime dependencies and uses the global `fetch`, so it needs Node 18 or later.

Usage is documented in [`docs/sdks/typescript.mdx`](../../docs/sdks/typescript.mdx).

```ts
import { PartnerClient } from '@prompt-shields/partner';

const client = new PartnerClient({ apiKey: 'ps-pk-your-key-here' });
for await (const asset of client.assets.list({ limit: 200 })) {
  console.log(asset.vendor, asset.useCaseName);
}
```

## Coverage

| Method | Endpoint |
|---|---|
| `client.assets.list(params)` / `listPage(params)` | `GET /partner/v1/assets` |
| `client.assets.get(id)` | `GET /partner/v1/assets/{id}` |
| `client.assets.dataFlows(id)` | `GET /partner/v1/assets/{id}/data-flows` |
| `client.assets.risks(id, { status })` | `GET /partner/v1/assets/{id}/risks` |
| `client.assets.attestations(id, { field, limit })` | `GET /partner/v1/assets/{id}/attestations` |
| `client.assets.attest(id, { field, state, attestedBy, comment })` | `POST /partner/v1/assets/{id}/attestations` (`attestations:write`) |
| `client.assets.usageSummary(id, { period })` | `GET /partner/v1/assets/{id}/usage-summary` |
| `client.assets.riskReviews(id, riskId, { limit })` | `GET /partner/v1/assets/{id}/risks/{riskId}/reviews` (review history; `meta.current` is the mapping now) |
| `client.assets.reviewRisk(id, riskId, { status, reviewedBy, comment })` | `POST /partner/v1/assets/{id}/risks/{riskId}/review` (`risks:review`) |
| `client.spendAlerts.list({ status, severity, since, until, limit })` | `GET /partner/v1/spend-alerts` (`spend-alerts:read`; no cursor, up to 500) |
| `client.spendAlerts.get(id)` / `acknowledge(id)` | `GET /partner/v1/spend-alerts/{id}`, `POST .../acknowledge` (`spend-alerts:acknowledge`) |
| `client.usage.focusExport({ start, end, format, granularity, costMode }, { signal })` | `GET /partner/v1/usage/focus-export` (`usage:export`; FinOps FOCUS v1.4 CSV or JSON Lines, streamed) |
| `client.usage.savings({ period, groupBy })` | `GET /partner/v1/usage/savings` (realised savings vs. counterfactual, by day/week/month, asset, use case or business unit) |
| `client.usage.sovereignShare({ period })` | `GET /partner/v1/usage/sovereign-share` (share of calls and spend served in the EEA, per sensitivity level) |
| `client.mcp.listTools({ serverName, source, limit })` / `listToolsPage(...)` | `GET /partner/v1/mcp/tools` (`mcp:read`; cursor-paged) |
| `client.mcp.listUsage({ since, until, toolName, serverName, source, limit })` / `listUsagePage(...)` | `GET /partner/v1/mcp/usage` (`mcp:read`; daily tool-call metering, cursor-paged) |
| `client.learner.listSnapshots({ limit })` / `getSnapshot(version)` | `GET /partner/v1/learner/snapshots`, `/snapshots/{learner_version}` (`learner:read`) |
| `client.learner.listRecommendations({ status, useCase, learnerVersion, limit })` / `getRecommendation(id)` | `GET /partner/v1/learner/recommendations`, `/recommendations/{id}` (`learner:read`) |
| `client.learner.reviewRecommendation(id, { decision, reviewedBy, comment })` | `POST /partner/v1/learner/recommendations/{id}/review` (`learner:review`; decided once) |
| `client.siem.calls({ cursor, since, limit, settleSeconds }, { signal })` / `callsFeed(...)` | `GET /partner/v1/siem/calls` (`evidence:read`; NDJSON, streamed) |
| `client.siem.audit({ cursor, since, limit }, { signal })` / `auditFeed(...)` | `GET /partner/v1/siem/audit` (`audit:read`; NDJSON, streamed) |
| `client.audit.list({ since, until, action, limit })` / `listPage(...)` | `GET /partner/v1/audit` (`audit:read`; JSON pages in chain order, `details` kept as stored) |
| `client.providerCredentials.list()` / `get(provider)` | `GET /partner/v1/provider-credentials`, `/{provider}` (`provider-credentials:manage`; masked) |
| `client.providerCredentials.create(provider, { adminKey })` / `rotate(...)` / `delete(provider)` | `POST /partner/v1/provider-credentials/{provider}`, `POST .../rotate`, `DELETE` (write-only keys) |
| `client.providerKeyFingerprints.list()` / `put(provider, apiKeyId, { apiKeyFingerprint })` / `delete(provider, apiKeyId)` | `GET /partner/v1/provider-key-fingerprints`, `PUT` / `DELETE .../{provider}/{api_key_id}` (`provider-credentials:manage`) |
| `client.telemetry.coverage()` | `GET /partner/v1/telemetry/coverage` (coverage per source: sdk, gateway, otel, kong) |
| `client.vendors.list()` / `models.list()` / `businessUnits.list()` | `GET /partner/v1/vendors`, `/models`, `/business-units` |
| `client.changes.list({ since })` / `listPage(...)` | `GET /partner/v1/changes` |
| `client.export.get()` | `GET /partner/v1/export` |
| `client.health()` | `GET /partner/v1/health` |
| `client.send(method, path, { query, body, retrySafe })` | Any other endpoint |
| `client.getStream(path, query, { signal, accept })` | Any other download: the `Response` with its body unread |

Webhook subscriptions are the tenant admin API (`/api/v1/admin/webhooks`), so they need a client built with the tenant's API key, not partner credentials:

| Method | Endpoint |
|---|---|
| `client.webhooks.subscriptions.create({ partnerId, url, eventTypes, description })` | `POST /subscriptions` (returns the secret once) |
| `client.webhooks.subscriptions.list({ partnerId })` / `get(id)` | `GET /subscriptions`, `/subscriptions/{id}` |
| `client.webhooks.subscriptions.update(id, { url, eventTypes, isActive })` | `PATCH /subscriptions/{id}` |
| `client.webhooks.subscriptions.delete(id)` | `DELETE /subscriptions/{id}` |
| `client.webhooks.subscriptions.rotateSecret(id, { gracePeriodSeconds })` | `POST /subscriptions/{id}/rotate-secret` |
| `client.webhooks.deliveries.list({ status, subscriptionId, limit })` / `redeliver(id)` | `GET /deliveries`, `POST /deliveries/{id}/redeliver` |

Responses are converted from the API's snake_case to camelCase. Request parameters and bodies are written in camelCase and sent as snake_case. The attestation maps (`asset.attestation`, `meta.current`) keep the wire field names (`data_classification`) as keys so they match `attestation.field`.

**Auth.** Pass `clientId` and `clientSecret` for OAuth client credentials, or `apiKey`. With OAuth, the client fetches a token from `/oauth/token`, caches it, refreshes it a minute before it expires, and fetches a new one once if a request gets a 401.

**Retries.** The client retries 429, 502, 503, 504 and network errors up to `maxRetries` times (default 2). POST requests (attestations, risk reviews, learner recommendation reviews, storing or rotating a provider admin key, creating a subscription, rotating a secret, redelivering) are retried only on 429, since after a network error or a 5xx the server may already have acted. `spendAlerts.acknowledge()` is idempotent, so it is retried like a GET. A DELETE that gets a 404 on a retry after a network error or 5xx resolves as a success: the first attempt may have deleted the resource and lost its response, and the resource is gone either way (a 404 on the first attempt still throws `NotFoundError`). `providerKeyFingerprints.put()` is retried too; its `meta.corroboratedAssets` is the count from the attempt that answered, so after a retry it can be 0 even though an earlier, lost attempt corroborated assets. On a 429 it waits for the time in `Retry-After`. If `Retry-After` is longer than `maxRetryAfterSeconds` (default 60), it throws `RateLimitError` straight away.

**Streamed downloads.** `client.usage.focusExport()` resolves with a `FocusExport` once the response headers arrive (errors before then throw as usual, with retries). It carries `focusVersion`, `windowStart`, `windowEnd` and `filename` from the headers. The body is read once, as you consume it, through `rows()` (parsed JSON Lines rows, FOCUS column names kept as sent), `lines()`, `for await (const chunk of exp)`, `saveTo(path)` (Node: streams into a temporary file and renames it to `path` when complete; on failure nothing is left behind and an existing file is kept) or `body` (a web `ReadableStream`). Only `text()` holds the body in memory. `timeoutMs` covers the wait for the headers; pass `{ signal }` to cancel or bound the download. A lost or aborted connection, or a body that ends mid-line (truncated), throws `APIConnectionError`.

**SIEM pull.** `client.siem.calls()` and `audit()` resolve with a `SiemBatch`, the same streamed `Download`, carrying `cursor` (`X-PS-Cursor`; `null` when the server sent none, so keep your previous one) and `hasMore` (`X-PS-Has-More`). `records()` parses each NDJSON line, keeping the Sentinel column names (`TimeGenerated`, `PsServedModel`, `PsAuditRowHash`). `callsFeed()` and `auditFeed()` follow the cursor until a batch is not full; save `feed.cursor` afterwards. It moves past a batch only once its body has been read to the end, so stopping or failing part way through a batch replays that batch next time (at-least-once). `feed.batches()` throws a `PartnerError` (cursor unchanged, batch cancelled) if you ask for the next batch before reading the current one in full or calling `batch.markConsumed()`.

**Errors.** Every error extends `PartnerError`. An HTTP error is an `APIError` with `status`, `title` and `detail`. The subclasses are `BadRequestError`, `AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `RateLimitError` (which has `retryAfter`) and `InternalServerError`. A network failure is an `APIConnectionError`, and a timeout is an `APITimeoutError`.

## Development

```bash
cd packages/partner-ts
npm ci        # dev dependencies only: typescript, jest, ts-jest, yaml, @types/*
npm run build
npm test
```

Tests mock `fetch` and make no network calls.

**Types and `docs/openapi.yaml`.** The types in `src/types.ts` are written by hand, because the SDK camelCases the wire format. `test/openapi.test.ts` (part of `npm test`, or alone with `npm run check:openapi`) reads `src/types.ts` with the TypeScript compiler and fails when a schema property, enum value, query parameter or operation in `docs/openapi.yaml` has no counterpart in the SDK, or the SDK has a field the spec does not. It also fails when a field is optional in one and required in the other, and it checks request bodies. The webhook admin API (`client.webhooks`) is covered too, and the FOCUS row columns are compared by their exact names. The provider credential routes, the rest of the tenant admin API, the operator routes and the tenant ingest-key routes are in the spec but not wrapped by the SDK (the test lists them as such); call them with `client.send()`.

**Releasing.** See [RELEASING.md](RELEASING.md). Publishing needs the `NPM_TOKEN` secret and the `@prompt-shields` npm org.
