"""The default DBOS system-database URL points at an absolute SQLite file."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import make_url

from memspine.workers.dbos_runner import default_system_database_url


def test_file_storage_url_is_absolute_and_unencoded(tmp_path: Path) -> None:
    storage = tmp_path / "dir with space" / "memspine.db"
    url = make_url(default_system_database_url(str(storage)))
    assert url.drivername == "sqlite"
    assert url.database is not None
    assert Path(url.database) == storage.resolve().with_name("memspine.db.dbos.sqlite")
    assert Path(url.database).is_absolute()
    assert "%20" not in url.database


def test_memory_storage_url_is_an_absolute_scratch_file() -> None:
    url = make_url(default_system_database_url(":memory:"))
    assert url.database is not None
    assert Path(url.database).is_absolute()
    assert url.database.endswith(".sqlite")
