from collections.abc import Callable
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseAppSettings(BaseSettings):
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

    @property
    def is_development(self) -> bool:
        return self.environment.lower() in {"development", "local", "test"}

    @property
    def azure_ad_scope_name(self) -> str | None:
        if not self.azure_ad_client_id:
            return None
        return f"api://{self.azure_ad_client_id}/{self.azure_ad_scope_description}"

    @property
    def azure_ad_scopes(self) -> dict[str, str]:
        if not self.azure_ad_scope_name:
            return {}
        return {self.azure_ad_scope_name: self.azure_ad_scope_description}


_settings_factory: Callable[[], BaseAppSettings] = BaseAppSettings


def configure_settings_factory(factory: Callable[[], BaseAppSettings]) -> None:
    global _settings_factory
    _settings_factory = factory
    get_settings.cache_clear()


@lru_cache(maxsize=1)
def get_settings() -> BaseAppSettings:
    return _settings_factory()
