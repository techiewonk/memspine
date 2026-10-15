"""I12: the judge date check imports its parser robustly and reports 'not applicable'."""

from __future__ import annotations

import sys
from typing import ClassVar

import pytest
from memspine_evals import date_check as dc


def test_not_applicable_for_non_single_day_gold() -> None:
    for gold in ("The weekend before 17 July 2023", "2022", "May 2023", "a pottery class", ""):
        assert dc.date_check(gold, "2023-07-15") is None
    assert dc.date_check(None, "2023-07-15") is None


def test_applicable_gold_matches_or_not() -> None:
    assert dc.date_check("7 May 2023", "Sunday, May 7, 2023") is True
    assert dc.date_check("7 May 2023", "8 May 2023") is False
    assert dc.date_check("7 May 2023", "") is False
    assert dc.date_check("7 May 2023", "She never went on 7 May 2023") is False
    assert dc.date_equivalent("a pottery class", "pottery") is False


def test_parser_resolves_without_evals_on_sys_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The evals folder is found from the package location, not from sys.path."""
    monkeypatch.setattr(dc, "_PARSER", None)
    evals = str(dc._EVALS_DIR)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != evals])
    monkeypatch.delitem(sys.modules, "failure_buckets", raising=False)
    monkeypatch.delitem(sys.modules, "error_analysis", raising=False)
    assert dc.date_equivalent("7 May 2023", "Sunday, May 7, 2023") is True


def test_missing_parser_is_loud_not_silent(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setattr(dc, "_PARSER", None)
    monkeypatch.setattr(dc, "_EVALS_DIR", dc._EVALS_DIR / "does_not_exist")
    monkeypatch.setattr(dc.importlib, "import_module", _raise_import)
    with caplog.at_level("ERROR"), pytest.raises(dc.DateParserUnavailable):
        dc.date_equivalent("7 May 2023", "May 7, 2023")
    assert any("date check unavailable" in r.message for r in caplog.records)


def test_guarded_judge_fails_at_construction_when_parser_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memspine_evals.judge import GuardedJudge

    monkeypatch.setattr(dc, "_PARSER", None)
    monkeypatch.setattr(dc.importlib, "import_module", _raise_import)

    class _Inner:
        class spec:
            judge_id = "j"
            scale = None
            model = "m"
            prompt_id = "p"
            prompt_hash = "h"
            makes_model_calls = False
            params: ClassVar[dict] = {}

    with pytest.raises(dc.DateParserUnavailable):
        GuardedJudge(_Inner(), date_check=True)


def _raise_import(name: str):
    raise ImportError(name)
