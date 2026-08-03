import json
from datetime import datetime

from grad_pylib.core.schemas import (
    CamelModel,
    DataResponse,
    ItemResponse,
    ListResponse,
    MetaResponse,
    StatusResponse,
    build_status_response,
)


class UserDto(CamelModel):
    user_netid: str
    full_name: str


class ListMeta(CamelModel):
    total_count: int
    next_cursor: str | None = None


def test_data_response_validates_nested_camel_case_payload() -> None:
    response = DataResponse[UserDto].model_validate(
        {"data": {"userNetid": "ada", "fullName": "Ada Lovelace"}},
    )

    assert response.data.user_netid == "ada"
    assert response.model_dump(by_alias=True) == {
        "data": {
            "userNetid": "ada",
            "fullName": "Ada Lovelace",
        },
    }


def test_item_and_list_responses_keep_canonical_data_envelope() -> None:
    user = UserDto(user_netid="ada", full_name="Ada Lovelace")

    assert ItemResponse[UserDto](data=user).model_dump(by_alias=True) == {
        "data": {
            "userNetid": "ada",
            "fullName": "Ada Lovelace",
        },
    }
    assert ListResponse[UserDto](data=[user]).model_dump(by_alias=True) == {
        "data": [
            {
                "userNetid": "ada",
                "fullName": "Ada Lovelace",
            },
        ],
    }


def test_meta_response_serializes_sibling_meta_object() -> None:
    response = MetaResponse[list[UserDto], ListMeta].model_validate(
        {
            "data": [{"userNetid": "ada", "fullName": "Ada Lovelace"}],
            "meta": {"totalCount": 1, "nextCursor": "cursor-1"},
        },
    )

    assert response.model_dump(by_alias=True) == {
        "data": [
            {
                "userNetid": "ada",
                "fullName": "Ada Lovelace",
            },
        ],
        "meta": {
            "totalCount": 1,
            "nextCursor": "cursor-1",
        },
    }


def test_status_response_supports_camel_case_and_json_serialization() -> None:
    parsed = StatusResponse.model_validate(
        {
            "status": "ok",
            "appName": "grad-service",
            "version": "1.2.3",
            "timestamp": "2026-08-03T23:00:00",
        },
    )

    assert parsed.app_name == "grad-service"
    assert json.loads(parsed.model_dump_json(by_alias=True, exclude_none=True)) == {
        "status": "ok",
        "appName": "grad-service",
        "version": "1.2.3",
        "timestamp": "2026-08-03T23:00:00",
    }


def test_build_status_response_returns_canonical_status_payload() -> None:
    timestamp = datetime(2026, 8, 3, 23, 0, 0)

    response = build_status_response(
        app_name="grad-service",
        version="1.2.3",
        timestamp=timestamp,
    )

    assert response == StatusResponse(
        status="ok",
        app_name="grad-service",
        version="1.2.3",
        timestamp=timestamp,
    )
