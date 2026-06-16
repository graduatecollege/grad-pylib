"""Enhanced test client with JSON response selection helpers."""

import re
from typing import Any

from fastapi.testclient import TestClient

# JSON-compatible type alias (not using Any)
type JsonScalar = str | int | float | bool | None
type JsonValue = dict[str, JsonValue] | list[JsonValue] | JsonScalar


class JsonResponse:
    """Wraps an httpx Response to provide JMESPath-like field selection."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self._json: JsonValue = response.json() if response.content else None

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def json(self) -> JsonValue:
        return self._json

    def as_list(self, path: str) -> list[JsonValue]:
        value = _resolve(self._json, _parse_path(path))
        if not isinstance(value, list):
            raise TypeError(f"Expected a list at {path!r}, got {type(value).__name__}")
        return value

    def select(self, path: str) -> JsonValue | set[JsonScalar]:
        return _resolve(self._json, _parse_path(path))


_SEGMENT_RE = re.compile(r"([^\[.\]]+)|\[(\*|\d+)]")


def _parse_path(path: str) -> list[str | int | None]:
    segments: list[str | int | None] = []
    for match in _SEGMENT_RE.finditer(path):
        key, bracket = match.groups()
        if key is not None:
            segments.append(key)
        elif bracket == "*":
            segments.append(None)
        else:
            segments.append(int(bracket))
    return segments


def _resolve(data: Any, segments: list[str | int | None]) -> JsonValue | set[JsonScalar]:
    for i, seg in enumerate(segments):
        if seg is None:
            remaining = segments[i + 1:]
            if not isinstance(data, list):
                raise TypeError(f"Wildcard [*] used on non-list value: {type(data)}")
            result: set[JsonScalar] = set()
            for item in data:
                resolved = _resolve(item, remaining)
                if isinstance(resolved, (set, dict, list)):
                    raise TypeError("Wildcard [*] elements must resolve to scalars")
                result.add(resolved)
            return result
        if isinstance(seg, int):
            if not isinstance(data, list):
                raise TypeError(f"Cannot index into {type(data)}")
            data = data[seg]
            continue

        if not isinstance(data, dict):
            raise TypeError(f"Cannot access key {seg!r} on {type(data)}")
        data = data[seg]
    return data


class JsonTestClient(TestClient):
    """TestClient subclass that adds JSON helper methods."""

    def _wrap(self, response: Any, status: int | None) -> JsonResponse:
        if status is not None:
            assert response.status_code == status, f"Expected status {status}, got {response.status_code}"
        else:
            assert 200 <= response.status_code < 300, f"Expected 2xx status, got {response.status_code}"
        return JsonResponse(response)

    def get_json(self, url: str, *, status: int | None = None, **kwargs: Any) -> JsonResponse:
        return self._wrap(self.get(url, **kwargs), status)

    def post_json(self, url: str, *, status: int | None = None, **kwargs: Any) -> JsonResponse:
        return self._wrap(self.post(url, **kwargs), status)

    def put_json(self, url: str, *, status: int | None = None, **kwargs: Any) -> JsonResponse:
        return self._wrap(self.put(url, **kwargs), status)

    def delete_json(self, url: str, *, status: int | None = None, **kwargs: Any) -> JsonResponse:
        return self._wrap(self.delete(url, **kwargs), status)

    def patch_json(self, url: str, *, status: int | None = None, **kwargs: Any) -> JsonResponse:
        return self._wrap(self.patch(url, **kwargs), status)
