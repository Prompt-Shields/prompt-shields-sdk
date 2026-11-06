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
# Listings go through files, not command-line arguments: Linux caps one
# argument at 128 KiB, which a large account's listings exceed.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
n=0
for region in "$@"; do
  n=$((n + 1))
  d="$tmp/r$n"
  mkdir "$d"
  aws bedrock list-custom-models --region "$region" --output json > "$d/c.json"
  aws bedrock list-imported-models --region "$region" --output json > "$d/i.json"
  aws bedrock list-provisioned-model-throughputs --region "$region" --output json > "$d/p.json"
  aws bedrock list-inference-profiles --type-equals APPLICATION --region "$region" --output json > "$d/a.json"
  jq -r '.modelSummaries[]?.modelArn // empty' "$d/c.json" "$d/i.json" > "$d/arns"
  jq -r '.provisionedModelSummaries[]?.provisionedModelArn // empty' "$d/p.json" >> "$d/arns"
  jq -r '.inferenceProfileSummaries[]?.inferenceProfileArn // empty' "$d/a.json" >> "$d/arns"
  echo '{}' > "$d/tags.json"
  # fd 3, so the aws CLI cannot read the ARN list from stdin.
  while IFS= read -r arn <&3; do
    [ -n "$arn" ] || continue
    aws bedrock list-tags-for-resource --resource-arn "$arn" --region "$region" --output json < /dev/null > "$d/t.json"
    jq --arg arn "$arn" --slurpfile t "$d/t.json" '. + {($arn): $t[0]}' "$d/tags.json" > "$d/tags.new"
    mv "$d/tags.new" "$d/tags.json"
  done 3< "$d/arns"
  jq -n --arg region "$region" \
    --slurpfile c "$d/c.json" --slurpfile i "$d/i.json" \
    --slurpfile p "$d/p.json" --slurpfile a "$d/a.json" --slurpfile t "$d/tags.json" \
    '{($region): {custom_models: $c[0], imported_models: $i[0],
      provisioned_throughputs: $p[0], inference_profiles: $a[0], tags: $t[0]}}' \
    > "$d/region.json"
done
jq -n --arg account "$account" \
  '{cloud: "aws", account_id: $account, regions: ([inputs] | add)}' \
  "$tmp"/r*/region.json
