# grad_pylib

Graduate College Python common library for web application APIs and other related projects.

## Reusable FastAPI parameter aliases

`grad_pylib` provides a small set of shared validated FastAPI parameter aliases for common route
signatures:

* `TermCodePath`
* `TermCodeQuery`
* `DepartmentCodePath`
* `DepartmentCodeQuery`
* `UniqueHashPath`
* `SnakeCaseNamePath`

These aliases replace repeated inline validation such as:

```python
from typing import Annotated

from fastapi import Path, Query

term_code: Annotated[str, Path(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")]
department_code: Annotated[str, Query(min_length=3, max_length=4, pattern=r"^[0-9]{3,4}$")]
unique_hash: Annotated[str, Path(min_length=1, max_length=50, pattern=r"^[a-zA-Z0-9]+$")]
table_name: Annotated[str, Path(pattern=r"^[a-z_]+$")]
```

Applications can instead use clearer shared aliases:

```python
from fastapi import APIRouter

from grad_pylib import DepartmentCodeQuery, SnakeCaseNamePath, TermCodePath, UniqueHashPath

router = APIRouter()


@router.get("/terms/{term_code}/records/{unique_hash}")
def read_record(
        term_code: TermCodePath,
        unique_hash: UniqueHashPath,
        department_code: DepartmentCodeQuery | None = None,
) -> dict[str, str | None]:
    return {
        "term_code": term_code,
        "unique_hash": unique_hash,
        "department_code": department_code,
    }


@router.get("/tables/{table_name}")
def read_table(table_name: SnakeCaseNamePath) -> dict[str, str]:
    return {"table_name": table_name}
```

Keep other identifiers local to the consuming application when they are only used by one service or
carry service-specific business rules.

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
