#!/usr/bin/env bash
# Export the Vertex AI inventory (models deployed to endpoints, and the Model
# Registry entries that name them) of Google Cloud projects for the Prompt
# Shields offline import (POST /api/v1/admin/cloud-inventory/import, or
# python -m collector.cloud_inventory_import --tenant <uuid> --file <file>).
#
# Usage: scripts/cloud-inventory-export-gcp.sh PROJECT[,PROJECT...] REGION [REGION ...] > export.json
#
# Read-only: it runs only `gcloud ai endpoints list` and `gcloud ai models
# list`. It uses the gcloud login already active in your shell
# (`gcloud auth login`); no token is written to the output, and only the
# fields the import reads are kept. roles/aiplatform.viewer on each project
# is enough; see docs/guides/discovery-sources.mdx ("Offline import").
# gcloud has no command that lists tuning jobs, so tuned models that are not
# deployed are not exported (the live pull finds them). Needs the gcloud CLI
# and jq.
set -euo pipefail
set -f  # no globbing of project or region names

if [ "$#" -lt 2 ]; then
  echo "usage: $0 PROJECT[,PROJECT...] REGION [REGION ...] > export.json" >&2
  exit 2
fi
command -v gcloud >/dev/null || { echo "gcloud CLI not found" >&2; exit 2; }
command -v jq >/dev/null || { echo "jq not found" >&2; exit 2; }

project_re='^[a-z][a-z0-9-]{4,28}[a-z0-9]$'
region_re='^[a-z]+-[a-z]+[1-9][0-9]?$'
projects="${1//,/ }"
shift
for project in $projects; do
  [[ "$project" =~ $project_re ]] || { echo "not a project id: $project" >&2; exit 2; }
done
for region in "$@"; do
  [[ "$region" =~ $region_re ]] || { echo "not a Vertex AI region: $region" >&2; exit 2; }
done

# Keep only what the import reads, so nothing else (container images,
# service accounts, network settings) leaves the shell.
keep_endpoint='[.[] | {name, displayName, labels, deployedModels: [(.deployedModels // [])[] | {id, model, displayName}]}]'
keep_model='[.[] | {name, displayName, labels, baseModelSource}]'

# Listings can be large: they go through files, never through argv (a
# single argument is limited to 128 KiB on Linux).
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
# merge FILE KEY VALUE_FILE: add {KEY: <contents of VALUE_FILE>} to FILE.
merge() {
  jq --arg k "$2" --slurpfile v "$3" '. + {($k): $v[0]}' "$1" > "$1.new"
  mv "$1.new" "$1"
}

echo '{}' > "$tmp/projects.json"
for project in $projects; do
  echo '{}' > "$tmp/regions.json"
  for region in "$@"; do
    gcloud ai endpoints list --project="$project" --region="$region" \
      --format=json --quiet | jq "$keep_endpoint" > "$tmp/endpoints.json"
    gcloud ai models list --project="$project" --region="$region" \
      --format=json --quiet | jq "$keep_model" > "$tmp/models.json"
    jq -n --slurpfile e "$tmp/endpoints.json" --slurpfile m "$tmp/models.json" \
      '{endpoints: $e[0], models: $m[0]}' > "$tmp/region.json"
    merge "$tmp/regions.json" "$region" "$tmp/region.json"
  done
  merge "$tmp/projects.json" "$project" "$tmp/regions.json"
done
jq '{cloud: "gcp", projects: .}' "$tmp/projects.json"
