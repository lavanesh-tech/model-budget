# Step 35 — Opt-in OpenTelemetry tracing

## Scope and privacy

Manual OpenTelemetry spans cover the ASGI request lifetime, routing execution
and each actual provider attempt, OpenAI token counting and generation, Redis
Lua execution, and the endpoint's authentication/replay/reservation/settlement
database helpers. Database spans include thread scheduling time and represent
helper execution, not SQL statements or a new transaction. If an awaiting task
is cancelled, its span ends; an already-running database thread may finish later.

No SQLAlchemy, Redis, HTTP-client or OpenAI auto-instrumentation is installed.
No SQL text, URLs, Redis keys, payloads, credentials, tenant IDs or idempotency
IDs are recorded. Dynamic model/provider names are also omitted. Only explicitly
allowed numeric and enumerated attributes are accepted. Exception messages,
stack traces and exception events are not recorded; errors use fixed categories.

HTTP route attributes use three known routes or `unmatched`; arbitrary 404 paths
cannot create unbounded tracing route values. The same bounded helper now fixes
Prometheus method/path labels. Existing logging policy is unchanged; raw paths
and client-controlled request IDs in logs warrant their own privacy review.

Existing JSON logs gain trace_id and span_id alongside request_id. Incoming
X-Request-ID is not a span attribute because clients control it. Only a valid
traceparent is accepted for incoming trace context: baggage and tracestate are
not propagated. No headers are added to OpenAI requests. Standalone provider
calls outside the gateway request have no trace unless explicitly scoped.

## Lifecycle and sampling

Tracing defaults to disabled. The API package remains an import dependency;
disabled mode creates no SDK provider, exporter or background exporter thread.
Each FastAPI application owns its provider; the process-wide global provider
is not replaced. Request context propagates through asyncio tasks and to_thread.
Shutdown runs outside the event loop, including when underlying app startup or
shutdown fails. Export failures do not change gateway results; telemetry may be
dropped. SDK batching uses a bounded 2048-span queue, 256-span batches and a
two-second exporter timeout. Shutdown is best effort, not durable delivery.

Local sampling defaults to 10%. Upstream sampled flags do not override that
budget. The exporter accepts a collector base URL or a full /v1/traces URL and
appends the traces path exactly once.
Resource attributes are explicitly constructed (only service.name); arbitrary
OTEL_RESOURCE_ATTRIBUTES are not merged. Collector addresses must not contain
credentials, query parameters or fragments. Configure collector authentication
in your deployment infrastructure. A local-only Docker collector configuration
is included; cloud deployment, a tracing UI and TLS credentials are not.

## Verification (Terminal, from model-budget)

The supplied core suite uses the real SDK and an in-memory exporter, a small
FastAPI test app, real OpenAI adapter with fake SDK, and fake Redis transport.
It also executes the actual lifespan functions with mocked resource factories,
testing enabled restarts, startup failure, and cleanup failure. Socket connections are blocked.
It does not call OpenAI or require PostgreSQL, Redis, a collector or API keys.

```bash
source .venv/bin/activate
python -m scripts.check_tracing
```

Then run existing gateway regression checks against local test data:

```bash
docker compose up -d db redis
OTEL_ENABLED=false python -m scripts.check_chat_completions
OTEL_ENABLED=false python -m scripts.check_circuit_breaker
OTEL_ENABLED=false python -m scripts.check_openai_provider
OTEL_ENABLED=false python -m scripts.check_rate_limit
OTEL_ENABLED=false python -m scripts.check_redis_integration
python -m pip check
alembic check
git diff --check
git status
```

Stop at the first failure and retain its full output. Do not delete or weaken
assertions to obtain a green result. Existing scripts are not modified by this
package. Some older tests may need separate fixture updates for earlier steps;
that is not grounds for skipping a regression failure.

## Enabling export (after the checks pass)

For a local smoke test, run the included collector and temporary /health check:

```bash
docker compose -p model-budget-tracing -f compose.tracing.yaml up -d
python -m scripts.check_tracing_collector
docker compose -p model-budget-tracing -f compose.tracing.yaml stop
```

The collector is pinned to 0.160.0 and exposes only loopback port 4318. It uses
the supported debug exporter. The test polls readiness, looks for its unique
service name in the collector, and terminates only its own gateway subprocess.
It does not send chat requests. No paid API usage is involved. Docker image
download requires network access. This smoke test was not run in this sandbox.

For continued tracing, arrange a reachable private collector. Add these to your local,
ignored .env using your editor; do not paste credentials into chat:

```dotenv
OTEL_ENABLED=true
OTEL_SERVICE_NAME=model-budget
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318/v1/traces
OTEL_SAMPLE_RATIO=1.0
```

The example assumes a collector already listening on local port 4318. In
containers, 127.0.0.1 refers to that container, not your host: use the collector's
private service address. Restart uvicorn after changing settings, call /health,
and verify the resulting http.request span in your collector/backend. Use 0.1
or another deliberately chosen sampling ratio for deployment. No OpenAI request
is needed to verify /health tracing. Do not enable a nonexistent collector and
mistake exporter connection errors for a gateway failure.

## Commit checkpoint — only after passing output is reviewed

Keep the new regression script and documentation in version control:

```bash
git add app/tracing.py app/main.py app/config.py app/logging_config.py app/api/chat_completions.py app/services/routing.py app/services/rate_limit.py app/providers/openai.py pyproject.toml scripts/check_tracing.py scripts/check_tracing_collector.py docs/step35_tracing.md compose.tracing.yaml otel-collector-config.yaml
git diff --cached --check
git diff --cached --stat
git commit -m "feat: add opt-in privacy-safe OpenTelemetry tracing"
```

Do not stage .env or unrelated existing scripts. A passing offline suite is not
proof of exported traces, production readiness, or financial correctness under
crashes. A collector smoke test and gateway regressions remain separate checks.

## Validation performed while preparing this package

- 21 real-SDK offline tests passed under Python 3.12.
- All changed Python files compiled; pyproject.toml parsed.
- All pre-existing function ASTs in the endpoint, routing, rate limiter and
  OpenAI adapter matched the provided baseline after tracing wrappers were
  removed. Formatting from the pasted source was normalized for NBSP spaces.
- Full gateway/PostgreSQL/Redis tests and collector export were not run here.

## Official references

- https://opentelemetry.io/docs/languages/python/instrumentation/
- https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.html
- https://opentelemetry.io/docs/collector/install/docker/

Manual spans follow the API's context management, with automatic exception
recording explicitly disabled. The SDK's batch processor handles export outside
request execution; PostgreSQL remains the accounting source of truth.
