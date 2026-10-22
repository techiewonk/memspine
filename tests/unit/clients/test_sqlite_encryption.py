"""#52: SQLCipher encryption at rest (``storage.encryption``), opt-in.

The real driver (``[encrypt]``) is not installed in CI. The wiring is checked with a
stand-in driver module: stdlib ``sqlite3`` under a connection class that records
every statement (plain SQLite ignores ``PRAGMA key``). The round trip against a real
SQLCipher build runs only when the driver is importable.
"""

from __future__ import annotations

import importlib.util
import logging
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from memspine import Engine
from memspine.clients.sqlite import SQLiteClient, SQLiteSpec, load_sqlcipher_driver
from memspine.config.schema.providers import PostgresSettings, SqliteSettings
from memspine.exceptions import ConfigError, MissingServiceError, StorageError

KEY_ENV = "MEMSPINE_TEST_DB_KEY"
SECRET = "correct-horse-battery-staple"

_HAS_DRIVER = any(
    importlib.util.find_spec(name.split(".")[0]) is not None
    for name in ("sqlcipher3", "pysqlcipher3")
)


class _Recording(sqlite3.Connection):
    statements: ClassVar[list[str]] = []

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:  # type: ignore[override]
        _Recording.statements.append(sql)
        return super().execute(sql, *args)


@pytest.fixture
def fake_driver(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """A ``sqlcipher3.dbapi2`` stand-in over stdlib sqlite3."""
    _Recording.statements = []
    dbapi2 = types.ModuleType("sqlcipher3.dbapi2")

    def connect(path: str, **kwargs: Any) -> sqlite3.Connection:
        return sqlite3.connect(path, factory=_Recording, **kwargs)

    dbapi2.connect = connect  # type: ignore[attr-defined]
    for name in dir(sqlite3):
        if not name.startswith("_") and name != "connect":
            setattr(dbapi2, name, getattr(sqlite3, name))
    package = types.ModuleType("sqlcipher3")
    package.dbapi2 = dbapi2  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sqlcipher3", package)
    monkeypatch.setitem(sys.modules, "sqlcipher3.dbapi2", dbapi2)
    return dbapi2


def _hide_drivers(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("sqlcipher3", "sqlcipher3.dbapi2", "pysqlcipher3", "pysqlcipher3.dbapi2"):
        monkeypatch.setitem(sys.modules, name, None)  # import raises ImportError


def test_config_defaults_off_and_requires_key_env_name() -> None:
    defaults = SqliteSettings(path=":memory:", busy_timeout_s=5.0)
    assert defaults.encryption.mode == "none"
    assert defaults.encryption.key_env is None
    with pytest.raises(ValidationError, match="key_env"):
        SqliteSettings.model_validate(
            {"path": ":memory:", "busy_timeout_s": 5.0, "encryption": {"mode": "sqlcipher"}}
        )


def test_missing_driver_names_the_encrypt_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    _hide_drivers(monkeypatch)
    with pytest.raises(MissingServiceError) as info:
        load_sqlcipher_driver()
    assert info.value.extra == "encrypt"
    assert "memspine[encrypt]" in str(info.value)


async def test_engine_start_fails_with_missing_service_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hide_drivers(monkeypatch)
    monkeypatch.setenv(KEY_ENV, SECRET)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {
                    "path": str(tmp_path / "enc.db"),
                    "encryption": {"mode": "sqlcipher", "key_env": KEY_ENV},
                    "busy_timeout_s": 5.0,
                },
            }
        },
        embedding={"provider": "hash", "settings": {}},
        read={"hybrid": False},
    )
    with pytest.raises(MissingServiceError, match=r"memspine\[encrypt\]"):
        await eng.start()


async def test_key_comes_only_from_the_named_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_driver: types.ModuleType
) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    client = SQLiteClient(SQLiteSpec(tmp_path / "a.db", cipher_key_env=KEY_ENV))
    with pytest.raises(ConfigError, match=KEY_ENV):
        await client.open()
    monkeypatch.setenv(KEY_ENV, SECRET)
    await client.open()
    try:
        assert _Recording.statements[0] == f"PRAGMA key = '{SECRET}'"  # keyed first
    finally:
        await client.close()


async def test_memory_database_is_rejected(fake_driver: types.ModuleType) -> None:
    client = SQLiteClient(SQLiteSpec(":memory:", cipher_key_env=KEY_ENV))
    with pytest.raises(ConfigError, match="file database"):
        await client.open()


async def test_quotes_in_the_key_are_escaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_driver: types.ModuleType
) -> None:
    monkeypatch.setenv(KEY_ENV, "it's")
    client = SQLiteClient(SQLiteSpec(tmp_path / "q.db", cipher_key_env=KEY_ENV))
    await client.open()
    await client.close()
    assert _Recording.statements[0] == "PRAGMA key = 'it''s'"


async def test_failed_key_check_raises_storage_error_without_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_driver: types.ModuleType
) -> None:
    def refuse(path: str, **kwargs: Any) -> sqlite3.Connection:
        conn = sqlite3.connect(path, **kwargs)
        conn.close()  # every statement now fails, as a wrong key would
        return conn

    monkeypatch.setattr(fake_driver, "connect", refuse)
    monkeypatch.setenv(KEY_ENV, SECRET)
    client = SQLiteClient(SQLiteSpec(tmp_path / "w.db", cipher_key_env=KEY_ENV))
    with pytest.raises(StorageError) as info:
        await client.open()
    assert SECRET not in str(info.value)
    assert "wrong key" in str(info.value)


async def test_engine_round_trip_never_logs_the_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_driver: types.ModuleType,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(KEY_ENV, SECRET)
    db = tmp_path / "enc.db"
    overrides: dict[str, Any] = {
        "storage": {
            "records": {
                "provider": "sqlite",
                "settings": {
                    "path": str(db),
                    "busy_timeout_s": 5.0,
                    "encryption": {"mode": "sqlcipher", "key_env": KEY_ENV},
                },
            }
        },
        "embedding": {"provider": "hash", "settings": {}},
        "read": {"hybrid": False},
    }
    caplog.set_level(logging.DEBUG)
    with capture_logs() as logs:
        eng = Engine(template="core", dotenv_path=None, **overrides)
        await eng.start()
        try:
            record = await eng.write("Ana lives in Lyon", namespace="a")
            assert [r.record_id for r in await eng.retrieve(namespace="a")] == [record.record_id]
            assert SECRET not in repr(eng)
            assert SECRET not in repr(await eng.provider_health())
            assert SECRET not in str(eng.describe())
        finally:
            await eng.stop()
    # migrations ran over keyed connections too (every connection keyed first)
    assert _Recording.statements.count(f"PRAGMA key = '{SECRET}'") >= 2
    assert SECRET not in repr(logs)
    assert SECRET not in caplog.text


def test_postgres_provider_rejects_sqlcipher_setting() -> None:
    with pytest.raises(ValidationError, match="encryption"):
        PostgresSettings.model_validate(
            {
                "url": "postgresql://unused",
                "connect_timeout_s": 5.0,
                "statement_timeout_s": 5.0,
                "encryption": {"mode": "sqlcipher", "key_env": KEY_ENV},
            }
        )


@pytest.mark.skipif(not _HAS_DRIVER, reason="SQLCipher driver ([encrypt]) not installed")
async def test_real_sqlcipher_file_is_unreadable_without_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # pragma: no cover - needs the [encrypt] extra
    monkeypatch.setenv(KEY_ENV, SECRET)
    db = tmp_path / "real.db"
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {
                    "path": str(db),
                    "encryption": {"mode": "sqlcipher", "key_env": KEY_ENV},
                    "busy_timeout_s": 5.0,
                },
            }
        },
        embedding={"provider": "hash", "settings": {}},
        read={"hybrid": False},
    )
    await eng.start()
    try:
        await eng.write("Ana lives in Lyon", namespace="a")
    finally:
        await eng.stop()
    assert b"Lyon" not in db.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        sqlite3.connect(db).execute("SELECT count(*) FROM sqlite_master").fetchone()
    monkeypatch.setenv(KEY_ENV, "wrong")
    with pytest.raises(StorageError):
        await SQLiteClient(SQLiteSpec(db, cipher_key_env=KEY_ENV)).open()
