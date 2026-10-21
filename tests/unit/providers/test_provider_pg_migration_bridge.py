from memspine.services.storage.postgres import migrations


def test_embedded_schema_bridge_runs_provider_migration_after_stamp(monkeypatch):
    calls = []

    class Engine:
        def dispose(self):
            calls.append(("dispose",))

    class Inspector:
        def has_table(self, name):
            return name == "memory_events"

    monkeypatch.setattr(migrations, "create_engine", lambda _: Engine())
    monkeypatch.setattr(migrations, "inspect", lambda _: Inspector())
    monkeypatch.setattr(
        migrations.command, "stamp", lambda _, revision: calls.append(("stamp", revision))
    )
    monkeypatch.setattr(
        migrations.command, "upgrade", lambda _, revision: calls.append(("upgrade", revision))
    )
    migrations.ensure_schema("postgresql://unused/test")
    assert calls == [("dispose",), ("stamp", "pg_lf_e_layer_backfill"), ("upgrade", "head")]
