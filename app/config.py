import logging
from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432

    # Upstream OpenAI credential used by THIS gateway to call the OpenAI
    # API. This is distinct from the per-team client keys (format
    # mb_<public_key_id>.<secret>) that external callers use to
    # authenticate to this gateway -- those are generated via
    # scripts/bootstrap_team.py and stored only as Argon2id hashes in
    # api_keys.secret_hash (see app/security/api_keys.py); there is no
    # single gateway-wide client-key setting to configure here.
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5-mini"
    openai_base_url: str | None = None
    openai_organization: str | None = None
    openai_project: str | None = None

    # Step 30 v3: how many attempts app.services.routing.RetryPolicy
    # permits per candidate. Default 3. Set to 1 via
    # OPENAI_RETRY_MAX_ATTEMPTS=1 for a live smoke test.
    openai_retry_max_attempts: int = 3

    # Step 31: log verbosity for app.logging_config.configure_logging().
    # Must be a real Python logging level name.
    log_level: str = "INFO"

    # Step 32 (Redis redesign): connection URL for the rate-limit Redis
    # instance (local Docker Compose by default; AWS ElastiCache for
    # Redis in production -- possibly with an embedded AUTH token or a
    # rediss:// TLS scheme, which is exactly why this is a SecretStr,
    # never logged or exposed in an error message anywhere in this
    # codebase). Postgres remains the durable system of record for
    # teams/budgets/idempotency/usage; Redis holds ONLY ephemeral
    # rate-limit counters.
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:6379/0")
    redis_socket_timeout_seconds: float = 2.0
    redis_socket_connect_timeout_seconds: float = 2.0

    # A single global default limit per team, per sliding window (see
    # app.services.rate_limit). Per-team overrides are real future work,
    # deliberately not added in this step.
    rate_limit_max_requests: int = 60
    rate_limit_window_seconds: int = 60

    # Step 34: circuit breaker for the "openai" provider (see
    # app.services.routing.CircuitBreaker). Per-process, in-memory only
    # -- see that class's own docstring for why this is not Redis-shared
    # like the rate limiter. After this many CONSECUTIVE failed attempts
    # against the candidate in a row (across requests), the circuit
    # opens and further requests fail fast for cooldown_seconds before a
    # single trial request is allowed through.
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_cooldown_seconds: float = 30.0

    @field_validator("openai_api_key")
    @classmethod
    def _openai_api_key_not_blank(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("openai_api_key must not be blank if provided")
        return value

    @field_validator("openai_model")
    @classmethod
    def _openai_model_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("openai_model must not be blank")
        return value

    @field_validator("openai_base_url", "openai_organization", "openai_project")
    @classmethod
    def _optional_openai_field_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("value must not be blank if provided")
        return value

    @field_validator("openai_retry_max_attempts")
    @classmethod
    def _retry_max_attempts_in_range(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"openai_retry_max_attempts must be a plain int, got {type(value).__name__}")
        if value < 1 or value > 10:
            raise ValueError("openai_retry_max_attempts must be between 1 and 10")
        return value

    @field_validator("log_level")
    @classmethod
    def _log_level_is_valid(cls, value: str) -> str:
        candidate = value.strip().upper()
        if not hasattr(logging, candidate) or not isinstance(getattr(logging, candidate), int):
            raise ValueError(
                f"log_level must be a real logging level name (e.g. DEBUG, INFO, WARNING, ERROR), got {value!r}"
            )
        return candidate

    @field_validator("redis_url")
    @classmethod
    def _redis_url_not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("redis_url must not be blank")
        return value

    @field_validator("redis_socket_timeout_seconds", "redis_socket_connect_timeout_seconds")
    @classmethod
    def _redis_timeout_positive(cls, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"value must be a plain number, got {type(value).__name__}")
        if value <= 0:
            raise ValueError("value must be > 0")
        return float(value)

    @field_validator("rate_limit_max_requests", "rate_limit_window_seconds")
    @classmethod
    def _rate_limit_field_positive(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"value must be a plain int, got {type(value).__name__}")
        if value < 1:
            raise ValueError("value must be >= 1")
        return value

    @field_validator("circuit_breaker_failure_threshold")
    @classmethod
    def _circuit_breaker_failure_threshold_in_range(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"circuit_breaker_failure_threshold must be a plain int, got {type(value).__name__}")
        if value < 1 or value > 100:
            raise ValueError("circuit_breaker_failure_threshold must be between 1 and 100")
        return value

    @field_validator("circuit_breaker_cooldown_seconds")
    @classmethod
    def _circuit_breaker_cooldown_seconds_in_range(cls, value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"circuit_breaker_cooldown_seconds must be a plain number, got {type(value).__name__}")
        if not (0.001 <= value <= 3600.0):
            raise ValueError("circuit_breaker_cooldown_seconds must be between 0.001 and 3600.0")
        return float(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()
