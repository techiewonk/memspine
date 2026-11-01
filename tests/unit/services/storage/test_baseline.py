"""``memspine.services.storage.baseline``: the complete fresh SQL schema."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.exc import OperationalError

from memspine.services.storage.baseline import create_schema, drop_schema, schema_metadata
from memspine.services.storage.intake_tables import intake_metadata
from memspine.services.storage.outbox_tables import outbox_metadata
from memspine.services.storage.provider_tables import provider_metadata
from memspine.store.side_tables import side_metadata


def _all_tables() -> set[str]:
    return {name for md in schema_metadata() for name in md.tables}


def _engine(tmp_path: Path) -> Engine:
    return create_engine(f"sqlite:///{tmp_path / 'baseline.db'}")


def test_schema_metadata_lists_every_collection_once() -> None:
    collections = schema_metadata()
    assert collections[0] is provider_metadata
    for md in (outbox_metadata, side_metadata, intake_metadata):
        assert any(md is c for c in collections)
    assert len({id(c) for c in collections}) == len(collections)


def test_no_table_is_defined_in_two_collections() -> None:
    names = [name for md in schema_metadata() for name in md.tables]
    assert len(names) == len(set(names))


def test_create_schema_creates_every_table_on_fresh_sqlite(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        create_schema(conn)
    assert set(inspect(engine).get_table_names()) == _all_tables()
    assert {"memory_events", "provider_change_plans"} <= _all_tables()


def test_create_schema_refuses_an_existing_schema(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        create_schema(conn)
    with pytest.raises(OperationalError), engine.begin() as conn:
        create_schema(conn)


def test_drop_schema_removes_every_baseline_table(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        create_schema(conn)
    with engine.begin() as conn:
        drop_schema(conn)
    assert inspect(engine).get_table_names() == []
