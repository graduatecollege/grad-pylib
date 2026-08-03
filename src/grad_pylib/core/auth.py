import logging
import secrets
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Annotated, Any, Protocol, Self

import structlog
from fastapi import Depends, HTTPException, Request, Security, status
from fastapi_azure_auth import SingleTenantAzureAuthorizationCodeBearer
from fastapi_azure_auth.user import User as AzureUser
from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationInfo
from sqlalchemy.orm import Session

from grad_pylib.core.config import BaseAppSettings

_logger = logging.getLogger(__name__)

AUDIT_LOGGER_NAME = "grad_pylib.audit.auth"
"""Name of the logger used for authentication audit events."""

_audit_logger = structlog.get_logger(AUDIT_LOGGER_NAME)

INSTITUTIONAL_EMAIL_DOMAINS = frozenset({"illinois.edu", "uillinois.edu"})
"""Email domains accepted as institutional identities. Other domains are rejected."""

LOOPBACK_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
"""Client hosts allowed to authenticate with the development API key."""

MECHANISM_AZURE_AD = "azure_ad"
MECHANISM_DEV_API_KEY = "dev_api_key"

EMPTY_ATTRIBUTES: Mapping[str, tuple[str, ...]] = MappingProxyType({})
"""The default (empty, read-only) attribute mapping of a user."""


class AuthUser(Protocol):
    """
    The contract this library requires of an authenticated user.

    Authorization only ever needs the effective roles, so consuming applications are free
    to model their user however they like -- dataclass, pydantic model, or anything else --
    as long as it satisfies this protocol. :class:`BaseUser` is a ready-made implementation,
    but subclassing it is a convenience rather than a requirement.

    Implementations must be immutable: authorization decisions are made from this object,
    so a request handler (or a cached reference to it) must not be able to change the roles
    after the fact.
    """

    @property
    def effective_roles(self) -> Sequence[str]:
        """The roles the user is authorized with for this request."""
        ...


@dataclass(frozen=True, slots=True)
class BaseUser:
    """
    A ready-made immutable :class:`AuthUser` implementation.

    The fields are stored exactly as declared: sequences must already be tuples. Untrusted
    input is normalized where it enters the application -- see :func:`parse_roles` and
    :func:`parse_distinct_strings` -- rather than being coerced by the constructor, so
    subclasses need no cooperation from this class and the annotations describe what is
    really stored.

    Attributes:
        email (str): The email address of the user.
        first_name (str): The first name of the user.
        last_name (str): The last name of the user.
        roles (tuple[str, ...]): The roles assigned to the user by the identity provider.
        roles_override (tuple[str, ...]): Roles that override the user's assigned roles.
        attributes (Mapping[str, tuple[str, ...]]): A read-only mapping of user attributes.
    """
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    roles: tuple[str, ...] = ()
    roles_override: tuple[str, ...] = ()
    attributes: Mapping[str, tuple[str, ...]] = field(default=EMPTY_ATTRIBUTES)

    @property
    def effective_roles(self) -> tuple[str, ...]:
        return self.roles_override or self.roles

    @property
    def netid(self) -> str | None:
        """The institutional netid, or None when the email is not an institutional address."""
        return netid_from_email(self.email)

    def with_roles_override(self, roles: Sequence[str]) -> Self:
        """Returns a copy of this user with the given override roles applied."""
        return replace(self, roles_override=tuple(roles))


def _normalize_valid_roles(value: tuple[str, ...]) -> tuple[str, ...]:
    roles = tuple(dict.fromkeys(role.strip() for role in value))
    if any(not role for role in roles):
        raise ValueError("valid_roles must not contain empty role names.")
    return roles


def _normalize_policy_roles(
        value: Mapping[str, frozenset[str]],
        info: ValidationInfo,
) -> Mapping[str, frozenset[str]]:
    """Canonicalizes policy roles against ``valid_roles`` and deep-freezes the mapping.

    Token roles are canonicalized through :func:`normalize_role`, so a policy naming a role
    with different casing -- or a typo -- would silently never match and lock the endpoint
    out permanently. Both are rejected here, at startup, instead.
    """
    valid_roles: tuple[str, ...] = info.data.get("valid_roles", ())
    normalized: dict[str, frozenset[str]] = {}
    for policy, roles in value.items():
        if not roles:
            raise ValueError(f"Policy '{policy}' has no roles configured.")
        resolved: set[str] = set()
        for role in roles:
            match = normalize_role(role, valid_roles)
            if match is None:
                raise ValueError(
                    f"Policy '{policy}' references unknown role '{role}'. "
                    f"Valid roles are: {', '.join(valid_roles)}."
                )
            resolved.add(match)
        normalized[policy] = frozenset(resolved)
    return MappingProxyType(normalized)


class AuthConfiguration(BaseModel):
    """
    Represents the configuration settings for authentication.

    This class provides a structured format to define and manage
    authentication-related configurations including roles, policies,
    and specific API header fields.

    Api-Key should only be used for development and testing purposes.

    A policy grants access when the user holds *any* of the roles configured for it.

    The configuration is validated and deep-frozen on construction: policy roles must name
    roles from `valid_roles` (matched case-insensitively and stored canonically), no policy
    may be empty, and a caller that keeps a reference to the mapping or the role sets it
    passed in cannot change authorization decisions at runtime.

    Attributes:
        valid_roles (tuple[str, ...]): A tuple of valid role names.
        policy_roles (Mapping[str, frozenset[str]]): A mapping of policy names to role names.
        api_key_header (str): The header name for the API key.
        api_role_header (str): The header name for the API role.
        api_key_allowed_hosts (frozenset[str]): Client hosts allowed to use the development
            API key. Defaults to loopback addresses only.
    """
    model_config = ConfigDict(frozen=True)

    valid_roles: Annotated[tuple[str, ...], AfterValidator(_normalize_valid_roles)]
    policy_roles: Annotated[Mapping[str, frozenset[str]], AfterValidator(_normalize_policy_roles)]
    api_key_header: str = "Api-Key"
    api_role_header: str = "Api-Role"
    api_key_allowed_hosts: frozenset[str] = LOOPBACK_CLIENT_HOSTS


type ClaimsToUser = Callable[[dict[str, Any]], AuthUser]
type OverrideLoader = Callable[[AuthUser, Session], AuthUser]
type ApiKeyUserBuilder = Callable[[str | None, Request], AuthUser]
type SettingsProvider = Callable[[], BaseAppSettings]
type SessionProvider = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class AuthRuntimeConfig:
    config: AuthConfiguration
    get_settings: SettingsProvider
    get_session: SessionProvider
    forbidden_error_factory: Callable[[str], Exception]
    claims_to_user: ClaimsToUser
    override_loader: OverrideLoader | None = None
    dev_api_key_enabled: Callable[[Any], bool] | None = None
    api_key_user_builder: ApiKeyUserBuilder | None = None
    allow_dev_placeholder_ids: bool = False
    development_client_id: str = "development-client-id"
    development_tenant_id: str = "development-tenant-id"


@dataclass(frozen=True, slots=True)
class AuthRuntime:
    azure_scheme: SingleTenantAzureAuthorizationCodeBearer
    require_policy: Callable[[str], Any]
    load_azure_openid_config: Callable[[], Awaitable[None]]


def netid_from_email(email: str) -> str | None:
    """
    Extracts the netid from an institutional email address.

    Returns None when the address is missing, malformed, or belongs to a domain outside
    :data:`INSTITUTIONAL_EMAIL_DOMAINS`. Callers must not treat a missing netid as an
    identity: every non-institutional account would otherwise collapse onto the same
    empty value and could collide in caches, dictionaries or database filters.
    """
    normalized = email.strip().lower()
    if "@" not in normalized:
        return None
    netid, domain = normalized.split("@", 1)
    if domain not in INSTITUTIONAL_EMAIL_DOMAINS:
        return None
    return netid or None


def azure_ad_configured(settings: BaseAppSettings) -> bool:
    return bool(settings.azure_ad_client_id and settings.azure_ad_tenant_id)


def warn_if_azure_ad_missing(settings: BaseAppSettings) -> None:
    if settings.is_development or azure_ad_configured(settings):
        return

    _logger.warning(
        "Azure AD is not configured (client_id or tenant_id missing). "
        "Authentication will reject all requests in non-development environments."
    )


def with_azure_development_placeholders[SettingsT: BaseAppSettings](
        settings: SettingsT,
        *,
        development_client_id: str = "development-client-id",
        development_tenant_id: str = "development-tenant-id",
) -> SettingsT:
    if azure_ad_configured(settings) or not settings.is_development:
        return settings

    return settings.model_copy(update={
        "azure_ad_client_id": settings.azure_ad_client_id or development_client_id,
        "azure_ad_tenant_id": settings.azure_ad_tenant_id or development_tenant_id,
    })


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


async def load_azure_openid_config(
        azure_scheme: SingleTenantAzureAuthorizationCodeBearer,
        *,
        get_settings: SettingsProvider,
) -> None:
    if not azure_ad_configured(get_settings()):
        return
    await azure_scheme.openid_config.load_config()


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


def parse_roles(values: Collection[str], valid_roles: tuple[str, ...]) -> tuple[str, ...]:
    """Normalizes untrusted role values into the canonical, deduplicated tuple stored on a user."""
    parsed: list[str] = []
    seen: set[str] = set()
    for value in values:
        role = normalize_role(str(value), valid_roles)
        if not role or role in seen:
            continue
        parsed.append(role)
        seen.add(role)
    return tuple(parsed)


def parse_distinct_strings(
        values: Collection[str],
) -> tuple[str, ...]:
    """Strips, drops empties and deduplicates untrusted values, preserving their order."""
    parsed: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        parsed.append(normalized)
        seen.add(normalized)
    return tuple(parsed)


def claim_list(claims: dict[str, Any], name: str) -> list[str]:
    value = claims.get(name) or []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping | bytes | bytearray):
        return []
    try:
        iterator = iter(value)
    except TypeError:
        return []
    return [str(item) for item in iterator]


def claim_value(claims: dict[str, Any], *names: str) -> str:
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def unauthorized_error(detail: str = "Not authenticated") -> HTTPException:
    """Builds the 401 raised for every authentication failure.

    The detail is deliberately generic so that a caller cannot distinguish between an
    unknown identity, a rejected domain, and a bad API key.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def default_claims_to_user(claims: dict[str, Any], valid_roles: tuple[str, ...]) -> BaseUser:
    """
    Converts Azure AD claims to a BaseUser object.

    The function processes claims from Azure AD, extracts the user's email (UPN),
    first name, last name, and roles. Tokens without a UPN claim, and identities
    outside :data:`INSTITUTIONAL_EMAIL_DOMAINS`, are rejected with a 401 rather than
    surfacing as a 500.

    Parameters:
        claims (dict[str, Any]): A dictionary of claims received from Azure AD.
        valid_roles (tuple[str, ...]): A tuple containing valid role names to
            filter and assign to the user.

    Returns:
        BaseUser: An instance representing the user with extracted details.
    """
    email = claim_value(claims, "upn")
    if not email:
        _audit_logger.warning(
            "auth.token.rejected",
            reason="missing_upn_claim",
            mechanism=MECHANISM_AZURE_AD,
        )
        raise unauthorized_error()
    if netid_from_email(email) is None:
        _audit_logger.warning(
            "auth.token.rejected",
            reason="non_institutional_domain",
            mechanism=MECHANISM_AZURE_AD,
            subject=email,
        )
        raise unauthorized_error()
    return BaseUser(
        email=email,
        first_name=claim_value(claims, "given_name", "name"),
        last_name=claim_value(claims, "family_name"),
        roles=parse_roles(claim_list(claims, "roles") or claim_list(claims, "role"), valid_roles),
    )


def azure_user_to_current_user(
        user: AzureUser,
        *,
        claims_to_user: ClaimsToUser,
) -> AuthUser:
    claims = dict(user.claims)
    return claims_to_user(claims)


def client_host(request: Request) -> str | None:
    """Returns the peer address of the connection, ignoring forwarding headers."""
    client = request.scope.get("client")
    if not client:
        return None
    return client[0]


def is_allowed_api_key_client(request: Request, allowed_hosts: Collection[str]) -> bool:
    """
    Checks whether the development API key may be used from this connection.

    Only the real peer address is considered. Forwarding headers such as
    ``X-Forwarded-For`` are deliberately ignored because they are client-controlled,
    and a connection with no peer address is rejected.
    """
    host = client_host(request)
    if not host:
        return False
    return host.lower() in {item.lower() for item in allowed_hosts}


def constant_time_equals(presented: str, expected: str | None) -> bool:
    """Compares two secrets in constant time, tolerating non-ASCII input.

    ``secrets.compare_digest`` raises TypeError for non-ASCII strings, which would
    otherwise turn a hostile header into a 500.
    """
    if not expected:
        return False
    return secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def store_request_user(request: Request, user: AuthUser) -> None:
    """
    Replaces the upstream Azure user on the request state with the application user.

    ``fastapi_azure_auth`` stores its own user object -- including the raw access token
    and the full claim set -- on ``request.state.user``. Anything that serializes the
    request state (error handlers, APM integrations) would then leak a live bearer
    token, so only the application's own user object is kept.
    """
    request.state.user = user


def subject_of(user: AuthUser) -> str | None:
    """Best-effort subject identifier for audit records."""
    return getattr(user, "netid", None) or getattr(user, "email", None) or None


def _request_fields(request: Request) -> dict[str, Any]:
    return {
        "http_method": request.scope.get("method"),
        "http_path": request.scope.get("path"),
    }


def _audit_failure(request: Request, policy: str, mechanism: str, reason: str) -> None:
    _audit_logger.warning(
        "auth.failed",
        policy=policy,
        mechanism=mechanism,
        reason=reason,
        client_host=client_host(request),
        **_request_fields(request),
    )


def _apply_override(
        user: AuthUser,
        session: Any,
        request: Request,
        policy: str,
        mechanism: str,
        override_loader: OverrideLoader | None,
) -> AuthUser:
    if not override_loader:
        return user

    original_roles = tuple(getattr(user, "effective_roles", ()))
    user = override_loader(user, session)
    effective_roles = tuple(getattr(user, "effective_roles", ()))
    if effective_roles != original_roles:
        _audit_logger.warning(
            "auth.roles.overridden",
            policy=policy,
            mechanism=mechanism,
            subject=subject_of(user),
            original_roles=list(original_roles),
            effective_roles=list(effective_roles),
            client_host=client_host(request),
            **_request_fields(request),
        )
    return user


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

    **Api-Key** is a full authentication bypass with a caller-chosen role and exists only
    for local development and automated testing. It is accepted only when all of the
    following hold: the environment is a development environment, `dev_api_key_enabled`
    returns True, and the request originates from a loopback address. Any request that
    presents the API key header without satisfying those conditions is rejected with a
    401 and audited; it never falls through to Azure AD authentication.

    Every authentication decision is written to the `grad_pylib.audit.auth` logger.

    Parameters:
        policy: Name of the policy to validate. The policy must be present in the
            `AuthConfiguration`, which guarantees it names at least one valid role.

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
            This is a privilege-granting hook: it may replace the roles issued by the
            identity provider entirely. Every override that changes the effective roles
            is audited.

        dev_api_key_enabled: Optional. A callable evaluating whether development
            API key-based authentication is enabled for a given application
            configuration.

        api_key_user_builder: Optional. A callable that generates a user object
            when a valid API key and associated role are provided in the request.

    Returns:
        A dependency callable which can be used within a framework like FastAPI
        to enforce role-based access control for endpoints.
    """
    required_roles = config.policy_roles.get(policy)

    if required_roles is None:
        raise ValueError(f"Policy '{policy}' is not configured.")

    def authenticate_with_api_key(request: Request, session: Any, api_key: str) -> AuthUser:
        settings = get_settings()
        if not (
                dev_api_key_enabled
                and api_key_user_builder
                and settings.is_development
                and dev_api_key_enabled(settings)
        ):
            _audit_failure(request, policy, MECHANISM_DEV_API_KEY, "dev_api_key_disabled")
            raise unauthorized_error()

        if not is_allowed_api_key_client(request, config.api_key_allowed_hosts):
            _audit_failure(request, policy, MECHANISM_DEV_API_KEY, "non_local_client")
            raise unauthorized_error()

        if not constant_time_equals(api_key, settings.dev_api_key):
            _audit_failure(request, policy, MECHANISM_DEV_API_KEY, "invalid_api_key")
            raise unauthorized_error()

        role_header = request.headers.get(config.api_role_header)
        _audit_logger.warning(
            "auth.api_key.bypass",
            policy=policy,
            mechanism=MECHANISM_DEV_API_KEY,
            requested_role=role_header,
            client_host=client_host(request),
            **_request_fields(request),
        )
        user = api_key_user_builder(role_header, request)
        user = _apply_override(user, session, request, policy, MECHANISM_DEV_API_KEY, override_loader)
        store_request_user(request, user)
        return _evaluate_policy(
            user, policy, required_roles, forbidden_error_factory,
            request=request, mechanism=MECHANISM_DEV_API_KEY,
        )

    def dependency(
            request: Request,
            session: Annotated[Session, Depends(get_session)],
            azure_user: Annotated[AzureUser | None, Security(azure_scheme)],
    ) -> AuthUser:
        api_key = request.headers.get(config.api_key_header)

        # The dev Api-Key is a full bypass. It never falls through to Azure AD:
        # presenting it when it is not usable is an authentication failure.
        if api_key is not None:
            return authenticate_with_api_key(request, session, api_key)

        if not azure_user:
            _audit_failure(request, policy, MECHANISM_AZURE_AD, "missing_or_invalid_token")
            raise unauthorized_error()

        user = azure_user_to_current_user(azure_user, claims_to_user=claims_to_user)
        user = _apply_override(user, session, request, policy, MECHANISM_AZURE_AD, override_loader)
        store_request_user(request, user)
        return _evaluate_policy(
            user, policy, required_roles, forbidden_error_factory,
            request=request, mechanism=MECHANISM_AZURE_AD,
        )

    return dependency


def build_auth_runtime(runtime_config: AuthRuntimeConfig) -> AuthRuntime:
    settings = runtime_config.get_settings()
    warn_if_azure_ad_missing(settings)
    azure_settings = settings
    if runtime_config.allow_dev_placeholder_ids:
        azure_settings = with_azure_development_placeholders(
            settings,
            development_client_id=runtime_config.development_client_id,
            development_tenant_id=runtime_config.development_tenant_id,
        )
    azure_scheme = build_azure_scheme(azure_settings)

    async def load_openid_config() -> None:
        await load_azure_openid_config(azure_scheme, get_settings=runtime_config.get_settings)

    def build_policy(policy: str) -> Any:
        return require_policy(
            policy,
            config=runtime_config.config,
            azure_scheme=azure_scheme,
            get_settings=runtime_config.get_settings,
            get_session=runtime_config.get_session,
            forbidden_error_factory=runtime_config.forbidden_error_factory,
            claims_to_user=runtime_config.claims_to_user,
            override_loader=runtime_config.override_loader,
            dev_api_key_enabled=runtime_config.dev_api_key_enabled,
            api_key_user_builder=runtime_config.api_key_user_builder,
        )

    return AuthRuntime(
        azure_scheme=azure_scheme,
        require_policy=build_policy,
        load_azure_openid_config=load_openid_config,
    )


def dev_api_key_enabled_for(
        settings: BaseAppSettings,
        *,
        allowed_environments: Collection[str],
) -> bool:
    environment = (settings.environment or "").lower()
    return bool(
        settings.enable_dev_api_key
        and settings.dev_api_key
        and environment in {item.lower() for item in allowed_environments}
    )


def _evaluate_policy(
        user: AuthUser,
        policy: str,
        required_roles: Collection[str],
        forbidden_error_factory: Callable[[str], Exception],
        *,
        request: Request | None = None,
        mechanism: str = MECHANISM_AZURE_AD,
) -> AuthUser:
    effective_roles = user.effective_roles
    if isinstance(effective_roles, str):
        # A single string would be exploded into individual characters by set().
        raise TypeError("effective_roles must be a sequence of role names, not a string.")
    roles = set(effective_roles)

    fields: dict[str, Any] = {
        "policy": policy,
        "mechanism": mechanism,
        "subject": subject_of(user),
        "roles": sorted(roles),
    }
    if request is not None:
        fields["client_host"] = client_host(request)
        fields.update(_request_fields(request))

    if roles.intersection(required_roles):
        _audit_logger.info("auth.access.granted", **fields)
        return user

    _audit_logger.info("auth.access.denied", **fields)
    raise forbidden_error_factory("You do not have permission to perform this action.")
