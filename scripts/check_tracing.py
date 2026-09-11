"""Offline Step 35 checks: real OTel SDK, in-memory exporter, no services/key."""
import asyncio
import ast
import json
import logging
import socket
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ALWAYS_OFF
from opentelemetry.trace import SpanKind, StatusCode

from app import tracing as t
from app.logging_config import _JSONFormatter, _RequestIDFilter, set_request_id

SECRET = "SENSITIVE_SENTINEL_6f8b13_do_not_export"


class TracingChecks(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("Unexpected network call"))
        self.network.start()
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider(resource=Resource({"service.name": "tracing-check"}), sampler=ALWAYS_ON, shutdown_on_exit=False)
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.token = t._provider.set(self.provider)

    def tearDown(self):
        t._provider.reset(self.token)
        self.provider.shutdown()
        self.network.stop()

    def spans(self):
        return self.exporter.get_finished_spans()

    def test_01_disabled_is_noop(self):
        self.assertIsNone(t.configure_tracing(enabled=False, service_name="x", endpoint=None))
        token = t._provider.set(None)
        try:
            with t.start_safe_span("disabled"):
                pass
        finally:
            t._provider.reset(token)
        self.assertEqual(len(self.spans()), 0)

    def test_02_nesting_and_thread_context(self):
        async def run():
            with t.start_safe_span("parent"):
                def work():
                    with t.start_safe_span("db.settlement"):
                        return 123
                self.assertEqual(await asyncio.to_thread(work), 123)
        asyncio.run(run())
        child, parent = self.spans()
        self.assertEqual(child.parent.span_id, parent.context.span_id)
        self.assertEqual(child.context.trace_id, parent.context.trace_id)

    def test_03_exception_privacy_and_identity(self):
        error = RuntimeError(SECRET)
        try:
            with t.start_safe_span("failure", **{"prompt": SECRET, "team_id": SECRET, "provider.model": SECRET, "http.route": SECRET}):
                raise error
        except RuntimeError as caught:
            self.assertIs(caught, error)
        span = self.spans()[0]
        self.assertEqual(span.status.status_code, StatusCode.ERROR)
        self.assertEqual(span.attributes, {"error.type": "operation_error"})
        self.assertFalse(span.events)
        self.assertIsNone(span.status.description)
        self.assertNotIn(SECRET, span.to_json())

    def test_04_cancellation_propagates(self):
        async def run():
            with t.start_safe_span("cancel"):
                raise asyncio.CancelledError(SECRET)
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(run())
        self.assertEqual(self.spans()[0].attributes["error.type"], "cancelled")
        self.assertFalse(self.spans()[0].events)

    def test_05_concurrent_context_isolation(self):
        async def worker():
            with t.start_safe_span("parent") as parent:
                await asyncio.sleep(0)
                with t.start_safe_span("child") as child:
                    return parent.get_span_context().trace_id, child.get_span_context().trace_id
        async def run():
            return await asyncio.gather(*(worker() for _ in range(20)))
        pairs = asyncio.run(run())
        self.assertTrue(all(a == b for a, b in pairs))
        self.assertEqual(len({a for a, _ in pairs}), 20)

    def app(self):
        app = FastAPI()
        app.state.tracing_provider = self.provider
        app.add_middleware(t.TracingMiddleware)
        @app.get("/health")
        async def health():
            with t.start_safe_span("child"):
                return {"status": "ok"}
        @app.get("/failure")
        async def failure():
            return JSONResponse({"error": "safe"}, status_code=503)
        @app.get("/raises")
        async def raises():
            raise RuntimeError(SECRET)
        return app

    def test_06_asgi_parent_and_status(self):
        with TestClient(self.app()) as client:
            result = client.get("/health", headers={"traceparent": "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01", "baggage": SECRET, "authorization": SECRET, "x-request-id": SECRET})
        self.assertEqual(result.json(), {"status": "ok"})
        child, root = self.spans()
        self.assertEqual(root.kind, SpanKind.SERVER)
        self.assertEqual(root.context.trace_id, int("1234567890abcdef1234567890abcdef", 16))
        self.assertEqual(root.parent.span_id, int("1234567890abcdef", 16))
        self.assertEqual(child.parent.span_id, root.context.span_id)
        self.assertEqual(root.attributes["http.response.status_code"], 200)
        self.assertNotIn(SECRET, "".join(s.to_json() for s in self.spans()))

    def test_07_unknown_paths_are_bounded(self):
        with TestClient(self.app()) as client:
            for i in range(10):
                self.assertEqual(client.get(f"/{SECRET}/{i}?key={SECRET}").status_code, 404)
        self.assertEqual({s.attributes["http.route"] for s in self.spans()}, {"unmatched"})
        self.assertNotIn(SECRET, "".join(s.to_json() for s in self.spans()))

    def test_08_handled_and_unhandled_errors(self):
        with TestClient(self.app(), raise_server_exceptions=False) as client:
            self.assertEqual(client.get("/failure").status_code, 503)
            self.assertEqual(client.get("/raises").status_code, 500)
        self.assertTrue(all(s.status.status_code == StatusCode.ERROR for s in self.spans()))
        self.assertNotIn(SECRET, "".join(s.to_json() for s in self.spans()))

    def test_09_logs_capture_context_at_emit(self):
        set_request_id("test-request")
        with t.start_safe_span("logged") as span:
            record = logging.LogRecord("test", logging.INFO, "", 0, "safe", (), None)
            _RequestIDFilter().filter(record)
            expected = format(span.get_span_context().trace_id, "032x")
        payload = json.loads(_JSONFormatter().format(record))
        self.assertEqual(payload["trace_id"], expected)
        self.assertEqual(payload["request_id"], "test-request")
        self.assertEqual(len(payload["span_id"]), 16)

    def test_10_exporter_failure_does_not_change_result(self):
        class BrokenExporter(SpanExporter):
            def export(self, spans):
                raise RuntimeError("deliberate exporter failure")
            def shutdown(self):
                pass
        self.provider.add_span_processor(SimpleSpanProcessor(BrokenExporter()))
        with self.assertLogs("opentelemetry.sdk.trace.export", level="ERROR"):
            with t.start_safe_span("export_failure"):
                result = 42
        self.assertEqual(result, 42)

    def test_11_repeated_app_instances_are_isolated(self):
        with TestClient(self.app()) as client:
            client.get("/health")
        count = len(self.spans())
        app = self.app()
        app.state.tracing_provider = None
        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(len(self.spans()), count)

    def test_12_redis_span_no_keys(self):
        from uuid import uuid4
        from app.services.rate_limit import RedisRateLimiter
        class FakeRedis:
            def register_script(self, script):
                async def execute(**kwargs):
                    return [1, 1, 10, "12345.5", 0]
                return execute
        result = asyncio.run(RedisRateLimiter(FakeRedis()).check(team_id=str(uuid4()), window_seconds=60, limit=10, member=SECRET))
        self.assertTrue(result.allowed)
        self.assertEqual(self.spans()[0].name, "redis.rate_limit.check")
        self.assertNotIn(SECRET, self.spans()[0].to_json())

    def test_13_config_rejects_unsafe_urls_and_sampling(self):
        for endpoint in [None, "file:///tmp/collector", "http://user:password@localhost", "http://localhost/?key=secret"]:
            with self.assertRaises(ValueError):
                t.configure_tracing(enabled=True, service_name="test", endpoint=endpoint)
        for ratio in [-1, 2, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                t.configure_tracing(enabled=True, service_name="test", endpoint="http://localhost:4318/v1/traces", sample_ratio=ratio)

    def test_14_sdk_lifecycle_does_not_replace_global(self):
        before = trace.get_tracer_provider()
        class FakeExporter(SpanExporter):
            closed = False
            def export(self, spans):
                return SpanExportResult.SUCCESS
            def shutdown(self):
                self.closed = True
        exporter = FakeExporter()
        with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", return_value=exporter):
            for _ in range(2):
                provider = t.configure_tracing(enabled=True, service_name="test", endpoint="http://localhost:4318/v1/traces")
                provider.shutdown()
        self.assertTrue(exporter.closed)
        self.assertIs(trace.get_tracer_provider(), before)

    def test_15_zero_sampling_exports_nothing(self):
        provider = TracerProvider(resource=Resource({}), sampler=ALWAYS_OFF, shutdown_on_exit=False)
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        token = t._provider.set(provider)
        try:
            with t.start_safe_span("not_sampled"):
                pass
        finally:
            t._provider.reset(token)
            provider.shutdown()
        self.assertFalse(self.spans())

    def test_16_real_openai_adapter_and_routing(self):
        from app.providers.openai import OpenAIProvider
        from app.services.routing import ProviderRegistration, RetryPolicy, RouteCandidate, build_provider_registry, build_route, execute_route
        calls = []
        async def count(**kwargs):
            calls.append(("count", kwargs))
            return SimpleNamespace(input_tokens=4)
        async def create(**kwargs):
            calls.append(("create", kwargs))
            return SimpleNamespace(output_text=SECRET, usage=SimpleNamespace(input_tokens=4, output_tokens=2))
        adapter = OpenAIProvider(SimpleNamespace(responses=SimpleNamespace(create=create, input_tokens=SimpleNamespace(count=count))))
        registry = build_provider_registry((ProviderRegistration("openai", adapter, frozenset({"gpt-5-mini"})),))
        route = build_route([RouteCandidate("openai", "gpt-5-mini")])
        async def run():
            with t.start_safe_span("request"):
                self.assertEqual(await adapter.count_input_tokens("gpt-5-mini", SECRET), 4)
                with t.start_safe_span("provider.execute_route"):
                    result = await execute_route(registry, route, SECRET, 16, RetryPolicy(max_attempts_per_candidate=1, base_backoff_ms=1, max_backoff_ms=10), 1000)
                    self.assertEqual(result.result.completion_text, SECRET)
        asyncio.run(run())
        spans = {span.name: span for span in self.spans()}
        self.assertEqual(spans["openai.responses.create"].parent.span_id, spans["provider.attempt"].context.span_id)
        self.assertEqual(spans["provider.attempt"].parent.span_id, spans["provider.execute_route"].context.span_id)
        self.assertEqual(len(calls), 2)
        self.assertNotIn(SECRET, "".join(s.to_json() for s in self.spans()))

    def lifecycle_fixture(self, *, fail_start=False, fail_close=False, enabled=True):
        # Execute the actual lifespan definitions without importing database
        # modules. Resource constructors are mocked; lifecycle code is not.
        from app.services.routing import RetryPolicy, CircuitBreakerConfig
        from app.services.rate_limit import RedisRateLimiter
        settings = SimpleNamespace(
            otel_enabled=enabled, otel_service_name="lifecycle-test",
            otel_exporter_otlp_endpoint="http://localhost:4318", otel_sample_ratio=1.0,
            log_level="INFO", openai_retry_max_attempts=1,
            redis_url=SimpleNamespace(get_secret_value=lambda: "redis://localhost:6379/0"),
            redis_socket_timeout_seconds=1, redis_socket_connect_timeout_seconds=1,
            rate_limit_max_requests=10, rate_limit_window_seconds=60,
            openai_api_key=SimpleNamespace(get_secret_value=lambda: "fake") if fail_start else None,
            openai_model="gpt-5-mini", openai_base_url=None, openai_organization=None,
            openai_project=None, circuit_breaker_failure_threshold=3, circuit_breaker_cooldown_seconds=10,
        )
        class FakeRedis:
            closed = False
            def register_script(self, source):
                return None
            async def aclose(self):
                self.closed = True
                if fail_close:
                    raise RuntimeError("cleanup failure")
        redis = FakeRedis()
        def fail_builder(**kwargs):
            raise RuntimeError("startup failure")
        names = {"lifespan", "_application_lifespan", "_close_owned_client", "_close_tracing"}
        source = ast.parse(Path("app/main.py").read_text())
        functions = [node for node in source.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        self.assertEqual({node.name for node in functions}, names)
        scope = dict(asyncio=asyncio, AsyncExitStack=AsyncExitStack, asynccontextmanager=asynccontextmanager,
                     FastAPI=FastAPI, AsyncIterator=__import__("typing").AsyncIterator,
                     get_settings=lambda: settings, configure_logging=lambda level: None,
                     configure_tracing=t.configure_tracing, logger=logging.getLogger("lifecycle.test"),
                     SessionLocal=object(), RetryPolicy=RetryPolicy, CircuitBreakerConfig=CircuitBreakerConfig,
                     RedisRateLimiter=RedisRateLimiter, _DEFAULT_PROVIDER_TIMEOUT_MS=30000,
                     redis_asyncio=SimpleNamespace(Redis=SimpleNamespace(from_url=lambda *a, **k: redis)),
                     build_production_provider_registry=fail_builder)
        exec(compile(ast.Module(body=functions, type_ignores=[]), "app/main.py", "exec"), scope)
        app = FastAPI(lifespan=scope["lifespan"])
        app.add_middleware(t.TracingMiddleware)
        @app.get("/health")
        def health():
            return {"status": "ok"}
        return app, redis

    def test_17_two_enabled_lifecycles_each_export(self):
        for _ in range(2):
            exporter = InMemorySpanExporter()
            app, redis = self.lifecycle_fixture()
            with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", return_value=exporter) as factory:
                with TestClient(app) as client:
                    self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(len(exporter.get_finished_spans()), 1)
            self.assertEqual(factory.call_args.kwargs["endpoint"], "http://localhost:4318/v1/traces")
            self.assertTrue(redis.closed)
            self.assertIsNone(app.state.tracing_provider)

    def test_18_startup_failure_closes_resources(self):
        exporter = InMemorySpanExporter()
        app, redis = self.lifecycle_fixture(fail_start=True)
        with patch.object(exporter, "shutdown", wraps=exporter.shutdown) as closed:
            with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", return_value=exporter):
                with self.assertRaisesRegex(RuntimeError, "startup failure"):
                    with TestClient(app):
                        pass
            closed.assert_called_once()
        self.assertTrue(redis.closed)
        self.assertIsNone(app.state.tracing_provider)

    def test_19_cleanup_failure_does_not_skip_tracing(self):
        exporter = InMemorySpanExporter()
        app, redis = self.lifecycle_fixture(fail_close=True)
        with patch.object(exporter, "shutdown", wraps=exporter.shutdown) as closed:
            with patch("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", return_value=exporter):
                with self.assertRaisesRegex(RuntimeError, "cleanup failure"):
                    with TestClient(app) as client:
                        self.assertEqual(client.get("/health").status_code, 200)
            closed.assert_called_once()
        self.assertTrue(redis.closed)

    def test_20_disabled_ignores_other_global_provider(self):
        token = t._provider.set(None)
        try:
            with patch.object(trace, "get_tracer", return_value=self.provider.get_tracer("external")):
                with t.start_safe_span("disabled"):
                    pass
        finally:
            t._provider.reset(token)
        self.assertFalse(self.spans())

    def test_21_streaming_span_ends_after_body(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            self.assertTrue(trace.get_current_span().is_recording())
            await asyncio.sleep(0)
            await send({"type": "http.response.body", "body": b"ok"})
            self.assertTrue(trace.get_current_span().is_recording())
        async def send(message):
            pass
        scope = {"type": "http", "method": "GET", "headers": [], "app": SimpleNamespace(state=SimpleNamespace(tracing_provider=self.provider))}
        asyncio.run(t.TracingMiddleware(app)(scope, None, send))
        self.assertEqual(len(self.spans()), 1)
        self.assertIsNotNone(self.spans()[0].end_time)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(TracingChecks))
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("All 21 tracing checks passed. No external services or API keys used.")
