"""Provenance is structural: the tests assert what the harness *cannot* do."""

from __future__ import annotations

import pytest
from memspine_evals.contracts import DatasetInfo
from memspine_evals.judge import JudgeScale, JudgeSpec
from memspine_evals.provenance import ReaderSpec, RunManifest, RunProtocol, SystemSpec


def dataset(**kwargs) -> DatasetInfo:
    base = {
        "dataset_id": "locomo",
        "revision_id": "2024-original",
        "licence": "unknown",
        "source_path": "/tmp/locomo10.json",
        "content_sha256": "abc",
        "n_items": 10,
        "n_queries": 100,
    }
    base.update(kwargs)
    return DatasetInfo(**base)


def manifest(**kwargs) -> RunManifest:
    parts = {
        "run_id": "r1",
        "dataset": dataset(),
        "system": SystemSpec(system_id="verbatim-bm25"),
        "reader": ReaderSpec(reader_id="openai-compat", model="gpt-4o", makes_model_calls=True),
        "judge": JudgeSpec(
            judge_id="llm",
            scale=JudgeScale.BINARY,
            model="gpt-4o",
            prompt_hash="h",
            makes_model_calls=True,
        ),
        "protocol": RunProtocol(protocol_id="p1", budget_tokens=4000, top_k=10, seed=0),
        "token_counter": {"counter_id": "heuristic-chars4"},
    }
    parts.update(kwargs)
    return RunManifest.build(**parts)


def test_dataset_without_revision_is_rejected() -> None:
    with pytest.raises(ValueError, match="revision_id"):
        dataset(revision_id="  ")


def test_judge_scale_cannot_be_implied() -> None:
    with pytest.raises(TypeError):
        JudgeSpec(judge_id="x", scale="binary")  # type: ignore[arg-type]


def test_llm_judge_must_record_model_and_prompt() -> None:
    with pytest.raises(ValueError, match="model id"):
        JudgeSpec(judge_id="x", scale=JudgeScale.BINARY, makes_model_calls=True)
    with pytest.raises(ValueError, match="prompt hash"):
        JudgeSpec(judge_id="x", scale=JudgeScale.BINARY, model="m", makes_model_calls=True)


def test_protocol_requires_a_budget() -> None:
    with pytest.raises(ValueError, match="token budget"):
        RunProtocol(protocol_id="p", budget_tokens=0, top_k=5, seed=0)


def test_complete_run_is_admissible_under_d16() -> None:
    m = manifest()
    assert m.missing_protocol_fields() == ()
    assert m.admissible_d16 is True


def test_retrieval_only_run_is_not_an_answer_metric() -> None:
    m = manifest(
        reader=ReaderSpec(reader_id="context-only", model="none", makes_model_calls=False),
        judge=JudgeSpec(judge_id="recall", scale=JudgeScale.RETRIEVAL_RECALL),
    )
    assert "reader.model" in m.missing_protocol_fields()
    assert m.admissible_d16 is False
    assert JudgeScale.RETRIEVAL_RECALL.is_answer_metric is False


def test_score_matrix_row_carries_the_two_mandatory_columns() -> None:
    row = manifest().score_matrix_row(0.8123, 100)
    assert row["judge_scale"] == "binary"
    assert row["data_revision"] == "2024-original"
    assert row["backbone"] == "gpt-4o"
    assert row["admissible_d16"] == "yes"
    assert row["evidence_class"] == "harness-run"


def test_manifest_records_code_and_environment() -> None:
    payload = manifest().to_dict()
    assert payload["code"]["memspine_git"]
    assert payload["env"]["python"]
    assert payload["system"]["config_hash"]
