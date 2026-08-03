from collections.abc import Iterator

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from grad_pylib.core.params import (
    DepartmentCodePath,
    DepartmentCodeQuery,
    SnakeCaseNamePath,
    TermCodePath,
    TermCodeQuery,
    UniqueHashPath,
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = FastAPI()

    @app.get("/terms/{term_code}")
    def read_term(term_code: TermCodePath) -> dict[str, str]:
        return {"term_code": term_code}

    @app.get("/term-search")
    def search_term(term_code: TermCodeQuery) -> dict[str, str]:
        return {"term_code": term_code}

    @app.get("/departments/{department_code}")
    def read_department(department_code: DepartmentCodePath) -> dict[str, str]:
        return {"department_code": department_code}

    @app.get("/department-search")
    def search_department(department_code: DepartmentCodeQuery) -> dict[str, str]:
        return {"department_code": department_code}

    @app.get("/records/{unique_hash}")
    def read_record(unique_hash: UniqueHashPath) -> dict[str, str]:
        return {"unique_hash": unique_hash}

    @app.get("/tables/{table_name}")
    def read_table(table_name: SnakeCaseNamePath) -> dict[str, str]:
        return {"table_name": table_name}

    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("/terms/{value}", "202508"),
        ("/term-search?term_code={value}", "202508"),
        ("/departments/{value}", "1234"),
        ("/department-search?department_code={value}", "123"),
        ("/records/{value}", "AbC123"),
        ("/tables/{value}", "degree_audit"),
    ],
)
def test_shared_parameter_aliases_accept_valid_values(client: TestClient, path: str, value: str) -> None:
    response = client.get(path.format(value=value))

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("/terms/{value}", "20250"),
        ("/terms/{value}", "20A508"),
        ("/term-search?term_code={value}", "2025"),
        ("/term-search?term_code={value}", "fall25"),
        ("/departments/{value}", "12"),
        ("/departments/{value}", "abcd"),
        ("/department-search?department_code={value}", "12345"),
        ("/department-search?department_code={value}", "12a"),
        ("/records/{value}", "hash-with-dash"),
        ("/records/{value}", "a" * 51),
        ("/tables/{value}", "degree-audit"),
        ("/tables/{value}", "Degree_Audit"),
    ],
)
def test_shared_parameter_aliases_reject_invalid_values(client: TestClient, path: str, value: str) -> None:
    response = client.get(path.format(value=value))

    assert response.status_code == 422


def test_shared_parameter_aliases_generate_openapi_validation_metadata(client: TestClient) -> None:
    openapi = client.app.openapi()

    parameters = {
        route: {
            parameter["name"]: parameter
            for parameter in openapi["paths"][route]["get"]["parameters"]
        }
        for route in (
            "/terms/{term_code}",
            "/term-search",
            "/departments/{department_code}",
            "/department-search",
            "/records/{unique_hash}",
            "/tables/{table_name}",
        )
    }

    assert parameters["/terms/{term_code}"]["term_code"] == {
        "name": "term_code",
        "in": "path",
        "required": True,
        "schema": {
            "type": "string",
            "minLength": 6,
            "maxLength": 6,
            "pattern": "^[0-9]{6}$",
            "title": "Term Code",
        },
    }
    assert parameters["/term-search"]["term_code"] == {
        "name": "term_code",
        "in": "query",
        "required": True,
        "schema": {
            "type": "string",
            "minLength": 6,
            "maxLength": 6,
            "pattern": "^[0-9]{6}$",
            "title": "Term Code",
        },
    }
    assert parameters["/departments/{department_code}"]["department_code"] == {
        "name": "department_code",
        "in": "path",
        "required": True,
        "schema": {
            "type": "string",
            "minLength": 3,
            "maxLength": 4,
            "pattern": "^[0-9]{3,4}$",
            "title": "Department Code",
        },
    }
    assert parameters["/department-search"]["department_code"] == {
        "name": "department_code",
        "in": "query",
        "required": True,
        "schema": {
            "type": "string",
            "minLength": 3,
            "maxLength": 4,
            "pattern": "^[0-9]{3,4}$",
            "title": "Department Code",
        },
    }
    assert parameters["/records/{unique_hash}"]["unique_hash"] == {
        "name": "unique_hash",
        "in": "path",
        "required": True,
        "schema": {
            "type": "string",
            "minLength": 1,
            "maxLength": 50,
            "pattern": "^[a-zA-Z0-9]+$",
            "title": "Unique Hash",
        },
    }
    assert parameters["/tables/{table_name}"]["table_name"] == {
        "name": "table_name",
        "in": "path",
        "required": True,
        "schema": {
            "type": "string",
            "pattern": "^[a-z_]+$",
            "title": "Table Name",
        },
    }
