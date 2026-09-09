import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import redis.asyncio as redis_asyncio
from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.chat_completions import router as chat_completions_router
from app.config import get_settings
from app.db import SessionLocal
from app.logging_config import configure_logging, new_request_id, set_request_id
from app.metrics import http_request_duration_seconds, http_requests_total
from app.providers.openai import build_production_provider_registry
from app.services.rate_limit import RedisRateLimiter
from app.services.routing import RetryPolicy

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
    http_request_duration_seconds). `request.url.path` is used directly
    as the "path" label -- safe here specifically because this
    application has exactly two routes (/health, /v1/chat/completions),
    neither with a path parameter, so the label's value set is small and
    fixed. A future route with a path parameter (e.g. /v1/teams/{id})
    would need to switch to the route TEMPLATE rather than the raw path,
    to avoid an unbounded-cardinality label -- noted here so that
    constraint travels with any future route addition.
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
                method=request.method, path=request.url.path, status_code="500"
            ).inc()
            http_request_duration_seconds.labels(
                method=request.method, path=request.url.path
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
            method=request.method, path=request.url.path, status_code=str(response.status_code)
        ).inc()
        http_request_duration_seconds.labels(
            method=request.method, path=request.url.path
        ).observe(elapsed_seconds)
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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
    if settings.openai_api_key is not None:
        registry = build_production_provider_registry(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.openai_model,
            base_url=settings.openai_base_url,
            organization=settings.openai_organization,
            project=settings.openai_project,
        )
        app.state.provider_registry = registry
        # owned_openai_provider is set ONLY here, in the one branch where
        # this process itself constructed the client. Anything injected
        # via app.dependency_overrides (every test) never touches
        # app.state at all, so it can never end up here.
        app.state.owned_openai_provider = registry["openai"].adapter
        logger.info("provider_configured", extra={"provider": "openai", "model": settings.openai_model})
    else:
        logger.warning("provider_not_configured", extra={"reason": "OPENAI_API_KEY not set"})

    try:
        yield
    finally:
        provider = app.state.owned_openai_provider
        # Duck-typed on purpose: we close whatever we ourselves
        # constructed (identified structurally by HOW
        # owned_openai_provider was populated above, not by its exact
        # type), so a test double with an aclose() method is correctly
        # closed too, and nothing externally injected is ever touched.
        if provider is not None:
            aclose = getattr(provider, "aclose", None)
            if callable(aclose):
                await aclose()
                logger.info("provider_client_closed")

        redis_client_to_close = app.state.redis_client
        if redis_client_to_close is not None:
            await redis_client_to_close.aclose()
            logger.info("redis_client_closed")


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
app.include_router(chat_completions_router)
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
