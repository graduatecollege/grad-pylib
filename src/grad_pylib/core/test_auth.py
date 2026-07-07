from starlette.requests import Request

from grad_pylib.core.auth import AuthConfiguration, BaseUser, require_policy
from grad_pylib.core.config import BaseAppSettings


def test_dev_api_key_path_applies_override_loader_without_azure_user() -> None:
    settings = BaseAppSettings(
        environment="test",
        enable_dev_api_key=True,
        dev_api_key="secret",
    )
    seen_session: list[object] = []
    session = object()

    def override_loader(user: BaseUser, active_session: object) -> BaseUser:
        seen_session.append(active_session)
        user.roles_override = ["Auditor"]
        return user

    dependency = require_policy(
        "Auditor",
        config=AuthConfiguration(
            valid_roles=("Department", "Auditor", "Admin"),
            policy_roles={"Auditor": {"Auditor"}},
        ),
        azure_scheme=lambda: None,
        get_settings=lambda: settings,
        get_session=lambda: None,
        forbidden_error_factory=PermissionError,
        claims_to_user=lambda claims: BaseUser(email=str(claims.get("upn") or "")),
        override_loader=override_loader,
        dev_api_key_enabled=lambda _settings: True,
        api_key_user_builder=lambda _role, _request: BaseUser(
            email="user@illinois.edu",
            roles=["Department"],
        ),
    )

    request = Request(
        {
            "type": "http",
            "headers": [
                (b"api-key", b"secret"),
                (b"api-role", b"Department"),
            ],
        }
    )

    user = dependency(request=request, session=session, azure_user=None)

    assert user.effective_roles == ["Auditor"]
    assert seen_session == [session]