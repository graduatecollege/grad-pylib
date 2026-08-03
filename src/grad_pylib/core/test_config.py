from pathlib import Path

import pytest
from pydantic import ValidationError

from grad_pylib.core.config import BaseAppSettings, configure_settings_factory, get_settings


class _CustomSettings(BaseAppSettings):
    app_name: str = "Custom"
    database_url: str | None = "Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=App"


def test_configure_settings_factory_overrides_default_settings() -> None:
    configure_settings_factory(_CustomSettings)
    try:
        settings = get_settings()
        assert isinstance(settings, _CustomSettings)
        assert settings.app_name == "Custom"
        assert settings.database_url is not None
    finally:
        configure_settings_factory(BaseAppSettings)


def test_base_settings_azure_scope_helpers() -> None:
    settings = BaseAppSettings(
        azure_ad_client_id="client-id",
        azure_ad_scope_description="user_impersonation",
    )

    assert settings.azure_ad_scope_name == "api://client-id/user_impersonation"
    assert settings.azure_ad_scopes == {
        "api://client-id/user_impersonation": "user_impersonation",
    }


def _isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: str) -> None:
    (tmp_path / ".env").write_text("APP_NAME=From Dotenv\nDEV_API_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for name in ("APP_NAME", "DEV_API_KEY", "ENABLE_DEV_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENVIRONMENT", environment)


def test_dotenv_is_used_in_development(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_environment(tmp_path, monkeypatch, "development")

    settings = BaseAppSettings()

    assert settings.app_name == "From Dotenv"
    assert settings.dev_api_key == "from-dotenv"


def test_dotenv_is_ignored_outside_development(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_environment(tmp_path, monkeypatch, "production")

    settings = BaseAppSettings()

    assert settings.app_name != "From Dotenv"
    assert settings.dev_api_key is None


def test_dev_api_key_bypass_settings_are_rejected_outside_development() -> None:
    with pytest.raises(ValidationError, match="enable_dev_api_key must not be enabled outside a development"):
        BaseAppSettings(
            environment="production",
            enable_dev_api_key=True,
            dev_api_key="secret",
        )
