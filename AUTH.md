
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
