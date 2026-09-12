#!/usr/bin/env bash
set -Eeuo pipefail

AWS_PROFILE_NAME="${AWS_PROFILE_NAME:-modelbudget}"
AWS_REGION_NAME="${AWS_REGION_NAME:-us-east-1}"
REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TERRAFORM_DIRECTORY="$REPOSITORY_ROOT/infrastructure/aws"
TEMP_DIRECTORY="$(mktemp -d /tmp/modelbudget-start.XXXXXX)"
trap 'rm -rf -- "$TEMP_DIRECTORY"' EXIT

for command_name in aws terraform node curl; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Missing required command: $command_name" >&2
    exit 1
  }
done

aws sts get-caller-identity --profile "$AWS_PROFILE_NAME" >/dev/null

instance_id="$(terraform -chdir="$TERRAFORM_DIRECTORY" output -raw instance_id)"
distribution_id="$(terraform -chdir="$TERRAFORM_DIRECTORY" output -raw cloudfront_distribution_id)"
application_url="$(terraform -chdir="$TERRAFORM_DIRECTORY" output -raw application_url)"

echo "Starting $instance_id..."
aws ec2 start-instances \
  --profile "$AWS_PROFILE_NAME" \
  --region "$AWS_REGION_NAME" \
  --instance-ids "$instance_id" >/dev/null

aws ec2 wait instance-status-ok \
  --profile "$AWS_PROFILE_NAME" \
  --region "$AWS_REGION_NAME" \
  --instance-ids "$instance_id"

for attempt in $(seq 1 60); do
  ping_status="$(
    aws ssm describe-instance-information \
      --profile "$AWS_PROFILE_NAME" \
      --region "$AWS_REGION_NAME" \
      --filters "Key=InstanceIds,Values=$instance_id" \
      --query 'InstanceInformationList[0].PingStatus' \
      --output text 2>/dev/null || true
  )"
  [[ "$ping_status" == "Online" ]] && break
  [[ "$attempt" == "60" ]] && { echo "SSM did not become online." >&2; exit 1; }
  sleep 5
done

command_id="$(
  aws ssm send-command \
    --profile "$AWS_PROFILE_NAME" \
    --region "$AWS_REGION_NAME" \
    --document-name AWS-RunShellScript \
    --instance-ids "$instance_id" \
    --parameters '{"commands":["for ATTEMPT in $(seq 1 60); do if curl --fail --silent http://127.0.0.1/health >/dev/null; then echo LOCAL_HEALTH=ok; exit 0; fi; sleep 5; done; echo Local health failed; exit 1"],"executionTimeout":["360"]}' \
    --query 'Command.CommandId' \
    --output text
)"

for attempt in $(seq 1 60); do
  command_status="$(
    aws ssm get-command-invocation \
      --profile "$AWS_PROFILE_NAME" \
      --region "$AWS_REGION_NAME" \
      --command-id "$command_id" \
      --instance-id "$instance_id" \
      --query 'Status' \
      --output text 2>/dev/null || true
  )"

  case "$command_status" in
    Success)
      break
      ;;
    Failed|Cancelled|TimedOut)
      aws ssm get-command-invocation \
        --profile "$AWS_PROFILE_NAME" \
        --region "$AWS_REGION_NAME" \
        --command-id "$command_id" \
        --instance-id "$instance_id" \
        --query '{Status:Status,Output:StandardOutputContent,Errors:StandardErrorContent}' \
        --output json
      echo "Remote application health check failed." >&2
      exit 1
      ;;
  esac

  [[ "$attempt" == "60" ]] && {
    echo "Timed out waiting for the remote application health check." >&2
    exit 1
  }
  sleep 10
done

new_origin="$(
  aws ec2 describe-instances \
    --profile "$AWS_PROFILE_NAME" \
    --region "$AWS_REGION_NAME" \
    --instance-ids "$instance_id" \
    --query 'Reservations[0].Instances[0].PublicDnsName' \
    --output text
)"

[[ "$new_origin" =~ ^ec2-[a-z0-9-]+\.compute-1\.amazonaws\.com$ ]] || {
  echo "Unexpected EC2 public DNS name: $new_origin" >&2
  exit 1
}

aws cloudfront get-distribution-config \
  --profile "$AWS_PROFILE_NAME" \
  --id "$distribution_id" \
  --output json > "$TEMP_DIRECTORY/current.json"

etag="$(
  CURRENT_CONFIG="$TEMP_DIRECTORY/current.json" node -e \
    'const fs=require("node:fs");const x=JSON.parse(fs.readFileSync(process.env.CURRENT_CONFIG,"utf8"));process.stdout.write(x.ETag)'
)"

CURRENT_CONFIG="$TEMP_DIRECTORY/current.json" \
UPDATED_CONFIG="$TEMP_DIRECTORY/updated.json" \
NEW_ORIGIN="$new_origin" node <<'JS'
const fs = require("node:fs");
const input = JSON.parse(fs.readFileSync(process.env.CURRENT_CONFIG, "utf8"));
const config = input.DistributionConfig;
const origins = config?.Origins?.Items;
if (!Array.isArray(origins) || origins.length !== 1 || origins[0].Id !== "modelbudget-ec2") {
  throw new Error("Expected exactly one ModelBudget CloudFront origin.");
}
origins[0].DomainName = process.env.NEW_ORIGIN;
fs.writeFileSync(process.env.UPDATED_CONFIG, JSON.stringify(config));
JS

aws cloudfront update-distribution \
  --profile "$AWS_PROFILE_NAME" \
  --id "$distribution_id" \
  --if-match "$etag" \
  --distribution-config "file://$TEMP_DIRECTORY/updated.json" >/dev/null

echo "Waiting for CloudFront..."
aws cloudfront wait distribution-deployed \
  --profile "$AWS_PROFILE_NAME" \
  --id "$distribution_id"

for attempt in $(seq 1 30); do
  if curl --fail --silent --show-error "$application_url/health" >/dev/null; then
    terraform -chdir="$TERRAFORM_DIRECTORY" apply -refresh-only -input=false -auto-approve >/dev/null
    echo "ModelBudget is ready: $application_url"
    exit 0
  fi
  [[ "$attempt" == "30" ]] && { echo "Public health check failed." >&2; exit 1; }
  sleep 10
done
