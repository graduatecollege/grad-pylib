import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi_azure_auth import SingleTenantAzureAuthorizationCodeBearer
from fastapi_azure_auth.user import User as AzureUser
from sqlalchemy.orm import Session

from grad_pylib.core.config import BaseAppSettings

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BaseUser:
    """
    Represents the current authenticated user.

    Attributes:
        email (str): The email address of the user.
        first_name (str): The first name of the user.
        last_name (str): The last name of the user.
        roles (list[str]): A list of roles assigned to the user.
        roles_override (list[str]): A list of roles that override the user's assigned roles.
        attributes (dict[str, list[str]]): A dictionary of user attributes.
    """
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
    """
    Represents the configuration settings for authentication.

    This class provides a structured format to define and manage
    authentication-related configurations including roles, policies,
    and specific API header fields.

    Api-Key should only be used for development and testing purposes.

    Attributes:
        valid_roles (tuple[str, ...]): A tuple of valid role names.
        policy_roles (Mapping[str, set[str] | None]): A mapping of policy names to sets of role names.
        api_key_header (str): The header name for the API key.
        api_role_header (str): The header name for the API role.
    """
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
    """
    Builds and returns an Azure authorization scheme configured for single-tenant authentication.

    Parameters:
        settings (BaseAppSettings): The application settings object containing the Azure AD
            configuration. Must have valid `azure_ad_client_id` and `azure_ad_tenant_id` attributes.
    """
    if not settings.azure_ad_client_id or not settings.azure_ad_tenant_id:
        raise ValueError(
            "Azure AD client ID and tenant ID must be set in the environment or settings."
        )
    return SingleTenantAzureAuthorizationCodeBearer(
        app_client_id=settings.azure_ad_client_id,
        tenant_id=settings.azure_ad_tenant_id,
        auto_error=not settings.is_development,
        scopes=settings.azure_ad_scopes,
    )


def normalize_role(value: str, valid_roles: tuple[str, ...]) -> str | None:
    """
    Normalize a role value to match a valid role if possible.

    This function takes a role value as a string, strips leading and trailing
    whitespace, converts it to lowercase, and checks if it matches any of the
    valid roles provided.

    Parameters:
        value: str
            The role value to normalize.
        valid_roles: tuple[str, ...]
            A tuple containing the valid roles to compare against.

    Returns:
        str | None
            The matched valid role from the valid_roles tuple if a match is found,
            otherwise None.
    """
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


def default_claims_to_user(claims: dict[str, Any], valid_roles: tuple[str, ...]) -> BaseUser:
    """
    Converts Azure AD claims to a CurrentUser object.

    The function processes claims from Azure AD, extracts the user's email (UPN),
    first name, last name, and roles. It validates the 'upn' field to ensure it
    is a non-empty string and returns a CurrentUser instance.

    Parameters:
        claims (dict[str, Any]): A dictionary of claims received from Azure AD.
        valid_roles (tuple[str, ...]): A tuple containing valid role names to
            filter and assign to the user.

    Returns:
        BaseUser: An instance representing the user with extracted details.
    """
    email = claims.get("upn")
    if not isinstance(email, str) or not email:
        raise ValueError("Azure AD user must have a UPN (email) claim.")
    return BaseUser(
        email=email,
        first_name=claims.get("given_name") or claims.get("name") or "",
        last_name=claims.get("family_name") or "",
        roles=parse_roles(claim_list(claims, "roles") or claim_list(claims, "role"), valid_roles),
    )


def azure_user_to_current_user(
        user: AzureUser,
        *,
        claims_to_user: ClaimsToUser,
) -> BaseUser:
    claims = dict(user.claims)
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
    """
    Provides a dependency function enforcing authorization policies through roles.

    This function dynamically constructs a dependency to validate if the user
    associated with the current request has the required roles to satisfy a
    given policy. Policies are defined in the `AuthConfiguration`.

    **Api-Key** should only be used for development purposes. Make sure to set
    ENVIRONMENT=production in Dockerfile to prevent using the dev Api-Key in production.

    Parameters:
        policy: Name of the policy to validate. The policy must be present in the
            `AuthConfiguration` and should have roles associated with it.

        config: The authentication configuration object containing required
            authorization-related settings.

        azure_scheme: The Azure AD Bearer token scheme object for performing
            user authentication.

        get_settings: A callable that retrieves application settings, such as
            environment configuration and development API key.

        get_session: A callable providing access to the database session for the
            current request.

        forbidden_error_factory: A callable that takes a role name as input and
            produces an exception to be raised if the user lacks the required role.

        claims_to_user: A callable that maps claims from a token to a user
            representation used within the application.

        override_loader: Optional. A callable that can modify or replace the
            current user object using information from the active database session.

        dev_api_key_enabled: Optional. A callable evaluating whether development
            API key-based authentication is enabled for a given application
            configuration.

        api_key_user_builder: Optional. A callable that generates a user object
            when a valid API key and associated role are provided in the request.

    Returns:
        A dependency callable which can be used within a framework like FastAPI
        to enforce role-based access control for endpoints.
    """
    policy_roles = config.policy_roles.get(policy)

    if policy_roles is None:
        raise ValueError(f"Policy '{policy}' is not configured.")
    if not policy_roles:
        raise ValueError(f"Policy '{policy}' has no roles configured.")

    # This helps the type checker narrow the type of required_roles
    required_roles = policy_roles

    def dependency(
            request: Request,
            session: Annotated[Session, Depends(get_session)],
            azure_user: Annotated[AzureUser | None, Security(azure_scheme)],
    ) -> Any:
        settings = get_settings()
        api_key = request.headers.get(config.api_key_header)

        # It's recommended to set ENVIRONMENT=production in Dockerfile to
        # prevent using the dev Api-Key in production
        if (
                dev_api_key_enabled
                and api_key
                and settings.is_development
                and dev_api_key_enabled(settings)
        ):
            dev_api_key = settings.dev_api_key
            if dev_api_key and secrets.compare_digest(api_key, dev_api_key) and api_key_user_builder:
                user = api_key_user_builder(request.headers.get(config.api_role_header), request)
                result = _evaluate_policy(user, policy, required_roles, forbidden_error_factory)
                _logger.warning("Using development API key for policy: %s", policy)
                return result

        if not azure_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated",
                headers={"WWW-Authenticate": "Bearer"},
            )

        user = azure_user_to_current_user(azure_user, claims_to_user=claims_to_user)
        if override_loader:
            user = override_loader(user, session)
        result = _evaluate_policy(user, policy, required_roles, forbidden_error_factory)

        return result

    return dependency


def _evaluate_policy(
        user: Any,
        policy: str,
        required_roles: set[str],
        forbidden_error_factory: Callable[[str], Exception],
) -> Any:
    roles = set(user.effective_roles)

    if roles.intersection(required_roles):
        _logger.debug("Access granted: policy=%s roles=%s", policy, roles)
        return user

    _logger.info("Access denied: policy=%s roles=%s", policy, roles)
    raise forbidden_error_factory("You do not have permission to perform this action.")
