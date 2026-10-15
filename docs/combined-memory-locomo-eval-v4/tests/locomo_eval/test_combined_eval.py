"""Leakage boundaries, scoring denominators, and combined engine orchestration."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from module.locomo_eval.backend import CombinedMemoryBackend
from module.locomo_eval.dataset import Question, Sample, Turn, load_dataset, parse_timestamp
from module.locomo_eval.metrics import score_rows, token_f1
from module.locomo_eval.runner import answer_messages, run_samples


def turn(role="user", speaker="Alice", dia_id="D1:1"):
    return Turn(1, dia_id, speaker, "I moved to Pune.", datetime(2023, 5, 8, tzinfo=UTC), role, 1)


def row(category=4, prediction="Pune", error=None, index=0):
    return dict(
        sample_id="s1",
        question_index=index,
        profile="current",
        memory_type="both",
        category=category,
        prediction=prediction,
        reference="Pune",
        contexts={"graph": "Pune"},
        error=error,
    )


def test_local_f1_penalizes_extra_and_missing_facts():
    assert token_f1("The Pune.", "Pune") == 1
    assert token_f1("Pune Mumbai", "Pune") == pytest.approx(2 / 3)
    assert token_f1("Pune", "Pune Mumbai") == pytest.approx(2 / 3)
    assert token_f1("", "Pune") == 0


def test_failures_remain_in_denominator_and_adversarial_is_separate():
    result = score_rows(
        [
            row(),
            row(error="TimeoutError", index=1),
            row(category=5, prediction="Not mentioned in the conversation", index=2),
        ]
    )
    group = result["current/both"]
    assert group["count"] == 3
    assert group["token_f1_categories_1_4_pct"] == 50
    assert group["adversarial_abstention_pct"] == 100
    assert group["error_count"] == 1
    assert "official_locomo_categories_1_4_pct" not in group
    with pytest.raises(ValueError, match="Duplicate"):
        score_rows([row(), row()])


def test_official_evaluator_receives_blanked_errors():
    def official(rows):
        assert rows[1]["prediction"] == ""
        return [0.8, 0], 0, [1, 1]

    group = score_rows([row(), row(error="ValueError", index=1)], official)["current/both"]
    assert group["official_locomo_categories_1_4_pct"] == 40


@pytest.mark.parametrize(
    "prediction,expected",
    [
        ("Not mentioned in the conversation.", 100),
        ("No relevant information found.", 100),
        ("It was not mentioned, but Alice moved to Pune.", 0),
        ("Not mentioned in the conversation. My guess is Pune.", 0),
    ],
)
def test_adversarial_abstention_requires_complete_response(prediction, expected):
    result = score_rows([row(category=5, prediction=prediction)])
    assert result["current/both"]["adversarial_abstention_pct"] == expected


def test_judge_errors_remain_in_denominator():
    a, b = row(), row(index=1)
    a["judge_score"] = 1
    b["judge_error"] = "TimeoutError"
    group = score_rows([a, b])["current/both"]
    assert group["strict_judge_pass_pct"] == 50
    assert group["judge_error_count"] == 1


def test_datetime_and_dataset_never_import_annotation_summaries(tmp_path):
    assert parse_timestamp("1:56 pm on 8 May, 2023").year == 2023
    raw = [
        {
            "sample_id": "sample",
            "conversation": {
                "speaker_a": "Alice",
                "speaker_b": "Bob",
                "session_10_date_time": "2023-05-09T00:00:00Z",
                "session_10": [
                    {"speaker": "Bob", "dia_id": "D10:1", "text": "Nice!", "blip_caption": "a dog"}
                ],
                "session_2_date_time": "2023-05-08T00:00:00Z",
                "session_2": [{"speaker": "Alice", "dia_id": "D2:1", "text": "Pune"}],
            },
            "qa": [
                {
                    "question": "Where?",
                    "answer": "SECRET_REFERENCE",
                    "category": 4,
                    "evidence": ["D2:1"],
                }
            ],
            "event_summary": {"secret": "SECRET_ANNOTATION"},
        }
    ]
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    sample = load_dataset(path)[0]
    assert [t.session for t in sample.turns] == [2, 10]
    assert "SECRET" not in repr(sample.turns)
    assert "Image caption: a dog" in sample.turns[1].content
    assert sample.turns[0].role == "user"
    raw[0]["qa"][0].pop("answer")
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="Missing reference"):
        load_dataset(path)


def test_answer_generation_boundary():
    prompt = json.loads(
        answer_messages("Where did Alice move?", {"long_term_context": "Alice moved to Pune"})[-1][
            "content"
        ]
    )
    assert set(prompt) == {"question", "memories"}


def test_runner_evaluates_both_backends_and_saves_errors(tmp_path):
    class FakeBackend:
        async def ingest(self, sample_id, turns):
            assert not hasattr(turns[0], "reference")
            return {"turns": len(turns)}

        def set_profile(self, profile):
            self.profile = profile

        def describe(self):
            return {"profile": self.profile}

        async def retrieve(self, sample_id, question, mode):
            if mode == "shortterm":
                raise TimeoutError()
            return {
                "long_term_context": "Alice moved to Pune",
                "short_term_context": "Bob knows Alice",
            }

        async def answer(self, messages):
            assert "SECRET_REFERENCE" not in str(messages)
            return "Pune"

    sample = Sample(
        "s1", (turn(),), (Question(0, "Where did Alice move?", "SECRET_REFERENCE", 4, ("D1:1",)),)
    )
    path = tmp_path / "predictions.jsonl"
    rows = asyncio.run(
        run_samples(
            [sample],
            FakeBackend(),
            path,
            profiles=["current", "enhanced"],
            memory_types=["both", "longterm", "shortterm"],
        )
    )
    assert len(rows) == 6
    assert sum(bool(r["error"]) for r in rows) == 2
    assert len(path.read_text(encoding="utf-8").splitlines()) == 6
    with pytest.raises(FileExistsError):
        asyncio.run(
            run_samples([sample], FakeBackend(), path, profiles=["current"], memory_types=["both"])
        )


@pytest.mark.parametrize("policy,episodes,vector_turns", [("production", 1, 1), ("corpus", 2, 2)])
def test_waitable_ingestion_preserves_speaker_and_historical_time(policy, episodes, vector_turns):
    backend = CombinedMemoryBackend(
        run_id="test",
        ingestion=policy,
        session_scope="all",
        last_n=0,
        answer_model=None,
        judge_model=None,
    )
    backend.set_profile = lambda profile: None
    backend.ingest_assistant_episodes = False
    backend.session_store = SimpleNamespace(create=AsyncMock())
    backend.message_store = SimpleNamespace(
        insert_one=AsyncMock(return_value=SimpleNamespace(message_id="id"))
    )
    backend.graph = SimpleNamespace(
        store_message=AsyncMock(
            return_value={"messages": [{"uuid": "episode"}], "summary_errors": []}
        )
    )
    backend.vector = SimpleNamespace(add_dialogues_batch=AsyncMock(return_value=3))
    counts = asyncio.run(backend.ingest("s1", (turn(), turn("assistant", "Bob", "D1:2"))))
    assert counts["graph_episodes"] == episodes
    for call in backend.graph.store_message.await_args_list:
        assert call.kwargs["is_migration"] is True
        assert call.kwargs["reference_time"].year == 2023
    call = backend.vector.add_dialogues_batch.await_args
    assert len(call.args[2]) == vector_turns
    assert all(name in content for name, content in call.args[2])
    assert call.kwargs["timestamp"].startswith("2023-05-08")


def test_retrieval_reuses_combined_production_service_per_session():
    backend = CombinedMemoryBackend(
        run_id="test",
        ingestion="production",
        session_scope="all",
        last_n=0,
        answer_model=None,
        judge_model=None,
    )
    backend.sessions["s1"] = ["session-1", "session-2"]
    backend.service = SimpleNamespace(
        get_memory_context=AsyncMock(
            return_value={
                "long_term_context": "same graph fact",
                "short_term_context": "vector fact",
                "last_n_messages_context": "",
            }
        )
    )
    result = asyncio.run(backend.retrieve("s1", "Where?", "both"))
    assert result["long_term_context"] == "same graph fact"
    assert backend.service.get_memory_context.await_count == 2
    assert all(
        call.kwargs["memory_type"] == "both"
        for call in backend.service.get_memory_context.await_args_list
    )
    backend.session_scope = "latest"
    backend.service.get_memory_context.reset_mock()
    asyncio.run(backend.retrieve("s1", "Where?", "both"))
    assert backend.service.get_memory_context.await_count == 1
