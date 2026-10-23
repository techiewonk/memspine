"""Legacy vectors survive full Engine startup without replaying the vector projector."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select

from memspine import Engine
from memspine.exceptions import ConfigError
from memspine.observability import tap
from memspine.providers.contracts import ProviderRef
from memspine.providers.registry import get_registry
from memspine.services.embedding.onnx import OnnxEmbedding
from memspine.services.storage.sqlite.schema import projector_offsets

MODEL = "BAAI/bge-small-en-v1.5"


def _engine(path: Path, *, versioned_spaces: bool = False, **settings: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"records": {"provider": "sqlite", "settings": {"path": str(path)}}},
        runtime={"shared_resources": {"warmup": "lazy"}},
        model={
            "embedding": {
                "provider": "onnx",
                "settings": {"model": MODEL, **settings},
                "versioned_spaces": versioned_spaces,
            }
        },
    )


async def _seed_legacy(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    versioned_spaces: bool = False,
) -> tuple[list[str], list[Any], str, int]:
    fixture_path = (
        Path(__file__).parents[1] / "services/embedding/data/BAAI__bge-small-en-v1.5.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    vector = fixture["vectors"][0]
    calls: list[str] = []

    async def embed(_self: OnnxEmbedding, texts: list[str]) -> list[list[float]]:
        calls.extend(texts)
        return [list(vector) for _ in texts]

    monkeypatch.setattr(OnnxEmbedding, "_embed", embed)
    registry = get_registry("embedders")
    descriptor = registry.get("onnx")
    # Simulate the legacy adapter's two documented outputs, keeping the actual Engine,
    # SQL checkpoint, projector, and physical LanceDB table lifecycle unchanged.
    with monkeypatch.context() as legacy:
        legacy.setitem(
            registry._items,
            "onnx",
            replace(
                descriptor,
                target_identity=lambda _: {"embedder_id": f"fastembed:{MODEL}", "dimension": 384},
            ),
        )
        legacy.setattr(OnnxEmbedding, "embedder_id", property(lambda _: f"fastembed:{MODEL}"))
        legacy.setattr(OnnxEmbedding, "legacy_embedder_id", property(lambda _: None))
        first = _engine(path, versioned_spaces=versioned_spaces)
        await first.start()
        try:
            await first.write("quartz memory survives the ONNX upgrade", namespace="tenant")
            assert first._sql is not None
            async with first._sql.engine.connect() as connection:
                checkpoint = int(
                    await connection.scalar(
                        select(projector_offsets.c.last_seq).where(
                            projector_offsets.c.projector_name == "vectors"
                        )
                    )
                    or 0
                )
            backend = (
                first._vector._backend(first._vector.current) if versioned_spaces else first._vector
            )
            table = await backend._ensure_table()
            rows = table.to_arrow().to_pylist()
            name = backend._name
        finally:
            await first.stop()
    return calls, rows, name, checkpoint


@pytest.mark.parametrize("versioned_spaces", [False, True])
async def test_engine_adopts_legacy_table_once_without_reembedding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    versioned_spaces: bool,
) -> None:
    path = tmp_path / "legacy.sqlite"
    stages: list[str] = []
    monkeypatch.setattr(tap, "event", lambda stage, **_: stages.append(stage))
    calls, rows, name, checkpoint = await _seed_legacy(
        path, monkeypatch, versioned_spaces=versioned_spaces
    )
    for attempt in range(2):
        before = len(calls)
        engine = _engine(path, versioned_spaces=versioned_spaces)
        await engine.start()
        try:
            assert engine._embedder.embedder_id == f"embedder:{MODEL}"
            assert len(calls) == before  # no vector replay and no model download at startup
            backend = (
                engine._vector._backend(engine._vector.current)
                if versioned_spaces
                else engine._vector
            )
            table = await backend._ensure_table()
            assert backend._name == name
            assert table.to_arrow().to_pylist() == rows
            async with engine._sql.engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(projector_offsets.c.last_seq).where(
                            projector_offsets.c.projector_name == "vectors"
                        )
                    )
                    == checkpoint
                )
            if attempt == 1:
                assert await engine.search("quartz", namespace="tenant")
        finally:
            await engine.stop()
    adoption_stage = (
        "provider.vector_space_adopted" if versioned_spaces else "provider.vector_identity_adopted"
    )
    assert stages.count(adoption_stage) == 1


@pytest.mark.parametrize("change", ["query_instruction", "custom_pooling"])
async def test_engine_requires_controlled_change_for_changed_vector_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    path = tmp_path / "legacy.sqlite"
    await _seed_legacy(path, monkeypatch)
    engine = _engine(path)
    await engine.start()
    await engine.stop()
    if change == "query_instruction":
        settings: dict[str, Any] = {"query_instruction": "different prefix: "}
    else:
        from memspine.clients.weights.onnx_catalogue import CATALOGUE

        spec = json.loads(json.dumps(asdict(CATALOGUE[MODEL])))
        spec["pooling"] = "mean"
        settings = {"model_spec": spec}
    changed = _engine(path, **settings)
    try:
        with pytest.raises(ConfigError, match="verified projection switch"):
            await changed.start()
    finally:
        await changed.stop()


async def test_backend_without_physical_adoption_cannot_relabel_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memspine.config.loader import load_config
    from memspine.runtime.container import preflight_projection_bindings

    registry = get_registry("vector_stores")
    name = "adoption_unsupported"
    monkeypatch.setitem(
        registry._items, name, replace(registry.get("lancedb"), ref=ProviderRef("vector", name))
    )
    config = load_config(
        template="core",
        overrides={
            "storage": {
                "records": {
                    "provider": "sqlite",
                    "settings": {"path": str(tmp_path / "unused.sqlite")},
                },
                "vector": {"provider": name},
            },
            "model": {"embedding": {"provider": "onnx", "settings": {"model": MODEL}}},
        },
    ).config

    class Bindings:
        async def adopt_equivalent_target(self, *_: Any) -> bool:
            raise AssertionError(
                "a backend without physical adoption must keep the original binding"
            )

        async def ensure(self, _: Any) -> str:
            raise ConfigError("a verified projection switch is required")

    binding_store = Bindings()
    wiring = SimpleNamespace(
        owner_id="owner",
        projection_routes={},
        records=SimpleNamespace(projection_bindings=lambda: binding_store),
    )
    with pytest.raises(ConfigError, match="verified projection switch"):
        await preflight_projection_bindings(config, cast(Any, wiring), [])
