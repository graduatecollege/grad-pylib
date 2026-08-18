import os
from collections.abc import Callable
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

ENVIRONMENT_ENV_VAR = "ENVIRONMENT"
DEVELOPMENT_ENVIRONMENTS = frozenset({"development", "local", "test"})


def is_development_environment(environment: str | None) -> bool:
    """Return whether `environment` permits local-development behavior.

    Matching is case-insensitive and ignores surrounding whitespace so the same definition
    safely controls dotenv loading and development-only authentication features.
    """
    return (environment or "").strip().lower() in DEVELOPMENT_ENVIRONMENTS


class BaseAppSettings(BaseSettings):
    """Common application settings loaded from environment variables and, locally, `.env`.

    Subclass this model for service-specific settings. Dotenv values are deliberately accepted
    only in development environments, preventing a mounted or shipped `.env` from overriding
    deployed configuration; enabling the development API-key bypass elsewhere is rejected.
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "FastAPI Service"
    version: str = "1.0.0"
    environment: str = "development"
    debug: bool = False
    allowed_origins: list[str] = []

    azure_ad_instance: str = "https://login.microsoftonline.com/"
    azure_ad_tenant_id: str | None = None
    azure_ad_client_id: str | None = None
    azure_ad_openapi_client_id: str | None = None
    azure_ad_scope_description: str = "user_impersonation"

    dev_api_key: str | None = None
    enable_dev_api_key: bool = False

    database_url: str | None = None
    log_level: str = "INFO"

    @classmethod
    def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """
        Drops the ``.env`` source outside development environments.

        Dotenv files are resolved relative to the process working directory, so a stray
        ``.env`` shipped in (or mounted into) a container could otherwise silently override
        production configuration, including the authentication settings.
        """
        if is_development_environment(os.environ.get(ENVIRONMENT_ENV_VAR, cls.model_fields["environment"].default)):
            return init_settings, env_settings, dotenv_settings, file_secret_settings
        return init_settings, env_settings, file_secret_settings

    @property
    def is_development(self) -> bool:
        """Return whether this settings instance enables development-only behavior."""
        return is_development_environment(self.environment)

    @model_validator(mode="after")
    def _guard_dev_api_key(self) -> BaseAppSettings:
        if self.enable_dev_api_key and not self.is_development:
            raise ValueError(
                "enable_dev_api_key must not be enabled outside a development "
                f"environment (environment={self.environment!r}). The development "
                "API key bypass is intended for local development only."
            )
        return self

    @property
    def azure_ad_scope_name(self) -> str:
        """Return this application's Azure AD delegated scope identifier.

        A client ID is required because the scope is used by the authentication bootstrap and
        OpenAPI configuration to describe the protected API.
        """
        if not self.azure_ad_client_id:
            raise ValueError("Azure AD scope must be set in the environment or settings.")
        return f"api://{self.azure_ad_client_id}/{self.azure_ad_scope_description}"

    @property
    def azure_ad_scopes(self) -> dict[str, str]:
        """Return Azure AD scopes in the mapping format required by the bearer scheme."""
        if not self.azure_ad_scope_name:
            return {}
        return {self.azure_ad_scope_name: self.azure_ad_scope_description}


_settings_factory: Callable[[], BaseAppSettings] = BaseAppSettings


def configure_settings_factory(factory: Callable[[], BaseAppSettings]) -> None:
    """Set the application settings factory and invalidate the shared cached instance.

    Applications use this during startup to register a `BaseAppSettings` subclass; tests can
    temporarily replace it without retaining stale settings from an earlier configuration.
    """
    global _settings_factory
    _settings_factory = factory
    get_settings.cache_clear()


@lru_cache(maxsize=1)
def get_settings() -> BaseAppSettings:
    """Return the process-wide settings instance produced by the configured factory.

    Settings are cached so request dependencies share one validated configuration; call
    `configure_settings_factory` before first use when an application defines a subclass.
    """
    return _settings_factory()
