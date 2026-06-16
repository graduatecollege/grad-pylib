import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any

import structlog
from fastapi import Depends, HTTPException, Request, Security, status
from fastapi_azure_auth import SingleTenantAzureAuthorizationCodeBearer
from fastapi_azure_auth.user import User as AzureUser
from sqlalchemy.orm import Session

from grad_pylib.core.config import BaseAppSettings

_logger = logging.getLogger(__name__)
_PLACEHOLDER_GUID = "00000000-0000-0000-0000-000000000000"


@dataclass(slots=True)
class CurrentUser:
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    roles: list[str] = field(default_factory=list)
    roles_override: list[str] = field(default_factory=list)
    attributes: dict[str, list[str]] = field(default_factory=dict)

    @property
    def effective_roles(self) -> list[str]:
        return self.roles_override or self.roles


@dataclass(frozen=True, slots=True)
class AuthConfiguration:
    valid_roles: tuple[str, ...]
    policy_roles: Mapping[str, set[str] | None]
    api_key_header: str = "Api-Key"
    api_role_header: str = "Api-Role"


type ClaimsToUser = Callable[[dict[str, Any]], Any]
type OverrideLoader = Callable[[Any, Session], Any]
type ApiKeyUserBuilder = Callable[[str | None, Request], Any]
type SettingsProvider = Callable[[], BaseAppSettings]
type SessionProvider = Callable[[], Any]


def build_azure_scheme(settings: BaseAppSettings) -> SingleTenantAzureAuthorizationCodeBearer:
    if not settings.azure_ad_client_id or not settings.azure_ad_tenant_id:
        raise ValueError(
            "Azure AD client ID and tenant ID must be set in the environment or settings."
        )
    return SingleTenantAzureAuthorizationCodeBearer(
        app_client_id=settings.azure_ad_client_id or _PLACEHOLDER_GUID,
        tenant_id=settings.azure_ad_tenant_id or _PLACEHOLDER_GUID,
        auto_error=False,
        scopes=settings.azure_ad_scopes or None,
    )


def normalize_role(value: str, valid_roles: tuple[str, ...]) -> str | None:
    normalized = value.strip().lower()
    for role in valid_roles:
        if role.lower() == normalized:
            return role
    return None


def parse_roles(values: list[str], valid_roles: tuple[str, ...]) -> list[str]:
    parsed: list[str] = []
    seen: set[str] = set()
    for value in values:
        role = normalize_role(str(value), valid_roles)
        if not role or role in seen:
            continue
        parsed.append(role)
        seen.add(role)
    return parsed


def claim_list(claims: dict[str, Any], name: str) -> list[str]:
    value = claims.get(name) or []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def default_claims_to_user(claims: dict[str, Any], valid_roles: tuple[str, ...]) -> CurrentUser:
    email = claims.get("email") or claims.get("preferred_username") or claims.get("upn") or ""
    return CurrentUser(
        email=email,
        first_name=claims.get("given_name") or claims.get("name") or "",
        last_name=claims.get("family_name") or "",
        roles=parse_roles(claim_list(claims, "roles") or claim_list(claims, "role"), valid_roles),
    )


def azure_user_to_current_user(
        user: AzureUser,
        *,
        claims_to_user: ClaimsToUser,
) -> CurrentUser:
    claims = dict(user.claims)
    if user.roles:
        claims["roles"] = user.roles
    return claims_to_user(claims)


def require_policy(
        policy: str,
        *,
        config: AuthConfiguration,
        azure_scheme: SingleTenantAzureAuthorizationCodeBearer,
        get_settings: SettingsProvider,
        get_session: SessionProvider,
        forbidden_error_factory: Callable[[str], Exception],
        claims_to_user: ClaimsToUser,
        override_loader: OverrideLoader | None = None,
        dev_api_key_enabled: Callable[[Any], bool] | None = None,
        api_key_user_builder: ApiKeyUserBuilder | None = None,
):
    required_roles = config.policy_roles.get(policy)

    def dependency(
            request: Request,
            session: Annotated[Session, Depends(get_session)],
            azure_user: Annotated[AzureUser | None, Security(azure_scheme)],
    ) -> Any:
        start_time = time.perf_counter()
        settings = get_settings()
        api_key = request.headers.get(config.api_key_header)

        if dev_api_key_enabled and api_key and dev_api_key_enabled(settings):
            dev_api_key = settings.dev_api_key
            if dev_api_key and secrets.compare_digest(api_key, dev_api_key) and api_key_user_builder:
                user = api_key_user_builder(request.headers.get(config.api_role_header), request)
                result = _evaluate_policy(user, policy, required_roles, forbidden_error_factory)
                duration = time.perf_counter() - start_time
                structlog.get_logger("performance.auth").info(
                    "auth_finished",
                    policy=policy,
                    type="api_key",
                    duration_ms=round(duration * 1000, 2),
                )
                return result

        if not azure_user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

        user = azure_user_to_current_user(azure_user, claims_to_user=claims_to_user)
        if override_loader:
            user = override_loader(user, session)
        result = _evaluate_policy(user, policy, required_roles, forbidden_error_factory)
        duration = time.perf_counter() - start_time
        structlog.get_logger("performance.auth").info(
            "auth_finished",
            policy=policy,
            type="azure_ad",
            duration_ms=round(duration * 1000, 2),
        )
        return result

    return dependency


def _evaluate_policy(
        user: Any,
        policy: str,
        required_roles: set[str] | None,
        forbidden_error_factory: Callable[[str], Exception],
) -> Any:
    roles = set(user.effective_roles)
    if required_roles is None or roles.intersection(required_roles):
        _logger.debug("Access granted: policy=%s roles=%s", policy, roles)
        return user

    _logger.debug("Access denied: policy=%s roles=%s", policy, roles)
    raise forbidden_error_factory("You do not have permission to perform this action.")
