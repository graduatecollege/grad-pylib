from collections.abc import Generator

import pytest
from sqlalchemy import String, column, table, tstring
from sqlalchemy.engine import Engine

from grad_pylib.core.querying import QuerySpec, order_by_fragment, where_fragment
from grad_pylib.testing import SqlServerFixtureConfig, create_e2e_database_bundle

A = table(
    "awards",
    column("term", String),
    column("department", String),
    column("program", String),
    column("submitted_at", String),
).alias("a")
SPEC = QuerySpec(
    filterable={"department": A.c.department, "program": A.c.program},
    sortable={"department": A.c.department, "submitted_at": A.c.submitted_at},
    default_sort="-submitted_at",
)


@pytest.fixture(scope="module")
def awards_engine() -> Generator[Engine]:
    fixture_config = SqlServerFixtureConfig(
        migration_runner=lambda _engine: None,
        tables_to_clean=(),
        database_prefix="GradPyLibQuerying",
    )
    with create_e2e_database_bundle(fixture_config) as databases:
        with databases.app_engine.begin() as connection:
            connection.exec_driver_sql("""
                CREATE TABLE dbo.awards (
                    term varchar(6) NOT NULL,
                    department varchar(10) NOT NULL,
                    program varchar(10) NULL,
                    submitted_at date NOT NULL
                )
            """)
            connection.exec_driver_sql("""
                INSERT INTO dbo.awards VALUES
                    ('120258', 'Chem', 'P1', '2026-01-01'),
                    ('120258', 'Bio', NULL, '2026-02-01'),
                    ('120258', 'CHEMENG', 'P3', '2026-03-01'),
                    ('120261', 'Chem', 'P4', '2026-04-01')
            """)
        yield databases.app_engine


def test_fragments_execute_on_sql_server(awards_engine: Engine):
    term = "120258"
    where = where_fragment(
        SPEC,
        {"department__in": ["Chem", "Bio", "CHEMENG"], "program__notnull": True},
        tstring(t"a.term = {term}"),
    )
    order_by = order_by_fragment(SPEC, None)
    with awards_engine.connect() as connection:
        rows = connection.execute(
            tstring(t"SELECT a.department FROM dbo.awards AS a {where} {order_by}")
        ).scalars().all()
    assert rows == ["CHEMENG", "Chem"]


def test_where_fragment_ilike_executes_on_sql_server(awards_engine: Engine):
    where = where_fragment(SPEC, {"department__ilike": "chem%"})
    order_by = order_by_fragment(SPEC, "department,submitted_at")
    with awards_engine.connect() as connection:
        rows = connection.execute(
            tstring(t"SELECT a.term, a.department FROM dbo.awards AS a {where} {order_by}")
        ).all()
    assert [tuple(row) for row in rows] == [
        ("120258", "Chem"),
        ("120261", "Chem"),
        ("120258", "CHEMENG"),
    ]
