"""The PA-084 trace must preserve one source identity across persisted stages."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "trace_memory_lifecycle.py"
PATH_BEFORE_SCRIPT_IMPORT = tuple(sys.path)
SPEC = importlib.util.spec_from_file_location("trace_memory_lifecycle", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
trace_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trace_module)
assert tuple(sys.path) == PATH_BEFORE_SCRIPT_IMPORT


@pytest.mark.asyncio
async def test_one_file_backed_message_reaches_derived_offline_and_retrieval(
    tmp_path: Path,
) -> None:
    paths_before = tuple(sys.path)
    report = await trace_module.run_trace(tmp_path / "memory.sqlite")
    assert tuple(sys.path) == paths_before, (paths_before, tuple(sys.path))

    raw = report["stages"]["ingest"]["raw_turn"]
    derived = report["stages"]["derive"]["derived_records"]
    providers = {
        (row.get("category"), row.get("resolved_provider")) for row in report["provider_selection"]
    }

    assert report["stages"]["ingest"]["source_identity_matches"] is True
    assert report["stages"]["ingest"]["timestamp_matches_input"] is True
    assert raw["message"]["message_id"] == report["source"]["message_id"]
    assert raw["message"]["sender"] == {
        "id": "mira",
        "kind": "human",
        "name": "Mira",
    }
    assert report["stages"]["derive"]["outcome"] == "ok"
    assert report["stages"]["derive"]["lineage_valid"] is True
    assert derived and all(row["parent_ids"] == [raw["record_id"]] for row in derived)
    assert {
        ("storage", "sqlite"),
        ("vector", "lancedb"),
        ("lexical", "tantivy"),
        ("embedding", "hash"),
    } <= providers
    assert any(category == "graph" and provider for category, provider in providers)
    assert report["stages"]["offline"]["outcome"] == "ok"
    assert all("status" in stage for stage in report["stages"]["offline"]["stages"].values())
    assert report["stages"]["retrieval"]["outcome"] == "ok"
    assert report["stages"]["retrieval"]["raw_or_derived_found"] is True
    assert report["stages"]["retrieval"]["derived_record_found"] is True
    assert report["configuration"]["database_file_backed"] is True
    assert report["configuration"]["sqlite_file_exists_during_trace"] is True


@pytest.mark.asyncio
async def test_litellm_transport_is_restored_after_success_and_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import litellm

    before = (litellm.acompletion, litellm.aembedding, litellm.arerank)
    paths_before = tuple(sys.path)
    await trace_module.run_trace(tmp_path / "success.sqlite")
    assert (litellm.acompletion, litellm.aembedding, litellm.arerank) == before
    assert tuple(sys.path) == paths_before

    def fail_catalog(_rows: list[dict]) -> list[dict]:
        raise RuntimeError("intentional trace failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(trace_module, "_safe_provider_rows", fail_catalog)
        with pytest.raises(RuntimeError, match="intentional trace failure"):
            await trace_module.run_trace(tmp_path / "failure.sqlite")
    assert (litellm.acompletion, litellm.aembedding, litellm.arerank) == before
    assert tuple(sys.path) == paths_before


@pytest.mark.asyncio
async def test_existing_database_path_is_refused_without_modification(tmp_path: Path) -> None:
    existing = tmp_path / "existing.sqlite"
    existing.write_bytes(b"existing user data")

    with pytest.raises(FileExistsError, match="refusing to use existing SQLite path"):
        await trace_module.run_trace(existing)

    assert existing.read_bytes() == b"existing user data"


def test_offline_outcome_reflects_failed_and_missing_stage_statuses() -> None:
    outcome, failures = trace_module._offline_stage_outcome(
        {"consolidate": {"status": "ok"}, "decay_sweep": {"status": "error"}}
    )
    assert outcome == "failed"
    assert failures == {"decay_sweep": {"status": "error"}}

    empty_outcome, empty_failures = trace_module._offline_stage_outcome({})
    assert empty_outcome == "failed"
    assert empty_failures == {"sleep": {"status": "unreported"}}
