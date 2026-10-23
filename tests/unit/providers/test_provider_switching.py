from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from memspine.providers.changes import (
    ChangeCoordinator,
    ChangeStatus,
    SourceCompleteness,
    SqlChangeStore,
)
from memspine.providers.switching import (
    CleanupEvidence,
    GenerationStatus,
    ProjectionGeneration,
    ProjectionSwitcher,
    ProjectionSwitchStrategy,
    ProjectionVerificationEvidence,
    ReplayBatch,
    SourceSnapshot,
    SqlProjectionSwitchStore,
    _projector_for_service,
)


class RevisionGuard:
    async def current_revision(self, logical_service_id: str) -> str:
        return "graph-r1"

    async def current_target_fingerprint(self, config_id: str) -> str:
        return "fp-target"


class Backend:
    def __init__(
        self, *, incomplete_snapshot: bool = False, fail_replay_once: bool = False
    ) -> None:
        self.incomplete_snapshot = incomplete_snapshot
        self.fail_replay_once = fail_replay_once
        self.events: list[str] = []
        self.replay_starts: list[int] = []
        self.live_references = False
        self.bad_cleanup_owner = False
        self.replay_failed = False

    async def stop_admission(self, deployment_id: str, fence_epoch: int) -> None:
        self.events.append("stop")

    async def capture_snapshot(self, deployment_id: str) -> SourceSnapshot:
        self.events.append("snapshot")
        return SourceSnapshot(
            snapshot_id="snapshot-4",
            checkpoint_seq=4,
            head_seq=5,
            complete=not self.incomplete_snapshot,
            evidence_id="snapshot-proof",
            missing_ranges=("5..5",) if self.incomplete_snapshot else (),
        )

    async def prepare_owned_target(self, generation: ProjectionGeneration) -> None:
        self.events.append("prepare-target")

    async def restore_snapshot(self, snapshot_id: str, generation: ProjectionGeneration) -> int:
        self.events.append("restore")
        return 4

    async def replay_batch(
        self,
        *,
        deployment_id: str,
        source_generation: str,
        target: ProjectionGeneration,
        after_seq: int,
        through_seq: int,
        max_events: int,
    ) -> ReplayBatch:
        self.replay_starts.append(after_seq)
        if self.fail_replay_once and not self.replay_failed:
            self.replay_failed = True
            raise OSError("interrupted replay")
        return ReplayBatch(source_checkpoint_seq=5, target_checkpoint_seq=5, done=True)

    async def complete_erasures(
        self, *, deployment_id: str, source_generation: str, target: ProjectionGeneration
    ) -> int:
        self.events.append("erasures")
        return 3

    async def verify(
        self,
        *,
        source: ProjectionGeneration,
        target: ProjectionGeneration,
        source_head_seq: int,
        erase_checkpoint_seq: int,
    ) -> ProjectionVerificationEvidence:
        self.events.append("verify")
        return ProjectionVerificationEvidence(
            report_id="report-1",
            source_head_seq=source_head_seq,
            target_checkpoint_seq=target.checkpoint_seq,
            erase_checkpoint_seq=erase_checkpoint_seq,
            owner_id=target.owner_id,
            target_fingerprint=target.target_fingerprint,
            successful_probes=("parity", "scope", "erasure"),
        )

    async def activate_target(
        self,
        *,
        deployment_id: str,
        expected_source_revision: str,
        source: ProjectionGeneration,
        target: ProjectionGeneration,
        evidence: ProjectionVerificationEvidence,
    ) -> None:
        self.events.append("activate")

    async def freeze_source(self, generation: ProjectionGeneration) -> None:
        self.events.append("freeze-source")

    async def has_live_references(self, generation: ProjectionGeneration) -> bool:
        return self.live_references

    async def delete_owned_generation(self, generation: ProjectionGeneration) -> CleanupEvidence:
        self.events.append("delete-source")
        return CleanupEvidence(
            report_id="cleanup-1",
            owner_id="someone-else" if self.bad_cleanup_owner else generation.owner_id,
            generation_id=generation.generation_id,
            target_fingerprint=generation.target_fingerprint,
            deleted=True,
            remaining_references=0,
        )


async def setup_switch(*, backend: Backend, now=None, retention_s: int = 0):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    change_store = SqlChangeStore(engine)
    switch_store = SqlProjectionSwitchStore(engine)
    await change_store.create_schema()
    await switch_store.create_schema()
    strategy = ProjectionSwitchStrategy(
        backend=backend,
        store=switch_store,
        owner_id="deploy-a:graph",
        target_provider="neo4j",
        target_fingerprint="fp-target",
        batch_size=2,
    )
    coordinator = ChangeCoordinator(
        store=change_store,
        revision_guard=RevisionGuard(),
        strategies={("graph", "projection_rebuild"): strategy},
        plan_ttl_s=3600,
        now=now or (lambda: datetime.now(UTC)),
    )
    switcher = ProjectionSwitcher(coordinator)
    plan = await switcher.plan_switch(
        category="graph",
        logical_service_id="graph.primary",
        source_revision="graph-r1",
        target_revision="neo4j-r2",
        deployment_id="deploy-a",
        owner_id="deploy-a:graph",
        source_generation="gen-old",
        target_generation="gen-new",
        source_target_fingerprint="fp-old",
        target_fingerprint="fp-target",
        source_provider="ladybug",
        target_provider="neo4j",
        config_id="graph-config-v2",
        source_completeness=SourceCompleteness(
            complete=True, evidence_id="source-proof", source_checkpoint=4, source_head_seq=5
        ),
        retention_s=retention_s,
        actor="operator",
    )
    return engine, switch_store, switcher, plan


@pytest.mark.asyncio
async def test_switch_replays_erases_verifies_activates_then_cleans_owned_source() -> None:
    backend = Backend()
    engine, store, switcher, plan = await setup_switch(backend=backend)
    try:
        operation = await switcher.execute_switch(plan.plan_id)
        assert operation.checkpoint.phase == "ready_to_verify"
        result = await switcher.verify_switch(operation.operation_id)
        assert result.status == ChangeStatus.ACTIVE
        assert backend.events.index("verify") < backend.events.index("activate")
        assert backend.events.index("activate") < backend.events.index("freeze-source")

        source = await store.get_generation("deploy-a", "graph.primary", "gen-old")
        target = await store.get_generation("deploy-a", "graph.primary", "gen-new")
        assert source.status == GenerationStatus.RETAINED and source.frozen
        assert target.status == GenerationStatus.ACTIVE

        cleaned = await switcher.cleanup_switch(operation.operation_id)
        assert cleaned.status == ChangeStatus.CLEANED
        assert (
            await store.get_generation("deploy-a", "graph.primary", "gen-old")
        ).status == GenerationStatus.CLEANED
        from memspine.providers.switching import verified_active_projection_routes

        async with engine.connect() as connection:
            routes = await verified_active_projection_routes(connection, "deploy-a")
        assert routes["graph"].generation_id == "gen-new"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_activated_switch_exposes_only_verified_generation_route() -> None:
    from memspine.providers.switching import verified_active_projection_routes

    backend = Backend()
    engine, _, switcher, plan = await setup_switch(backend=backend)
    try:
        operation = await switcher.execute_switch(plan.plan_id)
        await switcher.verify_switch(operation.operation_id)
        async with engine.connect() as connection:
            routes = await verified_active_projection_routes(connection, "deploy-a")

        route = routes["graph"]
        assert route.operation_id == operation.operation_id
        assert route.provider == "neo4j"
        assert route.source_provider == "ladybug"
        assert route.source_generation_id == "gen-old"
        assert route.generation_id == "gen-new"
        assert route.target_checkpoint_seq == 5
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_interrupted_replay_resumes_from_last_durable_checkpoint() -> None:
    backend = Backend(fail_replay_once=True)
    engine, _, switcher, plan = await setup_switch(backend=backend)
    try:
        with pytest.raises(OSError, match="interrupted replay"):
            await switcher.execute_switch(plan.plan_id)
        from memspine.providers.changes import SqlChangeStore

        changes = SqlChangeStore(engine)
        failed = await changes.get_operation_for_plan(plan.plan_id)
        assert failed.status == ChangeStatus.FAILED
        assert failed.checkpoint.phase == "replaying"
        assert failed.checkpoint.source_checkpoint == 4

        resumed = await switcher.resume_switch(failed.operation_id)
        assert resumed.operation_id == failed.operation_id
        assert resumed.fence_epoch > failed.fence_epoch
        assert resumed.checkpoint.phase == "ready_to_verify"
        assert backend.replay_starts == [4, 4]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_incomplete_coherent_snapshot_never_prepares_or_activates_target() -> None:
    backend = Backend(incomplete_snapshot=True)
    engine, _, switcher, plan = await setup_switch(backend=backend)
    try:
        with pytest.raises(ValueError, match="snapshot is incomplete"):
            await switcher.execute_switch(plan.plan_id)
        assert "prepare-target" not in backend.events
        assert "activate" not in backend.events
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_waits_for_references_and_rejects_mismatched_owner() -> None:
    backend = Backend()
    engine, _, switcher, plan = await setup_switch(backend=backend)
    try:
        operation = await switcher.execute_switch(plan.plan_id)
        await switcher.verify_switch(operation.operation_id)
        backend.live_references = True
        with pytest.raises(RuntimeError, match="active request"):
            await switcher.cleanup_switch(operation.operation_id)
        backend.live_references = False
        backend.bad_cleanup_owner = True
        with pytest.raises(RuntimeError, match="ownership"):
            await switcher.cleanup_switch(operation.operation_id)
        # The retained source remains represented after either failed cleanup.
        from memspine.providers.switching import SqlProjectionSwitchStore

        store = SqlProjectionSwitchStore(engine)
        pending_manifest = await store.get_switch(operation.operation_id)
        assert pending_manifest is not None
        assert pending_manifest.status.value == "cleanup_pending"
        assert (
            await store.get_generation("deploy-a", "graph.primary", "gen-old")
        ).status == GenerationStatus.CLEANUP_PENDING
        from memspine.providers.switching import verified_active_projection_routes

        async with engine.connect() as connection:
            routes = await verified_active_projection_routes(connection, "deploy-a")
        assert routes["graph"].generation_id == "gen-new"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_production_retention_and_target_scope_are_enforced() -> None:
    now = datetime.now(UTC)
    backend = Backend()
    engine, _, switcher, plan = await setup_switch(
        backend=backend, now=lambda: now, retention_s=604800
    )
    try:
        operation = await switcher.execute_switch(plan.plan_id)
        await switcher.verify_switch(operation.operation_id)
        with pytest.raises(Exception, match="retention"):
            await switcher.cleanup_switch(operation.operation_id)
        assert "delete-source" not in backend.events
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_categories_share_sql_with_identical_generation_labels() -> None:
    """The SQL keys and physical routes include logical service scope."""
    from memspine.providers.changes import ChangeCoordinator, SqlChangeStore
    from memspine.providers.switching import verified_active_projection_routes

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    change_store = SqlChangeStore(engine)
    switch_store = SqlProjectionSwitchStore(engine)
    await change_store.create_schema()
    await switch_store.create_schema()
    graph_backend, vector_backend = Backend(), Backend()
    coordinator = ChangeCoordinator(
        store=change_store,
        revision_guard=RevisionGuard(),
        strategies={
            ("graph", "projection_rebuild"): ProjectionSwitchStrategy(
                backend=graph_backend,
                store=switch_store,
                owner_id="deploy-a:graph",
                target_provider="neo4j",
                target_fingerprint="fp-target",
                batch_size=2,
            ),
            ("vector", "projection_rebuild"): ProjectionSwitchStrategy(
                backend=vector_backend,
                store=switch_store,
                owner_id="deploy-a:vector",
                target_provider="lancedb",
                target_fingerprint="fp-target",
                batch_size=2,
            ),
        },
        plan_ttl_s=3600,
    )
    switcher = ProjectionSwitcher(coordinator)
    common = {
        "source_revision": "graph-r1",
        "target_revision": "target-r2",
        "deployment_id": "deploy-a",
        "source_generation": "active",
        "target_generation": "next",
        "source_target_fingerprint": "fp-source",
        "target_fingerprint": "fp-target",
        "config_id": "target-config",
        "source_completeness": SourceCompleteness(
            complete=True, evidence_id="complete", source_checkpoint=4, source_head_seq=5
        ),
        "retention_s": 0,
        "actor": "operator",
    }
    try:
        graph_plan = await switcher.plan_switch(
            category="graph",
            logical_service_id="graph.primary",
            owner_id="deploy-a:graph",
            source_provider="ladybug",
            target_provider="neo4j",
            **common,
        )
        vector_plan = await switcher.plan_switch(
            category="vector",
            logical_service_id="vector",
            owner_id="deploy-a:vector",
            source_provider="qdrant",
            target_provider="lancedb",
            **common,
        )
        graph_op = await switcher.execute_switch(graph_plan.plan_id)
        await switcher.verify_switch(graph_op.operation_id)
        vector_op = await switcher.execute_switch(vector_plan.plan_id)
        await switcher.verify_switch(vector_op.operation_id)

        restarted_store = SqlProjectionSwitchStore(engine)
        graph_target = await restarted_store.get_generation("deploy-a", "graph.primary", "next")
        vector_target = await restarted_store.get_generation("deploy-a", "vector", "next")
        assert graph_target.status == vector_target.status == GenerationStatus.ACTIVE
        assert graph_target.physical_generation_id != vector_target.physical_generation_id

        async with engine.connect() as connection:
            routes = await verified_active_projection_routes(connection, "deploy-a")
        assert set(routes) == {"graph", "vectors"}
        assert routes["graph"].logical_service_id == "graph.primary"
        assert routes["vectors"].logical_service_id == "vector"
        assert routes["graph"].physical_generation_id != routes["vectors"].physical_generation_id
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("category", "logical_service_id"),
    [
        ("graph", "graph:primary"),
        ("graph", "graph.mirror"),
        ("vector", "vector.primary"),
        ("lexical", "lexical.primary"),
    ],
)
def test_switch_routes_accept_only_canonical_projector_service_ids(
    category: str, logical_service_id: str
) -> None:
    with pytest.raises(RuntimeError, match="cannot route"):
        _projector_for_service(category, logical_service_id)


@pytest.mark.asyncio
async def test_tantivy_cleanup_route_survives_fresh_engine_restart(tmp_path) -> None:
    """A cleaned switch still routes a real Tantivy service after Engine restart."""
    import shutil

    from memspine import Engine
    from memspine.providers.changes import ChangeCoordinator, SqlChangeStore
    from memspine.providers.projection_bindings import projection_target_fingerprint
    from memspine.providers.registry import get_registry, validate_settings

    database = tmp_path / "switch-restart.db"
    engine = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(database), "busy_timeout_s": 5.0},
            },
            "cache": {"provider": "memory", "settings": {}},
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"semantic": {"enabled": True}},
    )
    await engine.start()
    assert engine._sql is not None and engine._resolved is not None
    assert engine._lexical is not None
    await engine._lexical.search("default", "", top_k=1)
    source_path = Path(engine._lexical.connector.spec.path or "")
    assert source_path.is_dir()
    deployment_id = engine._provider_owner_id
    assert deployment_id is not None
    config = engine._resolved.config
    result = engine._provider_results["lexical"]
    descriptor = get_registry("lexical_indexes").get(result.provider.name)
    settings = validate_settings(descriptor, config.storage.lexical.settings)
    fingerprint = projection_target_fingerprint(
        config,
        category="lexical",
        provider=result.provider,
        settings=settings,
        deployment_id=deployment_id,
        embedder=engine._embedder,
    )
    storage_engine = engine._sql.engine

    class EmptyRevisionGuard:
        async def current_revision(self, logical_service_id: str) -> str:
            assert logical_service_id == "lexical"
            return "lexical-r1"

        async def current_target_fingerprint(self, config_id: str) -> str:
            assert config_id == "tantivy-config"
            return fingerprint

    class EmptyBackend(Backend):
        async def capture_snapshot(self, deployment_id: str) -> SourceSnapshot:
            return SourceSnapshot(
                snapshot_id="event-log-empty",
                checkpoint_seq=0,
                head_seq=0,
                complete=True,
                evidence_id="empty-log",
            )

        async def restore_snapshot(self, snapshot_id: str, generation: ProjectionGeneration) -> int:
            self.events.append("restore-empty")
            return 0

        async def replay_batch(self, **kwargs) -> ReplayBatch:
            return ReplayBatch(source_checkpoint_seq=0, target_checkpoint_seq=0, done=True)

        async def complete_erasures(self, **kwargs) -> int:
            return 0

        async def delete_owned_generation(
            self, generation: ProjectionGeneration
        ) -> CleanupEvidence:
            assert generation.generation_id == "active"
            # The original Engine has stopped before cleanup; this path came from its owned builder.
            shutil.rmtree(source_path)
            return CleanupEvidence(
                report_id="tantivy-source-deleted",
                owner_id=generation.owner_id,
                generation_id=generation.generation_id,
                target_fingerprint=generation.target_fingerprint,
                deleted=True,
                remaining_references=0,
            )

    backend = EmptyBackend()
    switch_store = SqlProjectionSwitchStore(storage_engine)
    change_store = SqlChangeStore(storage_engine)
    await switch_store.create_schema()
    await change_store.create_schema()
    strategy = ProjectionSwitchStrategy(
        backend=backend,
        store=switch_store,
        owner_id=f"{deployment_id}:lexical",
        target_provider="tantivy",
        target_fingerprint=fingerprint,
        batch_size=16,
    )
    coordinator = ChangeCoordinator(
        store=change_store,
        revision_guard=EmptyRevisionGuard(),
        strategies={("lexical", "projection_rebuild"): strategy},
        plan_ttl_s=300,
    )
    switcher = ProjectionSwitcher(coordinator)
    plan = await switcher.plan_switch(
        category="lexical",
        logical_service_id="lexical",
        source_revision="lexical-r1",
        target_revision="lexical-r2",
        deployment_id=deployment_id,
        owner_id=f"{deployment_id}:lexical",
        source_generation="active",
        target_generation="restart-target",
        source_target_fingerprint=fingerprint,
        target_fingerprint=fingerprint,
        source_provider="tantivy",
        target_provider="tantivy",
        config_id="tantivy-config",
        source_completeness=SourceCompleteness(
            complete=True, evidence_id="empty-source", source_checkpoint=0, source_head_seq=0
        ),
        retention_s=0,
        actor="unit-test",
    )
    try:
        operation = await switcher.execute_switch(plan.plan_id)
        await switcher.verify_switch(operation.operation_id)
    finally:
        await engine.stop()

    # Reopen SQL as an independent process would; cleanup persists CLEANED metadata.
    from sqlalchemy.ext.asyncio import create_async_engine as create_sql_engine

    reopened_sql = create_sql_engine(f"sqlite+aiosqlite:///{database}")
    reopened_store = SqlProjectionSwitchStore(reopened_sql)
    reopened_changes = SqlChangeStore(reopened_sql)
    coordinator = ChangeCoordinator(
        store=reopened_changes,
        revision_guard=EmptyRevisionGuard(),
        strategies={
            ("lexical", "projection_rebuild"): ProjectionSwitchStrategy(
                backend=backend,
                store=reopened_store,
                owner_id=f"{deployment_id}:lexical",
                target_provider="tantivy",
                target_fingerprint=fingerprint,
                batch_size=16,
            )
        },
        plan_ttl_s=300,
    )
    cleaned = await ProjectionSwitcher(coordinator).cleanup_switch(operation.operation_id)
    assert cleaned.status == ChangeStatus.CLEANED
    assert not source_path.exists()
    await reopened_sql.dispose()

    restarted = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(database), "busy_timeout_s": 5.0},
            },
            "cache": {"provider": "memory", "settings": {}},
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"semantic": {"enabled": True}},
    )
    await restarted.start()
    try:
        assert restarted._lexical is not None
        assert restarted._lexical.connector.spec.path is not None
        assert ".tantivy-generation-" in restarted._lexical.connector.spec.path
        assert Path(restarted._lexical.connector.spec.path) != source_path
        assert await restarted._lexical.search("health", "", top_k=1) == []
    finally:
        await restarted.stop()
