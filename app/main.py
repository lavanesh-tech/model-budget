from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.chat_completions import router as chat_completions_router
from app.config import get_settings
from app.db import SessionLocal
from app.providers.openai import build_production_provider_registry
from app.services.routing import RetryPolicy

_DEFAULT_PROVIDER_TIMEOUT_MS = 30_000


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

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


async def _validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Sanitized 422 body: FastAPI's default handler echoes each invalid
    field's submitted value back in the response, which could leak
    prompt content. This handler returns a generic, field-name-only body.
    """
    field_names = sorted({".".join(str(p) for p in err["loc"][1:]) for err in exc.errors()})
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
app.include_router(chat_completions_router)
app.add_exception_handler(RequestValidationError, _validation_exception_handler)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
