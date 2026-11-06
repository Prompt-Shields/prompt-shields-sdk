#!/usr/bin/env bash
# Export the Azure AI inventory (Azure OpenAI / AI Foundry accounts and their
# model deployments, and Azure Machine Learning / hub-based AI Foundry project
# endpoints) for the Prompt Shields offline import
# (POST /api/v1/admin/cloud-inventory/import, or
# python -m collector.cloud_inventory_import --tenant <uuid> --file <file>).
#
# Usage: scripts/cloud-inventory-export-azure.sh [SUBSCRIPTION_ID ...] > export.json
#   No subscription ids: every enabled subscription your login can see.
#   PS_EXPORT_SKIP_ML=1 leaves out Azure Machine Learning (then the `ml`
#   extension is not needed).
#
# Read-only: it runs only `az ... list` commands (account, cognitiveservices
# account, cognitiveservices account deployment, resource, extension, and
# ml serverless-endpoint / online-endpoint / online-deployment). It never
# lists keys or endpoint credentials. It uses the Azure CLI login already
# active in your shell (`az login`); no token is written to the output, and
# only the fields the import reads are kept. The Reader role on each
# subscription is enough; see docs/guides/discovery-sources.mdx
# ("Offline import"). Needs the Azure CLI, its `ml` extension (unless
# PS_EXPORT_SKIP_ML=1) and jq.
set -euo pipefail
set -f  # no globbing of ids read from the CLI

command -v az >/dev/null || { echo "az CLI not found" >&2; exit 2; }
command -v jq >/dev/null || { echo "jq not found" >&2; exit 2; }

guid='^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$'
skip_ml="${PS_EXPORT_SKIP_ML:-0}"

if [ "$#" -gt 0 ]; then
  sub_list="$(printf '%s\n' "$@")"
else
  sub_list="$(az account list --query "[?state=='Enabled'].id" --output tsv | tr -d '\r')"
fi
[ -n "$sub_list" ] || { echo "no subscriptions" >&2; exit 2; }
for sid in $sub_list; do
  [[ "$sid" =~ $guid ]] || { echo "not a subscription id: $sid" >&2; exit 2; }
done

if [ "$skip_ml" != 1 ] && ! az extension list --output json | jq -e 'any(.[]; .name == "ml")' >/dev/null; then
  echo "the Azure CLI ml extension is missing: az extension add --name ml (or PS_EXPORT_SKIP_ML=1)" >&2
  exit 2
fi

# Keep only what the import reads, so nothing else (endpoint URLs,
# environment variables, identities) leaves the shell.
keep_account='[.[] | {id, name, kind, location, tags}]'
keep_deployment='[.[] | {id, name, tags, properties: {provisioningState: .properties.provisioningState, model: {name: .properties.model.name, format: .properties.model.format}}}]'
keep_workspace='[.[] | {id, name, kind, location, tags}]'
keep_serverless='[.[] | {id, name, location, tags, model_id, provisioning_state, endpoint_state}]'
keep_online='[.[] | {id, name, location, tags, provisioning_state}]'
keep_online_deployment='[.[] | {id, name, location, tags, model, provisioning_state}]'

# Resource group and name from an ARM id
# (/subscriptions/<sid>/resourceGroups/<rg>/providers/<ns>/<type>/<name>);
# other ids are skipped. Names go to `az` as single quoted arguments.
arm_id='^/subscriptions/[0-9a-fA-F-]{36}/resourceGroups/[^/[:cntrl:]]{1,90}/providers/[A-Za-z0-9.]+/[A-Za-z0-9]+/[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$'
rg_and_name='.[] | select(.id | type == "string" and test($arm_id)) | (.id | split("/")) as $p | [.id, $p[4], $p[-1]] | @tsv'
arm_name='^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$'

# Listings can be large: they go through files, never through argv (a
# single argument is limited to 128 KiB on Linux).
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
# merge FILE KEY VALUE_FILE: add {KEY: <contents of VALUE_FILE>} to FILE.
merge() {
  jq --arg k "$2" --slurpfile v "$3" '. + {($k): $v[0]}' "$1" > "$1.new"
  mv "$1.new" "$1"
}

echo '{}' > "$tmp/subs.json"
for sid in $sub_list; do
  az cognitiveservices account list --subscription "$sid" --output json \
    | jq "$keep_account" > "$tmp/accounts.json"
  echo '{}' > "$tmp/deployments.json"
  while IFS=$'\t' read -r id rg name; do
    [ -n "$id" ] || continue
    az cognitiveservices account deployment list --subscription "$sid" \
      --resource-group "$rg" --name "$name" --output json </dev/null \
      | jq "$keep_deployment" > "$tmp/d.json"
    merge "$tmp/deployments.json" "$id" "$tmp/d.json"
  done < <(jq -r --arg arm_id "$arm_id" '[.[] | select((.kind // "" | ascii_downcase) as $k | $k == "openai" or $k == "aiservices")] | '"$rg_and_name" "$tmp/accounts.json")

  jq -n --slurpfile a "$tmp/accounts.json" --slurpfile d "$tmp/deployments.json" \
    '{accounts: $a[0], deployments: $d[0]}' > "$tmp/sub.json"
  if [ "$skip_ml" != 1 ]; then
    az resource list --subscription "$sid" \
      --resource-type Microsoft.MachineLearningServices/workspaces --output json \
      | jq "$keep_workspace" > "$tmp/workspaces.json"
    echo '{}' > "$tmp/ml.json"
    while IFS=$'\t' read -r id rg ws; do
      [ -n "$id" ] || continue
      az ml serverless-endpoint list --subscription "$sid" \
        --resource-group "$rg" --workspace-name "$ws" --output json </dev/null \
        | jq "$keep_serverless" > "$tmp/serverless.json"
      az ml online-endpoint list --subscription "$sid" \
        --resource-group "$rg" --workspace-name "$ws" --output json </dev/null \
        | jq "$keep_online" > "$tmp/online.json"
      echo '{}' > "$tmp/online_deployments.json"
      while IFS= read -r ep; do
        [ -n "$ep" ] || continue
        az ml online-deployment list --subscription "$sid" --resource-group "$rg" \
          --workspace-name "$ws" --endpoint-name "$ep" --output json </dev/null \
          | jq "$keep_online_deployment" > "$tmp/d.json"
        merge "$tmp/online_deployments.json" "$ep" "$tmp/d.json"
      done < <(jq -r --arg n "$arm_name" '.[].name | select(type == "string" and test($n))' "$tmp/online.json")
      jq -n --slurpfile s "$tmp/serverless.json" --slurpfile o "$tmp/online.json" \
        --slurpfile d "$tmp/online_deployments.json" \
        '{serverless_endpoints: $s[0], online_endpoints: $o[0], online_deployments: $d[0]}' \
        > "$tmp/w.json"
      merge "$tmp/ml.json" "$id" "$tmp/w.json"
    done < <(jq -r --arg arm_id "$arm_id" '[.[] | select((.kind // "" | ascii_downcase) as $k | $k != "hub" and $k != "featurestore")] | '"$rg_and_name" "$tmp/workspaces.json")
    jq --slurpfile w "$tmp/workspaces.json" --slurpfile m "$tmp/ml.json" \
      '. + {ml_workspaces: $w[0], ml_endpoints: $m[0]}' "$tmp/sub.json" > "$tmp/sub.new"
    mv "$tmp/sub.new" "$tmp/sub.json"
  fi
  merge "$tmp/subs.json" "$sid" "$tmp/sub.json"
done
jq '{cloud: "azure", subscriptions: .}' "$tmp/subs.json"
