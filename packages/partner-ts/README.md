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
| `client.assets.risks(id)` | `GET /partner/v1/assets/{id}/risks` |
| `client.assets.usageSummary(id, { period })` | `GET /partner/v1/assets/{id}/usage-summary` (needs #99) |
| `client.vendors.list()` / `models.list()` / `businessUnits.list()` | `GET /partner/v1/vendors`, `/models`, `/business-units` |
| `client.changes.list({ since })` / `listPage(...)` | `GET /partner/v1/changes` |
| `client.export.get()` | `GET /partner/v1/export` |
| `client.health()` | `GET /partner/v1/health` |

Responses are converted from the API's snake_case to camelCase. Request parameters are written in camelCase and sent as snake_case.

**Auth.** Pass `clientId` and `clientSecret` for OAuth client credentials, or `apiKey`. With OAuth, the client fetches a token from `/oauth/token`, caches it, refreshes it a minute before it expires, and fetches a new one once if a request gets a 401.

**Retries.** The client retries 429, 502, 503, 504 and network errors up to `maxRetries` times (default 2). On a 429 it waits for the time in `Retry-After`. If `Retry-After` is longer than `maxRetryAfterSeconds` (default 60), it throws `RateLimitError` straight away.

**Errors.** Every error extends `PartnerError`. An HTTP error is an `APIError` with `status`, `title` and `detail`. The subclasses are `BadRequestError`, `AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `RateLimitError` (which has `retryAfter`) and `InternalServerError`. A network failure is an `APIConnectionError`, and a timeout is an `APITimeoutError`.

## Development

```bash
cd packages/partner-ts
npm install   # dev dependencies only: typescript, jest, ts-jest, @types/*
npm run build
npm test
```

Tests mock `fetch` and make no network calls.
