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
    qs = [
        json.loads(x) for x in (bundle / "questions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
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
        (
            embed_key("m", "known", None, None),
            "m",
            __import__("array").array("d", [1.0, 0.0]).tobytes(),
            0.0,
        ),
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
    assert (
        miss[1]["embedding"] == [1.0, 0.0] and len(miss[0]["embedding"]) == 2
    )  # missed text: hashed, same dim
    assert cache.misses == [{"kind": "embed", "why": "miss (hashed)", "model": "m", "n": 1}]
    # nothing was stored for the miss
    again = CallCache(tmp_path)
    assert again._db.execute("SELECT COUNT(*) FROM embed").fetchone()[0] == 1
    again.close()


# ---------------------------------------------------------------------------------------------------
# TI-5b: manual testing (messages, interactions, sessions, retrieval) and tuning
def _drive(tmp_path: Path, fn, **kw) -> tuple[str, step.StepSession]:
    """Run ``fn(session)`` (a coroutine) against a started session; returns the transcript."""
    buf = io.StringIO()
    session = step.StepSession(
        load_dataset("demo"), "demo-1", "demo", out=tmp_path, offline=True, stream=buf, **kw
    )

    async def go() -> None:
        await session.start()
        try:
            await fn(session)
        finally:
            await session.close()

    asyncio.run(go())
    return buf.getvalue(), session


def _bundle_checks(bundle: Path) -> list:
    return [r for r in checks.run_checks(bundle) if r.status == "fail" and r.level == "error"]


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.fixture(scope="module")
def manual(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, step.StepSession]:
    """Messages, selections, interactions, sessions, asks: one engine run."""
    out = tmp_path_factory.mktemp("manual")
    asks = out / "asks.txt"
    asks.write_text(
        "# one per line\nq3\nWhere did Bram adopt Spike? | gold: the shelter | turns: U2\n",
        encoding="utf-8",
    )
    script = [
        # requirement 1: one and many messages
        "say Bram: I adopted a hedgehog called Spike. @2024-03-02 10:30",
        "say <<",
        "Ilse: Spike sounds adorable, where did you find him?",
        "Bram: At the shelter in Utrecht near the station. #D1",
        ".",
        "status",
        # dataset turns: by id, a range, a list, out of order, again, skip, goto
        "turn D1:3",
        "turns D1:1-D1:2",
        "turns D1:6,D1:4",
        "turn D1:3",
        "skip 1",
        "goto D2:1",
        "status",
        # requirement 2: interactions
        "interaction 2",
        # requirement 3: sessions
        "sessions D1-D2",
        "session",
        # requirement 4: retrieve one, several, any
        "ask q1",
        "ask q1,q5,q7",
        "ask cat:list",
        f"asks {asks}",
        "brief on",
        "ask all",
        "brief off",
        "ask Who adopted a hedgehog? | gold: Bram | turns: U1",
        "save",
    ]
    return _run(out, script)


def test_say_writes_custom_messages_and_default_time(manual: tuple[str, step.StepSession]) -> None:
    text, session = manual
    assert "--- say: U1  session D1  Bram  [2024-03-02 10:30]  (custom)" in text
    # no time given: the last turn's time + 1 minute, in the current session, never the wall clock
    assert "--- say: U2  session D1  Ilse  [2024-03-02 10:31]  (custom)" in text
    assert "--- say: U3  session D1  Bram  [2024-03-02 10:32]  (custom)" in text
    assert "--- summary: 2 custom messages" in text
    assert [t["turn_id"] for t in session.written if t.get("custom")] == ["U1", "U2", "U3"]


def test_turn_selection_order_skip_goto_status(manual: tuple[str, step.StepSession]) -> None:
    text, _ = manual
    assert "--- turn 1/30: D1:1" in text and "--- turn 3/30: D1:3" in text
    assert "warning: the selection is not in dataset order" in text
    assert "D1:3 is already written (skipped" in text
    assert "skipped (not written):" in text
    assert "cursor moved to D2:1 (nothing written)" in text
    assert "--- status" in text and "custom) of 30 in the dataset" in text


def test_interaction_writes_turn_and_reply_with_summary(
    manual: tuple[str, step.StepSession],
) -> None:
    text, _ = manual
    assert text.count("--- summary: interaction ") == 2
    assert "--- summary: interaction D2:1 (Noor)" in text


def test_sessions_range_and_rest_of_session(manual: tuple[str, step.StepSession]) -> None:
    text, session = manual
    assert "--- session D1: wrote" in text and "--- session D2: wrote" in text
    assert "--- summary: sessions" in text
    assert {"D1", "D2"} <= session.closed_sessions


def test_ask_summary_table_and_brief(manual: tuple[str, step.StepSession]) -> None:
    text, _ = manual
    assert text.count("--- SUMMARY TABLE") >= 3
    assert (
        "qid  category" in text
        and "gold found" in text
        and "first losing stage" in text
        and "answer correct" in text
    )
    assert (
        "--- SUMMARY TABLE (9 question(s))" in text or "--- SUMMARY TABLE (8 question(s))" in text
    )  # ask all
    assert "] gold " in text and "lost_at=" in text  # brief lines
    assert "gold turns in context: ['U1'] of ['U1']" in text  # a custom turn as gold


def test_saved_bundle_flags_custom_and_passes_checks(manual: tuple[str, step.StepSession]) -> None:
    _, session = manual
    bundle = session.out / f"{session.run_id}--bundle"
    turns = _jsonl(bundle / "turns.jsonl")
    assert {t["turn_id"] for t in turns if t.get("custom")} == {"U1", "U2", "U3"}
    assert not any(t.get("custom") for t in turns if t["turn_id"].startswith("D"))
    assert [t["ts"] for t in turns if t["turn_id"] == "U2"] == ["2024-03-02 10:31"]
    qs = _jsonl(bundle / "questions.jsonl")
    custom_q = [q for q in qs if q.get("custom")]
    assert len(custom_q) == 2 and all(q["query_id"].startswith("adhoc-") for q in custom_q)
    assert len({q["query_id"] for q in qs}) == len(qs)  # a question asked twice is one row
    assert not _bundle_checks(bundle)


def test_reset_clears_the_store_and_load_replaces_the_conversation(tmp_path: Path) -> None:
    other = tmp_path / "other.json"
    other.write_text(
        json.dumps(
            {
                "conversation_id": "mini",
                "sessions": [
                    {
                        "session_id": "S1",
                        "date": "2024-05-01",
                        "turns": [
                            {
                                "turn_id": "S1:1",
                                "speaker": "Noor",
                                "text": "I bought a violin from Tomasz yesterday.",
                            },
                            {
                                "turn_id": "S1:2",
                                "speaker": "Ilse",
                                "text": "Which one did you pick, Noor?",
                            },
                        ],
                    }
                ],
                "questions": [
                    {
                        "query_id": "m1",
                        "category": "single_hop",
                        "question": "What did Noor buy?",
                        "gold": "a violin",
                        "gold_turns": ["S1:1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    async def go(s: step.StepSession) -> None:
        await s.execute("say Bram: I keep a hedgehog named Spike in the garden.")
        await s.execute("turn 2")
        assert len(s.written) == 3 and len(await s._records()) == 3
        await s.execute("reset")
        assert s.written == [] and s.next_turn == 0 and await s._records() == []
        await s.execute(f"load {other}")
        assert s.item == "mini" and len(s.turns) == 2 and list(s.questions) == ["m1"]
        await s.execute("interaction")
        await s.execute("ask m1")

    text, _ = _drive(tmp_path / "run", go)
    assert "--- reset: fresh engine, same arm" in text and "nothing written; cursor at D1:1" in text
    assert "--- load: item mini" in text
    assert "--- summary: interaction S1:1 (Noor) -> S1:2 (Ilse)" in text
    assert "gold turns in context: ['S1:1'] of ['S1:1']" in text


# ---- tuning
STAGE_PARAMS = {
    "stages": {
        "B7": {
            "name": "Fusion",
            "keys": [
                {
                    "key": "read.fusion",
                    "default": "rrf",
                    "type": "str",
                    "choices": ["rrf", "minmax"],
                    "description": "how the legs merge",
                },
                {
                    "key": "read.assembly.relative_floor",
                    "default": None,
                    "type": "float",
                    "range": [0, 1],
                    "description": "drop weak hits",
                },
            ],
        }
    }
}


def test_tuning_set_unset_params_config_sweep_export(tmp_path: Path) -> None:
    params = tmp_path / "stage_params.json"
    params.write_text(json.dumps(STAGE_PARAMS), encoding="utf-8")
    arms = tmp_path / "arms"

    async def go(s: step.StepSession) -> None:
        s.stage_params_path, s.arms_dir = params, arms
        await s.execute("session D1")
        await s.execute("ask q1")
        before = s._last_read["q1"]["tokens"]
        await s.execute("config read.assembly")
        await s.execute("config diff")
        await s.execute("params B7")
        await s.execute("params fusion")  # a stage-name match
        await s.execute("set read.assembly.relative_floor=0.9")
        await s.execute("set read.assembly.relative_floor=banana")  # refused by the schema
        await s.execute("set read.nonsense=3")
        await s.execute("set embedding.provider=hash")  # a rebuild key
        await s.execute("rerun ask q1")
        assert s._last_read["q1"]["tokens"] < before
        await s.execute("sweep read.assembly.relative_floor 0.1,0.9 ask q1,q7")
        await s.execute("sweep write.window_extract off,facts ask q1")  # write side: refused
        await s.execute("unset read.assembly.relative_floor")
        await s.execute("rerun ask q1")
        assert s._last_read["q1"]["tokens"] == before
        await s.execute("set read.assembly.relative_floor=0.5")
        await s.execute("export arm tuned")
        await s.execute("save")

    text, session = _drive(tmp_path / "run", go)
    assert "read.assembly.relative_floor = 0.3" in text
    assert "--- config diff:" in text and "(default" in text
    assert (
        "--- params B7: Fusion" in text
        and 'choices ["rrf", "minmax"]' in text
        and "range [0, 1]" in text
    )
    assert "read.assembly.relative_floor: 0.3 -> 0.9   applied to the running engine" in text
    assert "set refused: " in text and "unknown key 'read.nonsense'" in text
    assert "needs a rebuild: run `reset`" in text
    assert "packed context: -" in text and "<== changed" in text
    assert "--- sweep read.assembly.relative_floor over 2 question(s)" in text
    assert (
        "sweep refused: 'write.window_extract' is not a read-side key" in text
        and "rerun window" in text
    )
    arm = json.loads((arms / "tuned.json").read_text(encoding="utf-8"))
    assert arm["read"]["assembly"]["relative_floor"] == 0.5 and arm["embedding"] == {
        "provider": "hash"
    }
    assert (
        "--- export arm tuned" in text and "read.assembly.relative_floor: (absent) -> 0.5" in text
    )
    from memspine_evals.rehearsal import validate_engine_config

    validate_engine_config(arm)
    bundle = session.out / f"{session.run_id}--bundle"
    log = _jsonl(bundle / "tuning.jsonl")
    assert {"set", "unset", "rerun ask", "sweep", "export arm"} <= {r["command"] for r in log}
    assert all({"ts", "command", "key", "old", "new", "effect"} <= set(r) for r in log)
    assert not _bundle_checks(bundle)


def test_tuning_params_fall_back_to_the_config_schema(tmp_path: Path) -> None:
    async def go(s: step.StepSession) -> None:
        s.stage_params_path = tmp_path / "missing.json"
        await s.execute("params read.fusion")

    text, _ = _drive(tmp_path / "run", go)
    assert "from the config schema (docs/v2/data/stage_params.json not found)" in text
    assert "read.fusion = rrf  default rrf  choices ['rrf', 'minmax']" in text


def test_prompt_edit_rerun_window_diff_and_reset(tmp_path: Path) -> None:
    from memspine.prompts import structured

    edit = tmp_path / "window_prompt.txt"
    # the shipped text plus the offline stub's threshold directive: shorter lines are no longer extracted
    edit.write_text(
        structured.window_messages(["facts", "edges", "episode"], "[1] X: y")[0]["content"]
        + "\n[stub: min_len=60]",
        encoding="utf-8",
    )
    body = tmp_path / "extract_body.txt"
    body.write_text("Extract facts from: {{ content }}", encoding="utf-8")

    async def go(s: step.StepSession) -> None:
        await s.execute("session D1")
        wid = next(
            r["window_id"]
            for r in s.sink.rows
            if r["event"] == "write.window.members" and r.get("member_record_ids")
        )
        n_records = len(await s._records())
        await s.execute("prompt list")
        await s.execute("prompt show structured.window")
        await s.execute(f"prompt edit structured.window {edit}")
        await s.execute(f"prompt edit window_extract {body}")
        await s.execute("prompt show window_extract")
        await s.execute(f"rerun window {wid}")
        assert len(await s._records()) == n_records  # scratch: nothing deposited
        await s.execute("prompt reset structured.window")
        await s.execute("prompt reset window_extract")
        await s.execute(f"rerun window {wid}")
        await s.execute("rerun window nosuchwindow")
        await s.execute("save")

    text, session = _drive(tmp_path / "run", go)
    assert "structured.window" in text and "window_extract" in text and "reader.qa" in text
    assert "(edited this session)" in text
    assert "(scratch: nothing deposited)" in text
    assert "items before 8, after 1:" in text and "-7 removed" in text
    assert "items before 8, after 8:" in text  # reset: the same eight items come back
    assert "no window 'nosuchwindow'" in text
    bundle = session.out / f"{session.run_id}--bundle"
    log = _jsonl(bundle / "tuning.jsonl")
    assert [r["command"] for r in log].count("rerun window") == 2
    assert {"prompt edit", "prompt reset"} <= {r["command"] for r in log}
    assert not _bundle_checks(bundle)
