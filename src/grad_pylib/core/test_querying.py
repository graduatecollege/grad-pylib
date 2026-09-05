from typing import Any

import pytest
from sqlalchemy import Select, String, select, text
from sqlalchemy.orm import Mapped, declarative_base, mapped_column

from grad_pylib.core.exceptions import BadRequestError
from grad_pylib.core.querying import (
    QuerySpec,
    apply_filters,
    apply_pagination,
    apply_query,
    apply_sort,
    bind_expanding_params,
    build_order_by_clause,
    build_where_clause,
)
from grad_pylib.testing.fake_models import FooNomination, t_foo_view

Base = declarative_base()


class AliasedColumnModel(Base):
    __tablename__ = "aliased_column_model"

    python_name: Mapped[str] = mapped_column("db_name", String, primary_key=True)


SPEC = QuerySpec(
    filterable={
        "term_code": FooNomination.term_code,
        "department_code": FooNomination.department_code,
        "requested_amount": FooNomination.requested_amount,
    },
    sortable={
        "submitted_at": FooNomination.submitted_at,
        "department_code": FooNomination.department_code,
    },
    default_sort="-submitted_at",
)


def _sql(stmt: Select[Any]) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_apply_filters_equality():
    stmt = apply_filters(select(FooNomination), SPEC, {"term_code": "120251"})
    assert "term_code = '120251'" in _sql(stmt)


def test_apply_filters_ignores_none_values():
    stmt = apply_filters(
        select(FooNomination), SPEC, {"term_code": None, "department_code": "1227"}
    )
    sql = _sql(stmt)
    assert "term_code" not in sql.split("WHERE", 1)[1]
    assert "department_code = '1227'" in sql


def test_apply_filters_operator_suffix():
    stmt = apply_filters(select(FooNomination), SPEC, {"requested_amount__gte": 100})
    assert "requested_amount >= 100" in _sql(stmt)


def test_apply_filters_isnull_true_uses_is_null():
    stmt = apply_filters(select(FooNomination), SPEC, {"requested_amount__isnull": True})
    assert "requested_amount IS NULL" in _sql(stmt)


def test_apply_filters_isnull_false_uses_is_not_null():
    stmt = apply_filters(
        select(FooNomination), SPEC, {"requested_amount__isnull": "false"}
    )
    assert "requested_amount IS NOT NULL" in _sql(stmt)


def test_apply_filters_notnull_true_uses_is_not_null():
    stmt = apply_filters(
        select(FooNomination), SPEC, {"requested_amount__notnull": "true"}
    )
    assert "requested_amount IS NOT NULL" in _sql(stmt)


def test_apply_filters_null_operators_require_boolean_values():
    with pytest.raises(BadRequestError, match="requires a boolean value"):
        apply_filters(select(FooNomination), SPEC, {"requested_amount__isnull": "maybe"})


def test_apply_filters_unknown_field_raises():
    with pytest.raises(BadRequestError):
        apply_filters(select(FooNomination), SPEC, {"uin": "123"})


def test_apply_filters_unknown_operator_raises():
    with pytest.raises(BadRequestError):
        apply_filters(select(FooNomination), SPEC, {"term_code__between": "x"})


def test_apply_filters_empty_in_list_raises():
    with pytest.raises(BadRequestError, match="requires at least one value"):
        apply_filters(select(FooNomination), SPEC, {"department_code__in": []})


def test_apply_sort_descending():
    stmt = apply_sort(select(FooNomination), SPEC, "-submitted_at")
    assert "ORDER BY" in _sql(stmt)
    assert "submitted_at DESC" in _sql(stmt)


def test_apply_sort_accepts_table_column_expression():
    table_spec = QuerySpec(
        sortable={"department_code": t_foo_view.c.department_code},
        default_sort="department_code",
    )
    stmt = apply_sort(select(t_foo_view), table_spec, None)
    assert "department_enrollment_preview.department_code ASC" in _sql(stmt)


def test_apply_sort_multiple_fields():
    stmt = apply_sort(select(FooNomination), SPEC, "department_code,-submitted_at")
    sql = _sql(stmt)
    assert "department_code ASC" in sql
    assert "submitted_at DESC" in sql


def test_apply_sort_uses_default_when_empty():
    stmt = apply_sort(select(FooNomination), SPEC, None)
    assert "submitted_at DESC" in _sql(stmt)


def test_apply_sort_unknown_field_raises():
    with pytest.raises(BadRequestError):
        apply_sort(select(FooNomination), SPEC, "uin")


def test_apply_sort_unknown_field_preferred_over_length_error():
    with pytest.raises(BadRequestError, match="Sorting by 'uin' is not supported"):
        apply_sort(select(FooNomination), SPEC, "department_code,submitted_at,uin")


def test_apply_pagination_limit_and_offset():
    stmt = apply_pagination(select(FooNomination), limit=25, offset=50)
    sql = _sql(stmt)
    assert "LIMIT 25" in sql
    assert "OFFSET 50" in sql


def test_apply_pagination_limit_must_be_positive():
    with pytest.raises(BadRequestError, match="'limit' must be greater than 0"):
        apply_pagination(select(FooNomination), limit=0)


def test_apply_pagination_offset_must_be_non_negative():
    with pytest.raises(
        BadRequestError, match="'offset' must be greater than or equal to 0"
    ):
        apply_pagination(select(FooNomination), offset=-1)


def test_apply_pagination_rejects_non_integer_values():
    with pytest.raises(BadRequestError, match="'limit' must be an integer"):
        apply_pagination(select(FooNomination), limit="abc")


def test_apply_query_combines_filter_and_sort():
    stmt = apply_query(
        select(FooNomination),
        SPEC,
        filters={"department_code": "1227"},
        sort="department_code",
    )
    sql = _sql(stmt)
    assert "department_code = '1227'" in sql
    assert "department_code ASC" in sql


def test_apply_query_combines_filter_sort_and_pagination():
    stmt = apply_query(
        select(FooNomination),
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
    where, params = build_where_clause(
        SPEC, {"department_code": "1227", "requested_amount__gte": 100}
    )
    assert (
        where
        == "WHERE (department_code = :__grad_pylib_filter_1) AND (requested_amount >= :__grad_pylib_filter_2)"
    )
    assert params == {"__grad_pylib_filter_1": "1227", "__grad_pylib_filter_2": 100}


def test_build_where_clause_in_operator():
    clause = build_where_clause(SPEC, {"department_code__in": ["1227", "1234"]})
    assert clause.sql == "WHERE (department_code IN :__grad_pylib_filter_1)"
    assert clause.params == {"__grad_pylib_filter_1": ["1227", "1234"]}
    assert clause.expanding_params == ("__grad_pylib_filter_1",)


def test_build_where_clause_isnull_true():
    clause = build_where_clause(SPEC, {"requested_amount__isnull": True})
    assert clause.sql == "WHERE (requested_amount IS NULL)"
    assert clause.params == {}
    assert clause.expanding_params == ()


def test_build_where_clause_notnull_true():
    clause = build_where_clause(SPEC, {"requested_amount__notnull": "true"})
    assert clause.sql == "WHERE (requested_amount IS NOT NULL)"
    assert clause.params == {}
    assert clause.expanding_params == ()


def test_build_where_clause_null_operators_require_boolean_values():
    with pytest.raises(BadRequestError, match="requires a boolean value"):
        build_where_clause(SPEC, {"requested_amount__notnull": "sometimes"})


def test_build_where_clause_composes_fixed_clauses():
    clause = build_where_clause(
        SPEC,
        {"department_code": "1227"},
        fixed_clauses=("term_code = :term_code", "status = :status"),
    )
    assert clause.sql == (
        "WHERE (term_code = :term_code) AND (status = :status) AND (department_code = :__grad_pylib_filter_1)"
    )
    assert clause.params == {"__grad_pylib_filter_1": "1227"}
    assert clause.expanding_params == ()


def test_build_where_clause_groups_fixed_or_predicates():
    clause = build_where_clause(
        SPEC,
        None,
        fixed_clauses=(
            "owner_id = :user OR is_public = 1",
            "tenant_id = :tenant",
        ),
    )
    assert clause.sql == (
        "WHERE (owner_id = :user OR is_public = 1) AND (tenant_id = :tenant)"
    )


def test_build_where_clause_supports_fixed_clauses_without_filters():
    clause = build_where_clause(SPEC, None, fixed_clauses=("term_code = :term_code",))
    assert clause.sql == "WHERE (term_code = :term_code)"
    assert clause.params == {}
    assert clause.expanding_params == ()


def test_raw_where_clause_bind_marks_text_clause_for_postcompile_expansion():
    clause = build_where_clause(SPEC, {"department_code__in": ["1227", "1234"]})
    stmt = clause.bind(
        text(f"SELECT department_code FROM foo_nominations {clause.sql}")
    )
    assert "__[POSTCOMPILE___grad_pylib_filter_1]" in str(stmt)
    assert stmt.compile().params == {"__grad_pylib_filter_1": ["1227", "1234"]}


def test_raw_where_clause_bind_preserves_prebound_scope_values():
    clause = build_where_clause(SPEC, {"department_code": "1227"})
    stmt = clause.bind(
        text(f"SELECT * FROM foo_nominations WHERE tenant_id = :tenant_id_1 {clause.sql}")
        .bindparams(tenant_id_1="trusted-tenant")
    )

    assert stmt.compile().params == {
        "tenant_id_1": "trusted-tenant",
        "__grad_pylib_filter_1": "1227",
    }


def test_raw_where_clause_bind_rejects_generated_parameter_collision():
    clause = build_where_clause(SPEC, {"department_code": "1227"})
    stmt = text(clause.sql).bindparams(__grad_pylib_filter_1="trusted-value")

    with pytest.raises(ValueError, match="already have values"):
        clause.bind(stmt)


def test_build_where_clause_rejects_reserved_parameter_namespace():
    with pytest.raises(ValueError, match="reserved parameter namespace"):
        build_where_clause(
            SPEC,
            {"department_code": "1227"},
            fixed_clauses=("tenant_id = :__grad_pylib_filter_scope",),
        )


def test_bind_expanding_params_marks_text_clause_for_postcompile_expansion():
    stmt = bind_expanding_params(
        text(
            "SELECT department_code FROM foo_nominations WHERE department_code IN :department_code_1"
        ),
        ("department_code_1",),
    ).params(department_code_1=["1227", "1234"])
    assert "__[POSTCOMPILE_department_code_1]" in str(stmt)
    assert stmt.compile().params == {"department_code_1": ["1227", "1234"]}


def test_build_where_clause_rejects_empty_in_values():
    with pytest.raises(BadRequestError, match="requires at least one value"):
        build_where_clause(SPEC, {"department_code__in": []})


def test_build_where_clause_unknown_field_raises():
    with pytest.raises(BadRequestError):
        build_where_clause(SPEC, {"uin": "123"})


def test_build_where_clause_prefers_database_column_names():
    aliased_spec = QuerySpec(
        filterable={"public_name": AliasedColumnModel.python_name},
    )
    clause = build_where_clause(aliased_spec, {"public_name": "value"})
    assert clause.sql == "WHERE (db_name = :__grad_pylib_filter_1)"
    assert clause.params == {"__grad_pylib_filter_1": "value"}


def test_build_order_by_clause_multiple_fields():
    clause = build_order_by_clause(SPEC, "department_code,-submitted_at")
    assert clause == "ORDER BY department_code ASC, submitted_at DESC"


def test_build_order_by_clause_uses_default_when_empty():
    clause = build_order_by_clause(SPEC, None)
    assert clause == "ORDER BY submitted_at DESC"


def test_build_order_by_clause_unknown_field_raises():
    with pytest.raises(BadRequestError):
        build_order_by_clause(SPEC, "uin")


def test_build_order_by_clause_prefers_database_column_names():
    aliased_spec = QuerySpec(
        sortable={"public_name": AliasedColumnModel.python_name},
        default_sort="public_name",
    )
    assert build_order_by_clause(aliased_spec, None) == "ORDER BY db_name ASC"


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
