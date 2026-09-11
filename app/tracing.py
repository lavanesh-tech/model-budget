"""Application-owned tracing; no payload, SQL, key, or header instrumentation.

The API dependency is required even when disabled, but the SDK/exporter is
loaded and constructed only when enabled. No global tracer provider is changed.

Step 36: added exactly one fixed constant, "db.prompt_version_lookup", to
_STRINGS["db.operation"] -- the new prompt-version lookup span this step
introduces uses that db.operation value, which would otherwise be silently
dropped by this allowlist (see app.api.chat_completions._traced_to_thread).
No ID, template, prompt, or other unbounded value was added anywhere.
"""
from __future__ import annotations

import asyncio
import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_provider = ContextVar("gateway_tracing_provider", default=None)
_noop = trace.NoOpTracerProvider()
_ROUTES = frozenset({"/health", "/metrics", "/v1/chat/completions"})
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_STRINGS = {
    "http.request.method": _METHODS | {"_OTHER"},
    "http.route": _ROUTES | {"unmatched"},
    "db.operation": {
        "db.authenticate", "db.replay_lookup", "db.reservation", "db.settlement", "db.authoritative_state",
        "db.prompt_version_lookup",
    },
    "openai.operation": {"input_tokens.count", "responses.create"},
    "error.type": {"timeout", "cancelled", "http_error", "operation_error"},
}
_NUMBERS = frozenset({"http.response.status_code", "provider.attempt", "provider.timeout_ms", "rate_limit.window_seconds", "rate_limit.limit"})


def bounded_route(scope):
    template = getattr(scope.get("route"), "path", None)
    return template if isinstance(template, str) and template in _ROUTES else "unmatched"


def bounded_method(method):
    return method if method in _METHODS else "_OTHER"


def record_safe_attributes(span, **attributes):
    """Allow bounded values as well as names; arbitrary caller strings are omitted."""
    for key, value in attributes.items():
        if key in _STRINGS and isinstance(value, str) and value in _STRINGS[key]:
            pass
        elif key in _NUMBERS and type(value) is int and 0 <= value <= 2**31 - 1:
            pass
        else:
            continue
        try:
            span.set_attribute(key, value)
        except Exception:
            # Telemetry must not change application results.
            continue


def _mark_error(span):
    try:
        span.set_status(Status(StatusCode.ERROR))
    except Exception:
        pass


def record_safe_exception(span, exc):
    category = "operation_error"
    if isinstance(exc, asyncio.CancelledError):
        category = "cancelled"
    elif isinstance(exc, TimeoutError):
        category = "timeout"
    record_safe_attributes(span, **{"error.type": category})
    _mark_error(span)


@contextmanager
def start_safe_span(name, *, kind=SpanKind.INTERNAL, context=None, **attributes):
    provider = _provider.get() or _noop
    try:
        span = provider.get_tracer("model_budget.gateway").start_span(name, kind=kind, context=context)
    except Exception:
        span = trace.INVALID_SPAN
    record_safe_attributes(span, **attributes)
    with trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
        try:
            yield span
        except BaseException as exc:
            record_safe_exception(span, exc)
            raise
        finally:
            try:
                span.end()
            except Exception:
                pass


def configure_tracing(*, enabled, service_name, endpoint, sample_ratio=0.1):
    if not enabled:
        return None
    try:
        parts = urlsplit(endpoint or "")
        valid = parts.scheme in {"http", "https"} and parts.hostname and not (parts.username or parts.password or parts.query or parts.fragment)
        valid = valid and parts.port != 0 and not any(c.isspace() for c in (endpoint or ""))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("Tracing requires an HTTP(S) collector URL without credentials, query or fragment")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", service_name):
        raise ValueError("Invalid tracing service name")
    if isinstance(sample_ratio, bool) or not math.isfinite(sample_ratio) or not 0 <= sample_ratio <= 1:
        raise ValueError("Tracing sample ratio must be between 0 and 1")
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
    # Resource(), not Resource.create(): no arbitrary environment attributes.
    # Ignore incoming sampled flags: this gateway owns its sampling budget.
    provider = TracerProvider(resource=Resource({"service.name": service_name}), sampler=TraceIdRatioBased(sample_ratio), shutdown_on_exit=False)
    try:
        base = endpoint.rstrip("/")
        trace_endpoint = base if base.endswith("/v1/traces") else base + "/v1/traces"
        exporter = OTLPSpanExporter(endpoint=trace_endpoint, timeout=2, headers={})
        provider.add_span_processor(BatchSpanProcessor(exporter, max_queue_size=2048, max_export_batch_size=256, schedule_delay_millis=1000, export_timeout_millis=2000))
    except Exception:
        provider.shutdown()
        raise
    return provider


class TracingMiddleware:
    """Trace full ASGI response lifetime; accept traceparent only, never baggage.

    Unknown URLs map to a constant. Request IDs are deliberately NOT attributes:
    existing clients can supply arbitrary content in X-Request-ID. Logs contain
    request_id alongside trace_id/span_id for correlation instead.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        provider = getattr(scope["app"].state, "tracing_provider", None)
        token = _provider.set(provider)
        try:
            if provider is None:
                return await self.app(scope, receive, send)
            carrier = {}
            for key, value in scope.get("headers", []):
                if key.lower() == b"traceparent" and len(value) == 55:
                    carrier["traceparent"] = value.decode("ascii", errors="ignore")
                    break
            parent = TraceContextTextMapPropagator().extract(carrier, context=Context())
            method = bounded_method(scope.get("method", "_OTHER"))
            with start_safe_span("http.request", kind=SpanKind.SERVER, context=parent,
                                 **{"http.request.method": method}) as span:
                async def traced_send(message):
                    if message["type"] == "http.response.start":
                        status = message["status"]
                        record_safe_attributes(span, **{"http.response.status_code": status, "http.route": bounded_route(scope)})
                        if status >= 500:
                            record_safe_attributes(span, **{"error.type": "http_error"})
                            _mark_error(span)
                    await send(message)
                try:
                    await self.app(scope, receive, traced_send)
                except Exception:
                    record_safe_attributes(span, **{"http.response.status_code": 500, "http.route": bounded_route(scope)})
                    raise
        finally:
            _provider.reset(token)
