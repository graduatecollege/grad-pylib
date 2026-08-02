from collections.abc import Iterable
from typing import Any

import pytest
from grad_pylib.testing.assert_models_align import assert_models_align
from pydantic import BaseModel

type ModelParityCaseInput = (
    tuple[Any, type[BaseModel]]
    | tuple[Any, type[BaseModel], set[str]]
)
type NormalizedModelParityCase = tuple[Any, type[BaseModel], set[str]]


def build_model_parity_test(
    cases: Iterable[ModelParityCaseInput],
):
    normalized_cases = [_normalize_case(case) for case in cases]
    case_ids = [_case_id(db_model, api_model) for db_model, api_model, _ in normalized_cases]

    @pytest.mark.parametrize(
        ("db_model", "api_model", "ignore_fields"),
        normalized_cases,
        ids=case_ids,
    )
    def test_models_align(
        db_model: Any,
        api_model: type[BaseModel],
        ignore_fields: set[str],
    ) -> None:
        assert_models_align(db_model=db_model, api_model=api_model, ignore_fields=ignore_fields)

    return test_models_align


def _normalize_case(case: ModelParityCaseInput) -> NormalizedModelParityCase:
    if len(case) == 2:
        db_model, api_model = case
        return db_model, api_model, set()

    db_model, api_model, ignore_fields = case
    return db_model, api_model, set(ignore_fields)


def _case_id(db_model: Any, api_model: type[BaseModel]) -> str:
    db_name = getattr(db_model, "__name__", type(db_model).__name__)
    return f"{db_name}-{api_model.__name__}"
