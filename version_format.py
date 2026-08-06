from typing import Protocol


class SupportsVersion(Protocol):
    version: object


def format_version(version: SupportsVersion) -> str:
    return str(version.version)
