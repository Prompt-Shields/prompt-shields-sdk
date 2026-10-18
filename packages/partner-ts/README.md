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
| `client.telemetry.coverage()` | `GET /partner/v1/telemetry/coverage` (coverage per source: sdk, gateway, otel) |
| `client.vendors.list()` / `models.list()` / `businessUnits.list()` | `GET /partner/v1/vendors`, `/models`, `/business-units` |
| `client.changes.list({ since })` / `listPage(...)` | `GET /partner/v1/changes` |
| `client.export.get()` | `GET /partner/v1/export` |
| `client.health()` | `GET /partner/v1/health` |
| `client.send(method, path, { query, body })` | Any other endpoint |

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

**Retries.** The client retries 429, 502, 503, 504 and network errors up to `maxRetries` times (default 2). POST requests (attestations, creating a subscription, rotating a secret, redelivering) are retried only on 429, since after a network error or a 5xx the server may already have acted. On a 429 it waits for the time in `Retry-After`. If `Retry-After` is longer than `maxRetryAfterSeconds` (default 60), it throws `RateLimitError` straight away.

**Errors.** Every error extends `PartnerError`. An HTTP error is an `APIError` with `status`, `title` and `detail`. The subclasses are `BadRequestError`, `AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `RateLimitError` (which has `retryAfter`) and `InternalServerError`. A network failure is an `APIConnectionError`, and a timeout is an `APITimeoutError`.

## Development

```bash
cd packages/partner-ts
npm ci        # dev dependencies only: typescript, jest, ts-jest, yaml, @types/*
npm run build
npm test
```

Tests mock `fetch` and make no network calls.

**Types and `docs/openapi.yaml`.** The types in `src/types.ts` are written by hand, because the SDK camelCases the wire format. `test/openapi.test.ts` (part of `npm test`, or alone with `npm run check:openapi`) reads `src/types.ts` with the TypeScript compiler and fails when a schema property, enum value, query parameter or operation in `docs/openapi.yaml` has no counterpart in the SDK, or the SDK has a field the spec does not. The webhook admin API is not in `docs/openapi.yaml`; its types follow `packages/collector/collector/webhook_admin.py`.

**Releasing.** See [RELEASING.md](RELEASING.md). Publishing needs the `NPM_TOKEN` secret and the `@prompt-shields` npm org.
