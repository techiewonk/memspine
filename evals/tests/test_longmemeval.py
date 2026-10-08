"""Tiny-fixture tests for the LongMemEval adapter — no real benchmark data touched.

The fixture is three hand-written instances covering the branches that matter: an
information-extraction question, a temporal-reasoning question whose sessions are
deliberately out of chronological order, and an abstention instance. Everything asserted
here is schema/contract behaviour; nothing about benchmark performance.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import orjson
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals.datasets.base import BuildCache, IngestionStats  # noqa: E402
from evals.datasets.longmemeval import (  # noqa: E402
    ADAPTER_NAME,
    Ability,
    AnswerOutcome,
    BenchmarkSample,
    DatasetAdapter,
    IngestionDriver,
    IngestUnit,
    LongMemEvalAdapter,
    LongMemEvalFormatError,
    SessionOrder,
    build_sample,
    drive_ingestion,
    is_false_abstention,
    load_samples,
    scored_correct,
)


def _session(*contents: tuple[str, str, bool]) -> list[dict[str, Any]]:
    return [
        {"role": role, "content": content, "has_answer": has_answer}
        for role, content, has_answer in contents
    ]


# Session order in the file is 17:50, 14:47, 17:15 — matching the shape of the real
# release, where the array is neither chronological nor id-ordered.
_TEMPORAL = {
    "question_id": "gpt4_aaa111",
    "question_type": "temporal-reasoning",
    "question": "What did I do first?",
    "answer": "changed the tyres",
    "question_date": "2023/04/11 (Tue) 09:00",
    "haystack_dates": [
        "2023/04/10 (Mon) 17:50",
        "2023/04/10 (Mon) 14:47",
        "2023/04/10 (Mon) 17:15",
    ],
    "haystack_session_ids": ["sess_2", "sess_3", "sess_1"],
    "haystack_sessions": [
        _session(("user", "second thing", False)),
        _session(("user", "first thing", True), ("assistant", "noted", False)),
        _session(("user", "middle thing", False)),
    ],
    "answer_session_ids": ["sess_3"],
}

_EXTRACTION = {
    "question_id": "gpt4_bbb222",
    "question_type": "single-session-user",
    "question": "Where do I live?",
    "answer": "Lisbon",
    "question_date": "2023/05/20 (Sat) 02:36",
    "haystack_dates": ["2023/05/19 (Fri) 10:00"],
    "haystack_session_ids": ["sess_a"],
    "haystack_sessions": [_session(("user", "I moved to Lisbon", True))],
    "answer_session_ids": ["sess_a"],
}

_ABSTENTION = {
    "question_id": "gpt4_ccc333_abs",
    "question_type": "single-session-user_abs",
    "question": "What is my cat's name?",
    "answer": "The user never mentioned a cat.",
    "question_date": "2023/05/20 (Sat) 02:36",
    "haystack_dates": ["2023/05/19 (Fri) 10:00"],
    "haystack_session_ids": ["sess_b"],
    "haystack_sessions": [_session(("user", "I moved to Lisbon", False))],
}


@pytest.fixture
def data_file(tmp_path: Path) -> Path:
    path = tmp_path / "longmemeval_tiny.json"
    path.write_bytes(orjson.dumps([_TEMPORAL, _EXTRACTION, _ABSTENTION]))
    return path


# ── schema → typed records ──────────────────────────────────────────────────


def test_loads_typed_records(data_file: Path) -> None:
    samples = list(load_samples(data_file))
    assert [s.sample_id for s in samples] == ["gpt4_aaa111", "gpt4_bbb222", "gpt4_ccc333_abs"]

    temporal = samples[0]
    assert temporal.dataset == "longmemeval"
    assert temporal.namespace == "longmemeval/gpt4_aaa111"
    assert temporal.unit_count == 3
    assert temporal.turn_count == 4
    assert temporal.char_count == sum(
        len(c) for c in ("second thing", "first thing", "noted", "middle thing")
    )

    (query,) = temporal.queries
    assert query.ability is Ability.TEMPORAL_REASONING
    assert query.question_type == "temporal-reasoning"
    assert query.gold_answer == "changed the tyres"
    assert query.expects_abstention is False
    assert query.asked_at is not None
    assert (query.asked_at.year, query.asked_at.month, query.asked_at.day) == (2023, 4, 11)


def test_ability_mapping_covers_every_question_type() -> None:
    """``single-session-preference`` is deliberately NOT information extraction.

    Upstream evaluates preference application separately from single-session recall,
    and its own human-eval table uses ``single-session-user`` alone for the
    information-extraction column. Folding the 30 preference questions in would make
    our IE number non-comparable with every other paper reporting this benchmark.
    """
    cases = {
        "single-session-user": Ability.INFORMATION_EXTRACTION,
        "single-session-assistant": Ability.INFORMATION_EXTRACTION,
        "single-session-preference": Ability.PREFERENCE,
        "multi-session": Ability.MULTI_SESSION_REASONING,
        "knowledge-update": Ability.KNOWLEDGE_UPDATE,
        "temporal-reasoning": Ability.TEMPORAL_REASONING,
    }
    for question_type, expected in cases.items():
        raw = {**_EXTRACTION, "question_type": question_type}
        (query,) = build_sample(raw).queries
        assert query.ability is expected, question_type
        # The dataset-native label always survives the grouping.
        assert query.question_type == question_type


def test_unknown_question_type_hard_fails_but_can_be_labelled() -> None:
    raw = {**_EXTRACTION, "question_type": "brand-new-type"}
    with pytest.raises(LongMemEvalFormatError, match="unknown question_type"):
        build_sample(raw)
    (query,) = build_sample(raw, strict=False).queries
    assert query.ability is Ability.OTHER


def test_parallel_array_mismatch_is_reported(data_file: Path) -> None:
    raw = {**_TEMPORAL, "haystack_dates": _TEMPORAL["haystack_dates"][:2]}
    with pytest.raises(LongMemEvalFormatError, match="parallel arrays"):
        build_sample(raw)


def test_missing_file_raises_eagerly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_samples(tmp_path / "nope.json")


# ── session ordering ────────────────────────────────────────────────────────


def test_chronological_order_is_the_default(data_file: Path) -> None:
    temporal = next(iter(load_samples(data_file, question_ids={"gpt4_aaa111"})))
    assert [u.unit_id for u in temporal.units] == ["sess_3", "sess_1", "sess_2"]
    assert [u.index for u in temporal.units] == [0, 1, 2]
    assert [u.timestamp_raw for u in temporal.units] == [
        "2023/04/10 (Mon) 14:47",
        "2023/04/10 (Mon) 17:15",
        "2023/04/10 (Mon) 17:50",
    ]


def test_file_order_is_preserved_when_asked(data_file: Path) -> None:
    temporal = next(
        iter(load_samples(data_file, question_ids={"gpt4_aaa111"}, order=SessionOrder.FILE))
    )
    assert [u.unit_id for u in temporal.units] == ["sess_2", "sess_3", "sess_1"]


def test_unparseable_date_blocks_chronological_order() -> None:
    raw = {**_TEMPORAL, "haystack_dates": ["not a date", "2023/04/10 (Mon) 14:47", "x"]}
    with pytest.raises(LongMemEvalFormatError, match="block chronological ordering"):
        build_sample(raw)
    sample = build_sample(raw, strict=False)
    assert [u.unit_id for u in sample.units] == ["sess_2", "sess_3", "sess_1"]
    assert sample.units[0].timestamp is None
    assert sample.units[0].timestamp_raw == "not a date"


# ── evidence ────────────────────────────────────────────────────────────────


def test_evidence_sessions_are_flagged(data_file: Path) -> None:
    temporal = next(iter(load_samples(data_file, question_ids={"gpt4_aaa111"})))
    assert [u.has_evidence for u in temporal.units] == [True, False, False]
    (query,) = temporal.queries
    assert query.evidence_unit_ids == ("sess_3",)
    assert temporal.units[0].turns[0].has_answer is True


def test_missing_answer_session_ids_falls_back_to_has_answer() -> None:
    raw = {k: v for k, v in _TEMPORAL.items() if k != "answer_session_ids"}
    sample = build_sample(raw)
    (query,) = sample.queries
    assert query.evidence_unit_ids == ("sess_3",)


# ── abstention as a first-class outcome ─────────────────────────────────────


def test_abstention_is_detected_from_the_id_suffix(data_file: Path) -> None:
    sample = next(iter(load_samples(data_file, abilities={Ability.ABSTENTION})))
    (query,) = sample.queries
    assert query.query_id == "gpt4_ccc333_abs"
    assert query.ability is Ability.ABSTENTION
    assert query.expects_abstention is True
    # The upstream answer string is kept verbatim; expects_abstention drives scoring.
    assert query.gold_answer == "The user never mentioned a cat."
    assert query.question_type == "single-session-user_abs"


def test_abstaining_is_correct_on_an_abstention_question(data_file: Path) -> None:
    (abstain_query,) = next(iter(load_samples(data_file, abilities={Ability.ABSTENTION}))).queries
    assert scored_correct(abstain_query, AnswerOutcome.ABSTAINED) is True
    assert scored_correct(abstain_query, AnswerOutcome.ANSWERED, answer_matches_gold=True) is False
    assert is_false_abstention(abstain_query, AnswerOutcome.ABSTAINED) is False


def test_abstaining_is_distinguishable_from_answering_wrongly(data_file: Path) -> None:
    """The headline requirement: 'abstained' must not collapse into 'wrong'."""
    (query,) = next(iter(load_samples(data_file, question_ids={"gpt4_bbb222"}))).queries
    assert query.expects_abstention is False

    wrong = scored_correct(query, AnswerOutcome.ANSWERED, answer_matches_gold=False)
    abstained = scored_correct(query, AnswerOutcome.ABSTAINED)
    assert wrong is False
    assert abstained is False
    # Both score zero, but the harness can still tell them apart:
    assert is_false_abstention(query, AnswerOutcome.ABSTAINED) is True
    assert is_false_abstention(query, AnswerOutcome.ANSWERED) is False
    assert scored_correct(query, AnswerOutcome.ANSWERED, answer_matches_gold=True) is True


def test_judge_verdict_is_required_when_a_question_was_answered(data_file: Path) -> None:
    (query,) = next(iter(load_samples(data_file, question_ids={"gpt4_bbb222"}))).queries
    with pytest.raises(ValueError, match="answer_matches_gold is required"):
        scored_correct(query, AnswerOutcome.ANSWERED)
    assert scored_correct(query, AnswerOutcome.ERROR) is False


# ── filters ─────────────────────────────────────────────────────────────────


def test_filters_compose(data_file: Path) -> None:
    assert len(list(load_samples(data_file, limit=2))) == 2
    by_id = list(load_samples(data_file, question_ids={"gpt4_bbb222", "gpt4_ccc333_abs"}))
    assert [s.sample_id for s in by_id] == ["gpt4_bbb222", "gpt4_ccc333_abs"]
    by_ability = list(load_samples(data_file, abilities={Ability.INFORMATION_EXTRACTION}))
    assert [s.sample_id for s in by_ability] == ["gpt4_bbb222"]
    assert list(load_samples(data_file, question_ids=set())) == []


# ── ingestion driver contract ───────────────────────────────────────────────


class _RecordingDriver:
    """Minimal IngestionDriver — stands in for the memspine engine driver."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.messages: list[list[dict[str, str]]] = []

    async def begin_sample(self, sample: BenchmarkSample) -> None:
        self.events.append(f"begin:{sample.namespace}")

    async def ingest_unit(self, sample: BenchmarkSample, unit: IngestUnit) -> None:
        self.events.append(f"unit:{unit.index}:{unit.unit_id}")
        self.messages.append(unit.messages())

    async def end_sample(self, sample: BenchmarkSample) -> None:
        self.events.append(f"end:{sample.sample_id}")


def test_driver_receives_units_in_order(data_file: Path) -> None:
    sample = next(iter(load_samples(data_file, question_ids={"gpt4_aaa111"})))
    driver = _RecordingDriver()
    assert isinstance(driver, IngestionDriver)

    ingested = asyncio.run(drive_ingestion(sample, driver))

    assert ingested == 3
    assert driver.events == [
        "begin:longmemeval/gpt4_aaa111",
        "unit:0:sess_3",
        "unit:1:sess_1",
        "unit:2:sess_2",
        "end:gpt4_aaa111",
    ]
    assert driver.messages[0] == [
        {"role": "user", "content": "first thing"},
        {"role": "assistant", "content": "noted"},
    ]


def test_adapter_satisfies_the_dataset_protocol(data_file: Path) -> None:
    adapter = LongMemEvalAdapter(limit=1, order=SessionOrder.FILE)
    assert isinstance(adapter, DatasetAdapter)
    assert adapter.name == "longmemeval"
    samples = list(adapter.load(data_file))
    assert len(samples) == 1
    assert [u.unit_id for u in samples[0].units] == ["sess_2", "sess_3", "sess_1"]


# ── per-sample caching contract ─────────────────────────────────────────────


def test_cache_key_is_stable_and_content_addressed(data_file: Path) -> None:
    first = next(iter(load_samples(data_file, question_ids={"gpt4_aaa111"})))
    again = next(iter(load_samples(data_file, question_ids={"gpt4_aaa111"})))
    assert first.content_hash == again.content_hash

    # Ordering changes what gets built, so it must change the key.
    file_order = next(
        iter(load_samples(data_file, question_ids={"gpt4_aaa111"}, order=SessionOrder.FILE))
    )
    assert file_order.content_hash != first.content_hash

    # Edited history invalidates; a different question over the same history does not
    # (the cache holds a built memory, which the question never touches).
    edited = build_sample(
        {
            **_TEMPORAL,
            "haystack_sessions": [
                _session(("user", "CHANGED", False)),
                *_TEMPORAL["haystack_sessions"][1:],
            ],
        }
    )
    assert edited.content_hash != first.content_hash
    requestioned = build_sample({**_TEMPORAL, "question": "something else entirely"})
    assert requestioned.content_hash == first.content_hash


def _stats(sample: BenchmarkSample) -> IngestionStats:
    return IngestionStats(
        sample_id=sample.sample_id,
        namespace=sample.namespace,
        sessions=sample.unit_count,
        turns=sample.turn_count,
        records=sample.turn_count,
        characters=sample.char_count,
        sleep_cycles=0,
        wall_seconds=0.0,
    )


def _cache(root: Path, *, profile: str = "lean", config: str = "c" * 64) -> BuildCache:
    return BuildCache(root=root, dataset=ADAPTER_NAME, profile=profile, config_hash=config)


def test_cache_prepare_commit_and_rebuild(tmp_path: Path, data_file: Path) -> None:
    sample = next(iter(load_samples(data_file, question_ids={"gpt4_aaa111"})))
    slot = _cache(tmp_path / "cache").slot(sample.sample_id)

    assert slot.path.parent == tmp_path / "cache" / "longmemeval"
    assert slot.matches(sample.content_hash) is False
    assert slot.prepare(sample.content_hash) is True

    built = slot.storage_path / "memory.bin"
    built.write_bytes(b"built")
    slot.commit(content_hash=sample.content_hash, namespace=sample.namespace, stats=_stats(sample))

    assert slot.matches(sample.content_hash) is True
    assert slot.prepare(sample.content_hash) is False  # reuse, nothing removed
    assert built.exists()

    assert slot.prepare(sample.content_hash, rebuild=True) is True  # --rebuild clears it
    assert not built.exists()
    assert slot.matches(sample.content_hash) is False


def test_stale_content_hash_invalidates_the_entry(tmp_path: Path, data_file: Path) -> None:
    sample = next(iter(load_samples(data_file, question_ids={"gpt4_aaa111"})))
    slot = _cache(tmp_path / "cache").slot(sample.sample_id)
    slot.prepare(sample.content_hash)
    slot.commit(content_hash=sample.content_hash, namespace=sample.namespace, stats=_stats(sample))
    assert slot.matches(sample.content_hash) is True
    assert slot.matches("deadbeef") is False
    assert slot.prepare("deadbeef") is True


def test_cache_separates_profiles_configs_and_ingest_options(
    tmp_path: Path, data_file: Path
) -> None:
    """The two components the original adapter's key was missing.

    Without ``config_hash`` a run under one governance configuration silently reuses a
    memory built under another; without ``options_hash`` the same happens when only the
    ingestion options differ. Neither fails — both report.
    """
    sample = next(iter(load_samples(data_file, question_ids={"gpt4_aaa111"})))
    root = tmp_path / "cache"
    lean = _cache(root, profile="lean").slot(sample.sample_id)
    full = _cache(root, profile="full").slot(sample.sample_id)
    other_config = _cache(root, profile="lean", config="d" * 64).slot(sample.sample_id)
    other_options = _cache(root, profile="lean").slot(sample.sample_id, options_hash="ff" * 32)

    lean.prepare(sample.content_hash)
    lean.commit(content_hash=sample.content_hash, namespace=sample.namespace, stats=_stats(sample))
    assert lean.matches(sample.content_hash) is True
    for other in (full, other_config, other_options):
        assert other.path != lean.path
        assert other.matches(sample.content_hash) is False
