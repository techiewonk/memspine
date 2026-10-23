from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from memspine import Engine
from memspine.providers.deployment import inspect_deployment


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["development", "production"])
async def test_deployment_checks_preserve_data_and_do_not_probe_models(
    tmp_path: Path,
    environment: str,
) -> None:
    engine = Engine(
        template="core",
        dotenv_path=None,
        runtime={"environment": environment},
        deployment={"id": "deployment-validation-test"},
        storage={"records": {"settings": {"path": str(tmp_path / "events.db")}}},
        model={"embedding": {"provider": "hash"}},
    )
    await engine.start()
    try:
        await engine.ingest_messages(
            [{"content": "Synthetic deployment test", "role": "user"}], namespace="validation"
        )
        before = await engine.retrieve(namespace="validation")
        report = await inspect_deployment(engine)
        assert report.infrastructure_ready
        assert report.production_profile is (environment == "production")
        assert report.production_validated is False
        assert report.providers
        assert engine.model_usage() == {}
        assert await engine.retrieve(namespace="validation") == before
        assert "Synthetic deployment test" not in str(asdict(report))
        assert "events.db" not in str(asdict(report))
        assert report.unchecked
    finally:
        await engine.stop()
