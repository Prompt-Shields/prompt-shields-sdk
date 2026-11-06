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

# Resource group and name from an ARM id (.../resourceGroups/<rg>/.../<name>).
rg_and_name='.[] | select(.id | type == "string") | (.id | split("/")) as $p | [.id, $p[4], $p[-1]] | @tsv'

subs='{}'
for sid in $sub_list; do
  accounts="$(az cognitiveservices account list --subscription "$sid" --output json | jq "$keep_account")"
  deployments='{}'
  while IFS=$'\t' read -r id rg name; do
    [ -n "$id" ] || continue
    d="$(az cognitiveservices account deployment list --subscription "$sid" \
      --resource-group "$rg" --name "$name" --output json </dev/null | jq "$keep_deployment")"
    deployments="$(jq -n --argjson all "$deployments" --arg id "$id" --argjson d "$d" '$all + {($id): $d}')"
  done < <(jq -r '[.[] | select((.kind // "" | ascii_downcase) as $k | $k == "openai" or $k == "aiservices")] | '"$rg_and_name" <<< "$accounts")

  sub="$(jq -n --argjson a "$accounts" --argjson d "$deployments" '{accounts: $a, deployments: $d}')"
  if [ "$skip_ml" != 1 ]; then
    workspaces="$(az resource list --subscription "$sid" \
      --resource-type Microsoft.MachineLearningServices/workspaces --output json | jq "$keep_workspace")"
    ml='{}'
    while IFS=$'\t' read -r id rg ws; do
      [ -n "$id" ] || continue
      serverless="$(az ml serverless-endpoint list --subscription "$sid" \
        --resource-group "$rg" --workspace-name "$ws" --output json </dev/null | jq "$keep_serverless")"
      online="$(az ml online-endpoint list --subscription "$sid" \
        --resource-group "$rg" --workspace-name "$ws" --output json </dev/null | jq "$keep_online")"
      online_deployments='{}'
      while IFS= read -r ep; do
        [ -n "$ep" ] || continue
        d="$(az ml online-deployment list --subscription "$sid" --resource-group "$rg" \
          --workspace-name "$ws" --endpoint-name "$ep" --output json </dev/null | jq "$keep_online_deployment")"
        online_deployments="$(jq -n --argjson all "$online_deployments" --arg ep "$ep" --argjson d "$d" '$all + {($ep): $d}')"
      done < <(jq -r '.[].name // empty' <<< "$online")
      ml="$(jq -n --argjson all "$ml" --arg id "$id" --argjson s "$serverless" \
        --argjson o "$online" --argjson d "$online_deployments" \
        '$all + {($id): {serverless_endpoints: $s, online_endpoints: $o, online_deployments: $d}}')"
    done < <(jq -r '[.[] | select((.kind // "" | ascii_downcase) as $k | $k != "hub" and $k != "featurestore")] | '"$rg_and_name" <<< "$workspaces")
    sub="$(jq -n --argjson s "$sub" --argjson w "$workspaces" --argjson m "$ml" '$s + {ml_workspaces: $w, ml_endpoints: $m}')"
  fi
  subs="$(jq -n --argjson all "$subs" --arg sid "$sid" --argjson s "$sub" '$all + {($sid): $s}')"
done
jq -n --argjson subs "$subs" '{cloud: "azure", subscriptions: $subs}'
