from __future__ import annotations

from sqlalchemy import create_engine, inspect

from memspine.services.storage.sqlite.migrations import ensure_schema


def test_sqlite_storage_migration_installs_durable_provider_change_tables(tmp_path) -> None:
    database = tmp_path / "provider-changes.db"
    ensure_schema(database)
    engine = create_engine(f"sqlite:///{database}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        generation_columns = {
            column["name"] for column in inspector.get_columns("provider_projection_generations")
        }
        generation_pk = inspector.get_pk_constraint("provider_projection_generations")[
            "constrained_columns"
        ]
    finally:
        engine.dispose()

    assert {
        "provider_change_plans",
        "provider_change_operations",
        "provider_change_fences",
        "provider_projection_generations",
        "provider_projection_switches",
        "provider_cleanup_fence_recoveries",
        "provider_deployment_identity",
    } <= tables
    assert "logical_service_id" in generation_columns
    assert generation_pk == ["deployment_id", "logical_service_id", "generation_id"]
