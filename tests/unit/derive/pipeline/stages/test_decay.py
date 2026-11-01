"""``memspine.derive.pipeline.stages.decay``: the public stage functions.

The end-to-end tier and compression wiring lives in ``tests/unit/wiring/test_ow_d_decay.py``."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from memspine import Engine
from memspine.config.schema import OfflineConfig
from memspine.derive.decay import CompressJob, LevelSpec, Resolution, Tier
from memspine.derive.pipeline.stages import decay

PARAGRAPH = (
    "The committee reviewed the roof repairs budget. Contractors start in March! "
    "Residents asked about roof access and repairs scheduling."
)


def _job(*levels: tuple[Resolution, Resolution, int]) -> CompressJob:
    return CompressJob(
        job_id="j",
        group_id="g",
        lineage=("r1", "r2"),
        input_tokens=500,
        levels=tuple(LevelSpec(level=a, source_level=b, token_budget=t) for a, b, t in levels),
        member_tier_after=Tier.DORMANT,
    )


def _engine() -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
        read={"record_access": False},
        offline={"decay": True},
    )


def test_decay_policy_reads_the_offline_config() -> None:
    offline = OfflineConfig(
        decay_boundaries=(0.6, 0.4, 0.2),
        decay_margin=0.02,
        decay_protected_kinds=["semantic", "procedural"],
    )
    ctx = SimpleNamespace(config=SimpleNamespace(offline=offline))
    policy = decay.decay_policy(ctx)  # type: ignore[arg-type]
    assert policy.boundaries == (0.6, 0.4, 0.2)
    assert policy.margin == 0.02
    assert policy.protected_kinds == frozenset({"semantic", "procedural"})


def test_level_texts_cuts_the_shorter_levels_from_the_paragraph() -> None:
    job = _job(
        (Resolution.PARAGRAPH, Resolution.FULL, 200),
        (Resolution.SENTENCE, Resolution.PARAGRAPH, 40),
        (Resolution.KEYWORD, Resolution.SENTENCE, 12),
    )
    out = decay.level_texts(job, "  " + PARAGRAPH.replace(" ", "\n ", 1))
    assert out[Resolution.PARAGRAPH] == PARAGRAPH  # whitespace normalised, under budget
    assert out[Resolution.SENTENCE] == "The committee reviewed the roof repairs budget."
    keywords = out[Resolution.KEYWORD].split(", ")
    assert keywords[:2] == ["repairs", "roof"]  # most frequent first, ties alphabetical
    assert "about" not in keywords and "the" not in keywords  # stop words and short words


def test_level_texts_respects_the_token_budget() -> None:
    job = _job((Resolution.PARAGRAPH, Resolution.FULL, 5), (Resolution.KEYWORD, Resolution.FULL, 3))
    out = decay.level_texts(job, PARAGRAPH)
    assert len(out[Resolution.PARAGRAPH]) <= 20 and PARAGRAPH.startswith(out[Resolution.PARAGRAPH])
    assert len(out[Resolution.KEYWORD]) <= 12


def test_level_texts_drops_empty_levels() -> None:
    job = _job((Resolution.KEYWORD, Resolution.FULL, 12))
    assert decay.level_texts(job, "a an of to") == {}


async def test_decay_sweep_skips_a_read_only_context() -> None:
    eng = await _engine().start()
    try:
        ctx = replace(eng._pipeline_ctx(), append_event=None)
        assert await decay.decay_sweep(ctx) == {
            "status": "skipped",
            "reason": "read-only context (no write door)",
        }
    finally:
        await eng.stop()


async def test_fresh_records_do_not_move_and_nothing_is_compressed() -> None:
    eng = await _engine().start()
    try:
        await eng.write("a fresh turn", namespace="a", memory_type="episodic")
        ctx = eng._pipeline_ctx()
        assert await decay.decay_sweep(ctx) == {"status": "ok", "transitions": 0}
        assert await decay.multires(ctx) == {
            "multires_planned": 0,
            "multires_run": 0,
            "multires_levels": 0,
        }
    finally:
        await eng.stop()
