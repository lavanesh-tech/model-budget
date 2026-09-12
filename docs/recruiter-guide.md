# ModelBudget Recruiter Guide

## Thirty-second explanation

ModelBudget is an LLM governance gateway I built to control how teams use AI providers. It exposes an OpenAI-compatible API, enforces team budgets and rate limits, governs prompt versions, isolates provider failures with a circuit breaker, and records privacy-safe cost and usage data. I built the backend with FastAPI, PostgreSQL, and Redis, an operator dashboard with Next.js, CI with GitHub Actions, and an on-demand AWS deployment with Terraform, EC2, CloudFront, SSM, Docker, and Caddy.

## Two-minute explanation

The main problem is that direct access to an LLM provider makes cost, reliability, auditability, and prompt governance inconsistent across teams. ModelBudget places one controlled gateway in front of providers. A request is authenticated, validated, checked against the team's active state, budget, and rate limit, then routed through a provider abstraction. Redis coordinates fast rate-limit and circuit-breaker state; PostgreSQL stores durable team, prompt-version, and usage records. The response remains OpenAI-compatible so clients require minimal changes.

The admin dashboard is deliberately read-only. Next.js owns the browser session and proxies only allowlisted resources to FastAPI using a server-side key. Sessions are signed and HttpOnly, login is same-origin protected, comparisons are constant-time, and admin responses exclude prompts, response snapshots, hashes, and internal IDs.

For deployment, Terraform creates a small AWS environment. CloudFront provides HTTPS, the EC2 origin only accepts CloudFront traffic, secrets come from SSM SecureStrings through an IAM role, IMDSv2 is required, and SSM replaces SSH. GitHub Actions verifies the backend, migrations, dashboard, and production images. I tested container restarts and an EC2 stop/start while preserving PostgreSQL state.

## Strong resume bullets

- Built an OpenAI-compatible LLM governance gateway using FastAPI, PostgreSQL, and Redis, implementing team budgets, usage accounting, rate limiting, prompt versioning, and provider circuit breaking.
- Developed a hardened Next.js/TypeScript operator dashboard with signed HttpOnly sessions, same-origin login protection, server-side credential isolation, and sanitized read-only administrative APIs.
- Added privacy-safe Prometheus/OpenTelemetry observability and GitHub Actions CI covering backend checks, Alembic migrations, dashboard verification, and production container builds.
- Provisioned a restartable AWS environment with Terraform, CloudFront HTTPS, EC2, encrypted EBS, SSM SecureStrings, least-privilege IAM, IMDSv2, CloudFront-restricted ingress, and no SSH exposure.

Only include technologies and behavior you can explain and demonstrate. Do not claim user scale, cost savings, latency improvements, or production customers without measured evidence.

## Likely questions and answer points

### Why did you build it?

Direct provider access scatters API keys and makes budgets, prompts, cost accounting, and failure handling inconsistent. A gateway centralizes those policies while preserving a familiar client interface.

### Why FastAPI?

It offers typed validation, dependency injection, async I/O support, and automatic OpenAPI generation. The project still keeps business rules in services instead of route handlers so the framework is not the architecture.

### Why PostgreSQL and Redis?

PostgreSQL holds durable relational and auditable state. Redis handles fast, shared, time-sensitive coordination such as rate limits and circuit state. They solve different consistency and latency needs.

### How is spending controlled?

Before provider execution, the gateway checks the team's configured budget. After a request, token usage and price information update durable usage records. Concurrent production systems would additionally need carefully designed atomic reservations or transactions to prevent simultaneous requests from overspending a remaining budget.

### How does the circuit breaker help?

Repeated provider failures open the circuit and temporarily reject or redirect work rather than continuing to overload an unhealthy dependency. After a recovery interval, controlled probes determine whether normal traffic can resume.

### How are prompts governed?

Prompts are versioned and move through explicit lifecycle states. Requests use approved versions, which makes changes reviewable and avoids silently changing behavior for every client.

### What security controls matter most?

Server-side-only secrets, constant-time credential checks, signed HttpOnly cookies, strict same-origin validation, bounded login bodies, no-store responses, allowlisted proxy resources, sanitized admin payloads, SSM SecureStrings, least-privilege IAM, IMDSv2, CloudFront-only ingress, and no SSH port.

### Why is the dashboard read-only?

Its purpose is operational visibility. Removing mutation reduces blast radius and prevents a compromised browser session from changing budgets, prompts, or credentials.

### What happens when EC2 restarts?

Docker starts at boot and containers use restart policies. PostgreSQL data remains on encrypted EBS. EC2 receives a new public address, so the demo start script updates CloudFront to the new origin, waits for deployment, verifies health, and refreshes Terraform state.

### Why not Kubernetes, RDS, and ElastiCache?

The portfolio environment optimizes for low idle cost and reproducibility. The application boundaries support later separation, but managed multi-AZ services would add cost without improving this demo's learning objective. I would use them for a real availability requirement.

### What would you improve next?

Managed multi-AZ data stores, automated backups and restore tests, WAF and distributed edge login limiting, autoscaling stateless gateway instances, centralized logs/traces, blue-green deployment, provider failover, atomic budget reservations, load-test evidence, and SLO-based alerting.

### What failure did you encounter during deployment?

AWS initially selected an opted-in Local Zone where the chosen EC2 type was unsupported, so I restricted Terraform discovery to standard availability zones. First boot also revealed that modern Docker Compose requires Buildx and that external downloads can fail transiently. I added an explicit pinned Buildx installation and download retries, validated the changes in CI, and proved recovery with container and EC2 restart tests.

## Honest ownership language

Say “I designed and implemented,” then explain the reasoning and trade-offs. If tools assisted you, describe how you validated the output through tests, source review, CI, and live failure recovery. Ownership is demonstrated by being able to explain the request path, data model, failure modes, security boundaries, and alternatives.
