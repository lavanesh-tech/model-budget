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
    #
    # SecretStr keeps the raw value out of repr()/str()/logs. Optional
    # (None) so the app can run against only the free MockProvider
    # without ever configuring OpenAI. When present, must be non-blank --
    # see the validator below.
    openai_api_key: SecretStr | None = None

    # Which OpenAI model this gateway requests when the "openai" provider
    # is selected. See app/services/pricing.py for the verified pricing
    # entry that must stay in sync with this default, and its own note
    # that OpenAI pricing must be reviewed before production deployment.
    openai_model: str = "gpt-5-mini"

    # All three below are optional and, when provided, must be non-blank.
    openai_base_url: str | None = None
    openai_organization: str | None = None
    openai_project: str | None = None

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
