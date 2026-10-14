"""R14-JUDGE (M-16/M-17/M-18/M-19): conventions v3, the strict-list scorer, the strict evidence parser,
the speaker-swap stratum and the offline re-score report. Invented fixtures only; offline, no model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import r14_rescore
from judge_agreement import has_text, load_set, with_conventions
from memspine_evals.cli import build_parser
from memspine_evals.datasets.locomo import LoCoMoDataset, parse_evidence_ids
from memspine_evals.experiments import C01Config
from memspine_evals.judge import GuardedJudge, JudgeScale, Verdict
from memspine_evals.judge_conventions import (
    CONVENTIONS_V3_VERSION,
    check_conventions_v2,
    check_conventions_v3,
    list_item_recall,
    strict_list_ok,
)

CALIBRATION = Path(__file__).resolve().parents[1] / "analysis" / "judge_calibration_dev.jsonl"


@pytest.mark.parametrize(
    ("question", "gold", "answer", "rule"),
    [
        # list: an unrelated note with a negation elsewhere no longer voids a right list
        (
            "Which cities has Ilse visited?",
            "Lyon, Porto, Graz, and Kiel.",
            "Ilse has been to these cities:\n* Lyon (a trip in March)\n* Porto (a trip in May)\n"
            "* Bremen (a suggestion for a later trip in autumn, no confirmation she has been there yet)\n"
            "* Graz (a trip in June)\n* Kiel (a trip in July)",
            "list_scoped",
        ),
        # containment with extra detail; a third-person pronoun in the gold is not content
        (
            "What setback did Bram face?",
            "He hurt his wrist and paused his rowing.",
            "In the spring Bram hurt his wrist and paused his rowing.",
            "contained",
        ),
        ("Which event did Noor run?", "charity relay race", "Noor ran a charity relay race in late May.", "contained"),
        ("What place does Tomasz name?", "Kestrel Point", "Kestrel Point and the old harbour.", "contained"),
        # geography: the question asks for a country, the answer names a city of it
        ("Which country did Ilse fly to in May?", "Canada", "Ilse flew to Toronto.", "geography"),
        (
            "Which countries has Bram visited?",
            "Italy, Turkey, Greenland",
            "Bram has visited Rome, Istanbul and Nuuk.",
            "geography",
        ),
        # annotation-equivalent year
        ("When did Noor buy the boat?", "A year ago", "Noor bought the boat in 2022.", "relative_year"),
        ("When did Noor buy it?", "Two years ago", "It was bought in 2021.", "relative_year"),
    ],
)
def test_v3_credits(question: str, gold: str, answer: str, rule: str) -> None:
    assert check_conventions_v2(question, answer, gold) is None  # v3 adds, v2 did not have it
    hit = check_conventions_v3(question, answer, gold)
    assert hit is not None and hit.rule == rule


@pytest.mark.parametrize(
    ("question", "gold", "answer"),
    [
        # a list missing an item stays wrong
        ("Which cities?", "Lyon, Porto, Graz", "Lyon and Porto."),
        # restriction anywhere blocks the list
        ("Which cities?", "Lyon, Porto", "Only Lyon, Porto was confirmed."),
        # a negation next to the item blocks the list
        ("Which cities?", "Lyon, Porto", "Not Lyon, and not Porto either, but Graz."),
        # containment: negated, hedged, alternative, long, side remark
        ("What team?", "The Wolves", "Tim does not support a specific team; he reads about the Wolves."),
        ("What team?", "Kestrel Point", "Maybe Kestrel Point, perhaps something else."),
        ("What team?", "Kestrel Point", "Either Kestrel Point or the old harbour."),
        ("Which boat is newer?", "Seraphim", "Susie is newer. She bought it years before Seraphim."),
        ("What event?", "charity relay race", "Noor ran a " + "very " * 50 + "long charity relay race."),
        # geography only when the question asks for a country
        ("Which city did Ilse fly to?", "Canada", "Ilse flew to Toronto."),
        # relative year: two years, a hedge, a negation, no year
        ("When?", "A year ago", "Between 2021 and 2022."),
        ("When?", "A year ago", "Probably in 2022."),
        ("When?", "A year ago", "It was not in 2022."),
        ("When?", "A year ago", "Last spring."),
        # abstention gold and refusal answer are never touched
        ("What?", "Not mentioned in the conversation", "Kestrel Point"),
        ("What?", "Kestrel Point", "It is not mentioned in the conversation: Kestrel Point."),
        # empty
        ("What?", "Kestrel Point", ""),
    ],
)
def test_v3_does_not_credit(question: str, gold: str, answer: str) -> None:
    assert check_conventions_v3(question, answer, gold) is None


def test_v3_keeps_v2_credits_and_names() -> None:
    hit = check_conventions_v3("When?", "Saturday, November 5, 2022.", "5 November, 2022")
    assert hit is not None and hit.rule == "date_format"
    assert check_conventions_v3("How many?", "five", "5").rule == "numeral"  # type: ignore[union-attr]


def test_v3_calibration_set_has_no_false_positive() -> None:
    rows = load_set(CALIBRATION)
    if not has_text(rows):
        pytest.skip("calibration text is local only (evals/runs/_analysis); see judge_agreement.FULL_SET")
    judged = {r["id"]: r["source_judge_label"] for r in rows}
    res = with_conventions(rows, judged)["v3"]
    assert res["false_positives"] == 0 and res["false_positives_beyond_v2"] == 0


# -- strict list ----------------------------------------------------------------------------------


def test_strict_list_all_items_present() -> None:
    gold = "Lyon, Porto, Graz"
    assert strict_list_ok("Which?", "Graz, Lyon and Porto were all visited.", gold) is True
    assert strict_list_ok("Which?", "Lyon.", gold) is False
    assert list_item_recall("Which?", "Lyon and Porto only", gold) == (2, 3)


def test_strict_list_paraphrased_item_is_not_a_miss_but_absent_one_is() -> None:
    gold = "routine examination, vaccination, dental scaling"
    assert strict_list_ok("What?", "A routine exam and she was vaccinated, plus dental scaling.", gold) is True
    assert strict_list_ok("What?", "A routine exam and she was vaccinated.", gold) is False


def test_strict_list_none_when_gold_is_no_comma_list() -> None:
    for gold in ("surreal and cool", "California or Florida", "Lyon", "Not mentioned in the conversation"):
        assert strict_list_ok("q", "Lyon", gold) is None


def test_strict_list_folds_geography_and_duplicates_count_once() -> None:
    assert strict_list_ok("Which countries?", "Rome and Toronto.", "Italy, Canada") is True
    assert list_item_recall("q", "a twisted ankle", "twisted ankle, twisted ankle, heart palpitations") == (1, 2)


# -- evidence parser ------------------------------------------------------------------------------


def _conv() -> dict[str, Any]:
    return {
        "speaker_a": "Ilse",
        "speaker_b": "Bram",
        "session_1": [{"speaker": "Ilse", "dia_id": "D1:1", "text": "a"}, {"speaker": "Bram", "dia_id": "D1:2", "text": "b"}],
        "session_2": [{"speaker": "Ilse", "dia_id": "D2:1", "text": "c"}],
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["D1:1 D1:2 D2:1"], ["D1:1", "D1:2", "D2:1"]),  # whitespace-joined
        (["D1:1; D2:1"], ["D1:1", "D2:1"]),  # the dataset's own list convention
        (["D1:1", "D", "D2:1"], ["D1:1", "D2:1"]),  # a lone fragment
        (["D:2:1"], ["D2:1"]),  # stray colon
        (["D01:01"], ["D1:1"]),  # leading zeros
        (["D1:1", "D1:1"], ["D1:1"]),  # duplicates
        (["D1:2, D9:9"], ["D1:2"]),  # an id of a turn that does not exist (existence check)
    ],
)
def test_parse_evidence_ids_with_existence_check(raw: list[str], expected: list[str]) -> None:
    assert parse_evidence_ids(raw, _conv()) == expected


def test_parse_evidence_ids_without_conversation_is_syntactic() -> None:
    assert parse_evidence_ids(["D9:1 D4:4 D4:6", "D", "D:11:26"]) == ["D9:1", "D4:4", "D4:6", "D11:26"]


def _write_locomo(tmp_path: Path) -> Path:
    sample = {
        "sample_id": "conv-x",
        "conversation": _conv(),
        "qa": [
            {"question": "Where did Ilse go?", "answer": "Graz", "evidence": ["D1:1 D2:1"], "category": 1},
            {"question": "Who spoke second?", "answer": "Bram", "evidence": ["D1:2"], "category": 4},
        ],
    }
    path = tmp_path / "locomo.json"
    path.write_text(json.dumps([sample]), encoding="utf-8")
    return path


def test_loader_flag_off_is_unchanged_and_on_repairs(tmp_path: Path) -> None:
    path = _write_locomo(tmp_path)
    off = next(iter(LoCoMoDataset(path, "t").items()))
    on = next(iter(LoCoMoDataset(path, "t", repair_evidence=True).items()))
    assert off.queries[0].gold_turn_ids == ("D1:1 D2:1",)
    assert on.queries[0].gold_turn_ids == ("D1:1", "D2:1")
    assert off.queries[1].gold_turn_ids == on.queries[1].gold_turn_ids == ("D1:2",)
    assert "Missing turn" in off.queries[0].meta["judge_evidence"]
    assert "Missing turn" not in on.queries[0].meta["judge_evidence"]
    assert "+evidence-repaired" in LoCoMoDataset(path, "t", repair_evidence=True).info().subset
    assert "+evidence-repaired" not in LoCoMoDataset(path, "t").info().subset


# -- speaker swap (M-16) --------------------------------------------------------------------------


def test_speaker_swapped() -> None:
    sp = ("Ilse", "Bram")
    assert r14_rescore.speaker_swapped("What did Ilse say about boats?", sp, ["Bram"])
    assert not r14_rescore.speaker_swapped("What did Ilse say about boats?", sp, ["Ilse", "Bram"])
    assert not r14_rescore.speaker_swapped("What did Ilse tell Bram?", sp, ["Bram"])  # names both
    assert not r14_rescore.speaker_swapped("What happened?", sp, ["Bram"])  # names neither
    assert not r14_rescore.speaker_swapped("What did Ilse say?", sp, [])


def test_swapped_keys_from_file(tmp_path: Path) -> None:
    # question 0 names Ilse but evidence turns are Ilse's own (D1:1) and D2:1 (Ilse): not swapped
    # question 1 names Ilse and the evidence D1:2 is Bram's: swapped
    sample = json.loads(_write_locomo(tmp_path).read_text())[0]
    sample["qa"][1]["question"] = "What did Ilse say second?"
    p = tmp_path / "l2.json"
    p.write_text(json.dumps([sample]), encoding="utf-8")
    assert r14_rescore.swapped_keys(p) == {("conv-x", "0-1")}


# -- the report -----------------------------------------------------------------------------------


def _row(i: int, cat: str, q: str, gold: str, ans: str, score: float, item: str = "c1") -> dict[str, Any]:
    return {
        "kind": "result", "item_id": item, "query_id": f"0-{i}", "question": q, "gold": gold,
        "answer": ans, "score": score, "scale": "binary", "status": "completed", "type_label": cat,
    }


def _fixture_rows() -> list[dict[str, Any]]:
    return [
        _row(0, "cat1", "Which cities?", "Lyon, Porto", "Lyon and Porto.", 1.0),  # right
        _row(1, "cat1", "Which cities?", "Lyon, Porto, Graz", "Lyon.", 1.0),  # judge FP: one item of three
        _row(2, "cat4", "Which country?", "Canada", "Toronto.", 0.0),  # judge FN: geography
        _row(3, "cat4", "What?", "Kestrel Point", "Harbour.", 0.0),  # truly wrong
        _row(4, "cat2", "When?", "A year ago", "In 2022.", 0.0),  # judge FN: relative year
        _row(5, "cat3", "What?", "Kestrel Point", "Kestrel Point.", 1.0),  # right, errata
    ]


def test_rescore_columns() -> None:
    res = r14_rescore.rescore(_fixture_rows(), {("c1", "0-5")})
    t = res["table"]["cat1-4"]
    assert (t["n"], t["raw"], t["v3"], t["strict"]) == (6, 3, 5, 4)
    assert (t["n-E"], t["raw-E"], t["strict-E"]) == (5, 2, 3)
    assert res["table"]["cat1"] == {
        "n": 2, "n_errata": 0, "raw": 2, "v3": 2, "strict": 1, "n-E": 2, "raw-E": 2, "strict-E": 1,
    }
    rules = {s["key"]: s["v3_rule"] for s in res["rows"] if s["v3_rule"]}
    assert rules == {("c1", "0-2"): "geography", ("c1", "0-4"): "relative_year"}


def test_rescore_never_lowers_raw_via_v3_and_strict_never_exceeds_v3() -> None:
    for s in r14_rescore.rescore(_fixture_rows(), set())["rows"]:
        assert s["v3"] >= s["raw"] and s["strict"] <= s["v3"]


def test_cli_end_to_end_counts_only_on_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = tmp_path / "runs" / "demo--memspine"
    run.mkdir(parents=True)
    (run / "results.jsonl").write_text(
        json.dumps({"kind": "manifest"}) + "\n" + "\n".join(json.dumps(r) for r in _fixture_rows()) + "\n",
        encoding="utf-8",
    )
    errata = tmp_path / "errata.json"
    errata.write_text(json.dumps({"schema": "errata/v1", "entries": [
        {"item": "c1", "qid": "0-5", "tag": "gold_error"}, {"item": "c1", "qid": "0-3", "tag": "evidence_label_error"}]}))
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "item,qid,hand_fn,hand_fp\nc1,0-2,clear,\nc1,0-4,likely,\nc1,0-1,,clear\nc1,0-3,,\n", encoding="utf-8"
    )
    out = tmp_path / "analysis_out"
    summ = tmp_path / "s.json"
    rc = r14_rescore.main([str(run), "--errata", str(errata), "--labels", str(labels), "--out", str(out), "--json", str(summ)])
    assert rc == 0
    stdout = capsys.readouterr().out
    assert "Kestrel" not in stdout and "Toronto" not in stdout  # text goes to the local listing only
    lm = json.loads(summ.read_text())["demo--memspine"]["labels"]
    assert lm["fn"]["credited_hand_fn_clear_likely"] == 2 and lm["fn"]["precision_any"] == 1.0
    assert lm["fp"]["dropped_hand_fp_clear_likely"] == 1 and lm["fp"]["precision_any"] == 1.0
    listing = (out / "r14_rescore_demo--memspine.tsv").read_text(encoding="utf-8")
    assert "Toronto" in listing and "geography" in listing


# -- wiring: opt-in, off by default, a separate column --------------------------------------------


class _Inner:
    spec = type("S", (), {"judge_id": "j", "scale": JudgeScale.BINARY, "model": "m", "prompt_id": "p",
                          "prompt_hash": "h", "makes_model_calls": False, "params": {}})()

    def __init__(self, score: float) -> None:
        self._score = score

    async def score(self, question: str, answer: str, gold: str | None) -> Verdict:
        return Verdict(score=self._score, scale=JudgeScale.BINARY, raw="x", model_calls=1)


async def test_guarded_judge_v3_column_keeps_official_score() -> None:
    judge = GuardedJudge(_Inner(0.0), conventions_v3=True)
    v = await judge.score("Which country did Ilse fly to?", "Ilse flew to Toronto.", "Canada")
    assert v.score == 0.0
    assert v.meta["score_conventions_v3"] == 1.0 and v.meta["convention_v3"] == "geography"
    assert "score_conventions" not in v.meta and "score_conventions_v2" not in v.meta
    assert judge.spec.params["conventions_v3"] == CONVENTIONS_V3_VERSION
    ok = await GuardedJudge(_Inner(1.0), conventions_v3=True).score("q", "x", "y")
    assert ok.score == 1.0 and ok.meta["score_conventions_v3"] == 1.0 and "convention_v3" not in ok.meta


async def test_guarded_judge_off_is_unchanged() -> None:
    off = GuardedJudge(_Inner(0.0))
    v = await off.score("Which country?", "Toronto", "Canada")
    assert v.meta == {} and "conventions_v3" not in off.spec.params


def test_cli_flags_and_config_defaults() -> None:
    parser = build_parser()
    base = parser.parse_args(["c0-1", "--dataset", "locomo"])
    assert base.judge_conventions_v3 is False and base.repair_evidence is False
    on = parser.parse_args(["c0-1", "--dataset", "locomo", "--judge-conventions-v3", "--repair-evidence"])
    assert on.judge_conventions_v3 is True and on.repair_evidence is True
    assert C01Config().judge_conventions_v3 is False
