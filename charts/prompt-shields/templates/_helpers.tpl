{{/* Chart name, truncated to the 63-character label limit. */}}
{{- define "ps.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Release fullname. Component names add a suffix, so keep room for it. */}}
{{- define "ps.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "ps.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common labels. Call with (dict "ctx" $ "component" "gateway"). */}}
{{- define "ps.labels" -}}
helm.sh/chart: {{ include "ps.chart" .ctx }}
{{ include "ps.selectorLabels" . }}
app.kubernetes.io/version: {{ .ctx.Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .ctx.Release.Service }}
app.kubernetes.io/part-of: prompt-shields
{{- with .ctx.Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "ps.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ps.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
{{- if .component }}
app.kubernetes.io/component: {{ .component }}
{{- end }}
{{- end -}}

{{- define "ps.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "ps.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Image reference. Call with (dict "ctx" $ "image" .Values.gateway.image).
global.imageRegistry replaces the image's registry; a digest wins over the
tag; the tag defaults to the chart appVersion.
*/}}
{{- define "ps.image" -}}
{{- $registry := default .image.registry .ctx.Values.global.imageRegistry -}}
{{- $repo := required "image.repository is required" .image.repository -}}
{{- $ref := ternary (printf "%s/%s" $registry $repo) $repo (ne (toString $registry) "") -}}
{{- if .image.digest -}}
{{- printf "%s@%s" $ref .image.digest -}}
{{- else -}}
{{- printf "%s:%s" $ref (toString (default .ctx.Chart.AppVersion .image.tag)) -}}
{{- end -}}
{{- end -}}

{{/* The migration image: migration.image merged over the collector's. */}}
{{- define "ps.migrationImage" -}}
{{- $img := mergeOverwrite (deepCopy .Values.collector.image) (default dict .Values.migration.image) -}}
{{- include "ps.image" (dict "ctx" . "image" $img) -}}
{{- end -}}

{{- define "ps.imagePullSecrets" -}}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/*
Name of the Secret a component's pods load with envFrom, or "" for none.
Call with (dict "ctx" $ "component" "collector").
*/}}
{{- define "ps.secretName" -}}
{{- $s := index .ctx.Values.secrets .component -}}
{{- $es := index .ctx.Values.externalSecrets .component -}}
{{- if $s.existingSecret -}}
{{- $s.existingSecret -}}
{{- else if and .ctx.Values.externalSecrets.enabled (or $es.data $es.dataFrom) -}}
{{- printf "%s-%s" (include "ps.fullname" .ctx) .component -}}
{{- else if .ctx.Values.secrets.create -}}
{{- printf "%s-%s" (include "ps.fullname" .ctx) .component -}}
{{- end -}}
{{- end -}}

{{/* The collector Secret, which the collector cannot run without. */}}
{{- define "ps.collectorSecretName" -}}
{{- $name := include "ps.secretName" (dict "ctx" . "component" "collector") -}}
{{- if not $name -}}
{{- fail "The collector needs a Secret with PS_DATABASE_URL and PS_PARTNER_JWT_SECRET: set secrets.collector.existingSecret, externalSecrets (collector.data / dataFrom), or secrets.create with secrets.collector.values (development only)." -}}
{{- end -}}
{{- $name -}}
{{- end -}}

{{/*
Where the migration Job's Secret comes from. The Job is a pre-install hook,
so it runs before the release's ordinary objects exist: any Secret the chart
makes for it (or an ExternalSecret) is a hook object too. Returns one of
  existing     secrets.migration.existingSecret
  eso          a hook ExternalSecret (migration data, else the collector's)
  create       a hook Secret (migration values, else the collector's
               PS_DATABASE_URL)
  collector    the collector's existingSecret (one role for both)
*/}}
{{- define "ps.migrationSecretMode" -}}
{{- $v := .Values -}}
{{- $esm := $v.externalSecrets.migration -}}
{{- $esc := $v.externalSecrets.collector -}}
{{- if $v.secrets.migration.existingSecret -}}existing
{{- else if and $v.externalSecrets.enabled (or $esm.data $esm.dataFrom) -}}eso
{{- else if and $v.externalSecrets.enabled (or $esc.data $esc.dataFrom) (not $v.secrets.collector.existingSecret) -}}eso
{{- else if and $v.secrets.create (or $v.secrets.migration.values (not $v.secrets.collector.existingSecret)) -}}create
{{- else -}}collector
{{- end -}}
{{- end -}}

{{- define "ps.migrationSecretName" -}}
{{- $mode := include "ps.migrationSecretMode" . -}}
{{- if eq $mode "existing" -}}
{{- .Values.secrets.migration.existingSecret -}}
{{- else if eq $mode "collector" -}}
{{- include "ps.collectorSecretName" . -}}
{{- else -}}
{{- printf "%s-migration" (include "ps.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "ps.collectorUrl" -}}
{{- if .Values.gateway.collectorUrl -}}
{{- .Values.gateway.collectorUrl -}}
{{- else if .Values.collector.enabled -}}
{{- printf "http://%s-collector:%v" (include "ps.fullname" .) .Values.collector.service.port -}}
{{- end -}}
{{- end -}}

{{/* Pod-level settings shared by every workload. */}}
{{- define "ps.podSecurityContext" -}}
{{- with .Values.podSecurityContext }}
securityContext:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- define "ps.containerSecurityContext" -}}
{{- with .Values.containerSecurityContext }}
securityContext:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* Collector-image env sources: ConfigMap, then Secret, then extras. */}}
{{- define "ps.collectorEnvFrom" -}}
envFrom:
  - configMapRef:
      name: {{ include "ps.fullname" .ctx }}-collector
  - secretRef:
      name: {{ .secretName }}
{{- with .ctx.Values.collector.extraEnvFrom }}
{{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* Volumes every collector-image pod mounts (read-only root fs). */}}
{{- define "ps.collectorVolumes" -}}
- name: tmp
  emptyDir:
    sizeLimit: {{ .Values.collector.tmpSizeLimit }}
{{- if .Values.collector.embeddingsCache.enabled }}
- name: embeddings-cache
  {{- if .Values.collector.embeddingsCache.existingClaim }}
  persistentVolumeClaim:
    claimName: {{ .Values.collector.embeddingsCache.existingClaim }}
  {{- else }}
  emptyDir:
    sizeLimit: {{ .Values.collector.embeddingsCache.sizeLimit }}
  {{- end }}
{{- end }}
{{- with .Values.collector.extraVolumes }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "ps.collectorVolumeMounts" -}}
- name: tmp
  mountPath: /tmp
{{- if .Values.collector.embeddingsCache.enabled }}
- name: embeddings-cache
  mountPath: {{ .Values.collector.embeddingsCache.mountPath }}
{{- end }}
{{- with .Values.collector.extraVolumeMounts }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* Probe. Call with (dict "probe" .Values.x.probes.liveness "port" "http"). */}}
{{- define "ps.httpProbe" -}}
httpGet:
  path: {{ .probe.path }}
  port: {{ .port }}
periodSeconds: {{ .probe.periodSeconds }}
failureThreshold: {{ .probe.failureThreshold }}
timeoutSeconds: {{ .probe.timeoutSeconds }}
{{- with .probe.initialDelaySeconds }}
initialDelaySeconds: {{ . }}
{{- end }}
{{- end -}}

{{/* preStop sleep hook, or nothing when seconds is 0. */}}
{{- define "ps.preStop" -}}
{{- if gt (int .) 0 }}
lifecycle:
  preStop:
    exec:
      command: ["sleep", "{{ . }}"]
{{- end }}
{{- end -}}

{{/* Validation run once from NOTES / config. */}}
{{- define "ps.validate" -}}
{{- if and .Values.secrets.create .Values.collector.enabled -}}
{{- $v := .Values.secrets.collector.values -}}
{{- if and (not .Values.secrets.collector.existingSecret) (not (hasKey $v "PS_DATABASE_URL")) -}}
{{- fail "secrets.create is true but secrets.collector.values.PS_DATABASE_URL is not set" -}}
{{- end -}}
{{- if and (not .Values.secrets.collector.existingSecret) (not (hasKey $v "PS_PARTNER_JWT_SECRET")) -}}
{{- fail "secrets.create is true but secrets.collector.values.PS_PARTNER_JWT_SECRET is not set (the collector would fall back to a public development secret)" -}}
{{- end -}}
{{- end -}}
{{- if and .Values.externalSecrets.enabled (not .Values.externalSecrets.secretStoreRef.name) -}}
{{- fail "externalSecrets.enabled needs externalSecrets.secretStoreRef.name" -}}
{{- end -}}
{{- $w := .Values.worker -}}
{{- if $w.enabled -}}
{{- $hook := float64 (default 0 $w.webhookIntervalSeconds) -}}
{{- $pull := float64 (default 0 $w.providerUsagePullIntervalSeconds) -}}
{{- if and (le $hook 0.0) (le $pull 0.0) -}}
{{- fail "worker.enabled needs worker.webhookIntervalSeconds or worker.providerUsagePullIntervalSeconds above 0 (python -m collector.workers exits when no loop is on)" -}}
{{- end -}}
{{- if and (gt $pull 0.0) (lt $pull 3600.0) -}}
{{- fail "worker.providerUsagePullIntervalSeconds must be 0 (off) or at least 3600" -}}
{{- end -}}
{{- if and (gt $hook 0.0) .Values.cronJobs.jobs.webhooksDeliver.enabled -}}
{{- fail "Webhook delivery is enabled in both the worker (webhookIntervalSeconds) and cronJobs.jobs.webhooksDeliver; enable one" -}}
{{- end -}}
{{- if and (gt $pull 0.0) .Values.cronJobs.jobs.providerUsage.enabled -}}
{{- fail "The provider usage pull is enabled in both the worker (providerUsagePullIntervalSeconds) and cronJobs.jobs.providerUsage; enable one" -}}
{{- end -}}
{{- end -}}
{{- $c := .Values.collector -}}
{{- if and $c.enabled $c.probes.readiness.enabled (le (float64 $c.probes.readiness.timeoutSeconds) (float64 $c.readyzTimeoutSeconds)) -}}
{{- fail "collector.probes.readiness.timeoutSeconds must be above collector.readyzTimeoutSeconds" -}}
{{- end -}}
{{- end -}}

{{/* "true" when any CronJob is enabled. */}}
{{- define "ps.anyCronJob" -}}
{{- range $name, $job := .Values.cronJobs.jobs -}}
{{- if and $job (kindIs "map" $job) $job.enabled -}}true{{- end -}}
{{- end -}}
{{- end -}}

{{/*
PS_COLLECTOR_RUN_WORKERS for the API pods: an explicit value wins; "" means
"false" when the worker Deployment or a CronJob does background work, and
unset otherwise (the image default, true).
*/}}
{{- define "ps.runWorkersValue" -}}
{{- $v := toString .Values.collector.runWorkers.value -}}
{{- if $v -}}
{{- $v -}}
{{- else if or .Values.worker.enabled (include "ps.anyCronJob" .) -}}
false
{{- end -}}
{{- end -}}

{{/*
The Secret key ({name, key}) a ServiceMonitor sends as the bearer token, as
YAML, or "". Call with (dict "ctx" $ "component" "gateway").
*/}}
{{- define "ps.metricsTokenRef" -}}
{{- $c := index .ctx.Values .component -}}
{{- $s := index .ctx.Values.secrets .component -}}
{{- if $c.serviceMonitor.bearerTokenSecret -}}
{{- toYaml $c.serviceMonitor.bearerTokenSecret -}}
{{- else if $c.metrics.tokenSecret -}}
{{- toYaml $c.metrics.tokenSecret -}}
{{- else if and .ctx.Values.secrets.create (not $s.existingSecret) (hasKey $s.values $c.metrics.tokenEnvName) -}}
{{- toYaml (dict "name" (printf "%s-%s" (include "ps.fullname" .ctx) .component) "key" $c.metrics.tokenEnvName) -}}
{{- end -}}
{{- end -}}

{{/* env entry from metrics.tokenSecret, or nothing. */}}
{{- define "ps.metricsTokenEnv" -}}
{{- $c := index .ctx.Values .component -}}
{{- with $c.metrics.tokenSecret }}
- name: {{ $c.metrics.tokenEnvName }}
  valueFrom:
    secretKeyRef:
      name: {{ required (printf "%s.metrics.tokenSecret.name is required" $.component) .name }}
      key: {{ required (printf "%s.metrics.tokenSecret.key is required" $.component) .key }}
{{- end }}
{{- end -}}

{{/* Egress rules every pod gets when networkPolicy.enabled. */}}
{{- define "ps.npCommonEgress" -}}
{{- $egress := .Values.networkPolicy.egress -}}
{{- if $egress.dns }}
- ports:
    - port: 53
      protocol: UDP
    - port: 53
      protocol: TCP
{{- end }}
- ports:
    - port: {{ $egress.database.port }}
      protocol: TCP
  {{- with $egress.database.cidrs }}
  to:
    {{- range . }}
    - ipBlock:
        cidr: {{ . }}
    {{- end }}
  {{- end }}
{{- if $egress.allowExternal }}
- {}
{{- end }}
{{- with $egress.extra }}
{{ toYaml . }}
{{- end }}
{{- end }}
