# grad_pylib

Graduate College Python common library for web application APIs and other related projects.

## Authentication

Applications authenticate with Azure AD through `grad_pylib.core.auth`. A few things are worth
knowing before deploying a service that uses it.

### Set `ENVIRONMENT`

`ENVIRONMENT` must be set explicitly for every deployment (`production` for production). Only
`development`, `local` and `test` are treated as development environments. Outside those, the
`.env` file is not loaded at all, so a stray dotenv file in the working directory cannot override
production configuration.

### The development API key is a full bypass

When `ENABLE_DEV_API_KEY` is turned on, a request carrying the `Api-Key` header authenticates as
any role it asks for through the `Api-Role` header, with no token involved. This is intentional:
arbitrary impersonation is needed for local development and automated end-to-end tests. It is
guarded as follows:

* `ENABLE_DEV_API_KEY` must be explicitly enabled, and settings validation refuses it outside a
  development environment.
* The request must arrive from a loopback address. Forwarding headers such as `X-Forwarded-For`
  are ignored, so a leaked key is not remotely exploitable.
* A request that presents `Api-Key` when the bypass is unavailable, or presents the wrong key, is
  rejected with a 401 and audited. It never falls through to Azure AD authentication.

Validating the requested role against the application's roles is the consuming project's
responsibility, inside its `api_key_user_builder`.

### Audit logging

Every authentication decision is emitted to the `grad_pylib.audit.auth` logger as a structured
event: `auth.access.granted`, `auth.access.denied`, `auth.failed`, `auth.token.rejected`,
`auth.api_key.bypass` and `auth.roles.overridden`. Records include the subject, policy,
mechanism, effective roles, client address and request path. Note that `override_loader` is a
privilege-granting hook, and any override that changes the effective roles is logged.

### Identities

Only `illinois.edu` and `uillinois.edu` UPNs are accepted; other domains and tokens without a UPN
claim are rejected with a 401. Only the application's own user object is stored on
`request.state.user`, so the raw access token is not left where an error handler or APM
integration could serialize it.

The library's contract for a user is the `AuthUser` protocol — a read-only `effective_roles`
sequence. An application can satisfy it with any immutable type it likes; `BaseUser` is a
ready-made implementation, not a required base class. Its fields are stored exactly as declared
(tuples, and a read-only attribute mapping), so untrusted values are normalized where they enter
the application, with `parse_roles()` and `parse_distinct_strings()`. Users are immutable — use
`with_roles_override()` or `dataclasses.replace()` to derive a modified user.

### Authorization configuration

`AuthConfiguration` is a frozen pydantic model that is validated once, at startup:

* `policy_roles` entries must name roles from `valid_roles`. They are matched case-insensitively
  and stored in the canonical casing, so a typo or a casing mismatch is a startup error rather
  than an endpoint that silently never grants access.
* A policy with no roles is rejected at construction rather than when its dependency is built.
* The policy mapping and its role sets are deep-frozen, so a caller holding a reference to what
  it passed in cannot change authorization decisions at runtime.

A policy grants access when the user holds *any* of its roles.

### Shared FastAPI auth bootstrap

`AuthAppFactory` builds on `AuthRuntimeConfig` and `build_auth_runtime()` to package the
repetitive pieces most services expose from `core/auth.py`:

* `azure_scheme`
* `load_azure_openid_config`
* `require_policy(...)`
* typed dependencies such as `Annotated[CurrentUser, auth.depends_on("Admin")]`

#### Simple one-runtime app

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

#### Apps that need override-aware and override-free policies

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
