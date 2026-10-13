# ruff: noqa: E501
"""ATLAS-TURNS: conversation viewer data builder (synthetic data only) and the ignored-path guard."""

from __future__ import annotations

import json
from pathlib import Path

import build_locomo_problem_atlas as ATLAS
import locomo_atlas_data as AD
import pytest
from locomo_atlas_html import render_html

RAW = [
    {
        "sample_id": "conv-1",
        "conversation": {
            "speaker_a": "Ann", "speaker_b": "Bo",
            "session_1_date_time": "1:00 pm on 1 May, 2023",
            "session_1": [
                {"speaker": "Ann", "dia_id": "D1:1", "text": "hello there"},
                {"speaker": "Bo", "dia_id": "D1:2", "text": "look at this", "blip_caption": "a dog", "query": "dog"},
            ],
            "session_2_date_time": "2:00 pm on 3 May, 2023",
            "session_2": [{"speaker": "Ann", "dia_id": "D2:1", "text": "I ran a race"}],
        },
        "qa": [
            {"question": "What did Ann do?", "answer": "ran", "evidence": ["D2:1"], "category": 1},
            {"question": "Who spoke first?", "answer": "Ann", "evidence": ["D1:1; D1:2"], "category": 2},
        ],
    }
]


def gt(turn, in_ctx=False):
    return {"turn": turn, "ts": "x", "in_context": in_ctx, "lost_at": None, "vector": 3, "lexical": None, "extra": {}, "fused": None, "pool": None, "rerank": None, "final": None, "text": None}


def rows():
    return [
        {"item": "conv-1", "qid": "0-0", "correct": True, "question": "What did Ann do?", "gold": "ran", "answer": "ran", "gold_turns": [gt("D2:1", True)]},
        {"item": "conv-1", "qid": "0-1", "correct": False, "question": "Who spoke first?", "gold": "Ann", "answer": "Bo", "gold_turns": [gt("D1:1")]},
    ]


def test_every_turn_and_session_present():
    convs = AD.build_conversations(RAW)
    ids = [t["id"] for s in convs["conv-1"]["sessions"] for t in s["turns"]]
    assert ids == ["D1:1", "D1:2", "D2:1"]
    assert [s["date"] for s in convs["conv-1"]["sessions"]] == ["1:00 pm on 1 May, 2023", "2:00 pm on 3 May, 2023"]
    assert convs["conv-1"]["sessions"][0]["turns"][1]["cap"] == "a dog"


def test_real_dataset_counts_if_present():
    p = ATLAS.DATA
    if not p.exists():
        pytest.skip("locomo10.json not available")
    raw = json.loads(p.read_text(encoding="utf-8"))
    convs = AD.build_conversations(raw)
    assert len(convs) == 10
    for c in raw:
        n = sum(len(v) for k, v in c["conversation"].items() if k.startswith("session_") and isinstance(v, list))
        assert sum(len(s["turns"]) for s in convs[c["sample_id"]]["sessions"]) == n


def test_parse_evidence_handles_joined_ids():
    assert AD.parse_evidence(["D1:1; D1:2", "D3:4"]) == ["D1:1", "D1:2", "D3:4"]
    assert AD.parse_evidence(None) == []


def test_enrich_covers_every_question_with_context_and_judge():
    convs = AD.build_conversations(RAW)
    results = {("conv-1", "0-0"): {"score": 1.0, "meta": {"judge_raw": '{"label": "CORRECT"}', "judge_prompt_id": "p"}}, ("conv-1", "0-1"): {"score": 0.0, "meta": {}, "retrieved_ids": ["D2:1"]}}
    forensics = [{"item": "conv-1", "query_id": "0-0", "context_records": [{"turn": "D2:1", "text": "Ann: I ran a race [= 3 May]"}]}]
    ctx = AD.build_context(forensics, results, convs)
    assert ctx[("conv-1", "0-0")]["source"] == "forensics.context_records"
    assert ctx[("conv-1", "0-1")]["source"] == "results.retrieved_ids"
    assert convs["conv-1"]["ctxtext"]["D2:1"] == "Ann: I ran a race [= 3 May]"
    rs = rows()
    stats = AD.enrich_rows(rs, convs, AD.qa_index(RAW), results, ctx)
    assert stats["rows"] == len(RAW[0]["qa"]) == 2 and stats["no_context"] == 0
    assert all(r["evidence"] and r["ctx"] is not None and "screen_flips" in r for r in rs)
    assert rs[0]["judge_raw"] == '{"label": "CORRECT"}' and rs[1]["judge_raw"] is None
    # evidence D1:2 was not in the forensics gold list: added; context membership recomputed from the reader context
    assert {g["turn"] for g in rs[1]["gold_turns"]} == {"D1:1", "D1:2"} and not any(g["in_context"] for g in rs[1]["gold_turns"])
    assert rs[0]["gold_turns"][0]["speaker"] == "Ann" and rs[0]["gold_turns"][0]["date"].startswith("2:00 pm")


def test_screen_flips_only_verdict_changes():
    ref = {("c", "1"): (1.0, "a"), ("c", "2"): (0.0, "b"), ("c", "3"): (1.0, "x")}
    run = {("c", "1"): (0.0, "z"), ("c", "2"): (1.0, "y"), ("c", "3"): (1.0, "different words"), ("c", "9"): (1.0, "n/a")}
    out = {f["qid"]: f["kind"] for f in AD.screen_flips(ref, run)}
    assert out == {"1": "loss", "2": "gain"}


def test_html_embeds_each_conversation_lazily():
    html = render_html("{}", {"conv-1": '{"sessions":[]}'})
    assert 'id="cv-conv-1"' in html and "__CONVS__" not in html and "http://" not in html.replace("http://www.w3.org", "")


def test_output_path_refused_outside_ignored_folder(tmp_path):
    with pytest.raises(SystemExit):
        ATLAS.assert_ignored(tmp_path / "atlas.html")
    with pytest.raises(SystemExit):
        ATLAS.assert_ignored(Path(ATLAS.REPO) / "docs" / "atlas.html")
    with pytest.raises(SystemExit):  # inside _analysis lexically is not enough if git does not ignore it
        ATLAS.write_local(Path(ATLAS.REPO) / "evals" / "locomo_problem_atlas.html", "x")
