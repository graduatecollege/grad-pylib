import asyncio
import dataclasses
from types import SimpleNamespace
from typing import Annotated

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from fastapi_azure_auth import SingleTenantAzureAuthorizationCodeBearer
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.requests import Request

from grad_pylib.core import auth as auth_module
from grad_pylib.core.auth import (
    AuthAppFactory,
    AuthConfiguration,
    AuthRuntimeConfig,
    BaseUser,
    build_auth_runtime,
    constant_time_equals,
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


def _azure_scheme() -> SingleTenantAzureAuthorizationCodeBearer:
    return SingleTenantAzureAuthorizationCodeBearer(
        app_client_id="client-id",
        tenant_id="tenant-id",
        auto_error=False,
        scopes={},
    )


def _claims_to_basic_user(claims: dict[str, object]) -> BaseUser:
    return BaseUser(email=str(claims.get("upn") or ""))


def _department_api_key_user(_role: str | None, _request: Request) -> BaseUser:
    return BaseUser(
        email="user@illinois.edu",
        roles=("Department",),
    )


def _auditor_override(user: auth_module.AuthUser) -> auth_module.AuthUser:
    if not isinstance(user, BaseUser):
        raise TypeError("test override loader expects BaseUser instances")
    return user.with_roles_override(["Auditor"])


@dataclasses.dataclass(slots=True)
class _AuditorOverrideLoader:
    seen_session: list[object]

    def __call__(self, user: auth_module.AuthUser, active_session: Session) -> auth_module.AuthUser:
        self.seen_session.append(active_session)
        return _auditor_override(user)


def _override_loader(seen_session: list[object]) -> auth_module.OverrideLoader:
    return _AuditorOverrideLoader(seen_session)


def _replacement_override(_user: auth_module.AuthUser, _session: Session) -> auth_module.AuthUser:
    return BaseUser(
        email="override@illinois.edu",
        roles=("Auditor",),
    )


def _empty_roles_override(user: auth_module.AuthUser, _session: Session) -> auth_module.AuthUser:
    if not isinstance(user, BaseUser):
        raise TypeError("test override loader expects BaseUser instances")
    return user.with_roles_override([])


def _policy_dependency(
    seen_session: list[object],
    settings: BaseAppSettings | None = None,
    policy: str = "Auditor",
    *,
    claims_to_user: auth_module.ClaimsToUser | None = None,
    override_loader: auth_module.OverrideLoader | None = None,
    api_key_user_builder: auth_module.ApiKeyUserBuilder | None = None,
):
    resolved = settings or _settings()
    return require_policy(
        policy,
        config=_CONFIG,
        azure_scheme=_azure_scheme(),
        get_settings=lambda: resolved,
        get_session=lambda: None,
        forbidden_error_factory=PermissionError,
        claims_to_user=claims_to_user or _claims_to_basic_user,
        override_loader=override_loader or _override_loader(seen_session),
        dev_api_key_enabled=lambda _settings: True,
        api_key_user_builder=api_key_user_builder or _department_api_key_user,
    )


def _auth_factory(
    settings: BaseAppSettings | None = None,
    *,
    claims_to_user: auth_module.ClaimsToUser | None = None,
    override_loader: auth_module.OverrideLoader | None = None,
    api_key_user_builder: auth_module.ApiKeyUserBuilder | None = None,
) -> AuthAppFactory:
    resolved = settings or _settings()
    return AuthAppFactory.configure(
        config=_CONFIG,
        get_settings=lambda: resolved,
        get_session=lambda: None,
        forbidden_error_factory=PermissionError,
        claims_to_user=claims_to_user or _claims_to_basic_user,
        override_loader=override_loader,
        dev_api_key_enabled=lambda _settings: True,
        api_key_user_builder=api_key_user_builder or _department_api_key_user,
        allow_dev_placeholder_ids=True,
    )


class _AuditRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def warning(self, event: str, /, **fields: object) -> None:
        self.events.append(("warning", event, fields))

    def info(self, event: str, /, **fields: object) -> None:
        self.events.append(("info", event, fields))


@pytest.fixture
def audit_recorder(monkeypatch: pytest.MonkeyPatch) -> _AuditRecorder:
    audit = _AuditRecorder()
    monkeypatch.setattr(auth_module, "_audit_logger", audit)
    return audit


def test_base_user_netid_is_derived_from_institutional_email() -> None:
    assert BaseUser(email="ABC123@Illinois.edu").netid == "abc123"
    assert BaseUser(email="abc123@example.com").netid is None


def test_base_user_is_immutable() -> None:
    user = BaseUser(email="abc123@illinois.edu", roles=("Department",))

    with pytest.raises(dataclasses.FrozenInstanceError):
        # noinspection dataclass
        user.roles_override = ("Admin",)  # ty: ignore[invalid-assignment]

    assert user.roles == ("Department",)
    assert user.with_roles_override(["Admin"]).effective_roles == ("Admin",)
    assert user.effective_roles == ("Department",)


def test_base_user_allows_overrides_to_revoke_all_roles() -> None:
    user = BaseUser(email="abc123@illinois.edu", roles=("Department",))

    overridden = user.with_roles_override([])

    assert overridden.roles == ("Department",)
    assert overridden.effective_roles == ()


def test_base_user_subclasses_need_no_normalization_hook() -> None:
    @dataclasses.dataclass(frozen=True, slots=True)
    class DepartmentUser(BaseUser):
        departments: tuple[str, ...] = ()

    user = DepartmentUser(email="abc123@illinois.edu", roles=("Department",), departments=("1434",))

    assert user.departments == ("1434",)
    assert isinstance(user.with_roles_override(["Admin"]), DepartmentUser)


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
        _policy_dependency([], policy="Unknown")


def test_dev_api_key_path_applies_override_loader_without_azure_user() -> None:
    seen_session: list[object] = []
    session = object()

    user = _policy_dependency(seen_session)(request=_request(), session=session, azure_user=None)

    assert user.effective_roles == ("Auditor",)
    assert seen_session == [session]


def test_dev_api_key_stores_only_the_application_user_on_request_state() -> None:
    request = _request()

    user = _policy_dependency([])(request=request, session=object(), azure_user=None)

    assert request.state.user is user


@pytest.mark.parametrize(
    ("auth_request", "settings"),
    [
        pytest.param(_request(api_key="wrong"), None, id="wrong-key"),
        pytest.param(_request(api_key="sécret"), None, id="non-ascii-key"),
        pytest.param(_request(client=("203.0.113.7", 4444)), None, id="remote-client"),
        pytest.param(_request(client=None), None, id="missing-client-address"),
        pytest.param(_request(), BaseAppSettings(environment="production"), id="production-environment"),
    ],
)
def test_dev_api_key_rejections_return_401(
    auth_request: Request,
    settings: BaseAppSettings | None,
) -> None:
    with pytest.raises(HTTPException) as error:
        _policy_dependency([], settings)(request=auth_request, session=object(), azure_user=None)

    assert error.value.status_code == 401


def test_missing_credentials_are_rejected_with_401() -> None:
    request = _request(api_key=None, api_role=None)

    with pytest.raises(HTTPException) as error:
        _policy_dependency([])(request=request, session=object(), azure_user=None)

    assert error.value.status_code == 401


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({}, id="missing-upn"),
        pytest.param({"upn": "someone@example.com"}, id="non-institutional-domain"),
    ],
)
def test_default_claims_to_user_rejects_invalid_identities(claims: dict[str, object]) -> None:
    with pytest.raises(HTTPException) as error:
        default_claims_to_user(claims, ("Auditor",))

    assert error.value.status_code == 401


def test_default_claims_to_user_accepts_institutional_upn() -> None:
    user = default_claims_to_user(
        {"upn": "abc123@illinois.edu", "given_name": "A", "family_name": "B", "roles": ["Auditor"]},
        ("Auditor",),
    )

    assert user.netid == "abc123"
    assert user.roles == ("Auditor",)


def test_default_claims_to_user_ignores_malformed_role_claims() -> None:
    user = default_claims_to_user(
        {"upn": "abc123@illinois.edu", "roles": {"Auditor": True}, "role": 7},
        ("Auditor",),
    )

    assert user.roles == ()


def test_constant_time_equals_rejects_malformed_unicode_input() -> None:
    assert constant_time_equals("\ud800", "secret") is False
    assert constant_time_equals("secret", "\ud800") is False


def test_require_policy_denies_insufficient_roles_and_audits_the_denial(
    audit_recorder: _AuditRecorder,
) -> None:
    dependency = require_policy(
        "Auditor",
        config=_CONFIG,
        azure_scheme=_azure_scheme(),
        get_settings=_settings,
        get_session=lambda: None,
        forbidden_error_factory=PermissionError,
        claims_to_user=lambda claims: default_claims_to_user(claims, _CONFIG.valid_roles),
    )
    request = _request(api_key=None, api_role=None)

    with pytest.raises(PermissionError, match="do not have permission"):
        dependency(
            request=request,
            session=object(),
            azure_user=SimpleNamespace(claims={"upn": "abc123@illinois.edu", "roles": ["Department"]}),
        )

    assert isinstance(request.state.user, BaseUser)
    assert request.state.user.roles == ("Department",)
    assert audit_recorder.events[-1] == (
        "info",
        "auth.access.denied",
        {
            "policy": "Auditor",
            "mechanism": "azure_ad",
            "subject": "abc123",
            "roles": ["Department"],
            "client_host": "127.0.0.1",
            "http_method": "GET",
            "http_path": "/things",
        },
    )


def test_override_loader_can_replace_identity_and_audits_the_override(
    audit_recorder: _AuditRecorder,
) -> None:
    dependency = require_policy(
        "Auditor",
        config=_CONFIG,
        azure_scheme=_azure_scheme(),
        get_settings=_settings,
        get_session=lambda: None,
        forbidden_error_factory=PermissionError,
        claims_to_user=lambda claims: default_claims_to_user(claims, _CONFIG.valid_roles),
        override_loader=_replacement_override,
    )
    request = _request(api_key=None, api_role=None)

    user = dependency(
        request=request,
        session=object(),
        azure_user=SimpleNamespace(claims={"upn": "abc123@illinois.edu", "roles": ["Department"]}),
    )

    assert user.email == "override@illinois.edu"
    assert request.state.user is user
    assert audit_recorder.events[0] == (
        "warning",
        "auth.roles.overridden",
        {
            "policy": "Auditor",
            "mechanism": "azure_ad",
            "subject": "abc123",
            "effective_subject": "override",
            "original_roles": ["Department"],
            "effective_roles": ["Auditor"],
            "client_host": "127.0.0.1",
            "http_method": "GET",
            "http_path": "/things",
        },
    )


def test_override_loader_can_revoke_all_roles_and_audits_the_override(
    audit_recorder: _AuditRecorder,
) -> None:
    dependency = require_policy(
        "Auditor",
        config=_CONFIG,
        azure_scheme=_azure_scheme(),
        get_settings=_settings,
        get_session=lambda: None,
        forbidden_error_factory=PermissionError,
        claims_to_user=lambda claims: default_claims_to_user(claims, _CONFIG.valid_roles),
        override_loader=_empty_roles_override,
    )
    request = _request(api_key=None, api_role=None)

    with pytest.raises(PermissionError, match="do not have permission"):
        dependency(
            request=request,
            session=object(),
            azure_user=SimpleNamespace(claims={"upn": "abc123@illinois.edu", "roles": ["Auditor"]}),
        )

    assert isinstance(request.state.user, BaseUser)
    assert request.state.user.effective_roles == ()
    assert audit_recorder.events[0] == (
        "warning",
        "auth.roles.overridden",
        {
            "policy": "Auditor",
            "mechanism": "azure_ad",
            "subject": "abc123",
            "effective_subject": "abc123",
            "original_roles": ["Auditor"],
            "effective_roles": [],
            "client_host": "127.0.0.1",
            "http_method": "GET",
            "http_path": "/things",
        },
    )
    assert audit_recorder.events[-1] == (
        "info",
        "auth.access.denied",
        {
            "policy": "Auditor",
            "mechanism": "azure_ad",
            "subject": "abc123",
            "roles": [],
            "client_host": "127.0.0.1",
            "http_method": "GET",
            "http_path": "/things",
        },
    )


def test_auth_audit_logs_exclude_raw_claims_and_tokens(audit_recorder: _AuditRecorder) -> None:
    dependency = require_policy(
        "Auditor",
        config=_CONFIG,
        azure_scheme=_azure_scheme(),
        get_settings=_settings,
        get_session=lambda: None,
        forbidden_error_factory=PermissionError,
        claims_to_user=lambda claims: default_claims_to_user(claims, _CONFIG.valid_roles),
    )

    dependency(
        request=_request(api_key=None, api_role=None),
        session=object(),
        azure_user=SimpleNamespace(claims={
            "upn": "abc123@illinois.edu",
            "roles": ["Auditor"],
            "access_token": "live-bearer-token",
            "id_token": "raw-id-token",
        }),
    )

    assert audit_recorder.events[-1][0] == "info"
    assert audit_recorder.events[-1][1] == "auth.access.granted"
    assert audit_recorder.events[-1][2]["subject"] == "abc123"
    assert audit_recorder.events[-1][2]["roles"] == ["Auditor"]
    for _, _, fields in audit_recorder.events:
        assert "access_token" not in fields
        assert "id_token" not in fields
        assert "claims" not in fields
        assert "authorization" not in fields
        assert "headers" not in fields


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


def test_auth_app_factory_builds_runtime_and_simple_consumer_dependency_helper() -> None:
    seen_session: list[object] = []
    session = object()
    auth = _auth_factory(override_loader=_override_loader(seen_session)).runtime()

    user = auth.require_policy("Auditor")(request=_request(), session=session, azure_user=None)

    assert isinstance(auth.azure_scheme, SingleTenantAzureAuthorizationCodeBearer)
    assert user.effective_roles == ("Auditor",)
    assert seen_session == [session]
    asyncio.run(auth.load_azure_openid_config())


def test_auth_runtime_depends_on_works_in_fastapi_routes() -> None:
    auth = _auth_factory(
        api_key_user_builder=lambda _role, _request: BaseUser(
            email="user@illinois.edu",
            roles=("Auditor",),
        ),
    ).runtime()
    app = FastAPI()

    @app.get("/me")
    def read_me(
        request: Request,
        user: Annotated[BaseUser, auth.depends_on("Auditor")],
    ) -> dict[str, object]:
        return {
            "email": user.email,
            "effective_roles": list(user.effective_roles),
            "request_state_email": request.state.user.email,
        }

    with TestClient(app) as client:
        response = client.get("/me", headers={"Api-Key": "secret", "Api-Role": "Auditor"})

    assert isinstance(auth.azure_scheme, SingleTenantAzureAuthorizationCodeBearer)
    assert response.status_code == 200
    assert response.json() == {
        "email": "user@illinois.edu",
        "effective_roles": ["Auditor"],
        "request_state_email": "user@illinois.edu",
    }


def test_auth_app_factory_supports_override_aware_and_override_free_runtimes() -> None:
    seen_session: list[object] = []
    factory = _auth_factory()
    auth_without_override = factory.without_overrides()
    auth_with_override = factory.with_overrides(_override_loader(seen_session))
    azure_user = SimpleNamespace(claims={"upn": "abc123@illinois.edu", "roles": ["Department"]})

    with pytest.raises(PermissionError, match="do not have permission"):
        auth_without_override.require_policy("Auditor")(
            request=_request(api_key=None, api_role=None),
            session=object(),
            azure_user=azure_user,
        )

    session = object()
    user = auth_with_override.require_policy("Auditor")(
        request=_request(api_key=None, api_role=None),
        session=session,
        azure_user=azure_user,
    )

    assert user.effective_roles == ("Auditor",)
    assert seen_session == [session]
