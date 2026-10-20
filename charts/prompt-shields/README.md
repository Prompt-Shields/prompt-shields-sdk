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
| `collector` (required) | `PS_DATABASE_URL` (application role), `PS_PARTNER_JWT_SECRET` (required in practice: without it the collector signs partner tokens with a public development secret), `PS_ADMIN_TOKEN`, `PS_COLLECTOR_METRICS_TOKEN`, `PS_WEBHOOK_SECRET_KEYS` / `PS_WEBHOOK_SECRET_KEY`, `PS_PROVIDER_ADMIN_KEYS`, `PS_LEANIX`, `PS_SERVICENOW`, `PS_CLOUD_INVENTORY`, `OPENAI_API_KEY` |
| `migration` | `PS_DATABASE_URL` (owner role). Falls back to the collector Secret |
| `gateway` (optional) | `PS_API_KEY`, `PS_GATEWAY_ADMIN_TOKEN`, `PS_METRICS_TOKEN`, `REDIS_CONNECTION_STRING`, `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, `PS_OTEL_HEADERS`. Callers send their own provider credentials with each request |

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
- `cronJobs.jobs.*`: one CronJob each, all off by default:

| Job | Command | Default schedule |
|---|---|---|
| `auditPurge` | `python -m collector.audit purge` (partner audit rows older than `PS_AUDIT_RETENTION_DAYS`) | `17 2 * * *` |
| `retentionPurge` | `python -m collector.retention purge` (usage events past retention) | `37 2 * * *` |
| `lifecycleSweep` | `python -m collector.lifecycle` (mark assets stale / retired) | `7 3 * * *` |
| `webhooksDeliver` | `python -m collector.webhooks deliver` (alternative to the worker) | every minute |
| `webhooksPurge` | `python -m collector.webhooks purge` | `47 3 * * *` |
| `providerUsage` | `python -m collector.provider_usage_admin --all-tenants` | `27 4 * * *` |

Add per-tenant connector syncs as extra entries, e.g.
`args: ["python", "-m", "collector.leanix", "--tenant", "<uuid>"]` (also
`collector.servicenow`, `collector.cloud_inventory`). One-off maintenance
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

The partner and ingest rate limits are kept per process, so with N collector
replicas a tenant can reach N times `PS_RATE_LIMIT_PER_MINUTE`. Divide the
limit by the replica count if it must hold globally.

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
| `gateway.collectorUrl` | in-cluster collector | `PS_COLLECTOR_URL` |
| `gateway.metrics.enabled` / `.path` | `false` / `/metrics` | `PS_METRICS_ENABLED` |
| `*.metrics.tokenSecret` / `.tokenEnvName` | `{}` / `PS_METRICS_TOKEN`, `PS_COLLECTOR_METRICS_TOKEN` | Metrics bearer token from a Secret key |
| `gateway.shutdown.timeoutMs` / `.envName` | `25000` / `PS_SHUTDOWN_TIMEOUT_MS` | Drain on SIGTERM |
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
| `cronJobs.jobs.<name>` | all off | `enabled`, `schedule`, `args`, and any default override |
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
| `PS_TELEMETRY_MAX_ATTEMPTS`, `PS_TELEMETRY_RETRY_BASE_MS`, `PS_TELEMETRY_MAX_PENDING`, `PS_TELEMETRY_SHUTDOWN_TIMEOUT_MS` | Telemetry delivery to the collector |
| `PS_OTEL_ENDPOINT`, `PS_OTEL_HEADERS`, `PS_OTEL_CAPTURE_CONTENT`, `PS_OTEL_MAX_BATCH`, `PS_OTEL_FLUSH_MS`, `PS_OTEL_MAX_QUEUE` | OpenTelemetry GenAI spans |
| `PS_SECURITY_EVENTS_LOG`, `PS_SECURITY_EVENTS_TENANT` | Security events as JSON lines on stdout |
| `PS_SECURITY_EVENTS_OTLP`, `PS_SECURITY_EVENTS_OTLP_ENDPOINT`, `PS_SECURITY_EVENTS_OTLP_HEADERS` | Security events as OTLP log records |
| `PS_ROUTER_ENABLED`, `PS_CACHE_DEFAULT`, `PS_CACHE_MAX_ENTRIES` | Router and response cache |
| `PS_MAX_CUSTOM_HOST_RESPONSE_BYTES`, `TRUSTED_CUSTOM_HOSTS`, `OAUTH_TOKEN_URL_ALLOWED_HOSTS` | Custom upstream hosts |
| `MAX_JSON_PAYLOAD_SIZE_IN_MB`, `MAX_FILE_PAYLOAD_SIZE_IN_MB` | Request size limits |
| `REDIS_CONNECTION_STRING` (secret) | Shared cache and rate limits across replicas |
| `AWS_AMBIENT_CREDENTIALS_ENABLED` | Allow IRSA / pod identity / instance credentials for Bedrock and SageMaker |
| `AWS_*`, `AZURE_*` identity variables | Cloud identity (usually injected by IRSA / Workload Identity) |
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
| `PS_RLS_REQUIRED` (*chart*) | `true` in the chart | Refuse to start unless RLS binds the role |
| `PS_COLLECTOR_RUN_WORKERS` (*chart*) | `true`; the chart sets `false` with the worker or the `webhooksDeliver` / `providerUsage` CronJob | Background loops in the API process |
| `PS_COLLECTOR_METRICS_ENABLED` (*chart*) | `false` | Prometheus `/metrics` |
| `PS_COLLECTOR_METRICS_TOKEN` (secret, or *chart* from `collector.metrics.tokenSecret`) | empty (no token) | Bearer token for `/metrics` |
| `PS_READYZ_TIMEOUT_SECONDS` (*chart*) | `2` | `/readyz` database check deadline |
| `PS_COLLECTOR_API_DOCS_ENABLED` (*chart*) | `false` | `/docs`, `/redoc`, `/openapi.json` |
| `PS_RATE_LIMIT_PER_MINUTE` | `100` | Per-tenant ingest limit (per process) |
| `PS_AUDIT_RETENTION_DAYS` | `365` | Partner audit log retention |
| `PS_RETENTION_DAYS_DEFAULT`, `PS_RETENTION_BATCH_SIZE` | `365`, `5000` | Usage event retention |
| `PS_ASSET_STALE_AFTER_DAYS`, `PS_ASSET_RETIRE_AFTER_DAYS`, `PS_ASSET_LIFECYCLE_SOURCE_THRESHOLDS` | `30`, `90`, `""` | Lifecycle sweep |
| `PS_EMBEDDINGS_BACKEND`, `PS_EMBEDDINGS_MODEL`, `PS_EMBEDDINGS_OPENAI_BASE_URL`, `PS_EMBEDDINGS_CACHE_DIR` | `auto`, —, `https://api.openai.com/v1`, — | Semantic search embeddings |
| `OPENAI_API_KEY` (secret) | — | Embeddings with the `openai` / `auto` backend |
| `PS_WEBHOOK_SECRET_KEYS`, `PS_WEBHOOK_SECRET_KEY` (secret) | — | Keyring for webhook and provider credential secrets |
| `PS_WEBHOOK_TIMEOUT_SECONDS`, `PS_WEBHOOK_MAX_ATTEMPTS`, `PS_WEBHOOK_DELIVERY_RETENTION_DAYS` | `10`, `8`, `30` | Webhook delivery |
| `PS_WEBHOOK_WORKER_INTERVAL_SECONDS` (*chart* on the worker) | `0` | Webhook delivery loop (set by `worker.webhookIntervalSeconds`) |
| `PS_WEBHOOK_ALLOW_HTTP`, `PS_WEBHOOK_ALLOW_PRIVATE_DESTINATIONS` | `false` | Development only |
| `PS_PROVIDER_USAGE_PULL_INTERVAL_SECONDS` (*chart* on the worker), `PS_PROVIDER_USAGE_PULL_DAYS` | `0`, `3` | Provider usage pull loop (set by `worker.providerUsagePullIntervalSeconds`) |
| `PS_PROVIDER_ADMIN_KEYS` (secret) | — | Operator-held provider admin keys per tenant (JSON) |
| `PS_LEANIX`, `PS_LEANIX_ALLOWED_HOST_SUFFIXES` | — | LeanIX connector (JSON config is secret) |
| `PS_SERVICENOW`, `PS_SERVICENOW_ALLOWED_HOST_SUFFIXES` | — | ServiceNow connector |
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
