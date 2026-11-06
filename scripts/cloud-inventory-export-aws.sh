#!/usr/bin/env bash
# Export the Amazon Bedrock AI inventory of one AWS account for the Prompt
# Shields offline import (POST /api/v1/admin/cloud-inventory/import, or
# python -m collector.cloud_inventory_import --tenant <uuid> --file <file>).
#
# Usage: scripts/cloud-inventory-export-aws.sh REGION [REGION ...] > export.json
#
# Read-only: it runs only `aws bedrock list-*` calls, plus
# `aws sts get-caller-identity` when AWS_ACCOUNT_ID is unset. It uses the
# AWS CLI credentials already active in your shell; they are never written
# to the output. The IAM permissions it needs are in
# docs/guides/discovery-sources.mdx ("Offline import").
# Needs the AWS CLI v2 and jq.
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: $0 REGION [REGION ...] > export.json" >&2
  exit 2
fi
command -v aws >/dev/null || { echo "aws CLI not found" >&2; exit 2; }
command -v jq >/dev/null || { echo "jq not found" >&2; exit 2; }

account="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
regions='{}'
for region in "$@"; do
  custom="$(aws bedrock list-custom-models --region "$region" --output json)"
  imported="$(aws bedrock list-imported-models --region "$region" --output json)"
  provisioned="$(aws bedrock list-provisioned-model-throughputs --region "$region" --output json)"
  profiles="$(aws bedrock list-inference-profiles --type-equals APPLICATION --region "$region" --output json)"
  arns="$(jq -rn \
    --argjson c "$custom" --argjson i "$imported" \
    --argjson p "$provisioned" --argjson a "$profiles" \
    '[$c.modelSummaries[]?.modelArn, $i.modelSummaries[]?.modelArn,
      $p.provisionedModelSummaries[]?.provisionedModelArn,
      $a.inferenceProfileSummaries[]?.inferenceProfileArn] | .[] | select(. != null)')"
  tags='{}'
  while IFS= read -r arn; do
    [ -n "$arn" ] || continue
    t="$(aws bedrock list-tags-for-resource --resource-arn "$arn" --region "$region" --output json)"
    tags="$(jq -n --argjson all "$tags" --arg arn "$arn" --argjson t "$t" '$all + {($arn): $t}')"
  done <<< "$arns"
  regions="$(jq -n --argjson r "$regions" --arg region "$region" \
    --argjson c "$custom" --argjson i "$imported" \
    --argjson p "$provisioned" --argjson a "$profiles" --argjson t "$tags" \
    '$r + {($region): {custom_models: $c, imported_models: $i,
      provisioned_throughputs: $p, inference_profiles: $a, tags: $t}}')"
done
jq -n --arg account "$account" --argjson regions "$regions" \
  '{cloud: "aws", account_id: $account, regions: $regions}'
