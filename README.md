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

## Schema helpers

`grad_pylib.core.schemas` also includes a small set of reusable helpers for common Pydantic
normalization patterns:

* `parse_comma_separated_strings()` parses either a comma-separated string or an existing list,
  strips whitespace, drops blanks, and can optionally deduplicate or sort.
* `parse_validated_comma_separated_strings()` builds on that parser and applies an app-local
  validator to each item.
* `parse_json_blob()` parses JSON when a field arrives as a string and can optionally turn invalid
  JSON into `None`.
* `normalize_email_list()` trims, lowercases, removes blanks, and can require at least one email
  after normalization.

These helpers are intentionally generic. Business rules such as “department codes must be four
digits” should still live in the consuming app.

### Before

```python
@field_validator("department_codes", mode="before")
@classmethod
def _parse_department_codes(cls, value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = value

    parsed: list[str] = []
    for item in items:
        code = str(item).strip()
        if not code:
            continue
        if not DEPARTMENT_CODE_PATTERN.fullmatch(code):
            raise ValueError(f"Invalid department code: {code}")
        if code not in parsed:
            parsed.append(code)
    return sorted(parsed)
```

### After

```python
from grad_pylib.core.schemas import parse_validated_comma_separated_strings


def _department_code(value: str) -> str:
    if not DEPARTMENT_CODE_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid department code: {value}")
    return value


@field_validator("department_codes", mode="before")
@classmethod
def _parse_department_codes(cls, value: object) -> list[str]:
    return parse_validated_comma_separated_strings(
        value,
        validator=_department_code,
        dedupe=True,
        sort=True,
    )
```

The same pattern works for smaller field validators:

```python
@field_validator("emails", mode="before")
@classmethod
def _normalize_emails(cls, value: object) -> list[str]:
    return normalize_email_list(value, dedupe=True, require_non_empty=True)


@field_validator("metadata", mode="before")
@classmethod
def _parse_metadata(cls, value: object) -> dict[str, object] | None:
    return parse_json_blob(value, invalid_to_none=True)
```
