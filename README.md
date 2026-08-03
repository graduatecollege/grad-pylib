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

## Pytest SQL Server integration bootstrap

`grad_pylib.testing` now includes a higher-level bootstrap object for the repeated SQL Server
integration-test wiring that application `conftest.py` files were hand-rolling around:

* shared `SharedSqlServerState`
* `pytest_configure`, `pytest_configure_node`, and `pytest_unconfigure` hooks
* session-scoped `mssql_engine`
* optional session-scoped `codebook_engine`
* per-test `db` fixture cleanup, with optional before/after hooks

### Simple app

```python
from pathlib import Path

from grad_pylib.testing import (
    SqlServerBootstrapConfig,
    SqlServerFixtureConfig,
    SqlServerTestBootstrap,
)

FIXTURE_CONFIG = SqlServerFixtureConfig(
    migration_runner=run_migrations,
    tables_to_clean=("example_table",),
)
CODEBOOK_SQL = Path(__file__).parent / "data" / "codebook_minimal.sql"

BOOTSTRAP = SqlServerTestBootstrap(
    SqlServerBootstrapConfig(
        fixture_config=FIXTURE_CONFIG,
        codebook_sql_path=CODEBOOK_SQL,
        include_codebook_engine=True,
    )
)

pytest_configure = BOOTSTRAP.pytest_configure
pytest_configure_node = BOOTSTRAP.pytest_configure_node
pytest_unconfigure = BOOTSTRAP.pytest_unconfigure

mssql_engine = BOOTSTRAP.mssql_engine_fixture()
codebook_engine = BOOTSTRAP.codebook_engine_fixture()
db = BOOTSTRAP.db_fixture()
```

### App with cache invalidation hooks

```python
from grad_pylib.testing import SqlServerBootstrapConfig, SqlServerTestBootstrap


def clear_test_caches() -> None:
    cache.clear()


BOOTSTRAP = SqlServerTestBootstrap(
    SqlServerBootstrapConfig(
        fixture_config=FIXTURE_CONFIG,
        codebook_sql_path=CODEBOOK_SQL,
        include_codebook_engine=True,
        before_db_test=clear_test_caches,
        after_db_test=clear_test_caches,
    )
)

pytest_configure = BOOTSTRAP.pytest_configure
pytest_configure_node = BOOTSTRAP.pytest_configure_node
pytest_unconfigure = BOOTSTRAP.pytest_unconfigure

mssql_engine = BOOTSTRAP.mssql_engine_fixture()
codebook_engine = BOOTSTRAP.codebook_engine_fixture()
db = BOOTSTRAP.db_fixture()
```

If a service only needs the application engine and `db` fixture, leave
`include_codebook_engine=False` and omit the `codebook_engine` binding. The bootstrap still keeps
Codebook provisioning explicit through `codebook_sql_path`.
