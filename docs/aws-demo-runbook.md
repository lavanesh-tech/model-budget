# AWS Recruiter Demo Runbook

## Prerequisites

- AWS CLI authenticated with profile `modelbudget`
- Terraform installed
- Node.js and curl installed locally
- Existing Terraform state in `infrastructure/aws`
- The four `/modelbudget/prod/` SSM SecureString parameters

Never print or paste decrypted parameter values into chat, screenshots, shell history, or documentation.

## Start the demo

From the repository root:

```bash
./scripts/aws_demo_start.sh
```

The script starts EC2, waits for AWS and SSM health, verifies the local application, updates CloudFront to the instance's new public DNS name, waits for deployment, verifies public HTTPS health, and refreshes Terraform state without changing resources.

Retrieve the dashboard password directly to the macOS clipboard:

```bash
AWS_PROFILE=modelbudget aws ssm get-parameter \
  --region us-east-1 \
  --name /modelbudget/prod/dashboard-login-password \
  --with-decryption \
  --query 'Parameter.Value' \
  --output text | tr -d '\n' | pbcopy
```

Clear the clipboard after signing in:

```bash
printf '' | pbcopy
```

## Suggested five-minute demonstration

1. Show the GitHub Actions checks and repository structure.
2. Open the CloudFront HTTPS URL and sign in.
3. Explain Teams, Usage, and Prompts as sanitized operator views.
4. Trace one request through CloudFront, Caddy, Next.js/FastAPI, Redis, PostgreSQL, and the provider adapter.
5. Show Terraform, SSM secrets, IMDSv2, the CloudFront-only security group, and the absence of SSH ingress.
6. Explain why the mock provider is the default and identify the production scaling trade-offs.

## Stop the demo

```bash
./scripts/aws_demo_stop.sh
```

The CloudFront URL will return an origin error while EC2 is intentionally stopped. The encrypted EBS volume and database remain preserved.

## Cost behavior

- Running EC2 and its public IPv4 accrue hourly charges.
- Stopping EC2 removes compute and running public-IPv4 charges.
- EBS storage continues to incur a small monthly charge while stopped.
- CloudFront usage is request-based.
- AWS Budgets sends alerts but does not enforce a hard spending limit.

## Troubleshooting

If startup fails, do not run a full Terraform apply immediately. Check, in order:

1. `aws sts get-caller-identity --profile modelbudget`
2. EC2 state and status checks
3. SSM `PingStatus`
4. `docker compose ps` through SSM
5. local `http://127.0.0.1/health` through SSM
6. CloudFront distribution status and origin DNS

A normal EC2 stop/start changes the public DNS name. The start script safely updates the existing CloudFront distribution to that new origin.
