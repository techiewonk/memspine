# ruff: noqa: E501
"""TI-5: the step-mode REPL on the invented demo conversation (offline: hash embedder, stub LLM)."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

pytest.importorskip("memspine")

from memspine_evals.inspect import checks, step
from memspine_evals.inspect.assemble import load_dataset

SCRIPT = [
    "turn 3",
    "session",
    "sleep",
    "ask q1",
    "ask Where does Bram work now?",
    "show D1:1",
    "links D1:1",
    "diff",
    "save",
]


def _run(tmp_path: Path, script: list[str], **kw) -> tuple[str, step.StepSession]:
    buf = io.StringIO()
    session = step.StepSession(
        load_dataset("demo"), "demo-1", "demo", out=tmp_path, offline=True, stream=buf, **kw
    )
    asyncio.run(session.run(script))
    return buf.getvalue(), session


@pytest.fixture(scope="module")
def transcript(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    out = tmp_path_factory.mktemp("step")
    text, session = _run(out, SCRIPT)
    return text, session.out / f"{session.run_id}--bundle"


def test_prints_each_stage_heading(transcript: tuple[str, Path]) -> None:
    text, _ = transcript
    for heading in (
        "--- turn 1/30: D1:1",
        "--- stamp / role",
        "--- record / index (store.index.record)",
        "--- window membership / extraction",
        "--- envelope stages (write.envelope.trace)",
        "--- deposits / ladder",
        "--- sleep stage consolidate",
        "--- legs (gold marked *)",
        "--- fusion / cuts / pool",
        "--- rerank",
        "--- packed context",
        "--- diff:",
        "--- saved",
    ):
        assert heading in text, heading
    assert "extract.io window=" in text and "tokens={'prompt'" in text
    assert "occ " in text and "compile:added" in text
    assert "gold turns in context: ['D1:1'] of ['D1:1']" in text


def test_show_and_links(transcript: tuple[str, Path]) -> None:
    text, _ = transcript
    assert "--- record " in text and "(turn D1:1)" in text
    assert "--- links of " in text and "children (" in text


def test_saved_bundle_passes_the_checks(transcript: tuple[str, Path]) -> None:
    _, bundle = transcript
    assert (bundle / "manifest.json").exists()
    for name in ("turns", "windows", "envelopes", "records", "links", "sleep", "questions"):
        assert (bundle / f"{name}.jsonl").stat().st_size > 0, name
    results = checks.run_checks(bundle)
    errors = [r for r in results if r.status == "fail" and r.level == "error"]
    assert not errors, [(r.check_id, r.id, r.detail) for r in errors[:5]]
    qs = [json.loads(x) for x in (bundle / "questions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {q["query_id"] for q in qs} == {"q1", "adhoc-1"} or len(qs) == 2


def test_commands_without_state_do_not_crash(tmp_path: Path) -> None:
    text, session = _run(tmp_path, ["help", "bogus", "ask", "show nothing", "diff", "quit", "turn"])
    assert "unknown command 'bogus'" in text
    assert "nothing known as 'nothing'" in text
    assert session.next_turn == 0  # quit stopped the script before the last turn


def test_resolve_arm_and_offline_swap() -> None:
    name, cfg = step.resolve_arm("scr-b8-write-v2")
    assert name == "scr-b8-write-v2" and cfg["embedding"]["provider"] == "st"
    off, notes = step.offline_config(cfg)
    assert off["embedding"] == {"provider": "hash"} and off["read"]["rerank"] == "off"
    assert any("embedder" in n for n in notes) and any("reranker" in n for n in notes)
    with pytest.raises(FileNotFoundError):
        step.resolve_arm("no-such-arm")


def test_replay_cache_serves_hits_and_lists_misses(tmp_path: Path) -> None:
    from memspine_evals.call_cache import CallCache, embed_key

    seed = CallCache(tmp_path)
    seed._db.execute(
        "INSERT INTO embed VALUES (?, ?, ?, ?)",
        (embed_key("m", "known", None, None), "m", __import__("array").array("d", [1.0, 0.0]).tobytes(), 0.0),
    )
    seed.close()
    cache = step._make_replay(tmp_path, live=False)

    async def go() -> tuple[list, list]:
        hit = await cache.aembedding(model="m", input=["known"])
        miss = await cache.aembedding(model="m", input=["unknown", "known"])
        return hit.data, miss.data

    hit, miss = asyncio.run(go())
    cache.close()
    assert hit[0]["embedding"] == [1.0, 0.0]
    assert miss[1]["embedding"] == [1.0, 0.0] and len(miss[0]["embedding"]) == 2  # missed text: hashed, same dim
    assert cache.misses == [{"kind": "embed", "why": "miss (hashed)", "model": "m", "n": 1}]
    # nothing was stored for the miss
    again = CallCache(tmp_path)
    assert again._db.execute("SELECT COUNT(*) FROM embed").fetchone()[0] == 1
    again.close()
