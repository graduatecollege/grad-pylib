import logging
import secrets
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Annotated, Any, Protocol, Self

import structlog
from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import SecurityScopes
from fastapi_azure_auth import SingleTenantAzureAuthorizationCodeBearer
from fastapi_azure_auth.user import User as AzureUser
from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationInfo
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

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
"""Audit-log identifier for Azure AD authentication."""
MECHANISM_DEV_API_KEY = "dev_api_key"
"""Audit-log identifier for development API-key authentication."""

EMPTY_ATTRIBUTES: Mapping[str, tuple[str, ...]] = MappingProxyType({})
"""The default (empty, read-only) attribute mapping of a user."""


class AuthUser(Protocol):
    """Contract for the immutable application user evaluated by authorization policies.

    Applications may use any immutable user model that implements this protocol; `BaseUser`
    is a ready-made implementation, not a required base class. Authorization decisions use
    this object, so handlers and cached references must not be able to change its roles.
    """

    @property
    def effective_roles(self) -> Sequence[str]:
        """Return the roles used to authorize the current request."""
        ...

    @property
    def audit_log_info(self) -> dict[str, Any] | None:
        """Return optional application-specific fields for authorization audit events."""
        ...


@dataclass(frozen=True, slots=True)
class BaseUser:
    """Ready-made immutable `AuthUser` populated from claims and optional role overrides.

    Fields are stored as declared: callers should normalize untrusted sequences before
    construction with helpers such as `parse_roles` and `parse_distinct_strings`. `roles`
    records identity-provider roles, while a non-`None` `roles_override` replaces them for
    authorization; `attributes` is a read-only mapping of application-specific values.
    """
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    roles: tuple[str, ...] = ()
    roles_override: tuple[str, ...] | None = None
    attributes: Mapping[str, tuple[str, ...]] = field(default=EMPTY_ATTRIBUTES)

    @property
    def effective_roles(self) -> tuple[str, ...]:
        """Return override roles when present, otherwise provider-issued roles."""
        if self.roles_override is None:
            return self.roles
        return self.roles_override

    @property
    def audit_log_info(self) -> dict[str, Any] | None:
        """Return extra audit fields; the default user does not add any."""
        return None

    @property
    def netid(self) -> str | None:
        """Return the institutional NetID derived from the user's email, when available."""
        return netid_from_email(self.email)

    def with_roles_override(self, roles: Sequence[str]) -> Self:
        """Return a copy whose supplied roles replace provider roles for authorization."""
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
    """Immutable role policies and development API-key settings for an application.

    A policy grants access to a user holding any of its configured valid roles. Construction
    canonicalizes policy roles against `valid_roles`, rejects empty policies and unknown
    roles, and freezes the result so authorization decisions cannot change at runtime.

    The API-key headers support only the local development/testing bypass configured by
    `require_policy`; `api_key_allowed_hosts` defaults to loopback clients.
    """
    model_config = ConfigDict(frozen=True)

    valid_roles: Annotated[tuple[str, ...], AfterValidator(_normalize_valid_roles)]
    policy_roles: Annotated[Mapping[str, frozenset[str]], AfterValidator(_normalize_policy_roles)]
    api_key_header: str = "Api-Key"
    api_role_header: str = "Api-Role"
    api_key_allowed_hosts: frozenset[str] = LOOPBACK_CLIENT_HOSTS


type ClaimsToUser = Callable[[dict[str, Any]], AuthUser]
"""Converts validated Azure AD claims into an application user."""
type OverrideLoader = Callable[[AuthUser, Session], AuthUser]
"""Loads application-specific authorization overrides for an authenticated user."""
type ApiKeyUserBuilder = Callable[[str | None, Request], AuthUser]
"""Builds the development user selected by an API-key request's role header."""
type SettingsProvider = Callable[[], BaseAppSettings]
"""Supplies current application settings to authentication helpers."""
type SessionProvider = Callable[[], Any]
"""Supplies the request-scoped session used by optional role overrides."""


@dataclass(frozen=True, slots=True)
class AuthRuntimeConfig:
    """Application-specific dependencies and options for an authentication runtime.

    This separates application claim parsing, persistence-backed role overrides, error
    choices, and development API-key behavior from the shared Azure AD policy machinery.
    """

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
    """Ready-to-export Azure scheme and policy dependencies for a FastAPI application.

    Applications commonly expose `azure_scheme`, `load_azure_openid_config`, and
    `require_policy` from this object during startup and endpoint declaration.
    """

    azure_scheme: SingleTenantAzureAuthorizationCodeBearer
    _build_policy: Callable[[str], Any]
    load_azure_openid_config: Callable[[], Awaitable[None]]

    def require_policy(self, policy: str) -> Any:
        """Build the dependency that authenticates a request and enforces `policy`.

        The returned synchronous FastAPI dependency converts the authenticated identity,
        applies this runtime's optional role override, and checks the policy's roles.
        """
        return self._build_policy(policy)

    def depends_on(self, policy: str) -> Any:
        """Wrap the dependency for `policy` in `Depends` for typed endpoint parameters.

        Use this in an `Annotated` endpoint parameter while `require_policy` remains useful
        for applications that export the raw dependency function.
        """
        return Depends(self.require_policy(policy))


@dataclass(frozen=True, slots=True)
class AuthAppFactory:
    """Build one or more policy runtimes from shared application authentication wiring.

    A single factory can produce override-aware and override-free runtimes for endpoints
    that differ only in whether application-specific role overrides are honored.
    """

    runtime_config: AuthRuntimeConfig

    @classmethod
    def configure(
            cls,
            *,
            config: AuthConfiguration,
            get_settings: SettingsProvider,
            get_session: SessionProvider,
            forbidden_error_factory: Callable[[str], Exception],
            claims_to_user: ClaimsToUser,
            override_loader: OverrideLoader | None = None,
            dev_api_key_enabled: Callable[[Any], bool] | None = None,
            api_key_user_builder: ApiKeyUserBuilder | None = None,
            allow_dev_placeholder_ids: bool = False,
            development_client_id: str = "development-client-id",
            development_tenant_id: str = "development-tenant-id",
    ) -> Self:
        """Capture application auth dependencies for later runtime construction.

        `claims_to_user` establishes the application's user model. Optional loaders and
        builders implement application-specific authorization overrides and development
        API-key identities without coupling them to the shared runtime.
        """
        return cls(
            AuthRuntimeConfig(
                config=config,
                get_settings=get_settings,
                get_session=get_session,
                forbidden_error_factory=forbidden_error_factory,
                claims_to_user=claims_to_user,
                override_loader=override_loader,
                dev_api_key_enabled=dev_api_key_enabled,
                api_key_user_builder=api_key_user_builder,
                allow_dev_placeholder_ids=allow_dev_placeholder_ids,
                development_client_id=development_client_id,
                development_tenant_id=development_tenant_id,
            )
        )

    def runtime(self) -> AuthRuntime:
        """Build a runtime that uses the factory's configured role-override loader."""
        return build_auth_runtime(self.runtime_config)

    def with_overrides(self, override_loader: OverrideLoader) -> AuthRuntime:
        """Build a runtime that applies `override_loader` before policy evaluation.

        This loader may grant or replace provider-issued roles, so every changed effective
        role set is written to the authentication audit log.
        """
        return build_auth_runtime(replace(self.runtime_config, override_loader=override_loader))

    def without_overrides(self) -> AuthRuntime:
        """Build a runtime that evaluates provider roles without application overrides.

        Use this for endpoints that must rely only on roles established by the identity
        provider or development API-key user builder.
        """
        return build_auth_runtime(replace(self.runtime_config, override_loader=None))


def netid_from_email(email: str) -> str | None:
    """Return an institutional email's normalized NetID, or `None` for other addresses.

    A missing NetID is not an identity: treating it as one would collapse all malformed or
    non-institutional addresses into the same value in caches, lookups, or audit records.
    """
    normalized = email.strip().lower()
    if "@" not in normalized:
        return None
    netid, domain = normalized.split("@", 1)
    if domain not in INSTITUTIONAL_EMAIL_DOMAINS:
        return None
    return netid or None


def azure_ad_configured(settings: BaseAppSettings) -> bool:
    """Return whether settings contain the Azure AD client and tenant identifiers."""
    return bool(settings.azure_ad_client_id and settings.azure_ad_tenant_id)


def warn_if_azure_ad_missing(settings: BaseAppSettings) -> None:
    """Warn when a non-development application will reject requests without Azure AD."""
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
    """Add placeholder Azure identifiers only to unconfigured development settings.

    This lets FastAPI Azure Auth initialize locally when Azure AD is intentionally absent;
    configured and non-development settings are returned unchanged.
    """
    if azure_ad_configured(settings) or not settings.is_development:
        return settings

    return settings.model_copy(update={
        "azure_ad_client_id": settings.azure_ad_client_id or development_client_id,
        "azure_ad_tenant_id": settings.azure_ad_tenant_id or development_tenant_id,
    })


class _AuditedAzureAuthorizationCodeBearer(SingleTenantAzureAuthorizationCodeBearer):
    # The upstream bearer validator is async; application policy dependencies stay synchronous.
    async def __call__(self, request: HTTPConnection, security_scopes: SecurityScopes) -> AzureUser | None:
        try:
            return await super().__call__(request, security_scopes)
        except HTTPException as error:
            _audit_logger.warning(
                "auth.failed",
                mechanism=MECHANISM_AZURE_AD,
                reason="azure_authentication_rejected",
                status_code=error.status_code,
                client_host=client_host(request),
                **_request_fields(request),
            )
            raise


def build_azure_scheme(settings: BaseAppSettings) -> SingleTenantAzureAuthorizationCodeBearer:
    """Build the single-tenant Azure AD bearer scheme from configured application settings.

    Missing client or tenant IDs are a configuration error. In development the scheme leaves
    missing bearer tokens for the policy dependency to handle, allowing the local API-key
    flow; other environments return Azure Auth errors immediately.
    """
    if not settings.azure_ad_client_id or not settings.azure_ad_tenant_id:
        raise ValueError(
            "Azure AD client ID and tenant ID must be set in the environment or settings."
        )
    return _AuditedAzureAuthorizationCodeBearer(
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
    """Load Azure OpenID metadata when Azure AD credentials are configured."""
    if not azure_ad_configured(get_settings()):
        return
    await azure_scheme.openid_config.load_config()


def normalize_role(value: str, valid_roles: tuple[str, ...]) -> str | None:
    """Match an untrusted role to a canonical configured name, ignoring case and space.

    Returning the configured spelling makes identity-provider roles and policy roles compare
    consistently; an unknown role returns `None` rather than gaining access.
    """
    normalized = value.strip().lower()
    for role in valid_roles:
        if role.lower() == normalized:
            return role
    return None


def parse_roles(values: Collection[str], valid_roles: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize untrusted claim roles into a canonical, deduplicated user-role tuple.

    Unknown and empty values are discarded, so claims can grant only roles explicitly
    configured by the application.
    """
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
    """Return a claim's scalar or iterable values as strings, ignoring unsafe containers.

    Mappings and byte sequences are rejected rather than iterated, preventing malformed
    token data from being mistaken for a collection of claims.
    """
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
    """Return the first nonempty string among alternative claim names."""
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def unauthorized_error(detail: str = "Not authenticated") -> HTTPException:
    """Build the bearer-authentication `401` without exposing failure details.

    Authentication paths deliberately share this generic response so callers cannot
    distinguish an unknown identity, rejected domain, or invalid API key.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def default_claims_to_user(claims: dict[str, Any], valid_roles: tuple[str, ...]) -> BaseUser:
    """Build a `BaseUser` from institutional Azure claims or reject the identity with `401`.

    Tokens require an institutional `upn`; names are read from standard Azure claims and
    token roles are filtered against `valid_roles`. Rejections are recorded in the audit log.
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
    """Convert FastAPI Azure Auth's token user into the application's authorization user.

    Only a copy of claims is passed to the application converter, separating the token-bearing
    upstream user representation from the object used for authorization and request state.
    """
    claims = dict(user.claims)
    return claims_to_user(claims)


def client_host(request: HTTPConnection) -> str | None:
    """Return the direct peer address, deliberately ignoring forwarding headers."""
    client = request.scope.get("client")
    if not client:
        return None
    return client[0]


def is_allowed_api_key_client(request: Request, allowed_hosts: Collection[str]) -> bool:
    """Return whether the direct peer is approved for development API-key use.

    Forwarding headers are intentionally ignored because clients control them, and a request
    without a peer address is rejected.
    """
    host = client_host(request)
    if not host:
        return False
    return host.lower() in {item.lower() for item in allowed_hosts}


def constant_time_equals(presented: str, expected: str | None) -> bool:
    """Compare API-key secrets in constant time and treat malformed input as nonmatching.

    UTF-8 encoding may fail for malformed surrogate input; authentication must still produce
    a normal `401` response rather than exposing an internal error.
    """
    if not expected:
        return False
    try:
        return secrets.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
    except UnicodeEncodeError:
        return False


def store_request_user(request: Request, user: AuthUser) -> None:
    """Store the application user in request state instead of token-bearing Azure data.

    This replaces FastAPI Azure Auth's upstream user, whose raw token and complete claims
    could otherwise be exposed by request-state serialization or observability tooling.
    """
    request.state.user = user


def subject_of(user: AuthUser) -> str | None:
    """Return the user's NetID or email for authorization audit records."""
    return getattr(user, "netid", None) or getattr(user, "email", None) or None


def _request_fields(request: HTTPConnection) -> dict[str, Any]:
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

    original_subject = subject_of(user)
    original_roles = tuple(getattr(user, "effective_roles", ()))
    user = override_loader(user, session)
    effective_roles = tuple(getattr(user, "effective_roles", ()))
    if effective_roles != original_roles:
        _audit_logger.warning(
            "auth.roles.overridden",
            policy=policy,
            mechanism=mechanism,
            subject=original_subject,
            effective_subject=subject_of(user),
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
    """Build a FastAPI dependency that authenticates a request and enforces `policy`.

    Azure AD tokens must contain every configured delegated scope before users are converted
    to the application's immutable user model and optionally role-overridden for authorization.
    Every grant, denial, and authentication failure is audited through `grad_pylib.audit.auth`.

    The development API key is a loopback-only, caller-role-selected bypass intended only for
    development and automated tests. It is enabled only when the application says so in a
    development environment; a presented but unusable key is audited and rejected with `401`,
    never allowed to fall through to Azure AD. `override_loader` is privilege-granting and may
    replace provider roles, so changes to effective roles are audited.

    Args:
        policy: Name of the configured policy to enforce.
        config: Role policies and development API-key header settings.
        azure_scheme: Azure AD bearer scheme that validates access tokens.
        get_settings: Supplies current application settings for each request.
        get_session: Supplies the request-scoped session for role overrides.
        forbidden_error_factory: Creates the error raised when authorization is denied.
        claims_to_user: Converts validated Azure AD claims into the application user.
        override_loader: Optionally loads roles that replace provider-issued roles.
        dev_api_key_enabled: Determines whether the local development API-key bypass is enabled.
        api_key_user_builder: Builds the development user selected by the API role header.
    """
    required_roles = config.policy_roles.get(policy)

    if required_roles is None:
        raise ValueError(f"Policy '{policy}' is not configured.")

    required_scope = get_settings().azure_ad_scope_description

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
            azure_user: Annotated[AzureUser | None, Security(azure_scheme, scopes=[required_scope])],
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
    """Build an Azure scheme and policy helpers from application authentication wiring.

    Missing Azure credentials warn in non-development environments. When explicitly enabled,
    placeholder IDs let an unconfigured development app initialize its local API-key flow.
    """
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
        _build_policy=build_policy,
        load_azure_openid_config=load_openid_config,
    )


def dev_api_key_enabled_for(
        settings: BaseAppSettings,
        *,
        allowed_environments: Collection[str],
) -> bool:
    """Return whether a configured development API key is enabled in an allowed environment.

    All three conditions—feature flag, nonempty secret, and case-insensitive environment
    membership—must hold before `require_policy` can accept the local bypass.
    """
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

    if user.audit_log_info is not None:
        fields["info"] = user.audit_log_info

    if roles.intersection(required_roles):
        _audit_logger.info("auth.access.granted", **fields)
        return user

    _audit_logger.info("auth.access.denied", **fields)
    raise forbidden_error_factory("You do not have permission to perform this action.")
