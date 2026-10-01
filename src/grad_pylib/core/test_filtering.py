from datetime import date
from typing import Annotated

import pytest
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from grad_pylib.core.filtering import (
    FILTER_OPERATORS,
    FilterField,
    create_filter_model,
    filter_key,
    filter_values,
)
from grad_pylib.core.querying import QuerySpec, apply_query
from grad_pylib.core.schemas import BaseDto
from grad_pylib.testing.fake_models import FooNomination

AwardFilters = create_filter_model(
    "AwardFilters",
    {
        "department_code": FilterField(str, "eq", "in", "like"),
        "submitted_at": FilterField(date, "gte", "lte", "isnull"),
        "requested_amount": FilterField(int, "eq", "gt"),
    },
    doc="Award filters.",
)


class AwardListRequest(AwardFilters):  # type: ignore[misc,valid-type]
    sort: str | None = None


def test_filter_operators_cover_querying_operators():
    assert set(FILTER_OPERATORS) == {
        "eq", "ne", "lt", "lte", "gt", "gte", "like", "ilike", "in", "isnull", "notnull",
    }


def test_filter_key_uses_bare_name_for_eq():
    assert filter_key("college", "eq") == "college"
    assert filter_key("college", "in") == "college__in"


def test_create_filter_model_declares_only_listed_operators():
    assert set(AwardFilters.model_fields) == {
        "department_code",
        "department_code__in",
        "department_code__like",
        "submitted_at__gte",
        "submitted_at__lte",
        "submitted_at__isnull",
        "requested_amount",
        "requested_amount__gt",
    }
    assert AwardFilters.__doc__ == "Award filters."
    assert issubclass(AwardFilters, BaseDto)


def test_create_filter_model_types_values():
    filters = AwardFilters.model_validate(
        {
            "department_code__in": ["1227", "1228"],
            "submitted_at__gte": "2025-01-01",
            "submitted_at__isnull": "false",
            "requested_amount__gt": "100",
        }
    )
    assert filters.department_code__in == ["1227", "1228"]
    assert filters.submitted_at__gte == date(2025, 1, 1)
    assert filters.submitted_at__isnull is False
    assert filters.requested_amount__gt == 100

    with pytest.raises(ValidationError):
        AwardFilters.model_validate({"requested_amount__gt": "lots"})


def test_filter_field_description_prefixes_operator_description():
    model = create_filter_model(
        "DescribedFilters", {"college": FilterField(str, "eq", description="College name")}
    )
    assert model.model_fields["college"].description == "College name: equal to"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((str,), "at least one operator"),
        ((str, "between"), "Unsupported filter operators: between"),
        ((int, "eq", "like"), "require a str field"),
        ((date, "ilike"), "require a str field"),
    ],
)
def test_filter_field_validation(args: tuple, message: str):
    with pytest.raises(ValueError, match=message):
        FilterField(*args)


def test_filter_field_dedupes_operators():
    assert FilterField(str, "eq", "in", "eq").operators == ("eq", "in")


def test_filter_values_excludes_none_and_undeclared_fields():
    request = AwardListRequest(department_code="1227", requested_amount__gt=5, sort="-submitted_at")
    assert filter_values(request, AwardFilters) == {
        "department_code": "1227",
        "requested_amount__gt": 5,
    }
    assert filter_values(None, AwardFilters) == {}


def test_filter_values_apply_to_query():
    request = AwardListRequest(department_code__in=["1227"], submitted_at__isnull=False)
    spec = QuerySpec(
        filterable={
            "department_code": FooNomination.department_code,
            "submitted_at": FooNomination.submitted_at,
        }
    )
    stmt = apply_query(select(FooNomination), spec, filters=filter_values(request, AwardFilters))
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "nominations.department_code IN ('1227')" in sql
    assert "nominations.submitted_at IS NOT NULL" in sql


def test_filter_model_as_fastapi_query_model():
    app = FastAPI()

    @app.get("/awards")
    def list_awards(filters: Annotated[AwardListRequest, Query()]) -> dict:
        return filter_values(filters, AwardFilters)

    client = TestClient(app)
    response = client.get(
        "/awards",
        params=[
            ("department_code__in", "1227"),
            ("department_code__in", "1228"),
            ("submitted_at__isnull", "true"),
            ("sort", "department_code"),
        ],
    )
    assert response.status_code == 200
    assert response.json() == {
        "department_code__in": ["1227", "1228"],
        "submitted_at__isnull": True,
    }

    parameters = {
        parameter["name"]
        for parameter in app.openapi()["paths"]["/awards"]["get"]["parameters"]
    }
    assert parameters == set(AwardListRequest.model_fields)
