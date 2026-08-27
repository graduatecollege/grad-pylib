import json
import re
from datetime import datetime

import pytest
from pydantic import BaseModel, ValidationError, field_validator

from grad_pylib.core.schemas import (
    BaseDto, DataResponse, ItemResponse, ListResponse, MetaResponse, StatusResponse, build_status_response)
from grad_pylib.core.schemas import (
    normalize_email_list,
    parse_comma_separated_strings,
    parse_json_blob,
    parse_validated_comma_separated_strings,
    validate_string_items,
)

DEPARTMENT_CODE_PATTERN = re.compile(r"^\d{4}$")


def _department_code(value: str) -> str:
    if not DEPARTMENT_CODE_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid department code: {value}")
    return value


class _DepartmentCodesModel(BaseModel):
    department_codes: list[str]

    @field_validator("department_codes", mode="before")
    @classmethod
    def _parse_department_codes(cls, value: object) -> list[str]:
        return parse_validated_comma_separated_strings(
            value,
            validator=_department_code,
            dedupe=True,
            sort=True,
        )


def test_parse_comma_separated_strings_trims_and_drops_blanks() -> None:
    assert parse_comma_separated_strings(" LAS, , Engineering , SCI ") == [
        "LAS",
        "Engineering",
        "SCI",
    ]


def test_parse_comma_separated_strings_accepts_lists_and_optional_dedupe_and_sort() -> None:
    assert parse_comma_separated_strings([" b ", "a", "", "b", " c "], dedupe=True, sort=True) == [
        "a",
        "b",
        "c",
    ]


def test_parse_json_blob_parses_strings_and_passes_through_existing_objects() -> None:
    payload = {"department_codes": ["1234"]}

    assert parse_json_blob('{"department_codes": ["1234"]}') == payload
    assert parse_json_blob(payload) is payload
    assert parse_json_blob(None) is None


def test_parse_json_blob_can_return_none_for_invalid_json() -> None:
    assert parse_json_blob("{not-json}", invalid_to_none=True) is None


def test_parse_json_blob_raises_for_invalid_json_when_requested() -> None:
    with pytest.raises(ValueError, match="Invalid JSON."):
        parse_json_blob("{not-json}")


def test_normalize_email_list_trims_lowercases_and_can_dedupe() -> None:
    assert normalize_email_list(
        [" A@Illinois.edu ", "", "a@illinois.edu", "B@Example.com "],
        dedupe=True,
    ) == ["a@illinois.edu", "b@example.com"]


def test_normalize_email_list_can_require_at_least_one_value() -> None:
    with pytest.raises(ValueError, match="at least one email address"):
        normalize_email_list([" ", ""], require_non_empty=True)


def test_validate_string_items_applies_item_validators() -> None:
    assert validate_string_items(["1234", "5678"], validator=_department_code) == [
        "1234",
        "5678",
    ]


def test_parse_validated_comma_separated_strings_fits_field_validators() -> None:
    model = _DepartmentCodesModel(department_codes=" 5678,1234,1234 ")

    assert model.department_codes == ["1234", "5678"]


def test_parse_validated_comma_separated_strings_keeps_business_rules_local() -> None:
    with pytest.raises(ValidationError, match="Invalid department code: AB12"):
        _DepartmentCodesModel(department_codes="1234,AB12")


class UserDto(BaseDto):
    user_netid: str
    full_name: str


class ListMeta(BaseDto):
    total_count: int
    next_cursor: str | None = None


def test_data_response_validates_nested_snake_case_payload() -> None:
    response = DataResponse[UserDto].model_validate(
        {"data": {"user_netid": "ada", "full_name": "Ada Lovelace"}},
    )

    assert response.data.user_netid == "ada"
    assert response.model_dump() == {
        "data": {
            "user_netid": "ada",
            "full_name": "Ada Lovelace",
        },
    }


def test_item_and_list_responses_keep_canonical_data_envelope() -> None:
    user = UserDto(user_netid="ada", full_name="Ada Lovelace")

    assert ItemResponse[UserDto](data=user).model_dump() == {
        "data": {
            "user_netid": "ada",
            "full_name": "Ada Lovelace",
        },
    }
    assert ListResponse[UserDto](data=[user]).model_dump() == {
        "data": [
            {
                "user_netid": "ada",
                "full_name": "Ada Lovelace",
            },
        ],
    }


def test_meta_response_serializes_sibling_meta_object() -> None:
    response = MetaResponse[list[UserDto], ListMeta].model_validate(
        {
            "data": [{"user_netid": "ada", "full_name": "Ada Lovelace"}],
            "meta": {"total_count": 1, "next_cursor": "cursor-1"},
        },
    )

    assert response.model_dump() == {
        "data": [
            {
                "user_netid": "ada",
                "full_name": "Ada Lovelace",
            },
        ],
        "meta": {
            "total_count": 1,
            "next_cursor": "cursor-1",
        },
    }


def test_status_response_supports_snake_case_and_json_serialization() -> None:
    parsed = StatusResponse.model_validate(
        {
            "status": "ok",
            "app_name": "grad-service",
            "version": "1.2.3",
            "timestamp": "2026-08-03T23:00:00",
        },
    )

    assert parsed.app_name == "grad-service"
    assert json.loads(parsed.model_dump_json(exclude_none=True)) == {
        "status": "ok",
        "app_name": "grad-service",
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
