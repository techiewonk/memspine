from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from memspine.config.schema.providers import PgvectorSettings
from memspine.exceptions import ConfigError
from memspine.providers.async_utils import run_blocking_drained
from memspine.providers.builtin.pgvector import PgvectorBuilder, _table_name
from memspine.providers.contexts import VectorBuildContext
from memspine.providers.contracts import ProjectionPlacement


def test_pgvector_settings_publish_native_defaults_and_reject_invalid_values() -> None:
    settings = PgvectorSettings()

    assert settings.index == "hnsw"
    assert settings.hnsw_m == 16
    assert settings.hnsw_ef_construction == 64
    assert settings.hnsw_ef_search == 40
    assert settings.namespace_index is True
    assert settings.provision_projection is False
    assert settings.create_extension is False
    with pytest.raises(ValidationError):
        PgvectorSettings(hnsw_m=1)
    with pytest.raises(ValidationError):
        PgvectorSettings(extra_option=True)


def test_pgvector_table_scope_is_stable_and_separated_by_owner_generation_and_space() -> None:
    def context(
        *, owner: str = "deployment-a", generation: str = "active", embedder: str = "hash:64"
    ) -> VectorBuildContext:
        return VectorBuildContext(
            shared=SimpleNamespace(owner_id=owner),
            placement=ProjectionPlacement(None, False, owner),
            embedding=SimpleNamespace(embedder_id=embedder, dim=64),
            quantization=None,
            matryoshka_dim=None,
            profile_meta={"provider": "hash", "dim": 64},
            versioned_spaces=True,
            query_instruction=None,
            generation_id=generation,
        )

    original = _table_name(context())
    assert original == _table_name(context())
    assert original != _table_name(context(owner="deployment-b"))
    assert original != _table_name(context(generation="vectors:target"))
    assert original != _table_name(context(embedder="other:64"))


@pytest.mark.asyncio
async def test_pgvector_requires_postgres_sql_before_constructing_a_lease() -> None:
    class Resources:
        def borrow(self, resource_id: str):
            raise AssertionError("must reject before borrowing SQL")

    ctx = SimpleNamespace(
        sql=None,
        shared=SimpleNamespace(resources=Resources()),
        embedding=SimpleNamespace(embedder_id="hash:64", dim=64),
    )

    with pytest.raises(ConfigError, match="requires storage.records.provider=postgres"):
        await PgvectorBuilder().build(PgvectorSettings(), ctx)


def test_pgvector_is_loaded_without_importing_native_driver_at_module_import() -> None:
    # Driver and pgvector SQLAlchemy types are imported only when the descriptor builds.
    import sys

    assert "pgvector.psycopg" not in sys.modules


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("versioned_spaces", "quantization", "matryoshka_dim", "message"),
    [
        (True, None, None, "versioned_spaces"),
        (False, "int8", None, "quantization"),
        (False, None, 32, "Matryoshka"),
    ],
)
async def test_pgvector_rejects_unimplemented_embedding_modes_before_sql_lease(
    versioned_spaces: bool,
    quantization: str | None,
    matryoshka_dim: int | None,
    message: str,
) -> None:
    class Resources:
        def borrow(self, resource_id: str):
            raise AssertionError("must reject before borrowing SQL")

    ctx = SimpleNamespace(
        sql=SimpleNamespace(capabilities={"sql.postgres"}),
        shared=SimpleNamespace(owner_id="deployment", resources=Resources()),
        embedding=SimpleNamespace(embedder_id="hash:64", dim=64),
        versioned_spaces=versioned_spaces,
        quantization=quantization,
        matryoshka_dim=matryoshka_dim,
    )

    with pytest.raises(ConfigError, match=message):
        await PgvectorBuilder().build(PgvectorSettings(), ctx)


@pytest.mark.asyncio
async def test_pgvector_extension_install_requires_explicit_projection_provisioning() -> None:
    class Resources:
        def borrow(self, resource_id: str):
            raise AssertionError("must reject before borrowing SQL")

    ctx = SimpleNamespace(
        sql=SimpleNamespace(capabilities={"sql.postgres"}),
        shared=SimpleNamespace(owner_id="deployment", resources=Resources()),
        embedding=SimpleNamespace(embedder_id="hash:64", dim=64),
        versioned_spaces=False,
        quantization=None,
        matryoshka_dim=None,
    )

    with pytest.raises(ConfigError, match="requires explicit.*provision_projection"):
        await PgvectorBuilder().build(PgvectorSettings(create_extension=True), ctx)


def test_pgvector_vector_validation_rejects_boolean_nonfinite_and_zero_vectors() -> None:
    from memspine.services.vector.pgvector_store import PgVectorStore

    store = object.__new__(PgVectorStore)
    store._embedder_id = "hash:2"
    store._dimension = 2
    for values in ([True, 0.0], [float("nan"), 1.0], [float("inf"), 1.0], [0.0, 0.0]):
        with pytest.raises(ConfigError, match="finite|nonzero"):
            store._validate_query("hash:2", values)


@pytest.mark.asyncio
async def test_blocking_provider_operation_drains_after_repeated_cancellation() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_operation() -> str:
        started.set()
        release.wait(timeout=3)
        finished.set()
        raise RuntimeError("worker failure after cancellation")

    task = asyncio.create_task(run_blocking_drained(blocking_operation))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


def test_postgres_storage_target_identity_is_specific_and_contains_no_password() -> None:
    from memspine.config.schema.providers import PostgresSettings
    from memspine.providers.registry import get_registry, provider_target_identity

    descriptor = get_registry("storage_backends").get("postgres")

    def settings(url: str) -> PostgresSettings:
        return PostgresSettings(
            url=url,
            connect_timeout_s=5.0,
            statement_timeout_s=5.0,
        )

    first = provider_target_identity(
        descriptor,
        settings("postgresql+psycopg://alice:secret-one@db.example.test:5432/memspine"),
    )
    same = provider_target_identity(
        descriptor,
        settings("postgresql://alice:secret-one@db.example.test/memspine"),
    )
    changed = provider_target_identity(
        descriptor,
        settings("postgresql://alice:secret-two@db.example.test/memspine"),
    )
    options = provider_target_identity(
        descriptor,
        settings(
            "postgresql://alice:secret-one@db.example.test/memspine?options=-csearch_path%3Dtenant_a"
        ),
    )
    changed_options = provider_target_identity(
        descriptor,
        settings(
            "postgresql://alice:secret-one@db.example.test/memspine?options=-csearch_path%3Dtenant_b"
        ),
    )
    assert first == same
    assert first != changed
    assert options != changed_options
    assert "secret-one" not in repr(first)
    assert "secret-two" not in repr(changed)
    assert "tenant_a" not in repr(options)
    assert "tenant_b" not in repr(changed_options)
