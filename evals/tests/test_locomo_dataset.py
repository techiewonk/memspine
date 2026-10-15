"""Unit tests for the LoCoMo adapter.

Everything here runs against a hand-written inline fixture. The real corpus is
Snap Inc.'s, is never committed to this repository, and must never be a test
dependency — a test that needs `locomo10.json` is a test that cannot run in CI.

The fixture reproduces the *shapes* that bite: an out-of-order session key, a
``session_N_date_time`` with no matching turn array, a multimodal turn, an
integer gold answer, and an adversarial item whose reference lives under
``adversarial_answer``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

# evals/ sits at the repo root, outside the wheel (D-19/D-35), so it is not on
# sys.path via the installed package. Add the repo root explicitly; `evals` then
# imports as a namespace package.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals.legacy_datasets.base import BuildCache  # noqa: E402
from evals.legacy_datasets.locomo import (  # noqa: E402
    ADAPTER_NAME,
    DATASET_PATH_ENV,
    MANIFEST_VERSION,
    IngestOptions,
    LocomoCategory,
    LocomoFormatError,
    LocomoSample,
    SupportsEngineWrite,
    build_content_hash,
    build_or_load_memory,
    category_counts,
    config_hash,
    ingest_sample,
    iter_locomo,
    load_locomo,
    parse_locomo,
    resolve_dataset_path,
    sample_namespace,
)

# ── fixture ──────────────────────────────────────────────────────────────────

FIXTURE: list[dict[str, Any]] = [
    {
        "sample_id": "conv-fixture",
        "conversation": {
            "speaker_a": "Ada",
            "speaker_b": "Bo",
            # Deliberately out of numeric order: the loader must sort, not trust
            # dict insertion order.
            "session_2": [
                {"speaker": "Ada", "dia_id": "D2:1", "text": "I adopted a greyhound."},
                {
                    "speaker": "Bo",
                    "dia_id": "D2:2",
                    "text": "She looks fast!",
                    "img_url": ["https://example.invalid/dog.jpg"],
                    "blip_caption": "a greyhound on a beach",
                    "query": "greyhound beach",
                    "re-download": 0,
                },
            ],
            "session_2_date_time": "9:15 am on 3 June, 2023",
            "session_1": [
                {"speaker": "Ada", "dia_id": "D1:1", "text": "I started a pottery class."},
                {"speaker": "Bo", "dia_id": "D1:2", "text": "Nice, when did you start?"},
                {"speaker": "Ada", "dia_id": "D1:3", "text": "Last Tuesday."},
            ],
            "session_1_date_time": "1:56 pm on 8 May, 2023",
            # A date for a session that was never released — present in the real
            # corpus, and it must not conjure an empty session.
            "session_7_date_time": "4:00 pm on 1 August, 2023",
        },
        "qa": [
            {
                "question": "What did Ada start?",
                "answer": "a pottery class",
                "evidence": ["D1:1"],
                "category": 4,
            },
            {
                "question": "In what year did Ada start pottery?",
                "answer": 2023,
                "evidence": ["D1:1"],
                "category": 2,
            },
            {
                "question": "Why did Ada give up pottery?",
                "evidence": ["D1:3"],
                "category": 5,
                "adversarial_answer": "No information available.",
            },
            {
                "question": "What links the pottery class and the greyhound?",
                "answer": "both are new commitments Ada made in 2023",
                "evidence": ["D1:1", "D2:1"],
                "category": 1,
            },
        ],
    }
]


@pytest.fixture
def sample() -> LocomoSample:
    return parse_locomo(FIXTURE)[0]


@pytest.fixture
def dataset_file(tmp_path: Path) -> Path:
    path = tmp_path / "locomo_fixture.json"
    path.write_text(json.dumps(FIXTURE), encoding="utf-8")
    return path


# ── fake engine ──────────────────────────────────────────────────────────────


class FakeEngine:
    """Records what the write door was asked to do. Structurally an engine."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.sleeps = 0

    async def write_messages(
        self,
        messages: Sequence[Mapping[str, str]],
        namespace: str = "default",
        actor: str = "user",
        session_id: str | None = None,
        channel: str = "messages",
        group_id: str | None = None,
        tags: list[str] | None = None,
    ) -> Sequence[object]:
        self.calls.append(
            {
                "messages": [dict(m) for m in messages],
                "namespace": namespace,
                "session_id": session_id,
                "channel": channel,
                "group_id": group_id,
                "tags": list(tags or []),
            }
        )
        return [object() for _ in messages]

    async def sleep(self) -> Mapping[str, Mapping[str, object]]:
        self.sleeps += 1
        return {}


def test_fake_engine_satisfies_the_protocol() -> None:
    assert isinstance(FakeEngine(), SupportsEngineWrite)


# ── parsing ──────────────────────────────────────────────────────────────────


def test_sessions_are_sorted_and_orphan_date_keys_ignored(sample: LocomoSample) -> None:
    assert [s.index for s in sample.sessions] == [1, 2]
    assert sample.session_count == 2
    assert sample.turn_count == 5
    assert sample.sessions[0].date_time == "1:56 pm on 8 May, 2023"


def test_turn_fields_and_image_metadata(sample: LocomoSample) -> None:
    turn = sample.sessions[1].turns[1]
    assert turn.dia_id == "D2:2"
    assert turn.speaker == "Bo"
    assert turn.session_index == 2
    assert turn.turn_index == 1
    assert turn.image_urls == ("https://example.invalid/dog.jpg",)
    assert turn.image_caption == "a greyhound on a beach"
    assert turn.image_query == "greyhound beach"


def test_render_carries_date_and_image_caption(sample: LocomoSample) -> None:
    plain = sample.sessions[0].turns[0]
    assert plain.render() == "[1:56 pm on 8 May, 2023] Ada: I started a pottery class."
    assert plain.render(with_timestamp=False) == "Ada: I started a pottery class."

    with_image = sample.sessions[1].turns[1]
    rendered = with_image.render()
    assert rendered.startswith("[9:15 am on 3 June, 2023] Bo: She looks fast!")
    assert rendered.endswith("[shared image: a greyhound on a beach]")


def test_categories_resolve_and_survive_end_to_end(sample: LocomoSample) -> None:
    by_category = sample.questions_by_category()
    assert set(by_category) == {
        LocomoCategory.SINGLE_HOP,
        LocomoCategory.TEMPORAL,
        LocomoCategory.ADVERSARIAL,
        LocomoCategory.MULTI_HOP,
    }
    assert category_counts(sample) == {
        "single-hop": 1,
        "temporal": 1,
        "adversarial": 1,
        "multi-hop": 1,
    }
    assert next(q.category_label for q in sample.questions) == "single-hop"


def test_question_ids_are_stable_and_addressable(sample: LocomoSample) -> None:
    assert [q.question_id for q in sample.questions] == [
        "conv-fixture:q0",
        "conv-fixture:q1",
        "conv-fixture:q2",
        "conv-fixture:q3",
    ]


def test_integer_gold_answers_are_normalised(sample: LocomoSample) -> None:
    temporal = sample.questions[1]
    assert temporal.answer == "2023"
    assert temporal.gold == "2023"


def test_adversarial_gold_falls_back_to_adversarial_answer(sample: LocomoSample) -> None:
    adversarial = sample.questions[2]
    assert adversarial.is_adversarial
    assert adversarial.answer is None
    assert adversarial.gold == "No information available."
    assert adversarial.evidence == ("D1:3",)


def test_unknown_category_degrades_without_losing_the_raw_code() -> None:
    payload = json.loads(json.dumps(FIXTURE))
    payload[0]["qa"][0]["category"] = 9
    question = parse_locomo(payload)[0].questions[0]
    assert question.category is LocomoCategory.UNKNOWN
    assert question.raw_category == 9
    assert question.category_label == "unknown"


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda p: p[0].pop("sample_id"), "sample_id"),
        (lambda p: p[0]["conversation"].pop("speaker_a"), "speaker_a"),
        (lambda p: p[0]["qa"][0].pop("category"), "category"),
        (lambda p: p[0]["conversation"]["session_1"][0].pop("text"), "text"),
    ],
)
def test_malformed_payloads_raise_locomo_format_error(mutate: Any, fragment: str) -> None:
    payload = json.loads(json.dumps(FIXTURE))
    mutate(payload)
    with pytest.raises(LocomoFormatError) as excinfo:
        parse_locomo(payload)
    assert fragment in str(excinfo.value)


def test_non_array_payload_is_rejected() -> None:
    with pytest.raises(LocomoFormatError):
        parse_locomo({"sample_id": "nope"})


# ── file loading ─────────────────────────────────────────────────────────────


def test_load_and_iterate_from_a_file(dataset_file: Path) -> None:
    samples = load_locomo(dataset_file)
    assert [s.sample_id for s in samples] == ["conv-fixture"]
    assert [s.sample_id for s in iter_locomo(dataset_file)] == ["conv-fixture"]


def test_sample_id_filter_and_limit(dataset_file: Path) -> None:
    assert load_locomo(dataset_file, sample_ids=["conv-fixture"])
    assert load_locomo(dataset_file, limit=0) == ()
    with pytest.raises(LocomoFormatError):
        load_locomo(dataset_file, sample_ids=["conv-missing"])


def test_dataset_path_resolution(dataset_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DATASET_PATH_ENV, raising=False)
    with pytest.raises(FileNotFoundError, match="does not vendor"):
        resolve_dataset_path(None)
    monkeypatch.setenv(DATASET_PATH_ENV, str(dataset_file))
    assert resolve_dataset_path(None) == dataset_file
    assert resolve_dataset_path(dataset_file) == dataset_file
    with pytest.raises(FileNotFoundError):
        resolve_dataset_path(dataset_file.parent / "absent.json")


# ── ingestion driver ─────────────────────────────────────────────────────────


async def test_ingest_uses_one_write_per_session_through_the_write_door(
    sample: LocomoSample,
) -> None:
    engine = FakeEngine()
    stats = await ingest_sample(engine, sample)

    assert len(engine.calls) == 2
    first, second = engine.calls
    assert first["session_id"] == "conv-fixture:session:1"
    assert first["group_id"] == first["session_id"]
    assert first["namespace"] == sample_namespace(sample) == "locomo/conv-fixture"
    assert first["channel"] == "locomo"
    assert first["tags"] == ["locomo:conv-fixture", "session:1"]
    assert second["session_id"] == "conv-fixture:session:2"

    messages = first["messages"]
    assert isinstance(messages, list)
    assert [m["role"] for m in messages] == ["user", "user", "user"]
    assert messages[0]["content"].startswith("[1:56 pm on 8 May, 2023] Ada:")

    assert stats.sessions == 2
    assert stats.turns == 5
    assert stats.records == 5
    assert stats.sleep_cycles == 0
    assert stats.characters > 0
    assert stats.wall_seconds >= 0.0


async def test_speaker_role_mapping_is_overridable(sample: LocomoSample) -> None:
    engine = FakeEngine()
    options = IngestOptions(role_for_speaker=lambda s: "assistant" if s == "Bo" else "user")
    await ingest_sample(engine, sample, options=options)
    messages = engine.calls[0]["messages"]
    assert isinstance(messages, list)
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]


async def test_sleep_cadence_and_progress_callback(sample: LocomoSample) -> None:
    engine = FakeEngine()
    seen: list[tuple[int, int]] = []
    options = IngestOptions(
        sleep_every_sessions=1, sleep_at_end=True, on_session=lambda i, n: seen.append((i, n))
    )
    stats = await ingest_sample(engine, sample, options=options)
    assert engine.sleeps == 3
    assert stats.sleep_cycles == 3
    assert seen == [(1, 2), (2, 2)]


async def test_empty_sessions_are_skipped_not_written() -> None:
    payload = json.loads(json.dumps(FIXTURE))
    payload[0]["conversation"]["session_3"] = []
    engine = FakeEngine()
    await ingest_sample(engine, parse_locomo(payload)[0])
    assert [call["session_id"] for call in engine.calls] == [
        "conv-fixture:session:1",
        "conv-fixture:session:2",
    ]


# ── cache ────────────────────────────────────────────────────────────────────


def test_config_hash_is_stable_and_order_independent() -> None:
    left = config_hash({"profile": "benchmark", "firewall": True})
    right = config_hash({"firewall": True, "profile": "benchmark"})
    assert left == right
    assert left != config_hash({"profile": "benchmark", "firewall": False})
    assert len(left) == 64


def _cache(root: Path, *, profile: str = "benchmark", config: str = "c" * 64) -> BuildCache:
    return BuildCache(
        root=root, dataset=ADAPTER_NAME, profile=profile, config_hash=config, adapter_version="1"
    )


def test_cache_key_slug_is_filesystem_safe() -> None:
    cache = _cache(Path("/cache"), profile="lean/one", config="a" * 64)
    slug = cache.slot("conv/26", options_hash="b" * 64).key.slug()
    assert "/" not in slug
    assert slug == "conv_26__lean_one__" + "a" * 16 + "__" + "b" * 8 + "__v1"


def test_ingest_options_that_change_the_build_change_the_cache_key() -> None:
    """The regression this key exists to prevent: pricing the consolidation layer by
    flipping the sleep cadence must not return a store that never consolidated."""
    lean = IngestOptions()
    slept = IngestOptions(sleep_every_sessions=1, sleep_at_end=True)
    other_channel = IngestOptions(channel="ingest")
    no_timestamps = IngestOptions(with_timestamp=False)
    roles = IngestOptions(role_for_speaker=lambda s: "assistant", role_map_id="ab-alternating")

    digests = {options.digest() for options in (lean, slept, other_channel, no_timestamps, roles)}
    assert len(digests) == 5

    # A progress callback observes the build; it does not change it.
    assert IngestOptions(on_session=lambda i, n: None).digest() == lean.digest()
    # An unused role_map_id label likewise must not fragment the cache.
    assert IngestOptions(role_map_id="unused-label").digest() == lean.digest()


def _factory(engine: FakeEngine, opened: list[Path]) -> Any:
    @asynccontextmanager
    async def factory(storage_dir: Path) -> AsyncIterator[SupportsEngineWrite]:
        opened.append(storage_dir)
        yield engine

    return factory


async def test_build_then_reuse_skips_construction(sample: LocomoSample, tmp_path: Path) -> None:
    cache = _cache(tmp_path / "cache")
    engine = FakeEngine()
    opened: list[Path] = []

    first = await build_or_load_memory(sample, cache=cache, engine_factory=_factory(engine, opened))
    assert first.reused is False
    assert first.stats.records == 5
    assert first.namespace == "locomo/conv-fixture"
    assert opened == [first.slot.storage_path]
    assert first.slot.storage_path.is_dir()

    manifest_data = json.loads(first.slot.manifest_path.read_text(encoding="utf-8"))
    assert manifest_data["version"] == MANIFEST_VERSION
    assert manifest_data["dataset"] == ADAPTER_NAME
    assert manifest_data["sample_id"] == "conv-fixture"
    assert manifest_data["content_hash"] == build_content_hash(sample)
    assert manifest_data["options_hash"] == IngestOptions().digest()

    second = await build_or_load_memory(
        sample, cache=cache, engine_factory=_factory(engine, opened)
    )
    assert second.reused is True
    # The engine was never opened again, and the cached run still reports cost.
    assert opened == [first.slot.storage_path]
    assert len(engine.calls) == 2
    assert second.stats.records == 5
    assert second.namespace == "locomo/conv-fixture"


async def test_rebuild_flag_forces_reconstruction(sample: LocomoSample, tmp_path: Path) -> None:
    cache = _cache(tmp_path / "cache")
    engine = FakeEngine()
    opened: list[Path] = []
    await build_or_load_memory(sample, cache=cache, engine_factory=_factory(engine, opened))
    outcome = await build_or_load_memory(
        sample, cache=cache, engine_factory=_factory(engine, opened), rebuild=True
    )
    assert outcome.reused is False
    assert len(opened) == 2
    assert len(engine.calls) == 4


async def test_changed_config_or_content_misses_the_cache(
    sample: LocomoSample, tmp_path: Path
) -> None:
    root = tmp_path / "cache"
    engine = FakeEngine()
    opened: list[Path] = []
    built = await build_or_load_memory(
        sample,
        cache=_cache(root),
        engine_factory=_factory(engine, opened),
    )

    # A different engine config is a different slot entirely.
    other_config = _cache(root, config="d" * 64)
    assert other_config.slot(sample.sample_id).path != built.slot.path
    assert other_config.slot(sample.sample_id).matches(build_content_hash(sample)) is False

    # Same cache, different ingest options: also a different slot entirely.
    slept = IngestOptions(sleep_every_sessions=1)
    assert _cache(root).slot(sample.sample_id, options_hash=slept.digest()).path != built.slot.path

    # Same slot, edited corpus: the content hash invalidates the build.
    payload = json.loads(json.dumps(FIXTURE))
    payload[0]["conversation"]["session_1"][0]["text"] = "I started a welding class."
    edited = parse_locomo(payload)[0]
    assert built.slot.matches(build_content_hash(edited)) is False
    assert built.slot.matches(build_content_hash(sample)) is True


async def test_corrupt_manifest_is_treated_as_a_miss(sample: LocomoSample, tmp_path: Path) -> None:
    cache = _cache(tmp_path / "cache")
    engine = FakeEngine()
    opened: list[Path] = []
    built = await build_or_load_memory(sample, cache=cache, engine_factory=_factory(engine, opened))
    built.slot.manifest_path.write_text("{not json", encoding="utf-8")
    assert built.slot.read_manifest() is None
    assert built.slot.matches(build_content_hash(sample)) is False

    outcome = await build_or_load_memory(
        sample, cache=cache, engine_factory=_factory(engine, opened)
    )
    assert outcome.reused is False


def test_reset_clears_a_previous_build(sample: LocomoSample, tmp_path: Path) -> None:
    cache = _cache(tmp_path / "cache")
    slot = cache.slot(sample.sample_id)
    slot.reset()
    stray = slot.storage_path / "events.sqlite"
    stray.write_text("not really a database", encoding="utf-8")
    slot.manifest_path.write_text("{}", encoding="utf-8")
    slot.reset()
    assert slot.storage_path.is_dir()
    assert not stray.exists()
    assert not slot.manifest_path.exists()
