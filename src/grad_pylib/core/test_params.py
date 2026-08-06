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

    def assert_parameter(
            parameter: dict[str, object],
            *,
            name: str,
            location: str,
            required: bool,
            schema: dict[str, object],
    ) -> None:
        assert parameter["name"] == name
        assert parameter["in"] == location
        assert parameter["required"] is required
        assert isinstance(parameter["schema"], dict)
        assert parameter["schema"] == {**parameter["schema"], **schema}

    assert_parameter(
        parameters["/terms/{term_code}"]["term_code"],
        name="term_code",
        location="path",
        required=True,
        schema={
            "type": "string",
            "minLength": 6,
            "maxLength": 6,
            "pattern": "^[0-9]{6}$",
            "title": "Term Code",
        },
    )
    assert_parameter(
        parameters["/term-search"]["term_code"],
        name="term_code",
        location="query",
        required=True,
        schema={
            "type": "string",
            "minLength": 6,
            "maxLength": 6,
            "pattern": "^[0-9]{6}$",
            "title": "Term Code",
        },
    )
    assert_parameter(
        parameters["/departments/{department_code}"]["department_code"],
        name="department_code",
        location="path",
        required=True,
        schema={
            "type": "string",
            "minLength": 3,
            "maxLength": 4,
            "pattern": "^[0-9]{3,4}$",
            "title": "Department Code",
        },
    )
    assert_parameter(
        parameters["/department-search"]["department_code"],
        name="department_code",
        location="query",
        required=True,
        schema={
            "type": "string",
            "minLength": 3,
            "maxLength": 4,
            "pattern": "^[0-9]{3,4}$",
            "title": "Department Code",
        },
    )
    assert_parameter(
        parameters["/records/{unique_hash}"]["unique_hash"],
        name="unique_hash",
        location="path",
        required=True,
        schema={
            "type": "string",
            "minLength": 1,
            "maxLength": 50,
            "pattern": "^[a-zA-Z0-9]+$",
            "title": "Unique Hash",
        },
    )
    assert_parameter(
        parameters["/tables/{table_name}"]["table_name"],
        name="table_name",
        location="path",
        required=True,
        schema={
            "type": "string",
            "pattern": "^[a-z_]+$",
            "title": "Table Name",
        },
    )


# ---- Body alias tests ----

from grad_pylib.core.params import (
    DepartmentCodeBody,
    SnakeCaseNameBody,
    TermCodeBody,
    UniqueHashBody,
)


@pytest.fixture
def body_client() -> Iterator[TestClient]:
    app = FastAPI()

    @app.post("/terms")
    def create_term(term_code: TermCodeBody) -> dict[str, str]:
        return {"term_code": term_code}

    @app.post("/departments")
    def create_department(department_code: DepartmentCodeBody) -> dict[str, str]:
        return {"department_code": department_code}

    @app.post("/records")
    def create_record(unique_hash: UniqueHashBody) -> dict[str, str]:
        return {"unique_hash": unique_hash}

    @app.post("/tables")
    def create_table(table_name: SnakeCaseNameBody) -> dict[str, str]:
        return {"table_name": table_name}

    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/terms", "202508"),
        ("/departments", "1234"),
        ("/records", "AbC123"),
        ("/tables", "degree_audit"),
    ],
)
def test_body_aliases_accept_valid_values(body_client: TestClient, path: str, body: str) -> None:
    response = body_client.post(path, json=body)

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/terms", "20250"),
        ("/terms", "20A508"),
        ("/departments", "12"),
        ("/departments", "abcd"),
        ("/records", "hash-with-dash"),
        ("/records", "a" * 51),
        ("/tables", "degree-audit"),
        ("/tables", "Degree_Audit"),
    ],
)
def test_body_aliases_reject_invalid_values(body_client: TestClient, path: str, body: str) -> None:
    response = body_client.post(path, json=body)

    assert response.status_code == 422


def test_body_aliases_generate_openapi_validation_metadata(body_client: TestClient) -> None:
    openapi = body_client.app.openapi()

    def get_body_schema(path: str) -> dict[str, object]:
        return openapi["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]

    term_schema = get_body_schema("/terms")
    assert term_schema.get("minLength") == 6
    assert term_schema.get("maxLength") == 6
    assert term_schema.get("pattern") == "^[0-9]{6}$"

    dept_schema = get_body_schema("/departments")
    assert dept_schema.get("minLength") == 3
    assert dept_schema.get("maxLength") == 4
    assert dept_schema.get("pattern") == "^[0-9]{3,4}$"

    hash_schema = get_body_schema("/records")
    assert hash_schema.get("minLength") == 1
    assert hash_schema.get("maxLength") == 50
    assert hash_schema.get("pattern") == "^[a-zA-Z0-9]+$"

    table_schema = get_body_schema("/tables")
    assert table_schema.get("pattern") == "^[a-z_]+$"
