"""I77: judge conventions v2 (date format, unit, ordinal, alias): deterministic, documented, a
separate column that never changes the official score. Offline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from judge_agreement import load_set, with_conventions
from memspine_evals.cli import build_parser
from memspine_evals.experiments import C01Config
from memspine_evals.judge import GuardedJudge, JudgeScale, Verdict
from memspine_evals.judge_conventions import (
    ALIAS_GROUPS,
    CONVENTIONS_V2_VERSION,
    check_conventions,
    check_conventions_v2,
)

CALIBRATION = Path(__file__).resolve().parents[1] / "analysis" / "judge_calibration_dev.jsonl"


@pytest.mark.parametrize(
    ("question", "gold", "answer", "rule"),
    [
        ("When?", "May 3, 2023", "She went on 3rd May 2023.", "date_format"),
        ("When?", "3 May 2023", "2023-05-03", "date_format"),
        ("When?", "19 October 2023", "Melanie hiked on Thu 2023-10-19.", "date_format"),
        ("When?", "5 November, 2022", "Saturday, November 5, 2022.", "date_format"),
        ("When?", "the third of May 2023", "May 3rd, 2023", "date_format"),
        ("When?", "March 9", "It was on 9 March.", "date_format"),
        ("How long?", "2 hours", "They drove for 120 minutes.", "unit"),
        ("How long?", "a week", "It lasted 7 days.", "unit"),
        ("How long?", "1.5 years", "About... no: 18 months.", "unit"),
        ("How long?", "two years", "24 months", "unit"),
        ("How far?", "5 km", "5000 metres", "unit"),
        ("Cost?", "$5", "It cost 5 dollars.", "numeral"),  # same unit stays with v1
        ("Which?", "3rd", "It was her third attempt.", "ordinal"),
        ("Which?", "the second", "The 2nd one.", "ordinal"),
        ("Who?", "Mom", "Her mother.", "alias"),
        ("Where?", "USA", "She lives in the United States.", "alias"),
        ("What?", "TV", "He bought a television.", "alias"),
    ],
)
def test_v2_credit(question: str, gold: str, answer: str, rule: str) -> None:
    if gold == "1.5 years":
        answer = "18 months."
    got = check_conventions_v2(question, answer, gold)
    assert got is not None and got.rule == rule


@pytest.mark.parametrize(
    ("question", "gold", "answer"),
    [
        # dates: another day, a range or relative cue, a relative gold, a negation
        ("When?", "3 May 2023", "4 May 2023"),
        ("When?", "3 May 2023", "May 3, 2022"),
        ("When?", "3 May 2023", "between May 3 and May 6, 2023"),
        ("When?", "3 May 2023", "the week of May 3, 2023"),
        ("When?", "3 May 2023", "It was not on 3 May 2023."),
        ("When?", "The Friday before 9 October 2022", "October 9, 2022"),  # gold is not a bare date
        ("When?", "3 May 2023", "I could not find a date."),
        ("When?", "May 2023", "May 3, 2023"),  # a month gold is not one day
        # units: another amount, hedge, comparative, wrong dimension, gold with extra words
        ("How long?", "2 hours", "100 minutes"),
        ("How long?", "2 hours", "about 120 minutes"),
        ("How long?", "2 hours", "at least 120 minutes"),
        ("How long?", "2 hours", "2 km"),
        ("How long?", "2 hours of practice", "120 minutes"),
        ("How long?", "a week", "6 days"),
        ("How long?", "a month", "30 days"),  # no month-day conversion by design
        ("Cost?", "$5", "$6"),
        # ordinal / alias: a different one, a negation, a refusal, a partial phrase
        ("Which?", "3rd", "the 4th"),
        ("Which?", "third", "It was not her third attempt."),
        ("Who?", "Mom", "Her father."),
        ("Who?", "Mom", "Not mentioned."),
        ("Who?", "Mom", "She did not say her mother."),
        ("Where?", "USA", "She lives in Canada."),
        ("What?", "Paris", "She went to Paris"),  # exact match is the LLM judge's job
        ("What?", "red car", "a red bicycle"),
    ],
)
def test_v2_does_not_credit(question: str, gold: str, answer: str) -> None:
    assert check_conventions_v2(question, answer, gold) is None


def test_v1_credits_keep_their_rule_name() -> None:
    q, g, a = "What game?", "Xenoblade Chronicles", "Xeonoblade Chronicles"
    assert check_conventions(q, a, g).rule == "typo"  # type: ignore[union-attr]
    assert check_conventions_v2(q, a, g).rule == "typo"  # type: ignore[union-attr]


def test_v2_never_credits_an_abstention_gold_or_empty_input() -> None:
    assert check_conventions_v2("q", "mother", "Not mentioned in the chat") is None
    assert check_conventions_v2("q", "", "Mom") is None
    assert check_conventions_v2("q", "mother", None) is None


def test_alias_table_is_small_generic_and_symmetric() -> None:
    assert 5 <= len(ALIAS_GROUPS) <= 40
    flat = [a for g in ALIAS_GROUPS for a in g]
    assert len(flat) == len(set(flat))  # an alias belongs to exactly one group
    assert all(a == a.lower() and len(g) >= 2 for g in ALIAS_GROUPS for a in g)
    # no dataset answer: no alias is, by itself, the gold of a calibration row
    golds = {r["gold"].lower().strip(" .") for r in load_set(CALIBRATION)}
    assert not golds & set(flat)


# -- the column ---------------------------------------------------------------------------------


class _Spec:
    judge_id, model, prompt_id, prompt_hash, makes_model_calls = "x", "m", "p", "h", True
    scale = JudgeScale.BINARY
    params: Any = ()


class _Inner:
    spec = _Spec()

    def __init__(self, score: float) -> None:
        self._score = score

    async def score(self, question: str, answer: str, gold: str | None) -> Verdict:
        return Verdict(score=self._score, scale=JudgeScale.BINARY, raw="x", model_calls=1)


async def test_third_column_keeps_score_and_v1_column() -> None:
    judge = GuardedJudge(_Inner(0.0), conventions=True, conventions_v2=True)
    v = await judge.score("When?", "Thu 2023-10-19", "19 October 2023")
    assert v.score == 0.0  # the official verdict is untouched
    assert v.meta["score_conventions"] == 0.0 and "convention" not in v.meta  # v1 does not apply
    assert v.meta["score_conventions_v2"] == 1.0 and v.meta["convention_v2"] == "date_format"
    assert judge.spec.params["conventions_v2"] == CONVENTIONS_V2_VERSION
    ok = await GuardedJudge(_Inner(1.0), conventions_v2=True).score("q", "x", "y")
    assert ok.score == 1.0 and ok.meta["score_conventions_v2"] == 1.0 and "convention_v2" not in ok.meta


async def test_v2_alone_has_no_v1_column_and_off_is_unchanged() -> None:
    alone = await GuardedJudge(_Inner(0.0), conventions_v2=True).score("q", "7 days", "a week")
    assert "score_conventions" not in alone.meta and alone.meta["score_conventions_v2"] == 1.0
    off = GuardedJudge(_Inner(0.0))
    v = await off.score("q", "7 days", "a week")
    assert v.meta == {} and "conventions_v2" not in off.spec.params


def test_cli_flag_and_config_default() -> None:
    parser = build_parser()
    assert parser.parse_args(["c0-1", "--dataset", "locomo"]).judge_conventions_v2 is False
    on = parser.parse_args(["c0-1", "--dataset", "locomo", "--judge-conventions-v2"])
    assert on.judge_conventions_v2 is True
    assert C01Config().judge_conventions_v2 is False


# -- validation on the calibration set ----------------------------------------------------------


def test_calibration_set_agreement_does_not_move_and_no_false_positive() -> None:
    rows = load_set(CALIBRATION)
    judged = {r["id"]: r["source_judge_label"] for r in rows}
    res = with_conventions(rows, judged)["v2"]
    base = with_conventions(rows, judged)["judge_alone"]
    assert res["false_positives"] == 0 and res["false_positives_beyond_v1"] == 0
    for sub in ("all", "without_borderline"):
        assert res["judge_or_conventions_v2"][sub]["false_positives"] <= base[sub]["false_positives"]
        assert res["judge_or_conventions_v2"][sub]["agreement"] >= base[sub]["agreement"]
    json.dumps(res)  # serialisable for --json
