from typing import Any

import pytest
from sqlalchemy import Select, select

from grad_pylib.core.exceptions import BadRequestError
from grad_pylib.core.querying import (
    QuerySpec,
    apply_filters,
    apply_pagination,
    apply_query,
    apply_sort,
    build_order_by_clause,
    build_where_clause,
)
from data.generated.models import Nomination, t_department_enrollment_preview

SPEC = QuerySpec(
    filterable={
        "term_code": Nomination.term_code,
        "department_code": Nomination.department_code,
        "requested_amount": Nomination.requested_amount,
    },
    sortable={
        "submitted_at": Nomination.submitted_at,
        "department_code": Nomination.department_code,
    },
    default_sort="-submitted_at",
)

def _sql(stmt: Select[Any]) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_apply_filters_equality():
    stmt = apply_filters(select(Nomination), SPEC, {"term_code": "120251"})
    assert "term_code = '120251'" in _sql(stmt)


def test_apply_filters_ignores_none_values():
    stmt = apply_filters(select(Nomination), SPEC, {"term_code": None, "department_code": "1227"})
    sql = _sql(stmt)
    assert "term_code" not in sql.split("WHERE", 1)[1]
    assert "department_code = '1227'" in sql


def test_apply_filters_operator_suffix():
    stmt = apply_filters(select(Nomination), SPEC, {"requested_amount__gte": 100})
    assert "requested_amount >= 100" in _sql(stmt)


def test_apply_filters_unknown_field_raises():
    with pytest.raises(BadRequestError):
        apply_filters(select(Nomination), SPEC, {"uin": "123"})


def test_apply_filters_unknown_operator_raises():
    with pytest.raises(BadRequestError):
        apply_filters(select(Nomination), SPEC, {"term_code__between": "x"})


def test_apply_filters_too_many_fields_raises():
    with pytest.raises(BadRequestError, match="Too many filter fields"):
        apply_filters(
            select(Nomination),
            SPEC,
            {
                "term_code": "120251",
                "department_code": "1227",
                "requested_amount__gte": 100,
                "uin": "123",
            },
        )


def test_apply_sort_descending():
    stmt = apply_sort(select(Nomination), SPEC, "-submitted_at")
    assert "ORDER BY" in _sql(stmt)
    assert "submitted_at DESC" in _sql(stmt)


def test_apply_sort_accepts_table_column_expression():
    table_spec = QuerySpec(
        sortable={"department_code": t_department_enrollment_preview.c.department_code},
        default_sort="department_code",
    )
    stmt = apply_sort(select(t_department_enrollment_preview), table_spec, None)
    assert "department_enrollment_preview.department_code ASC" in _sql(stmt)


def test_apply_sort_multiple_fields():
    stmt = apply_sort(select(Nomination), SPEC, "department_code,-submitted_at")
    sql = _sql(stmt)
    assert "department_code ASC" in sql
    assert "submitted_at DESC" in sql


def test_apply_sort_uses_default_when_empty():
    stmt = apply_sort(select(Nomination), SPEC, None)
    assert "submitted_at DESC" in _sql(stmt)


def test_apply_sort_unknown_field_raises():
    with pytest.raises(BadRequestError):
        apply_sort(select(Nomination), SPEC, "uin")


def test_apply_sort_too_many_fields_raises():
    with pytest.raises(BadRequestError, match="Too many sort fields"):
        apply_sort(select(Nomination), SPEC, "department_code,submitted_at,uin")


def test_apply_pagination_limit_and_offset():
    stmt = apply_pagination(select(Nomination), limit=25, offset=50)
    sql = _sql(stmt)
    assert "LIMIT 25" in sql
    assert "OFFSET 50" in sql


def test_apply_pagination_limit_must_be_positive():
    with pytest.raises(BadRequestError, match="'limit' must be greater than 0"):
        apply_pagination(select(Nomination), limit=0)


def test_apply_pagination_offset_must_be_non_negative():
    with pytest.raises(BadRequestError, match="'offset' must be greater than or equal to 0"):
        apply_pagination(select(Nomination), offset=-1)


def test_apply_pagination_rejects_non_integer_values():
    with pytest.raises(BadRequestError, match="'limit' must be an integer"):
        apply_pagination(select(Nomination), limit="abc")


def test_apply_query_combines_filter_and_sort():
    stmt = apply_query(
        select(Nomination),
        SPEC,
        filters={"department_code": "1227"},
        sort="department_code",
    )
    sql = _sql(stmt)
    assert "department_code = '1227'" in sql
    assert "department_code ASC" in sql


def test_apply_query_combines_filter_sort_and_pagination():
    stmt = apply_query(
        select(Nomination),
        SPEC,
        filters={"department_code": "1227"},
        sort="department_code",
        limit=10,
        offset=20,
    )
    sql = _sql(stmt)
    assert "department_code = '1227'" in sql
    assert "department_code ASC" in sql
    assert "LIMIT 10" in sql
    assert "OFFSET 20" in sql


def test_build_where_clause_equality_and_operator_suffix():
    where, params = build_where_clause(SPEC, {"department_code": "1227", "requested_amount__gte": 100})
    assert where == "WHERE department_code = :department_code_1 AND requested_amount >= :requested_amount_2"
    assert params == {"department_code_1": "1227", "requested_amount_2": 100}


def test_build_where_clause_in_operator():
    where, params = build_where_clause(SPEC, {"department_code__in": ["1227", "1234"]})
    assert where == "WHERE department_code IN (:department_code_1_1, :department_code_1_2)"
    assert params == {"department_code_1_1": "1227", "department_code_1_2": "1234"}


def test_build_where_clause_unknown_field_raises():
    with pytest.raises(BadRequestError):
        build_where_clause(SPEC, {"uin": "123"})


def test_build_order_by_clause_multiple_fields():
    clause = build_order_by_clause(SPEC, "department_code,-submitted_at")
    assert clause == "ORDER BY department_code ASC, submitted_at DESC"


def test_build_order_by_clause_uses_default_when_empty():
    clause = build_order_by_clause(SPEC, None)
    assert clause == "ORDER BY submitted_at DESC"


def test_build_order_by_clause_unknown_field_raises():
    with pytest.raises(BadRequestError):
        build_order_by_clause(SPEC, "uin")


def test_build_where_clause_rejects_non_identifier_column_name():
    from sqlalchemy import column as sa_column

    malicious_spec = QuerySpec(
        filterable={"term_code": sa_column("term_code; DROP TABLE nominations")},
    )
    with pytest.raises(BadRequestError, match="Unable to build SQL"):
        build_where_clause(malicious_spec, {"term_code": "120251"})


def test_build_order_by_clause_rejects_non_identifier_column_name():
    from sqlalchemy import column as sa_column

    malicious_spec = QuerySpec(
        sortable={"term_code": sa_column("term_code) --")},
        default_sort="term_code",
    )
    with pytest.raises(BadRequestError, match="Unable to build SQL"):
        build_order_by_clause(malicious_spec, "term_code")
