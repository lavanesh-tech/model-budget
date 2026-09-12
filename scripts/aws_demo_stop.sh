#!/usr/bin/env bash
set -Eeuo pipefail

AWS_PROFILE_NAME="${AWS_PROFILE_NAME:-modelbudget}"
AWS_REGION_NAME="${AWS_REGION_NAME:-us-east-1}"
REPOSITORY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TERRAFORM_DIRECTORY="$REPOSITORY_ROOT/infrastructure/aws"

for command_name in aws terraform; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Missing required command: $command_name" >&2
    exit 1
  }
done

aws sts get-caller-identity --profile "$AWS_PROFILE_NAME" >/dev/null
instance_id="$(terraform -chdir="$TERRAFORM_DIRECTORY" output -raw instance_id)"

state="$(
  aws ec2 describe-instances \
    --profile "$AWS_PROFILE_NAME" \
    --region "$AWS_REGION_NAME" \
    --instance-ids "$instance_id" \
    --query 'Reservations[0].Instances[0].State.Name' \
    --output text
)"

if [[ "$state" == "stopped" ]]; then
  echo "ModelBudget EC2 instance is already stopped."
  exit 0
fi

[[ "$state" == "running" ]] || {
  echo "Refusing to stop instance while state is $state. Try again after the transition completes." >&2
  exit 1
}

echo "Stopping $instance_id..."
aws ec2 stop-instances \
  --profile "$AWS_PROFILE_NAME" \
  --region "$AWS_REGION_NAME" \
  --instance-ids "$instance_id" >/dev/null

aws ec2 wait instance-stopped \
  --profile "$AWS_PROFILE_NAME" \
  --region "$AWS_REGION_NAME" \
  --instance-ids "$instance_id"

echo "ModelBudget is stopped. EBS data remains preserved."
