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
