# prompt-shields Helm chart

Deploys the Prompt Shields **gateway** (Node, port 8787) and **collector**
(FastAPI AI asset registry, port 8000) on Kubernetes, with a database
migration hook, optional background workers and CronJobs, and the usual
production objects (HPA, PDB, NetworkPolicy, Ingress / Gateway API
HTTPRoute, ServiceMonitor, ExternalSecret).

PostgreSQL is **bring-your-own**: the chart does not install a database.
Use a managed service (Amazon RDS / Aurora, Cloud SQL, Azure Database for
PostgreSQL Flexible Server) or an in-cluster operator such as CloudNativePG.
PostgreSQL 15 or later with the `pgvector` and `uuid-ossp` extensions.

- [Install](#install)
- [Database](#database) — roles, row-level security, pgvector on managed Postgres
- [Secrets](#secrets)
- [Background work](#background-work)
- [Security defaults](#security-defaults)
- [Networking](#networking)
- [Observability](#observability)
- [Air-gapped install](#air-gapped-install)
- [Values](#values)
- [Environment variables](#environment-variables)
- [Development](#development)

## Install

```shell
# 1. Database roles and extensions (once; see "Database").
# 2. Secrets (see "Secrets"):
kubectl create namespace prompt-shields
kubectl -n prompt-shields create secret generic prompt-shields-migration \
  --from-literal=PS_DATABASE_URL='postgresql+asyncpg://ps_owner:...@db.internal:5432/prompt_shields'
kubectl -n prompt-shields create secret generic prompt-shields-collector \
  --from-literal=PS_DATABASE_URL='postgresql+asyncpg://ps_app:...@db.internal:5432/prompt_shields' \
  --from-literal=PS_PARTNER_JWT_SECRET="$(openssl rand -hex 32)" \
  --from-literal=PS_ADMIN_TOKEN="$(openssl rand -hex 32)"
kubectl -n prompt-shields create secret generic prompt-shields-gateway \
  --from-literal=PS_API_KEY='<tenant API key>' \
  --from-literal=PS_GATEWAY_ADMIN_TOKEN="$(openssl rand -hex 24)"

# 3. Install
helm install prompt-shields oci://ghcr.io/prompt-shields/charts/prompt-shields \
  --version 0.1.0 --namespace prompt-shields --timeout 15m \
  --set secrets.migration.existingSecret=prompt-shields-migration \
  --set secrets.collector.existingSecret=prompt-shields-collector \
  --set secrets.gateway.existingSecret=prompt-shields-gateway
```

Verify the chart signature before installing (see [Air-gapped install](#air-gapped-install)).

The chart fails to render without a collector Secret. Database URLs use the
SQLAlchemy asyncpg driver: `postgresql+asyncpg://user:password@host:5432/db`
(add `?ssl=require` for TLS).

## Database

### Roles and row-level security

The collector enforces tenant isolation with PostgreSQL row-level security
(RLS): every tenant table has a `tenant_isolation` policy (migration `015`,
`packages/db/rls.py`), and `python -m collector.rls enable` turns enforcement
on with `FORCE ROW LEVEL SECURITY`. With `collector.rlsRequired: true` (the
default, `PS_RLS_REQUIRED=true`) the collector refuses to start unless RLS
binds its role. RLS never binds a superuser or a role with `BYPASSRLS`.

Use two roles:

| Role | Used by | Needs |
|---|---|---|
| migration (owner) | the migration Job (`secrets.migration`) | owns the schema and tables; runs `alembic upgrade head` and `collector.rls enable` |
| application | collector, worker, CronJobs (`secrets.collector`) | `SELECT, INSERT, UPDATE, DELETE` on the tables; **not** the owner, not superuser, no `BYPASSRLS` |

The owner can switch RLS off (`ALTER TABLE ... NO FORCE ROW LEVEL SECURITY`),
so the application should not connect as it. If you give the chart only a
collector Secret, the migration Job uses it too and `NOTES.txt` warns.

```sql
-- As an administrator (RDS: the master user; Cloud SQL: cloudsqlsuperuser).
CREATE ROLE ps_owner LOGIN PASSWORD '...';
CREATE ROLE ps_app   LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
CREATE DATABASE prompt_shields OWNER ps_owner;
\c prompt_shields
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
GRANT CONNECT ON DATABASE prompt_shields TO ps_app;
GRANT USAGE ON SCHEMA public TO ps_app;
-- Tables created by later migrations get the same grants:
ALTER DEFAULT PRIVILEGES FOR ROLE ps_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ps_app;
ALTER DEFAULT PRIVILEGES FOR ROLE ps_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO ps_app;
-- On PostgreSQL 15+ the public schema is not writable by default:
GRANT CREATE ON SCHEMA public TO ps_owner;
```

If tables already exist (an upgrade from a single-role install), also run
`GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ps_app;`
and the same for sequences, as the owner.

### pgvector on managed PostgreSQL

Migrations `001` and `002` run `CREATE EXTENSION IF NOT EXISTS "uuid-ossp"`
and `CREATE EXTENSION IF NOT EXISTS vector`. Creating an extension needs
elevated rights on most managed services, which the migration role usually
lacks, so **pre-create both extensions as an administrator** (the SQL above);
the migrations then find them and carry on.

- **Amazon RDS / Aurora PostgreSQL**: pgvector is available on current
  versions; connect as the master user (`rds_superuser`) and run
  `CREATE EXTENSION vector;`.
- **Google Cloud SQL**: connect as a `cloudsqlsuperuser` member and run
  `CREATE EXTENSION vector;`.
- **Azure Database for PostgreSQL Flexible Server**: first allow-list the
  extensions (server parameter `azure.extensions` = `VECTOR,UUID-OSSP`), then
  `CREATE EXTENSION vector;` as the admin user.
- **CloudNativePG**: use an image with pgvector and list the extensions in
  the `Database` resource or `postInitApplicationSQL`.

### Migration Job

A Helm `pre-install,pre-upgrade` hook (Argo CD runs it as `PreSync`) that runs,
in the collector image (which ships alembic) from its WORKDIR `/app`:

```shell
alembic -c packages/db/alembic.ini upgrade head
python -m collector.rls enable
```

`migration.enableRls: false` skips the RLS step; `migration.command` /
`migration.args` replace the script; `migration.image` overrides the image.
Because hooks run before the release's ordinary
objects exist, the Job uses no chart ConfigMap or ServiceAccount, and any
Secret or ExternalSecret the chart makes for it is itself a hook object.
That hook Secret or ExternalSecret (it holds the owner role's URL) is deleted
once the migration succeeds; after a failure it is kept, with the Job, for
debugging, and replaced on the next install or upgrade.

**Use `--timeout 15m` for `helm install` and `helm upgrade`** (or any value
at least `migration.activeDeadlineSeconds`, 900 s by default). Helm waits
for the migration hook only for `--timeout`, which defaults to 5 minutes. A
migration that runs longer (a large table, a pgvector index build) makes the
release fail while the Job keeps running, and the next install or upgrade
deletes that Job (`before-hook-creation`) mid-migration.

```shell
helm upgrade --install prompt-shields oci://ghcr.io/prompt-shields/charts/prompt-shields \
  --version 0.1.0 --namespace prompt-shields --timeout 15m -f values.yaml
```
For GitOps tools that do not run Helm hooks, set `migration.enabled: false`
and run the same commands from your own pipeline.

## Secrets

Each component reads a Secret with `envFrom`, so **keys are environment
variable names**. For each component the chart uses the first of:

1. `secrets.<component>.existingSecret` — a Secret you manage (recommended);
2. an ExternalSecret (`externalSecrets.enabled` and `externalSecrets.<component>.data` / `dataFrom`), which creates `<fullname>-<component>`;
3. `secrets.create: true` with `secrets.<component>.values` — development
   only: the values are stored in the Helm release history.

| Component | Keys |
|---|---|
| `collector` (required) | `PS_DATABASE_URL` (application role), `PS_PARTNER_JWT_SECRET` (required in practice: without it the collector signs partner tokens with a public development secret), `PS_ADMIN_TOKEN`, `PS_COLLECTOR_METRICS_TOKEN`, `PS_WEBHOOK_SECRET_KEYS` / `PS_WEBHOOK_SECRET_KEY`, `PS_PROVIDER_ADMIN_KEYS`, `PS_LEANIX`, `PS_SERVICENOW`, `PS_SENTINEL_EXPORT`, `PS_SIEM_EXPORT`, `PS_CLOUD_INVENTORY`, `OPENAI_API_KEY` |
| `migration` | `PS_DATABASE_URL` (owner role). Falls back to the collector Secret |
| `gateway` (optional) | `PS_API_KEY`, `PS_GATEWAY_ADMIN_TOKEN`, `PS_METRICS_TOKEN`, `REDIS_CONNECTION_STRING`, `PS_BUDGET_REDIS_URL`, `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, `PS_OTEL_HEADERS`, `PS_MCP_TOKEN` and the MCP server credentials the MCP config names. Callers send their own provider credentials with each request, except with `gateway.oidc`: then `PS_OIDC_PROVIDER_KEYS` (JSON, provider to API key) and `PS_OIDC_SUBJECT_SALT` |

ExternalSecret example (External Secrets Operator with Vault or AWS Secrets
Manager behind a `ClusterSecretStore`):

```yaml
externalSecrets:
  enabled: true
  secretStoreRef: {name: vault, kind: ClusterSecretStore}
  collector:
    dataFrom:
      - extract: {key: prompt-shields/collector}
  migration:
    data:
      - secretKey: PS_DATABASE_URL
        remoteRef: {key: prompt-shields/migration, property: database_url}
```

Some gateway variables (the `AWS_*` credential and region variables,
`AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `KMS_*`, `HTTPS_PROXY`) may hold an
absolute file path instead of a value, and the gateway reads the file. So a
Secrets Store CSI volume works for them: mount it with
`gateway.extraVolumes` / `extraVolumeMounts` and set e.g.
`AWS_ACCESS_KEY_ID: /mnt/secrets/aws-access-key-id` in `gateway.config`.

## Background work

The collector has two background loops, webhook delivery and the provider
usage pull, each on when its interval is above 0
(`PS_WEBHOOK_WORKER_INTERVAL_SECONDS`, `PS_PROVIDER_USAGE_PULL_INTERVAL_SECONDS`;
both 0 by default). Run each in exactly one place, not in every API replica:

- `worker.enabled: true`: one Deployment (one replica, `Recreate`) running
  `python -m collector.workers`. The chart sets both intervals on it from
  `worker.webhookIntervalSeconds` (default 5) and
  `worker.providerUsagePullIntervalSeconds` (default 0, off; at least 3600,
  usually 86400). At least one must be above 0, since the process exits
  with status 2 when no loop is on; the chart refuses to render otherwise.
- `cronJobs.jobs.*`: one CronJob each. `auditPurge`, `retentionPurge` and
  `spendAnomaly` are on by default (since chart 0.4.11; see
  [Upgrading](#upgrading)), the others are off:

| Job | Command | Default | Default schedule |
|---|---|---|---|
| `auditPurge` | `python -m collector.audit purge` (partner audit rows older than `PS_AUDIT_RETENTION_DAYS`) | on | `17 2 * * *` |
| `retentionPurge` | `python -m collector.retention purge` (usage events past retention, and usage rollups past `PS_USAGE_ROLLUP_RETENTION_DAYS`) | on | `37 2 * * *` |
| `spendAnomaly` | `python -m collector.spend_anomalies --all-tenants` (spend anomaly alerts for yesterday, UTC) | on | `57 3 * * *` |
| `lifecycleSweep` | `python -m collector.lifecycle` (mark assets stale / retired) | off | `7 3 * * *` |
| `webhooksDeliver` | `python -m collector.webhooks deliver` (alternative to the worker) | off | every minute |
| `webhooksPurge` | `python -m collector.webhooks purge` | off | `47 3 * * *` |
| `providerUsage` | `python -m collector.provider_usage_admin --all-tenants` | off | `27 4 * * *` |

The usage rollups (hourly and daily aggregates behind
`/partner/v1/usage/series`) are refreshed by a third loop that is on by
default, `PS_USAGE_ROLLUP_INTERVAL_SECONDS=30`. It is safe in every API
replica (a run skips a tenant another run is refreshing); set it to `0` in
`collector.extraEnv` and add an entry running
`python -m collector.usage_rollups refresh --all-tenants` to run it as a
CronJob instead.

Add per-tenant connector syncs as extra entries, e.g.
`args: ["python", "-m", "collector.leanix", "--tenant", "<uuid>"]` (also
`collector.servicenow`, `collector.cloud_inventory`; the Microsoft Sentinel
export takes `python -m collector.sentinel_export --all-tenants`, the Splunk
HEC / syslog / webhook SIEM export `python -m collector.siem_export --all-tenants`). One-off maintenance
(`collector.reembed`, `collector.dedup`, `collector.webhooks rekey`) is best
run with `kubectl create job --from=cronjob/...` or a temporary pod. The
chart refuses to render webhook delivery or the provider pull in both the
worker and a CronJob.

`PS_COLLECTOR_RUN_WORKERS` (default `true` in the image) lets the API
process run the loops too. With `collector.runWorkers.value: ""` (the
default) the chart sets it to `false` on the API pods whenever the worker
Deployment or the `webhooksDeliver` or `providerUsage` CronJob (the jobs that
run those loops) is enabled, and leaves it unset otherwise: purges, the
lifecycle sweep and connector CronJobs do not change it. `"true"` or
`"false"` forces it, e.g. for a custom CronJob that runs a loop. `collector.workers` ignores it, so the worker and
the API share one ConfigMap and Secret.

The partner and ingest rate limits are kept per process unless
`PS_COLLECTOR_REDIS_URL` is set: without it, N collector replicas (or uvicorn
workers) let a tenant reach N times `PS_RATE_LIMIT_PER_MINUTE`. With it every
replica counts in the same Redis (atomic INCR + EXPIRE per key and minute;
keys are hashed), so the limits hold globally. Set it in `collector.config`,
or in the collector Secret when the URL holds a password. If Redis is
unreachable a request is counted per process instead (fail open), logged,
and counted in `ps_collector_rate_limit_store_errors`.

Operators can override a tenant's ingest limits in
`settings.ingest_rate_limits` with `PATCH /api/v1/operator/tenants/{id}`,
e.g. `{"settings": {"ingest_rate_limits": {"events": 1000}}}` (keys
`events`, `kong`, `mcp`, `feedback`, `feedback_user`, `security_events`; `null` removes the
override). Each process caches overrides for
`PS_TENANT_LIMITS_CACHE_TTL_SECONDS` (default 30).

Tenant key auth keeps two more per-process structures. A key that passed its
bcrypt check is remembered for `PS_INGEST_KEY_CACHE_TTL_SECONDS` (by an HMAC
of the key, never the key), so its next requests skip bcrypt while the stored
hash is unchanged; the key row is still read on every request, so revoking,
rotating or expiring a key, or disabling its tenant, applies at once on every
replica. A client address with more than `PS_AUTH_FAILURE_LIMIT_PER_MINUTE`
failed authentications in a minute gets `429` (with `Retry-After`) instead of
further bcrypt checks for unknown, revoked or expired keys; a valid, live key
is never held back. The address is the one uvicorn reports, which follows
`X-Forwarded-For` only from proxies listed in uvicorn's `FORWARDED_ALLOW_IPS`
(default `127.0.0.1`). Behind an ingress or load balancer, set
`FORWARDED_ALLOW_IPS` in `collector.config` to its addresses, or every client
shares one failure count and one bad client can make other clients' wrong
keys get `429` instead of `401`.

## Upgrading

### To 0.4.11: purge CronJobs on by default

`cronJobs.jobs.auditPurge` and `cronJobs.jobs.retentionPurge` are now
enabled by default, so retention settings take effect without extra values.
**On their first run after the upgrade they delete data older than each
retention window:** partner audit rows older than `PS_AUDIT_RETENTION_DAYS`
(default 365 days), and each tenant's usage events (with the MCP usage,
call feedback and spend alert rows that `collector.retention` purges with
them) older than the tenant's `retention_days` setting, else
`PS_RETENTION_DAYS_DEFAULT` (default 365 days). Purge runs are recorded on
the audit trail (counts only).

Before upgrading, check the retention settings, and preview the usage event
purge with `python -m collector.retention purge --dry-run` (for example in a
temporary pod with the collector image and Secret). To keep the old
behaviour, disable the jobs:

```yaml
cronJobs:
  jobs:
    auditPurge:
      enabled: false
    retentionPurge:
      enabled: false
```

The new `spendAnomaly` CronJob (daily, `57 3 * * *`) is also on by default.
It writes spend alerts and queues `spend.anomaly` webhook events only;
it deletes nothing. Disable it with `cronJobs.jobs.spendAnomaly.enabled: false`,
or per tenant with `tenants.settings["spend_anomaly"] = {"enabled": false}`.

## Security defaults

Every pod meets the **restricted** Pod Security Standard:
`runAsNonRoot`, UID/GID 10001, `readOnlyRootFilesystem`,
`allowPrivilegeEscalation: false`, all capabilities dropped,
`seccompProfile: RuntimeDefault`, and no ServiceAccount token
(`serviceAccount.automountServiceAccountToken: false`; turn it on for IRSA /
Workload Identity). Writable paths are `emptyDir` volumes: `/tmp` everywhere
and the optional fastembed cache. The collector gets `HOME=/tmp` and
`PYTHONDONTWRITEBYTECODE=1`.

- **OpenShift**: set `podSecurityContext.runAsUser`, `runAsGroup` and
  `fsGroup` to `null` so the restricted SCC assigns a UID.
- The gateway runs `node build/start-server.js --headless` directly (no npm
  in front of it, no local UI), so SIGTERM reaches the server. On SIGTERM it
  drains in-flight requests for up to `gateway.shutdown.timeoutMs`
  (`PS_SHUTDOWN_TIMEOUT_MS`, 25000), after a 5 s `preStop` sleep that lets
  endpoints update. `terminationGracePeriodSeconds` (40) covers preStop +
  drain + telemetry flush (`PS_TELEMETRY_SHUTDOWN_TIMEOUT_MS`, 5 s); raise
  it with the drain timeout.
- Pin images by digest with `gateway.image.digest` / `collector.image.digest`.
- Tenant admin routes (`/api/v1/admin/*`) accept scoped admin tokens only
  (`PS_ADMIN_REQUIRE_SCOPED_TOKENS`, default `true` since 0.4.16).

### To 0.4.16: scoped admin tokens required

The collector now ships roles (principals, teams, projects, memberships;
migration `039`), and `PS_ADMIN_REQUIRE_SCOPED_TOKENS` defaults to `true`:
a tenant ingest key (the key SDKs and gateways send) gets **403 on every
`/api/v1/admin` route**. Ingest, the registry and the Partner API are
unchanged, and existing admin tokens and partner credentials keep working.

Before upgrading, move any automation that calls `/api/v1/admin` with an
ingest key to an admin token: issue one with
`POST /api/v1/admin/tokens` while the old version still runs, or afterwards
with the operator route
`POST /api/v1/operator/tenants/{tenant_id}/admin-tokens` (`PS_ADMIN_TOKEN`).
To keep the old behaviour for now, set it back in `collector.config`:

```yaml
collector:
  config:
    PS_ADMIN_REQUIRE_SCOPED_TOKENS: "false"
```

Even then an ingest key never reaches member management (`admin:members`).
- The images are **amd64 only** for now, so every workload defaults to
  `nodeSelector: {kubernetes.io/arch: amd64}` (CronJobs use
  `collector.nodeSelector`). Replacing a `nodeSelector` drops that entry.

## Networking

- Services are `ClusterIP`. Expose the gateway with `gateway.ingress`,
  `gateway.httpRoute` (Gateway API `v1`), or put Kong or another API gateway
  in front of the ClusterIP Service. The collector only needs exposing for
  SDKs, `ps-scan` or partners outside the cluster.
- `networkPolicy.enabled` (default on) renders three policies: the gateway
  accepts traffic from `networkPolicy.gateway.ingressFrom` (default: any);
  the collector only from gateway pods plus
  `networkPolicy.collector.extraIngressFrom` (add your ingress controller and
  Prometheus namespaces there); worker, CronJob and migration pods accept
  nothing. Egress allows DNS, the database (`networkPolicy.egress.database`)
  and, with `allowExternal: true` (default), everything else, because LLM
  providers, webhooks and connectors are external. To lock egress down, set
  `allowExternal: false` and list an egress proxy or VPC endpoints in
  `networkPolicy.egress.extra`; set `HTTPS_PROXY` for the gateway in
  `gateway.config`. FQDN-based egress needs a CNI with FQDN policies (Cilium,
  Calico Enterprise) and is not templated.
- `gateway.config.TRUSTED_CUSTOM_HOSTS` defaults to `none`: no host is
  exempt from the gateway's SSRF checks. Unset, the gateway would trust
  `localhost`, `127.0.0.1`, `::1` and `host.docker.internal` for provider
  custom hosts, which in a pod reach the gateway itself and its sidecars.
  Guardrail egress (webhook and log URLs, vendor base-URL overrides) never
  trusts localhost unless it is listed. To reach an internal LLM or
  guardrail endpoint, replace `none` with those host names (comma-separated).
  The check is hostname-only; keep `allowExternal: false` or an egress proxy
  for names that resolve to private addresses.
- With more than one gateway replica, set `REDIS_CONNECTION_STRING` in the
  gateway Secret so the cache and rate limits are shared.

## Observability

- Gateway: `gateway.metrics.enabled` sets `PS_METRICS_ENABLED=true` and
  serves Prometheus metrics on `/metrics`.
- Collector: `collector.metrics.enabled` sets
  `PS_COLLECTOR_METRICS_ENABLED=true` (`/metrics`, 404 when off).
- Metrics tokens: point `*.metrics.tokenSecret` (`{name, key}`) at a Secret
  key. The chart sets `PS_METRICS_TOKEN` (gateway) or
  `PS_COLLECTOR_METRICS_TOKEN` (collector) from it, and the ServiceMonitor
  sends it as the bearer token. If you put the token in the component's
  Secret instead, set `*.serviceMonitor.bearerTokenSecret` to the same key;
  with `secrets.create` the chart finds it there itself. Without a token the
  endpoint is open to anything the NetworkPolicy lets in.
- Probes: the collector's readiness is `/readyz` (`SELECT 1` within
  `collector.readyzTimeoutSeconds`, `PS_READYZ_TIMEOUT_SECONDS`, default 2;
  the probe's `timeoutSeconds` must be larger) and its liveness
  `/partner/v1/health`; the gateway uses `/v1/health` for both.
- `*.serviceMonitor.enabled` renders a Prometheus Operator ServiceMonitor.
- OpenTelemetry traces from the gateway: `PS_OTEL_ENDPOINT` in
  `gateway.config`. Security events as OTLP log records:
  `PS_SECURITY_EVENTS_OTLP: "true"` (sent to `PS_OTEL_ENDPOINT`'s `/v1/logs`
  unless `PS_SECURITY_EVENTS_OTLP_ENDPOINT` is set).
- `grafanaDashboards.enabled` ships the gateway traffic, guardrails and
  security, and collector health dashboards as a ConfigMap labelled for the
  Grafana sidecar (`grafana_dashboard: "1"` by default).
- `prometheusRule.enabled` renders the alert rules as a PrometheusRule; add
  the label your Prometheus `ruleSelector` matches to `prometheusRule.labels`.
- Both are copies of `deploy/grafana/dashboards` and
  `deploy/prometheus/alerts.yaml`; see `docs/guides/observability.mdx`.

The probe paths and the env var names the chart sets
(`collector.runWorkers.envName`, `collector.metrics.envName`,
`collector.apiDocs.envName`, `gateway.shutdown.envName`) are values, so they
can follow the images if those change.

## Air-gapped install

1. **Mirror the chart and images** from a connected machine:

   ```shell
   VERSION=0.1.0
   helm pull oci://ghcr.io/prompt-shields/charts/prompt-shields --version $VERSION
   cosign verify ghcr.io/prompt-shields/charts/prompt-shields:$VERSION \
     --certificate-identity-regexp '^https://github.com/Prompt-Shields/prompt-shields-sdk/\.github/workflows/helm\.yml@refs/tags/chart-v' \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com
   for img in gateway collector; do
     crane copy ghcr.io/prompt-shields/prompt-shields-sdk/$img:$VERSION \
       registry.internal.example.com/prompt-shields/prompt-shields-sdk/$img:$VERSION
   done
   helm push prompt-shields-$VERSION.tgz oci://registry.internal.example.com/charts
   ```

   `oras copy` or `skopeo copy --all` work as well as `crane`. Artifactory and
   Harbor can also proxy `ghcr.io` as a remote OCI repository.

2. **Point every image at the mirror** with one value; the repository path
   after the registry stays the same:

   ```yaml
   global:
     imageRegistry: registry.internal.example.com
     imagePullSecrets:
       - name: internal-registry
   ```

3. **No runtime downloads.** Keep `PS_EMBEDDINGS_BACKEND` at `none`, or at
   `openai` with `PS_EMBEDDINGS_OPENAI_BASE_URL` pointing at an internal
   OpenAI-compatible endpoint. The `auto` backend uses `api.openai.com` when
   `OPENAI_API_KEY` is set. The `local` backend downloads fastembed weights at
   start: either build a collector image with the weights baked in, or
   pre-load them into a PVC and set `collector.embeddingsCache.enabled: true`
   with `existingClaim` (a `ReadOnlyMany` or `ReadWriteMany` claim, since all
   collector pods mount it).

4. **Database**: pre-create `vector` and `uuid-ossp` (see above).

5. **Egress**: set `networkPolicy.egress.allowExternal: false`, and route
   provider calls through your egress proxy (`HTTPS_PROXY`) or private
   endpoints (`TRUSTED_CUSTOM_HOSTS` for internal custom hosts).

## Values

`values.yaml` is commented; the main groups:

| Key | Default | Description |
|---|---|---|
| `global.imageRegistry` | `""` | Registry that replaces every image's registry (mirrors) |
| `global.imagePullSecrets` | `[]` | Pull secrets for every pod |
| `nameOverride` / `fullnameOverride` | `""` | Object name overrides |
| `commonLabels` | `{}` | Labels on every object |
| `serviceAccount.create` / `.name` / `.annotations` | `true` / `""` / `{}` | ServiceAccount (IRSA / Workload Identity annotations) |
| `serviceAccount.automountServiceAccountToken` | `false` | Mount a token (needed for projected-token cloud identity) |
| `podSecurityContext` / `containerSecurityContext` | restricted PSS | Shared by every workload |
| `secrets.create` | `false` | Create Secrets from `secrets.*.values` (development only) |
| `secrets.{collector,migration,gateway}.existingSecret` | `""` | Secrets you manage |
| `externalSecrets.enabled` / `.apiVersion` / `.secretStoreRef` / `.refreshInterval` | `false` / `external-secrets.io/v1beta1` / — / `1h` | External Secrets Operator |
| `externalSecrets.{collector,migration,gateway}.data` / `.dataFrom` | `[]` | ExternalSecret contents |
| `gateway.enabled` / `collector.enabled` | `true` | Components |
| `*.image.registry` / `.repository` / `.tag` / `.digest` / `.pullPolicy` | `ghcr.io` / `prompt-shields/prompt-shields-sdk/{gateway,collector}` / appVersion / `""` / `IfNotPresent` | Images |
| `*.command` / `*.args` | see values | Container entrypoint (gateway also gets `--port=<port>`) |
| `*.replicaCount` | `2` | Replicas without autoscaling |
| `*.config` | see values | Non-secret env, rendered into a ConfigMap |
| `*.extraEnv` / `extraEnvFrom` / `extraVolumes` / `extraVolumeMounts` | `[]` | Extra container config |
| `*.resources` | see values | Requests and limits |
| `*.probes.{startup,liveness,readiness}` | see values | `enabled`, `path`, `periodSeconds`, `failureThreshold`, `timeoutSeconds` |
| `*.terminationGracePeriodSeconds` / `*.preStopSleepSeconds` | 40/5 (gateway), 30/5 (collector) | Shutdown |
| `*.service.type` / `.port` / `.annotations` | `ClusterIP` / 8787, 8000 | Services |
| `*.autoscaling.*` | off; min 2, CPU 70% | HPA (`autoscaling/v2`) |
| `*.podDisruptionBudget.*` | on, `minAvailable: 1` | PDB, rendered only with more than one replica |
| `*.ingress.*` | off | Ingress |
| `*.httpRoute.*` | off | Gateway API HTTPRoute (`parentRefs` required) |
| `*.serviceMonitor.*` | off | ServiceMonitor (needs `*.metrics.enabled`); `bearerTokenSecret` defaults to `*.metrics.tokenSecret` |
| `*.nodeSelector` | `kubernetes.io/arch: amd64` | The images are amd64 only |
| `*.podAnnotations` / `podLabels` / `tolerations` / `affinity` / `topologySpreadConstraints` / `priorityClassName` | empty | Scheduling |
| `gateway.collectorUrl` | in-cluster collector | `PS_COLLECTOR_URL`; a loopback or private-IP URL must also be in `gateway.config.TRUSTED_CUSTOM_HOSTS`, or telemetry is dropped |
| `gateway.metrics.enabled` / `.path` | `false` / `/metrics` | `PS_METRICS_ENABLED` |
| `*.metrics.tokenSecret` / `.tokenEnvName` | `{}` / `PS_METRICS_TOKEN`, `PS_COLLECTOR_METRICS_TOKEN` | Metrics bearer token from a Secret key |
| `gateway.shutdown.timeoutMs` / `.envName` | `25000` / `PS_SHUTDOWN_TIMEOUT_MS` | Drain on SIGTERM |
| `gateway.oidc.enabled` | `false` | `PS_OIDC_ENABLED`: OIDC/JWT authentication and IdP-group policy on `/v1/*` ([guide](../../docs/guides/gateway-oidc.mdx)) |
| `gateway.oidc.issuer` / `.audience` | `""` / `[]` | `PS_OIDC_ISSUER` / `PS_OIDC_AUDIENCE`, both required when enabled |
| `gateway.oidc.jwksUrl` / `.algorithms` / `.clockSkewSeconds` | discovery / asymmetric RS, PS, ES / `60` | `PS_OIDC_JWKS_URL` / `PS_OIDC_ALGORITHMS` / `PS_OIDC_CLOCK_SKEW_SECONDS` |
| `gateway.oidc.groupsClaim` / `.telemetrySubject` | `groups` / `hash` | `PS_OIDC_GROUPS_CLAIM` / `PS_OIDC_TELEMETRY_SUBJECT` |
| `gateway.oidc.policy` | `{}` (authentication only) | `PS_OIDC_POLICY` as JSON: groups to provider/model allowlists and rate limits (per replica) |
| `gateway.budgets.enabled` / `.budgets` | `false` / `[]` | `PS_BUDGETS`: spend budgets (`budget.v1`, [schema](../../schemas/budget/)) per business unit, use case, owner or deployment, by day, week or month; `warn`, `downgrade` (to a cheaper model that still meets the data policy) or `block` (HTTP 429) |
| `gateway.budgets.redis.url` / `.redis.existingSecret` | `""` / `{}` | `PS_BUDGET_REDIS_URL`: the Redis every replica (and the SDK) shares spend through (a Secret key for a URL with a password). Without one each replica counts its own spend |
| `gateway.routePolicy` | `{}` | A `route-policy.v1` document ([schema](../../schemas/route-policy/)) as YAML. Rendered into a ConfigMap mounted at `/etc/prompt-shields/route-policy` and set as `PS_ROUTE_POLICY_FILE`, which turns on the router with this policy and enforces its `data_policy` on every model call (403 `policy_violation`). An invalid document stops the gateway at start; changing it rolls the pods ([guide](../../docs/guides/routing.mdx#gateway-route-policy-file)) |
| `gateway.routePolicyReloadSeconds` | `0` (off) | `PS_ROUTE_POLICY_RELOAD_SECONDS`: also poll the mounted file and apply a changed policy without a restart; a bad reload keeps the last good policy |
| `gateway.budgets.namespace` / `.onStoreError` / `.reservationTtlMs` | `default` / `open` / `900000` | The document's `namespace` and `on_store_error` (`closed`: block budgets answer 503 while Redis is unreachable) / `PS_BUDGET_RESERVATION_TTL_MS` |
| `collector.rlsRequired` | `true` | `PS_RLS_REQUIRED` |
| `collector.runWorkers.envName` / `.value` | `PS_COLLECTOR_RUN_WORKERS` / `""` (auto) | Background loops in API pods; auto = `false` with the worker or the `webhooksDeliver` / `providerUsage` CronJob |
| `collector.metrics.enabled` / `.envName` / `.path` | `false` / `PS_COLLECTOR_METRICS_ENABLED` / `/metrics` | Collector metrics |
| `collector.readyzTimeoutSeconds` | `2` | `PS_READYZ_TIMEOUT_SECONDS` |
| `collector.apiDocs.enabled` / `.envName` | `false` / `PS_COLLECTOR_API_DOCS_ENABLED` | `/docs`, `/redoc`, `/openapi.json` |
| `collector.tmpSizeLimit` | `256Mi` | `/tmp` emptyDir |
| `collector.embeddingsCache.*` | off; `/var/cache/fastembed` | fastembed cache (`PS_EMBEDDINGS_CACHE_DIR`) |
| `migration.enabled` / `.enableRls` | `true` / `true` | Migration hook and RLS enable |
| `migration.image` / `.command` / `.args` | collector image / generated | Overrides |
| `migration.serviceAccountName` | `""` | An existing ServiceAccount for the Job |
| `migration.backoffLimit` / `.activeDeadlineSeconds` / `.ttlSecondsAfterFinished` / `.hookDeletePolicy` / `.annotations` | `1` / `900` / `86400` / `before-hook-creation,hook-succeeded` / `{}` | Job settings |
| `worker.enabled` / `.command` | `false` / `python -m collector.workers` | Singleton worker Deployment |
| `worker.webhookIntervalSeconds` / `.providerUsagePullIntervalSeconds` | `5` / `0` | Loop intervals; at least one above 0 |
| `cronJobs.defaults` | see values | Applied to every CronJob (`timeZone`, history, deadlines, resources) |
| `cronJobs.jobs.<name>` | `auditPurge`, `retentionPurge`, `spendAnomaly` on; the rest off | `enabled`, `schedule`, `args`, and any default override |
| `networkPolicy.enabled` | `true` | NetworkPolicies |
| `networkPolicy.gateway.ingressFrom` / `.collector.extraIngressFrom` | `[]` | Ingress peers |
| `networkPolicy.egress.dns` / `.database.cidrs` / `.database.port` / `.allowExternal` / `.extra` | `true` / `[]` / `5432` / `true` / `[]` | Egress |
| `grafanaDashboards.enabled` / `.labels` / `.annotations` / `.namespace` | `false` / `grafana_dashboard: "1"` / `{}` / release namespace | Dashboards ConfigMap for the Grafana sidecar |
| `prometheusRule.enabled` / `.labels` | `false` / `{}` | Alert rules as a PrometheusRule |
| `extraObjects` | `[]` | Extra manifests (templated) |

## Environment variables

Set non-secret ones in `gateway.config` / `collector.config` and secret ones
in the component's Secret. The chart itself sets the ones marked *chart*.

### Gateway

| Variable | Purpose |
|---|---|
| `PS_COLLECTOR_URL` (*chart*) | Collector base URL for telemetry |
| `PS_API_KEY` (secret) | Tenant API key sent to the collector (and as the OTLP bearer token) |
| `PS_GATEWAY_ADMIN_TOKEN` (secret) | Admin token for the local UI (not served with `--headless`) |
| `PS_METRICS_ENABLED` (*chart*), `PS_METRICS_TOKEN` (secret, or *chart* from `gateway.metrics.tokenSecret`) | Prometheus `/metrics` and its bearer token |
| `PS_SHUTDOWN_TIMEOUT_MS` (*chart*) | Drain deadline on SIGTERM |
| `PS_MCP_CONFIG_FILE`, `PS_MCP_TOKEN` (secret), `PS_MCP_ALLOW_UNAUTHENTICATED` | MCP proxy (`/v1/mcp/{server}`): the `mcp_servers` file (mount it from a ConfigMap with `gateway.extraVolumes` / `extraVolumeMounts`) and the client bearer token. See `gateway/docs/mcp-proxy.md` |
| `PS_TELEMETRY_MAX_ATTEMPTS`, `PS_TELEMETRY_RETRY_BASE_MS`, `PS_TELEMETRY_MAX_PENDING`, `PS_TELEMETRY_SHUTDOWN_TIMEOUT_MS` | Telemetry delivery to the collector |
| `PS_TELEMETRY_CREDENTIAL_FINGERPRINT` | `true` adds a stable `api_key_fingerprint` (static key hash, or client identity hash for keyless auth; never a token) to gateway telemetry. Default off |
| `PS_OTEL_ENDPOINT`, `PS_OTEL_HEADERS`, `PS_OTEL_CAPTURE_CONTENT`, `PS_OTEL_MAX_BATCH`, `PS_OTEL_FLUSH_MS`, `PS_OTEL_MAX_QUEUE` | OpenTelemetry GenAI spans |
| `PS_SECURITY_EVENTS_LOG`, `PS_SECURITY_EVENTS_TENANT` | Security events as JSON lines on stdout |
| `PS_SECURITY_EVENTS_OTLP`, `PS_SECURITY_EVENTS_OTLP_ENDPOINT`, `PS_SECURITY_EVENTS_OTLP_HEADERS` | Security events as OTLP log records |
| `PS_ROUTER_ENABLED`, `PS_CACHE_DEFAULT`, `PS_CACHE_MAX_ENTRIES` | Router and response cache |
| `PS_ROUTE_POLICY_FILE`, `PS_ROUTE_POLICY_RELOAD_SECONDS` | Route policy file (set by `gateway.routePolicy`): turns on the router with that `route-policy.v1` policy and its data policy. An invalid file stops the gateway; a bad reload (SIGHUP or poll) keeps the last good policy |
| `PS_POLICY_PULL_ENABLED`, `PS_POLICY_PULL_INTERVAL_SECONDS`, `PS_POLICY_PULL_API_KEY` (secret) | Central policy pulled from the collector (`/api/v1/policy/effective`, an ingest key with `policy:read`; default `PS_API_KEY`) every 30 s: its `route` and `access` sections replace the route policy file and `PS_OIDC_POLICY`; an unreachable collector or a malformed policy keeps the last good one. See `docs/guides/central-policy.mdx` |
| `PS_SEMANTIC_CACHE_EMBEDDER`, `PS_SEMANTIC_CACHE_THRESHOLD`, `PS_SEMANTIC_CACHE_TTL_SECONDS`, `PS_SEMANTIC_CACHE_MAX_ENTRIES`, `PS_SEMANTIC_CACHE_MAX_MB`, `PS_SEMANTIC_CACHE_MAX_PER_PARTITION`, `PS_SEMANTIC_CACHE_MAX_INPUT_CHARS`, `PS_SEMANTIC_CACHE_MAX_SENSITIVITY`, `PS_SEMANTIC_CACHE_ALLOW_PII`, `PS_SEMANTIC_CACHE_SHARE_PRINCIPALS`, `PS_SEMANTIC_CACHE_LOCAL_DIMS`, `PS_SEMANTIC_CACHE_EMBEDDING_URL`, `PS_SEMANTIC_CACHE_EMBEDDING_MODEL`, `PS_SEMANTIC_CACHE_EMBEDDING_API_KEY` (secret), `PS_SEMANTIC_CACHE_EMBEDDING_TIMEOUT_MS`, `PS_SEMANTIC_CACHE_EMBEDDING_{PROVIDER,JURISDICTION,ZDR,US_CLOUD_ACT_EXPOSURE}` | Semantic cache (`PS_CACHE_DEFAULT=semantic` or `X-PS-Cache: semantic`); off until an embedder is set. See `gateway/docs/semantic-cache.md` |
| `PS_BASELINE_FILE` | Frozen savings baseline (`baseline.v1` JSON, mount it from a ConfigMap with `gateway.extraVolumes` / `extraVolumeMounts`): routed events carry `baseline_id` and `counterfactual_cost`. An invalid file stops the gateway |
| `PS_FEEDBACK_RATE_LIMIT_PER_MINUTE` | `POST /v1/feedback` limit per user and replica (default 60, `0` off) |
| `PS_MAX_CUSTOM_HOST_RESPONSE_BYTES`, `TRUSTED_CUSTOM_HOSTS`, `OAUTH_TOKEN_URL_ALLOWED_HOSTS` | Custom upstream hosts |
| `MAX_JSON_PAYLOAD_SIZE_IN_MB`, `MAX_FILE_PAYLOAD_SIZE_IN_MB` | Request size limits |
| `REDIS_CONNECTION_STRING` (secret) | Shared cache and rate limits across replicas |
| `AWS_AMBIENT_CREDENTIALS_ENABLED` | Allow IRSA / pod identity / instance credentials for Bedrock and SageMaker |
| `AWS_*`, `AZURE_*` identity variables | Cloud identity (usually injected by IRSA / Workload Identity) |
| `OPENAI_AMBIENT_CREDENTIALS_ENABLED`, `OPENAI_IDENTITY_TOKEN_FILE`, `OPENAI_IDENTITY_PROVIDER_ID`, `OPENAI_SERVICE_ACCOUNT_ID`, `OPENAI_SUBJECT_TOKEN_TYPE` | OpenAI workload identity federation for callers that send `openai_auth_type: workload` (mount a projected service-account token with `gateway.extraVolumes`). Default off |
| `ANTHROPIC_AMBIENT_CREDENTIALS_ENABLED`, `ANTHROPIC_IDENTITY_TOKEN_FILE`, `ANTHROPIC_FEDERATION_RULE_ID`, `ANTHROPIC_ORGANIZATION_ID`, `ANTHROPIC_SERVICE_ACCOUNT_ID`, `ANTHROPIC_WORKSPACE_ID` | Anthropic workload identity federation for callers that send `anthropic_auth_type: workload`. Default off |
| `HTTPS_PROXY` | Egress proxy |
| `TLS_KEY_PATH`, `TLS_CERT_PATH`, `TLS_CA_PATH` | Serve TLS from the pod (mount the files) |
| `LOG_LEVEL`, `LOG_TIMESTAMP`, `LOG_COLORS` | Logging |
| `NODE_ENV` (*chart*: `production`) | Also disables the local UI |

### Collector, worker, CronJobs

| Variable | Default | Purpose |
|---|---|---|
| `PS_DATABASE_URL` (secret) | — | Application role URL (migration Job: owner role URL) |
| `PS_PARTNER_JWT_SECRET` (secret) | development value | Partner token signing key |
| `PS_ADMIN_TOKEN` (secret) | empty (off) | Operator endpoints `/api/v1/operator/*` |
| `PS_ADMIN_REQUIRE_SCOPED_TOKENS` | `true` (since chart 0.4.16) | Tenant admin routes (`/api/v1/admin/*`) take only scoped admin tokens and refuse tenant ingest keys. `false` lets ingest keys back onto every admin route but member management. Set it in `collector.config`; see "To 0.4.16: scoped admin tokens required" |
| `PS_RLS_REQUIRED` (*chart*) | `true` in the chart | Refuse to start unless RLS binds the role |
| `PS_COLLECTOR_RUN_WORKERS` (*chart*) | `true`; the chart sets `false` with the worker or the `webhooksDeliver` / `providerUsage` CronJob | Background loops in the API process |
| `PS_COLLECTOR_METRICS_ENABLED` (*chart*) | `false` | Prometheus `/metrics` |
| `PS_COLLECTOR_METRICS_TOKEN` (secret, or *chart* from `collector.metrics.tokenSecret`) | empty (no token) | Bearer token for `/metrics` |
| `PS_READYZ_TIMEOUT_SECONDS` (*chart*) | `2` | `/readyz` database check deadline |
| `PS_COLLECTOR_API_DOCS_ENABLED` (*chart*) | `false` | `/docs`, `/redoc`, `/openapi.json` |
| `PS_RATE_LIMIT_PER_MINUTE` | `100` | Per-tenant ingest limit (per process without Redis; per-tenant override in `settings.ingest_rate_limits`) |
| `PS_KONG_RATE_LIMIT_PER_MINUTE` | `6000` | Per-tenant `/ingest/kong` limit, counted apart |
| `PS_MCP_RATE_LIMIT_PER_MINUTE` | `600` | Per-tenant `/ingest/mcp` limit, counted apart |
| `PS_FEEDBACK_RATE_LIMIT_PER_MINUTE`, `PS_FEEDBACK_USER_RATE_LIMIT_PER_MINUTE` | `600`, `30` | `/ingest/feedback` limits per tenant and per `user_id` |
| `PS_SECURITY_EVENTS_RATE_LIMIT_PER_MINUTE` | `600` | Per-tenant limit on `/ingest/security-events` and `/ingest/otel/v1/logs`, counted apart |
| `PS_COLLECTOR_REDIS_URL` (*config* or secret) | empty (per process) | Redis shared by the partner and ingest rate limiters, e.g. `redis://redis:6379/0` |
| `PS_COLLECTOR_REDIS_TIMEOUT_SECONDS`, `PS_COLLECTOR_REDIS_KEY_PREFIX` | `0.5`, `ps:collector:rl` | Redis call deadline before the per-process fallback; key prefix |
| `PS_TENANT_LIMITS_CACHE_TTL_SECONDS` | `30` | Per-process cache of tenant ingest limit overrides |
| `PS_INGEST_KEY_CACHE_TTL_SECONDS`, `PS_INGEST_KEY_CACHE_MAX_ENTRIES` | `30`, `10000` | Verified tenant key cache (skips bcrypt; `0` TTL turns it off; per process) |
| `PS_AUTH_FAILURE_LIMIT_PER_MINUTE`, `PS_AUTH_FAILURE_TRACKED_CLIENTS` | `60`, `10000` | Failed tenant key / admin token checks per client address per minute before `429` (`0` turns it off; per process) |
| `FORWARDED_ALLOW_IPS` (uvicorn) | `127.0.0.1` | Proxies whose `X-Forwarded-For` sets the client address (failed-auth limiter, audit) |
| `PS_AUDIT_RETENTION_DAYS` | `365` | Partner audit log retention |
| `PS_AUDIT_EVENT_RETENTION_DAYS` | `0` (keep) | Audit trail retention for `python -m collector.audit_trail purge` (leaves a checkpoint row) |
| `PS_RETENTION_DAYS_DEFAULT`, `PS_RETENTION_BATCH_SIZE` | `365`, `5000` | Usage event retention |
| `PS_ASSET_STALE_AFTER_DAYS`, `PS_ASSET_RETIRE_AFTER_DAYS`, `PS_ASSET_LIFECYCLE_SOURCE_THRESHOLDS` | `30`, `90`, `""` | Lifecycle sweep |
| `PS_EMBEDDINGS_BACKEND`, `PS_EMBEDDINGS_MODEL`, `PS_EMBEDDINGS_OPENAI_BASE_URL`, `PS_EMBEDDINGS_CACHE_DIR` | `auto`, —, `https://api.openai.com/v1`, — | Semantic search embeddings |
| `OPENAI_API_KEY` (secret) | — | Embeddings with the `openai` / `auto` backend |
| `PS_WEBHOOK_SECRET_KEYS`, `PS_WEBHOOK_SECRET_KEY` (secret) | — | Keyring for webhook and provider credential secrets |
| `PS_WEBHOOK_TIMEOUT_SECONDS`, `PS_WEBHOOK_MAX_ATTEMPTS`, `PS_WEBHOOK_DELIVERY_RETENTION_DAYS` | `10`, `8`, `30` | Webhook delivery |
| `PS_WEBHOOK_WORKER_INTERVAL_SECONDS` (*chart* on the worker) | `0` | Webhook delivery loop (set by `worker.webhookIntervalSeconds`) |
| `PS_WEBHOOK_ALLOW_HTTP`, `PS_WEBHOOK_ALLOW_PRIVATE_DESTINATIONS` | `false` | Development only |
| `PS_PROVIDER_USAGE_PULL_INTERVAL_SECONDS` (*chart* on the worker), `PS_PROVIDER_USAGE_PULL_DAYS` | `0`, `3` | Provider usage pull loop (set by `worker.providerUsagePullIntervalSeconds`) |
| `PS_USAGE_ROLLUP_INTERVAL_SECONDS`, `PS_USAGE_ROLLUP_RETENTION_DAYS` | `30`, `760` | Usage rollup loop (in the worker set: the worker Deployment, else every API replica; overlapping runs skip a tenant another run holds) and how long hourly/daily usage rollups are kept, never less than raw retention |
| `PS_PROVIDER_ADMIN_KEYS` (secret) | — | Operator-held provider admin keys per tenant (JSON) |
| `PS_LEANIX`, `PS_LEANIX_ALLOWED_HOST_SUFFIXES` | — | LeanIX connector (JSON config is secret) |
| `PS_SERVICENOW`, `PS_SERVICENOW_ALLOWED_HOST_SUFFIXES` | — | ServiceNow connector |
| `PS_SENTINEL_EXPORT` | — | Microsoft Sentinel / Defender XDR export (Logs Ingestion API), per tenant; secret |
| `PS_SIEM_EXPORT` | — | Splunk HEC, syslog (CEF/JSON over TLS) and webhook SIEM export, per tenant; secret |
| `PS_CLOUD_INVENTORY` (secret) | — | Cloud inventory connector |
| `HOME`, `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED` (*chart*) | `/tmp`, `1`, `1` | Read-only root filesystem |

## Development

```shell
helm lint charts/prompt-shields -f charts/prompt-shields/ci/dev-values.yaml
helm unittest charts/prompt-shields          # helm-unittest plugin
helm template ps charts/prompt-shields -f charts/prompt-shields/ci/full-values.yaml \
  | kubeconform -strict -summary \
      -schema-location default \
      -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
```

`ci/*-values.yaml` are the value sets `ct lint` and CI render. CI
(`.github/workflows/helm.yml`) runs these on pull requests that touch
`charts/`, and on a `chart-v<version>` tag (matching `Chart.yaml`'s
`version`) packages the chart, pushes it to
`oci://ghcr.io/prompt-shields/charts` and signs it with cosign (keyless).
