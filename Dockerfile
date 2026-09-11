# syntax=docker/dockerfile:1.7
#
# Multi-stage build for the FastAPI gateway. No build toolchain (gcc,
# libpq-dev) is needed anywhere: psycopg[binary] (see pyproject.toml)
# ships prebuilt wheels.
#
# Deliberately does NOT `pip install .` / `pip install -e .`: this
# project's pyproject.toml has no [tool.setuptools.packages.find]
# configuration, and naive package installation against it has a known
# discovery problem (flagged earlier in this project's own history).
# Instead, the pinned dependency list below is installed directly, and
# the application runs straight from its source layout (COPYed into
# /app, executed via `python -m uvicorn` with /app as the working
# directory) -- exactly how local development already runs it, never
# via an installed "model-budget" package. If pyproject.toml's
# dependency list ever changes, this list must be updated to match --
# there is intentionally no single source of truth shared between them
# (an alternative using `pip install --no-cache-dir --no-deps -e .`
# only works once that packaging gap is fixed at the source; this
# avoids depending on that fix ever happening).

FROM python:3.12.7-slim-bookworm AS base

FROM base AS builder
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip==24.3.1
RUN pip install --no-cache-dir \
    "fastapi>=0.141" \
    "uvicorn[standard]>=0.52" \
    "pydantic-settings>=2.0" \
    "sqlalchemy>=2.0" \
    "psycopg[binary]>=3.1" \
    "alembic>=1.13" \
    "argon2-cffi>=23.1" \
    "openai>=2.0.0" \
    "redis>=5.0" \
    "prometheus-client>=0.20" \
    "opentelemetry-api>=1.27,<2" \
    "opentelemetry-sdk>=1.27,<2" \
    "opentelemetry-exporter-otlp-proto-http>=1.27,<2"

FROM base AS runtime
RUN groupadd --system --gid 1001 modelbudget \
    && useradd --system --uid 1001 --gid modelbudget --no-create-home --shell /usr/sbin/nologin modelbudget
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Source only -- no .env, no .git, no scripts/, no tests, no local
# virtualenv (see .dockerignore). alembic/ + alembic.ini are included
# so migrations can be run against this SAME image as a one-off command
# (see the compose verification commands) -- they are never run
# automatically as part of container startup, so a container restart
# never implicitly touches the database schema.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

RUN chown -R modelbudget:modelbudget /app
USER modelbudget

EXPOSE 8000

# /health returns 200 without touching Postgres/Redis/OpenAI (see
# app/main.py) -- this HEALTHCHECK verifies the process is up and
# serving, not downstream connectivity. Compose-level `depends_on:
# condition: service_healthy` against db/redis's OWN healthchecks (see
# compose.app.yaml) is what enforces startup ORDER; this HEALTHCHECK
# is what lets Compose/orchestrators detect the gateway process itself
# going unhealthy after that.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
