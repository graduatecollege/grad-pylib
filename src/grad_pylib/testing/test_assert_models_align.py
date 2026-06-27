from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Numeric
from sqlalchemy.dialects.mssql import DATETIME2, TINYINT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from grad_pylib.testing.assert_models_align import assert_models_align


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class Base(DeclarativeBase):
    pass


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class CamelCaseExample(Base):
    __tablename__ = "camel_case_example"

    user_netid: Mapped[str] = mapped_column(primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(DATETIME2(), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False)


class CamelCaseExampleDto(CamelModel):
    user_netid: str
    updated_at: datetime
    is_active: bool


class ValidationAliasExample(Base):
    __tablename__ = "validation_alias_example"

    row_id: Mapped[int] = mapped_column(TINYINT(), primary_key=True)
    need_500: Mapped[str | None]
    amount_due: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)


class ValidationAliasExampleDto(BaseModel):
    row_id: int
    need500: str | None = Field(default=None, validation_alias="need_500")
    amount_due: Decimal


def test_assert_models_align_accepts_camel_case_alias_generator() -> None:
    assert_models_align(CamelCaseExample, CamelCaseExampleDto)


def test_assert_models_align_matches_explicit_validation_aliases() -> None:
    assert_models_align(ValidationAliasExample, ValidationAliasExampleDto)
