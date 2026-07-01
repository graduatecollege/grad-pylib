from sqlalchemy import Column, ForeignKeyConstraint, Integer, MetaData, String, Table, create_engine
from sqlacodegen.models import ColumnAttribute, ModelClass, RelationshipAttribute, RelationshipType
from sqlacodegen.generators import DeclarativeGenerator

from grad_pylib.tools.generate_models import (
    generator_with_non_autoincrement_primary_keys,
    mark_secondary_overlapping_relationships_viewonly,
)


def test_render_column_includes_autoincrement_false_for_single_integer_primary_key() -> None:
    metadata = MetaData()
    table = Table(
        "example",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=False),
    )
    column = table.c.id
    generator_class = generator_with_non_autoincrement_primary_keys(DeclarativeGenerator)
    generator = generator_class(metadata, create_engine("sqlite://"), {"use_inflect", "nojoined", "nobidi"})

    assert generator.render_column(column, show_name=True, is_table=True) == (
        "Column('id', Integer, primary_key=True, autoincrement=False)"
    )


def test_render_column_skips_autoincrement_false_for_identity_primary_key() -> None:
    metadata = MetaData()
    table = Table(
        "example",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
    )
    column = table.c.id
    generator_class = generator_with_non_autoincrement_primary_keys(DeclarativeGenerator)
    generator = generator_class(metadata, create_engine("sqlite://"), {"use_inflect", "nojoined", "nobidi"})

    assert generator.render_column(column, show_name=True, is_table=True) == (
        "Column('id', Integer, primary_key=True, autoincrement=True)"
    )


def test_mark_secondary_overlapping_relationships_viewonly_prefers_fuller_foreign_key() -> None:
    metadata = MetaData()
    students = Table("students", metadata, Column("pers_id", Integer, primary_key=True))
    graduations = Table(
        "graduations",
        metadata,
        Column("pers_id", Integer, primary_key=True),
        Column("seq", Integer, primary_key=True),
        ForeignKeyConstraint(["pers_id"], ["students.pers_id"]),
    )
    ok_statuses = Table(
        "ok_statuses",
        metadata,
        Column("pers_id", Integer, primary_key=True),
        Column("seq", Integer, primary_key=True),
        ForeignKeyConstraint(["pers_id"], ["students.pers_id"]),
        ForeignKeyConstraint(["pers_id", "seq"], ["graduations.pers_id", "graduations.seq"]),
    )

    student_model = ModelClass(students)
    student_model.name = "Student"
    graduation_model = ModelClass(graduations)
    graduation_model.name = "Graduation"
    ok_status_model = ModelClass(ok_statuses)
    ok_status_model.name = "OkStatus"

    pers_id_attr = ColumnAttribute(ok_status_model, ok_statuses.c.pers_id)
    pers_id_attr.name = "pers_id"
    seq_attr = ColumnAttribute(ok_status_model, ok_statuses.c.seq)
    seq_attr.name = "seq"

    student_relationship = RelationshipAttribute(
        type=RelationshipType.MANY_TO_ONE,
        source=ok_status_model,
        target=student_model,
        constraint=next(iter(ok_statuses.foreign_key_constraints)),
    )
    student_relationship.name = "per"

    graduation_relationship = RelationshipAttribute(
        type=RelationshipType.MANY_TO_ONE,
        source=ok_status_model,
        target=graduation_model,
        constraint=next(
            constraint
            for constraint in ok_statuses.foreign_key_constraints
            if len(constraint.columns) == 2
        ),
    )
    graduation_relationship.name = "graduation"

    ok_status_model.columns = [pers_id_attr, seq_attr]
    ok_status_model.relationships = [student_relationship, graduation_relationship]

    mark_secondary_overlapping_relationships_viewonly([student_model, graduation_model, ok_status_model])

    assert not getattr(graduation_relationship, "viewonly", False)
    assert getattr(student_relationship, "viewonly", False)


def test_mark_secondary_overlapping_relationships_viewonly_ignores_same_named_non_overlapping_columns() -> None:
    metadata = MetaData()
    departments = Table("departments", metadata, Column("unique_hash", String(50), primary_key=True))
    students = Table("students", metadata, Column("unique_hash", String(50), primary_key=True))
    enrollments = Table(
        "enrollments",
        metadata,
        Column("department_hash", String(50), primary_key=True),
        Column("student_hash", String(50), primary_key=True),
        ForeignKeyConstraint(["department_hash"], ["departments.unique_hash"]),
        ForeignKeyConstraint(["student_hash"], ["students.unique_hash"]),
    )

    department_model = ModelClass(departments)
    department_model.name = "Department"
    student_model = ModelClass(students)
    student_model.name = "Student"
    enrollment_model = ModelClass(enrollments)
    enrollment_model.name = "Enrollment"

    department_relationship = RelationshipAttribute(
        type=RelationshipType.MANY_TO_ONE,
        source=enrollment_model,
        target=department_model,
        constraint=next(
            constraint
            for constraint in enrollments.foreign_key_constraints
            if "department_hash" in constraint.column_keys
        ),
    )
    department_relationship.name = "department"

    student_relationship = RelationshipAttribute(
        type=RelationshipType.MANY_TO_ONE,
        source=enrollment_model,
        target=student_model,
        constraint=next(
            constraint
            for constraint in enrollments.foreign_key_constraints
            if "student_hash" in constraint.column_keys
        ),
    )
    student_relationship.name = "student"

    enrollment_model.relationships = [department_relationship, student_relationship]

    mark_secondary_overlapping_relationships_viewonly([department_model, student_model, enrollment_model])

    assert not getattr(department_relationship, "viewonly", False)
    assert not getattr(student_relationship, "viewonly", False)


def test_render_relationship_includes_viewonly_for_marked_relationship() -> None:
    metadata = MetaData()
    source_table = Table(
        "children",
        metadata,
        Column("parent_id", Integer, primary_key=True),
        ForeignKeyConstraint(["parent_id"], ["parents.id"]),
    )
    target_table = Table("parents", metadata, Column("id", Integer, primary_key=True))

    source_model = ModelClass(source_table)
    source_model.name = "Child"
    target_model = ModelClass(target_table)
    target_model.name = "Parent"

    relationship = RelationshipAttribute(
        type=RelationshipType.MANY_TO_ONE,
        source=source_model,
        target=target_model,
        constraint=next(iter(source_table.foreign_key_constraints)),
    )
    relationship.name = "parent"
    setattr(relationship, "viewonly", True)

    generator_class = generator_with_non_autoincrement_primary_keys(DeclarativeGenerator)
    generator = generator_class(metadata, create_engine("sqlite://"), {"use_inflect", "nojoined", "nobidi"})

    assert generator.render_relationship(relationship) == (
        "parent: Mapped['Parent'] = relationship('Parent', viewonly=True)"
    )