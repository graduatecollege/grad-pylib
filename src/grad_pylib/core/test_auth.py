import asyncio
import dataclasses

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from grad_pylib.core.auth import (
    AuthConfiguration,
    AuthRuntimeConfig,
    AuthUser,
    BaseUser,
    build_auth_runtime,
    default_claims_to_user,
    parse_distinct_strings,
    parse_roles,
    require_policy,
)
from grad_pylib.core.config import BaseAppSettings

_CONFIG = AuthConfiguration(
    valid_roles=("Department", "Auditor", "Admin"),
    policy_roles={"Auditor": {"Auditor"}},
)


def _settings() -> BaseAppSettings:
    return BaseAppSettings(
        environment="test",
        enable_dev_api_key=True,
        dev_api_key="secret",
    )


def _request(
        *,
        api_key: str | None = "secret",
        api_role: str | None = "Department",
        client: tuple[str, int] | None = ("127.0.0.1", 51234),
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if api_key is not None:
        headers.append((b"api-key", api_key.encode()))
    if api_role is not None:
        headers.append((b"api-role", api_role.encode()))
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/things",
        "headers": headers,
        "client": client,
    })


def _override_loader(seen_session: list[object]):
    def loader(user: BaseUser, active_session: object) -> BaseUser:
        seen_session.append(active_session)
        return user.with_roles_override(["Auditor"])

    return loader


def _dependency(
        seen_session: list[object],
        settings: BaseAppSettings | None = None,
        policy: str = "Auditor",
):
    resolved = settings or _settings()
    return require_policy(
        policy,
        config=_CONFIG,
        azure_scheme=lambda: None,
        get_settings=lambda: resolved,
        get_session=lambda: None,
        forbidden_error_factory=PermissionError,
        claims_to_user=lambda claims: BaseUser(email=str(claims.get("upn") or "")),
        override_loader=_override_loader(seen_session),
        dev_api_key_enabled=lambda _settings: True,
        api_key_user_builder=lambda _role, _request: BaseUser(
            email="user@illinois.edu",
            roles=("Department",),
        ),
    )


def test_base_user_netid_is_shared() -> None:
    assert BaseUser(email="ABC123@Illinois.edu").netid == "abc123"
    assert BaseUser(email="abc123@example.com").netid is None


def test_base_user_is_immutable() -> None:
    user = BaseUser(email="abc123@illinois.edu", roles=("Department",))

    with pytest.raises(dataclasses.FrozenInstanceError):
        user.roles_override = ("Admin",)  # ty: ignore[invalid-assignment]

    assert user.roles == ("Department",)
    assert user.with_roles_override(["Admin"]).effective_roles == ("Admin",)
    assert user.effective_roles == ("Department",)


def test_base_user_subclasses_need_no_normalization_hook() -> None:
    @dataclasses.dataclass(frozen=True, slots=True)
    class DepartmentUser(BaseUser):
        departments: tuple[str, ...] = ()

    user = DepartmentUser(email="abc123@illinois.edu", roles=("Department",), departments=("1434",))

    assert user.departments == ("1434",)
    assert isinstance(user.with_roles_override(["Admin"]), DepartmentUser)


def test_base_user_satisfies_the_auth_user_protocol() -> None:
    user: AuthUser = BaseUser(email="abc123@illinois.edu", roles=("Department",))

    assert user.effective_roles == ("Department",)


def test_parse_helpers_normalize_at_the_boundary() -> None:
    assert parse_roles([" auditor ", "AUDITOR", "nope"], ("Auditor",)) == ("Auditor",)
    assert parse_distinct_strings([" a ", "a", "", "b"]) == ("a", "b")


def test_auth_configuration_is_deep_frozen() -> None:
    roles = {"Auditor"}
    config = AuthConfiguration(valid_roles=("Auditor",), policy_roles={"Auditor": roles})

    roles.add("Admin")

    assert config.policy_roles["Auditor"] == frozenset({"Auditor"})
    with pytest.raises(TypeError):
        config.policy_roles["Auditor"] = {"Admin"}  # ty: ignore[invalid-assignment]
    with pytest.raises(ValidationError):
        config.api_key_header = "X-Key"  # ty: ignore[invalid-assignment]


def test_auth_configuration_canonicalizes_policy_role_casing() -> None:
    config = AuthConfiguration(valid_roles=["Auditor", "Admin"], policy_roles={"reports": ["auditor"]})

    assert config.valid_roles == ("Auditor", "Admin")
    assert config.policy_roles["reports"] == frozenset({"Auditor"})


def test_auth_configuration_rejects_unknown_policy_roles() -> None:
    with pytest.raises(ValidationError, match="unknown role 'Editor'"):
        AuthConfiguration(valid_roles=("Auditor",), policy_roles={"reports": {"Editor"}})


def test_auth_configuration_rejects_empty_policies() -> None:
    with pytest.raises(ValidationError, match="no roles configured"):
        AuthConfiguration(valid_roles=("Auditor",), policy_roles={"reports": set()})


def test_auth_configuration_rejects_a_bare_string_of_roles() -> None:
    with pytest.raises(ValidationError):
        AuthConfiguration(valid_roles="Auditor", policy_roles={})


def test_require_policy_rejects_an_unconfigured_policy() -> None:
    with pytest.raises(ValueError, match="is not configured"):
        _dependency([], policy="Unknown")


def test_dev_api_key_path_applies_override_loader_without_azure_user() -> None:
    seen_session: list[object] = []
    session = object()

    user = _dependency(seen_session)(request=_request(), session=session, azure_user=None)

    assert user.effective_roles == ("Auditor",)
    assert seen_session == [session]


def test_dev_api_key_stores_only_the_application_user_on_request_state() -> None:
    request = _request()

    user = _dependency([])(request=request, session=object(), azure_user=None)

    assert request.state.user is user


def test_invalid_dev_api_key_is_rejected_instead_of_falling_through() -> None:
    with pytest.raises(HTTPException) as error:
        _dependency([])(request=_request(api_key="wrong"), session=object(), azure_user=None)

    assert error.value.status_code == 401


def test_non_ascii_dev_api_key_is_rejected_with_401() -> None:
    with pytest.raises(HTTPException) as error:
        _dependency([])(request=_request(api_key="sécret"), session=object(), azure_user=None)

    assert error.value.status_code == 401


def test_dev_api_key_is_rejected_from_remote_clients() -> None:
    with pytest.raises(HTTPException) as error:
        _dependency([])(request=_request(client=("203.0.113.7", 4444)), session=object(), azure_user=None)

    assert error.value.status_code == 401


def test_dev_api_key_is_rejected_when_client_address_is_unknown() -> None:
    with pytest.raises(HTTPException) as error:
        _dependency([])(request=_request(client=None), session=object(), azure_user=None)

    assert error.value.status_code == 401


def test_dev_api_key_is_rejected_outside_development() -> None:
    settings = BaseAppSettings(environment="production")

    with pytest.raises(HTTPException) as error:
        _dependency([], settings)(request=_request(), session=object(), azure_user=None)

    assert error.value.status_code == 401


def test_missing_credentials_are_rejected_with_401() -> None:
    request = _request(api_key=None, api_role=None)

    with pytest.raises(HTTPException) as error:
        _dependency([])(request=request, session=object(), azure_user=None)

    assert error.value.status_code == 401


def test_default_claims_to_user_rejects_missing_upn_with_401() -> None:
    with pytest.raises(HTTPException) as error:
        default_claims_to_user({}, ("Auditor",))

    assert error.value.status_code == 401


def test_default_claims_to_user_rejects_non_institutional_domain() -> None:
    with pytest.raises(HTTPException) as error:
        default_claims_to_user({"upn": "someone@example.com"}, ("Auditor",))

    assert error.value.status_code == 401


def test_default_claims_to_user_accepts_institutional_upn() -> None:
    user = default_claims_to_user(
        {"upn": "abc123@illinois.edu", "given_name": "A", "family_name": "B", "roles": ["Auditor"]},
        ("Auditor",),
    )

    assert user.netid == "abc123"
    assert user.roles == ("Auditor",)


def test_build_auth_runtime_supports_placeholder_scheme_and_dev_api_key() -> None:
    settings = _settings()
    seen_session: list[object] = []
    session = object()

    auth = build_auth_runtime(
        AuthRuntimeConfig(
            config=_CONFIG,
            get_settings=lambda: settings,
            get_session=lambda: None,
            forbidden_error_factory=PermissionError,
            claims_to_user=lambda claims: BaseUser(email=str(claims.get("upn") or "")),
            override_loader=_override_loader(seen_session),
            dev_api_key_enabled=lambda _settings: True,
            api_key_user_builder=lambda _role, _request: BaseUser(
                email="user@illinois.edu",
                roles=("Department",),
            ),
            allow_dev_placeholder_ids=True,
        )
    )

    user = auth.require_policy("Auditor")(request=_request(), session=session, azure_user=None)

    assert user.effective_roles == ("Auditor",)
    assert seen_session == [session]
    asyncio.run(auth.load_azure_openid_config())
