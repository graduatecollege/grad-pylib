"""Reusable SQL Server data tooling."""

import os

from grad_pylib.core.config import ENVIRONMENT_ENV_VAR, get_settings


def ensure_cli_environment() -> None:
    """Default `ENVIRONMENT` to `development` for local CLI execution when unset."""
    if ENVIRONMENT_ENV_VAR not in os.environ:
        os.environ[ENVIRONMENT_ENV_VAR] = "development"
        get_settings.cache_clear()

