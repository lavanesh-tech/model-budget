
import asyncio
import logging

import time

from collections.abc import AsyncIterator, Awaitable, Callable

from contextlib import AsyncExitStack, asynccontextmanager

import redis.asyncio as redis_asyncio

from fastapi import FastAPI, Request, Response, status

from fastapi.exceptions import RequestValidationError

from fastapi.responses import JSONResponse

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from starlette.middleware.base import BaseHTTPMiddleware

from app.api.admin import router as admin_router
from app.api.chat_completions import router as chat_completions_router

from app.config import get_settings

from app.db import SessionLocal

from app.logging_config import configure_logging, new_request_id, set_request_id

from app.metrics import http_request_duration_seconds, http_requests_total

from app.providers.openai import build_production_provider_registry

from app.services.rate_limit import RedisRateLimiter

from app.services.routing import CircuitBreakerConfig, RetryPolicy

from app.tracing import TracingMiddleware, bounded_method, bounded_route, configure_tracing

_DEFAULT_PROVIDER_TIMEOUT_MS = 30_000

logger = logging.getLogger("app.request")


class RequestIDMiddleware(BaseHTTPMiddleware):

    """Assigns a correlation ID to every request: reuses an incoming

    X-Request-ID header if the caller already has one (so a request can

    be traced across systems that assign their own IDs), otherwise

    generates a fresh uuid4. The ID is set on a ContextVar (see

    app.logging_config) so every log line emitted while handling this

    request -- including from the synchronous helpers run via

    asyncio.to_thread -- is automatically stamped with it, without any

    application code needing to pass it explicitly. As of Step 32, this

    same ID is also reused as the Redis sorted-set member for the rate

    limiter (see app.services.rate_limit and app.api.chat_completions) --

    one correlation ID serves both purposes, deliberately, rather than

    inventing a second one. The ID is echoed back as X-Request-ID on the

    response, including on error responses.

    Step 33: this same middleware also records the two HTTP-level

    Prometheus metrics (app.metrics.http_requests_total and

    http_request_duration_seconds). Step 35 uses bounded method and
    matched route-template labels. Unknown paths are "unmatched";
    raw client URLs never become metric labels or tracing attributes.

    """

    async def dispatch(

        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]

    ) -> Response:

        request_id = request.headers.get("X-Request-ID") or new_request_id()

        set_request_id(request_id)

        request.state.request_id = request_id

        start = time.perf_counter()

        logger.info(

            "request_received",

            extra={"method": request.method, "path": request.url.path},

        )

        try:

            response = await call_next(request)

        except Exception:

            elapsed_seconds = time.perf_counter() - start

            elapsed_ms = int(elapsed_seconds * 1000)

            logger.exception(

                "request_unhandled_exception",

                extra={"method": request.method, "path": request.url.path, "elapsed_ms": elapsed_ms},

            )

            http_requests_total.labels(

                method=bounded_method(request.method), path=bounded_route(request.scope), status_code="500"

            ).inc()

            http_request_duration_seconds.labels(

                method=bounded_method(request.method), path=bounded_route(request.scope)

            ).observe(elapsed_seconds)

            raise

        response.headers["X-Request-ID"] = request_id

        elapsed_seconds = time.perf_counter() - start

        elapsed_ms = int(elapsed_seconds * 1000)

        logger.info(

            "request_completed",

            extra={

                "method": request.method,

                "path": request.url.path,

                "status_code": response.status_code,

                "elapsed_ms": elapsed_ms,

            },

        )

        http_requests_total.labels(

            method=bounded_method(request.method), path=bounded_route(request.scope), status_code=str(response.status_code)

        ).inc()

        http_request_duration_seconds.labels(

            method=bounded_method(request.method), path=bounded_route(request.scope)

        ).observe(elapsed_seconds)

        return response


@asynccontextmanager

async def _application_lifespan(app: FastAPI) -> AsyncIterator[None]:

    settings = get_settings()

    configure_logging(settings.log_level)

    app.state.session_factory = SessionLocal

    # Step 30 v3: retry attempt count is configurable (OPENAI_RETRY_MAX_ATTEMPTS),

    # so a live smoke test can force exactly 1 attempt per candidate --

    # otherwise the default of 3 means one manual request could become up

    # to 3 real, separately-billed OpenAI generation calls.

    app.state.retry_policy = RetryPolicy(

        max_attempts_per_candidate=settings.openai_retry_max_attempts,

        base_backoff_ms=200,

        max_backoff_ms=2000,

    )

    app.state.provider_timeout_ms = _DEFAULT_PROVIDER_TIMEOUT_MS

    app.state.provider_registry = None

    app.state.owned_openai_provider = None

    # Step 32: Redis-backed rate limiting. Postgres remains the durable

    # system of record for everything else; Redis holds ONLY ephemeral

    # rate-limit counters (see app.services.rate_limit's module

    # docstring). redis.asyncio.Redis.from_url(...) is lazy -- it parses

    # the URL and builds a connection pool but makes NO network call

    # here, mirroring how OpenAIProvider.from_settings already works

    # below. The connection URL itself (which may embed an AWS

    # ElastiCache AUTH token) is read via get_secret_value() exactly

    # once, right here, and never logged.

    redis_client = redis_asyncio.Redis.from_url(

        settings.redis_url.get_secret_value(),

        socket_timeout=settings.redis_socket_timeout_seconds,

        socket_connect_timeout=settings.redis_socket_connect_timeout_seconds,

        decode_responses=False,

    )

    app.state.redis_client = redis_client

    app.state.rate_limiter = RedisRateLimiter(redis_client)

    app.state.rate_limit_max_requests = settings.rate_limit_max_requests

    app.state.rate_limit_window_seconds = settings.rate_limit_window_seconds

    logger.info(

        "rate_limiter_configured",

        extra={

            "limit": settings.rate_limit_max_requests,

            "window_seconds": settings.rate_limit_window_seconds,

        },

    )

    # Production only ever uses the real OpenAIProvider, never

    # MockProvider. If OPENAI_API_KEY is not configured, the registry

    # stays None and every /v1/chat/completions request cleanly returns

    # 503 "no_provider_configured" -- never a silent fallback to a free

    # mock model. Constructing the client performs NO network call.

    # Step 34: a per-process CircuitBreaker is attached to the "openai"

    # registration -- see app.services.routing.CircuitBreaker's own

    # docstring for the full design.

    if settings.openai_api_key is not None:

        registry = build_production_provider_registry(

            api_key=settings.openai_api_key.get_secret_value(),

            model=settings.openai_model,

            base_url=settings.openai_base_url,

            organization=settings.openai_organization,

            project=settings.openai_project,

            circuit_breaker_config=CircuitBreakerConfig(

                failure_threshold=settings.circuit_breaker_failure_threshold,

                cooldown_seconds=settings.circuit_breaker_cooldown_seconds,

            ),

        )

        app.state.provider_registry = registry

        # owned_openai_provider is set ONLY here, in the one branch where

        # this process itself constructed the client. Anything injected

        # via app.dependency_overrides (every test) never touches

        # app.state at all, so it can never end up here.

        app.state.owned_openai_provider = registry["openai"].adapter

        logger.info(

            "provider_configured",

            extra={

                "provider": "openai",

                "model": settings.openai_model,

                "circuit_breaker_failure_threshold": settings.circuit_breaker_failure_threshold,

                "circuit_breaker_cooldown_seconds": settings.circuit_breaker_cooldown_seconds,

            },

        )

    else:

        logger.warning("provider_not_configured", extra={"reason": "OPENAI_API_KEY not set"})

    yield


async def _close_owned_client(app, attribute, event):
    client = getattr(app.state, attribute, None)
    setattr(app.state, attribute, None)
    close = getattr(client, "aclose", None)
    if callable(close):
        await close()
        logger.info(event)


async def _close_tracing(app, provider):
    app.state.tracing_provider = None
    if provider is not None:
        try:
            await asyncio.to_thread(provider.shutdown)
        except Exception:
            logger.warning("tracing_shutdown_failed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.redis_client = None
    app.state.owned_openai_provider = None
    app.state.tracing_provider = None
    async with AsyncExitStack() as cleanup:
        provider = configure_tracing(
            enabled=getattr(settings, "otel_enabled", False),
            service_name=getattr(settings, "otel_service_name", "model-budget"),
            endpoint=getattr(settings, "otel_exporter_otlp_endpoint", None),
            sample_ratio=getattr(settings, "otel_sample_ratio", 0.1),
        )
        app.state.tracing_provider = provider
        # Register cleanup before any client construction. All callbacks run
        # even when startup or another callback fails; errors still propagate.
        cleanup.push_async_callback(_close_tracing, app, provider)
        cleanup.push_async_callback(_close_owned_client, app, "redis_client", "redis_client_closed")
        cleanup.push_async_callback(_close_owned_client, app, "owned_openai_provider", "provider_client_closed")
        async with _application_lifespan(app):
            yield


async def _validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:

    """Sanitized 422 body: FastAPI's default handler echoes each invalid

    field's submitted value back in the response, which could leak

    prompt content. This handler returns a generic, field-name-only body.

    """

    field_names = sorted({".".join(str(p) for p in err["loc"][1:]) for err in exc.errors()})

    logger.warning("request_validation_failed", extra={"fields": field_names})

    return JSONResponse(

        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,

        content={

            "detail": {

                "code": "invalid_request",

                "message": "request body failed validation",

                "fields": field_names,

            }

        },

    )


app = FastAPI(title="ModelBudget", lifespan=lifespan)

app.add_middleware(RequestIDMiddleware)

# Last-added middleware runs outermost, so request logs inherit the root span.
app.add_middleware(TracingMiddleware)

app.include_router(chat_completions_router)
app.include_router(admin_router)

app.add_exception_handler(RequestValidationError, _validation_exception_handler)


@app.get("/health")

def health() -> dict[str, str]:

    return {"status": "ok"}


@app.get("/metrics")

def metrics() -> Response:

    """Prometheus text-exposition endpoint. No authentication (see

    app.metrics module docstring). Reads only in-process counters/

    histograms -- no Postgres, Redis, or OpenAI I/O occurs here.

    """

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
