"""
Prometheus metrics for observability (Step 33), pairing with the
structured JSON logs from Step 31.

Cardinality discipline (read before adding any new label): every label
used here has a small, BOUNDED set of possible values known at
code-review time -- HTTP method, route path (this app has exactly three
routes, neither with path parameters), a small fixed status-code set,
a small closed set of named outcome strings, and the configured OpenAI
model name (effectively one value per deployment). team_id,
idempotency_key_id, request_id, prompt content, and anything else with
an unbounded or per-request-unique value must NEVER become a label --
Prometheus cannot garbage-collect label combinations, so an unbounded
label is a genuine memory-leak/cardinality-explosion bug, not a style
nit.

No I/O: every metric here is updated purely in-process, in memory.
Recording a metric never touches Postgres, Redis, or the network.

Financial accuracy: chat_completion_actual_cost_usd_total exists for
OPERATIONAL/MONITORING purposes only (dashboards, alerting on cost
rate-of-change) -- it is a float-based running sum, not a financial
record. PostgreSQL's usage_records table remains the sole authoritative
financial ledger for this project (unchanged since Step 18); this
counter is never used, and must never be used, as a source of truth for
billing, reconciliation, or accounting. Decimal -> float conversion
happens ONLY here, at the metrics boundary -- every actual accounting
calculation elsewhere in this project stays strictly Decimal, as it
always has.

Authentication: GET /metrics requires no authentication in this step
(the typical pattern for same-VPC/cluster-internal Prometheus
scraping). Restricting network access to the metrics port is an
infrastructure-level concern (an AWS security group / VPC boundary),
not something this application enforces itself -- exactly like a real
Prometheus deployment would expect.

Safety: this module never touches prompts, completions, API keys,
secret hashes, or the Redis connection URL. Every value recorded here
is either a fixed label string from the small sets above, or a
Decimal-derived cost figure -- never raw request/response content.
"""

from prometheus_client import Counter, Histogram

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled, labeled by method, route path, and status code.",
    labelnames=("method", "path", "status_code"),
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds, labeled by method and route path.",
    labelnames=("method", "path"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)

# Every possible value for this label is enumerated in
# app.api.chat_completions -- a small, fixed, closed set. Adding a new
# outcome branch to that module means adding its label value here too,
# in this same review.
chat_completion_outcomes_total = Counter(
    "chat_completion_outcomes_total",
    "Total /v1/chat/completions outcomes, labeled by a small fixed set of named outcomes. "
    "Never labeled by team_id, idempotency_key_id, or any other unbounded value.",
    labelnames=("outcome",),
)

chat_completion_actual_cost_usd_total = Counter(
    "chat_completion_actual_cost_usd_total",
    "Approximate running total of actual_cost (USD) across successfully settled requests, "
    "labeled by model. MONITORING ONLY -- PostgreSQL usage_records is the authoritative "
    "financial ledger; this is a float-based sum, never used for billing or reconciliation.",
    labelnames=("model",),
)

chat_completion_reservation_gap_usd = Histogram(
    "chat_completion_reservation_gap_usd",
    "Unused portion of the budget reservation (reserved_cost - actual_cost, USD) for "
    "successfully settled requests -- indicates how tight or loose the worst-case "
    "reservation estimate was relative to what was actually billed. Only observed when "
    "the gap is non-negative (it always should be, by settlement's own cost-capping "
    "policy -- see app.services.settlement).",
    buckets=(0, 0.00005, 0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1),
)


def record_chat_completion_outcome(outcome: str) -> None:
    chat_completion_outcomes_total.labels(outcome=outcome).inc()


def record_chat_completion_cost(*, model: str, actual_cost, reserved_cost=None) -> None:
    """actual_cost/reserved_cost are Decimal; converted to float ONLY
    for this in-memory metrics counter -- see module docstring on why
    this is never a financial record.
    """
    chat_completion_actual_cost_usd_total.labels(model=model).inc(float(actual_cost))
    if reserved_cost is not None:
        gap = float(reserved_cost) - float(actual_cost)
        if gap >= 0:
            chat_completion_reservation_gap_usd.observe(gap)
