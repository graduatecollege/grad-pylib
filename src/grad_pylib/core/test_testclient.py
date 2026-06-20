import pytest

from grad_pylib.core.testclient import JsonResponse


class _ResponseStub:
    def __init__(self, payload: object) -> None:
        self.status_code = 200
        self.content = b"{}"
        self._payload = payload

    def json(self) -> object:
        return self._payload


def _json_response(payload: object) -> JsonResponse:
    return JsonResponse(_ResponseStub(payload))


def test_json_response_as_type_returns_typed_value() -> None:
    response = _json_response({"user": {"name": "Ada", "tags": ["admin", "active"]}})

    name = response.as_type("user.name", str)
    tags = response.as_type("user.tags", list)

    assert name == "Ada"
    assert tags == ["admin", "active"]


def test_json_response_as_type_supports_parameterized_list() -> None:
    response = _json_response({"user": {"scores": [1, 2, 3]}})

    scores = response.as_type("user.scores", list[int])

    assert scores == [1, 2, 3]


def test_json_response_as_type_supports_nested_parameterized_types() -> None:
    response = _json_response({"user": {"meta": [{"id": 1}, {"id": 2}]}})

    meta = response.as_type("user.meta", list[dict[str, int]])

    assert meta == [{"id": 1}, {"id": 2}]


def test_json_response_as_type_supports_wildcard_scalar_set() -> None:
    response = _json_response({"items": [{"id": 1}, {"id": 2}]})

    ids = response.as_type("items[*].id", set)

    assert ids == {1, 2}


def test_json_response_as_type_raises_for_incompatible_type() -> None:
    response = _json_response({"user": {"name": "Ada"}})

    with pytest.raises(TypeError, match=r"Expected int at 'user\.name', got str"):
        response.as_type("user.name", int)


def test_json_response_as_type_raises_for_incompatible_parameterized_type() -> None:
    response = _json_response({"user": {"scores": [1, "2", 3]}})

    with pytest.raises(TypeError, match=r"Expected list\[int\] at 'user\.scores', got list"):
        response.as_type("user.scores", list[int])
