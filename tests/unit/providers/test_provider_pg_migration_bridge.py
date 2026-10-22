from types import SimpleNamespace

from memspine.services.storage.postgres import migrations


class _Connection:
    """A connector-lent connection stand-in (RM-113): not Postgres, so no advisory lock."""

    dialect = SimpleNamespace(name="fake")

    def __init__(self, calls: list[tuple[str, ...]]) -> None:
        self.calls = calls

    def in_transaction(self) -> bool:
        return False

    def commit(self) -> None:
        self.calls.append(("commit",))


def test_embedded_schema_bridge_runs_provider_migration_after_stamp(monkeypatch):
    calls: list[tuple[str, ...]] = []

    class Inspector:
        def has_table(self, name):
            return name == "memory_events"

    seen: list[object] = []
    monkeypatch.setattr(migrations, "inspect", lambda _: Inspector())

    def stamp(cfg, revision):
        seen.append(cfg.attributes["connection"])
        calls.append(("stamp", revision))

    monkeypatch.setattr(migrations.command, "stamp", stamp)
    monkeypatch.setattr(
        migrations.command, "upgrade", lambda _, revision: calls.append(("upgrade", revision))
    )
    connection = _Connection(calls)
    migrations.ensure_schema(connection)  # type: ignore[arg-type]
    assert calls == [("stamp", "pg_lf_e_layer_backfill"), ("upgrade", "head")]
    assert seen == [connection]  # Alembic runs over the lent connection
