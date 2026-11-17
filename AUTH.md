# Authentication and authorization

Applications authenticate with Azure AD through `grad_pylib.core.auth`.

## Deployment and configuration

### Set `ENVIRONMENT`

`BaseAppSettings` defaults `ENVIRONMENT` to `production` (fail-closed). Set it explicitly to
`development`, `local`, or `test` for local development and automated tests; only those values
are treated as development environments. Outside those, the `.env` file is not loaded, so a
stray dotenv file in the working directory cannot override production configuration.

### The development API key is a full bypass

The development API key allows caller-selected roles through the `Api-Role` header without
an Azure AD token. This is intentional for local development and automated end-to-end tests:

* `ENABLE_DEV_API_KEY` must be explicitly enabled, and settings validation refuses it outside
  a development environment. The application must also configure `dev_api_key_enabled`,
  a nonempty `dev_api_key`, and an `api_key_user_builder`.
* The direct peer must be in `AuthConfiguration.api_key_allowed_hosts`, which defaults to
  loopback hosts and the test client. Forwarding headers such as `X-Forwarded-For` are ignored.
  Keep this bypass local; a proxy can make remote traffic appear to come from a local peer.
* A request that presents `Api-Key` when the bypass is unavailable, or presents the wrong key,
  is rejected with a 401 and audited. It never falls through to Azure AD authentication.

Validating the requested role against the application's roles is the consuming project's
responsibility, inside its `api_key_user_builder`. The resulting user must still satisfy the
selected role policy.

### Authorization configuration

`AuthConfiguration` is a frozen Pydantic model validated at construction:

* `policy_roles` entries must name roles from `valid_roles`. Matching is case-insensitive,
  and roles are stored in the canonical casing. Unknown roles cause a startup error.
* A policy with no roles is rejected at construction rather than when its dependency is built.
* The policy mapping and its role sets are deep-frozen, so callers cannot mutate authorization
  decisions through references to the original configuration.

A policy grants access when the user holds *any* of its roles.

### Azure AD scopes

Azure requests must satisfy both the configured delegated scope and the selected role policy.
`azure_ad_scope_description` is the short scope name (for example, `Hooding.Access`) required
in the token's space-separated `scp` claim. `azure_ad_scopes` supplies the full URI for OAuth
configuration. Scope checks run before application role overrides, so overrides cannot
compensate for a missing scope.

## Identities

With `default_claims_to_user()`, only `illinois.edu` and `uillinois.edu` UPNs are accepted;
other domains and tokens without a UPN claim are rejected with a 401. Only the application's
own user object is stored on `request.state.user`, not the upstream token-bearing Azure user.

The `AuthUser` protocol exposes a read-only `effective_roles` sequence and an `audit_log_info`
property for optional application-specific audit fields. Applications can use any immutable
type satisfying that protocol; `BaseUser` is a ready-made implementation, not a required
base class. Its fields are stored as supplied, so normalize untrusted values with
`parse_roles()` and `parse_distinct_strings()` before construction, and supply tuples and a
read-only attribute mapping. Use `with_roles_override()` or `dataclasses.replace()` to derive
a modified user.

## Audit logging

Authentication decisions emit structured events through `grad_pylib.audit.auth`:
`auth.access.granted`, `auth.access.denied`, `auth.failed`, `auth.token.rejected`,
`auth.api_key.bypass`, and `auth.roles.overridden`. Fields vary by event and include the
subject, policy, mechanism, effective roles, client address, and request path when available.
`override_loader` is a privilege-granting hook; changes to effective roles or subject are logged.

Production keeps Azure's `auto_error=True`. The runtime's bearer validator records rejected
requests as `auth.failed` events with the HTTP status, method, path, and client host, then
re-raises the original exception without changing its response. These events omit policy
and identity fields because rejection occurs before application policy evaluation; they
do not include tokens, claims, or exception details.

## Shared FastAPI auth bootstrap

`AuthAppFactory` builds on `AuthRuntimeConfig` and `build_auth_runtime()` to package the
repetitive pieces most services expose from `core/auth.py`:

* `azure_scheme`
* `load_azure_openid_config`
* `require_policy(...)`
* typed dependencies such as `Annotated[CurrentUser, auth.depends_on("Admin")]`

### Simple one-runtime app

```python
from typing import Annotated

from grad_pylib.core.auth import (
    AuthAppFactory,
    AuthConfiguration,
    BaseUser,
    default_claims_to_user,
    dev_api_key_enabled_for,
)
from grad_pylib.core.exceptions import ForbiddenError

AUTH_CONFIG = AuthConfiguration(
    valid_roles=("User", "Admin"),
    policy_roles={
        "User": {"User", "Admin"},
        "Admin": {"Admin"},
    },
)

auth = AuthAppFactory.configure(
    config=AUTH_CONFIG,
    get_settings=get_settings,
    get_session=get_session,
    forbidden_error_factory=ForbiddenError,
    claims_to_user=lambda claims: default_claims_to_user(claims, AUTH_CONFIG.valid_roles),
    dev_api_key_enabled=lambda settings: dev_api_key_enabled_for(
        settings,
        allowed_environments={"development", "test"},
    ),
    api_key_user_builder=build_api_key_user,
    allow_dev_placeholder_ids=True,
).runtime()

azure_scheme = auth.azure_scheme
load_azure_openid_config = auth.load_azure_openid_config
require_policy = auth.require_policy

UserPolicy = Annotated[BaseUser, auth.depends_on("User")]
AdminPolicy = Annotated[BaseUser, auth.depends_on("Admin")]
```

### Apps that need override-aware and override-free policies

Use one shared factory and build two runtimes when some endpoints should honor role overrides and
others should not.

```python
auth_factory = AuthAppFactory.configure(
    config=AUTH_CONFIG,
    get_settings=get_settings,
    get_session=get_session,
    forbidden_error_factory=ForbiddenError,
    claims_to_user=claims_to_current_user,
    dev_api_key_enabled=is_dev_api_key_enabled,
    api_key_user_builder=build_api_key_user,
)

auth = auth_factory.with_overrides(load_roles_override)
auth_without_override = auth_factory.without_overrides()

azure_scheme = auth.azure_scheme
load_azure_openid_config = auth.load_azure_openid_config
require_policy = auth.require_policy
require_policy_without_override = auth_without_override.require_policy

UserPolicy = Annotated[CurrentUser, auth.depends_on("User")]
OverrideFreeUserPolicy = Annotated[CurrentUser, auth_without_override.depends_on("User")]
```

This keeps application-specific roles, claims parsing, and override rules in the application while
moving the bootstrap wiring into `grad-pylib`.

### Role override loaders

Override loaders receive the session from `get_session`. The session is rolled back after the loader
returns so the endpoint starts without a transaction in progress, as `retry_on_transient_conflict`
requires (see [Transactions and retries](.github/skills/database-access-patterns/references/transactions.md)).
Loaders must therefore only read, and should return plain data such as role names rather than
ORM objects, which the rollback expires. Loaders that cache their results need no session calls
on a cache hit.

FastAPI shares a dependency within a request only when it is the same callable. Pass
`get_session=databases["app"].get_session` to reuse the endpoint's `DbSession` (from
`databases.session_dependency()`); `databases.get_session` is a different callable and opens a
second session for the request.
