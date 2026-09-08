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

    # Step 30 v3 addition: how many attempts app.services.routing.RetryPolicy
    # permits per candidate. Default 3 for normal operation. Set to 1 via
    # OPENAI_RETRY_MAX_ATTEMPTS=1 for a live smoke test, so a single manual
    # request cannot silently become multiple real, separately-billed
    # OpenAI generation calls.
    openai_retry_max_attempts: int = 3

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
