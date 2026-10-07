"""TOFU and MUSE-News erasure-probe adapters, on synthetic fixtures that copy the schemas."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from memspine_evals.datasets.tofu_muse import (
    MUSENewsDataset,
    TOFUDataset,
    erase_and_verify,
    membership_auroc,
)

FIX = Path(__file__).resolve().parent / "fixtures"
DATA = Path(__file__).resolve().parents[1] / "data"


def test_tofu_maps_facts_and_probe_sets() -> None:
    ds = TOFUDataset(FIX / "tofu", revision_id="auto", split="forget01")
    (item,) = list(ds.items())
    assert [t.turn_id for t in item.history] == [f"tofu:{n}" for n in range(4)]
    assert item.history[2].text.endswith("Bram Tolk.")  # the fact is the answer
    by_set: dict[str, list[Any]] = {}
    for q in item.queries:
        by_set.setdefault(q.meta["probe_set"], []).append(q)
    assert {k: len(v) for k, v in by_set.items()} == {
        "forget": 2,
        "forget_paraphrase": 2,
        "retain": 2,
        "retain_paraphrase": 2,
        "holdout": 1,
    }
    assert [q.gold_turn_ids for q in by_set["forget"]] == [("tofu:2",), ("tofu:3",)]
    assert by_set["forget_paraphrase"][0].gold_turn_ids == ("tofu:2",)
    assert by_set["retain"][0].gold_turn_ids == ("tofu:0",)
    assert by_set["holdout"][0].gold_turn_ids == ()
    assert item.meta["forget_turn_ids"] == ["tofu:2", "tofu:3"]
    assert len(item.meta["forget_probes"]["tofu:2"]) == 2  # original + paraphrased answer
    info = ds.info()
    assert info.revision_id.startswith("sha256:") and info.n_queries == 9


def test_tofu_rejects_unknown_split() -> None:
    with pytest.raises(ValueError):
        TOFUDataset(FIX / "tofu", revision_id="x", split="forget50")


def test_muse_maps_passages_and_prefix_probes() -> None:
    ds = MUSENewsDataset(FIX / "muse_news", revision_id="506bd5b", probe_words=5)
    (item,) = list(ds.items())
    assert [t.turn_id for t in item.history] == ["muse:forget:0", "muse:forget:1", "muse:retain:0"]
    q = {query.query_id: query for query in item.queries}
    assert q["muse:forget:0"].text == "The harbour council voted to"
    assert q["muse:forget:0"].gold_turn_ids == ("muse:forget:0",)
    assert q["muse:retain:0"].meta["probe_set"] == "retain"
    assert q["muse:holdout:0"].gold_turn_ids == ()
    assert item.meta["forget_turn_ids"] == ["muse:forget:0", "muse:forget:1"]


class _FakeEngine:
    """Records forget calls; one record keeps a residual hit to exercise the counting."""

    def __init__(self, leaky: str) -> None:
        self.leaky = leaky
        self.forgotten: list[tuple[str, bool]] = []
        self.verified: list[tuple[str, str | None]] = []

    async def forget(self, record_id: str, namespace: str = "default", hard: bool = False) -> None:
        self.forgotten.append((record_id, hard))

    async def verify_forget(
        self, record_id: str, namespace: str = "default", *, probe: str | None = None
    ) -> Mapping[str, Any]:
        self.verified.append((record_id, probe))
        residual = ["copy-1"] if record_id == self.leaky else []
        return {"clean": not residual, "residual_recall": residual}


def test_erase_and_verify_hard_deletes_and_counts_residuals() -> None:
    (item,) = list(TOFUDataset(FIX / "tofu", revision_id="x").items())
    engine = _FakeEngine(leaky="r3")
    deposits = {"tofu:2": ["r2"], "tofu:3": ["r3"], "tofu:0": ["r0"]}
    report = asyncio.run(erase_and_verify(engine, item, deposits))
    assert engine.forgotten == [("r2", True), ("r3", True)]  # retain record untouched
    assert {p for _, p in engine.verified} >= {"Bram Tolk won the Northern Lantern Prize."}
    assert report["n_records"] == 2 and report["n_clean"] == 1 and report["clean_rate"] == 0.5
    assert report["n_probe_checks"] == 4 and report["n_residual"] == 2
    assert report["residual_records"] == ["r3"]
    assert report["turns_without_records"] == []


def test_erase_and_verify_reports_undeposited_turns() -> None:
    (item,) = list(MUSENewsDataset(FIX / "muse_news", revision_id="x").items())
    report = asyncio.run(erase_and_verify(_FakeEngine(leaky=""), item, {"muse:forget:0": ["a"]}))
    assert report["turns_without_records"] == ["muse:forget:1"]
    assert report["residual_rate"] == 0.0 and report["clean_rate"] == 1.0


def test_membership_auroc() -> None:
    assert membership_auroc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert membership_auroc([0.5], [0.5]) == 0.5
    assert membership_auroc([], [0.1]) is None


@pytest.mark.skipif(
    not (DATA / "tofu" / "full.json").exists() or not (DATA / "muse_news" / "privleak").exists(),
    reason="TOFU / MUSE-News not fetched",
)
def test_local_tofu_and_muse_parse() -> None:
    (tofu,) = list(TOFUDataset(DATA / "tofu", revision_id="324592d", max_retain=5).items())
    assert len(tofu.history) == 4000 and len(tofu.meta["forget_turn_ids"]) == 40
    (muse,) = list(MUSENewsDataset(DATA / "muse_news", revision_id="506bd5b").items())
    assert len(muse.history) == 200 and len(muse.meta["forget_turn_ids"]) == 100
