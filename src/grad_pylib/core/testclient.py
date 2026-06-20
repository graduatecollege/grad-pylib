"""Enhanced test client with JSON response selection helpers."""

import re
from types import UnionType
from typing import Any, TypeVar, Union, cast, get_args, get_origin

from fastapi.testclient import TestClient

# JSON-compatible type alias (not using Any)
type JsonScalar = str | int | float | bool | None
type JsonValue = dict[str, JsonValue] | list[JsonValue] | JsonScalar
T = TypeVar("T")


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

    def as_type(self, path: str, expected_type: type[T]) -> T:
        value = _resolve(self._json, _parse_path(path))
        if not _matches_type(value, expected_type):
            raise TypeError(f"Expected {_type_label(expected_type)} at {path!r}, got {type(value).__name__}")
        return cast(T, value)

    def select(self, path: str) -> Any:
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


def _matches_type(value: Any, expected_type: Any) -> bool:
    if expected_type is Any:
        return True

    origin = get_origin(expected_type)
    args = get_args(expected_type)

    if origin is None:
        return isinstance(value, expected_type)

    if origin in (UnionType, Union):
        return any(_matches_type(value, arg) for arg in args)

    if origin is list:
        if not isinstance(value, list):
            return False
        if not args:
            return True
        (item_type,) = args
        return all(_matches_type(item, item_type) for item in value)

    if origin is set:
        if not isinstance(value, set):
            return False
        if not args:
            return True
        (item_type,) = args
        return all(_matches_type(item, item_type) for item in value)

    if origin is dict:
        if not isinstance(value, dict):
            return False
        if len(args) != 2:
            return True
        key_type, val_type = args
        return all(_matches_type(k, key_type) and _matches_type(v, val_type) for k, v in value.items())

    if origin is tuple:
        if not isinstance(value, tuple):
            return False
        if not args:
            return True
        if len(args) == 2 and args[1] is Ellipsis:
            return all(_matches_type(item, args[0]) for item in value)
        if len(value) != len(args):
            return False
        return all(_matches_type(item, item_type) for item, item_type in zip(value, args, strict=True))

    return isinstance(value, origin)


def _type_label(expected_type: Any) -> str:
    if get_origin(expected_type) is not None:
        return str(expected_type)
    if hasattr(expected_type, "__name__"):
        return expected_type.__name__
    return str(expected_type)


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
