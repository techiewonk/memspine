"""HaluMem retrieval proxy: memory-point -> turn lexical mapping (synthetic fixture)."""

from __future__ import annotations

from pathlib import Path

from memspine_evals.contracts import Turn
from memspine_evals.datasets.halumem_proxy import HaluMemProxyDataset, map_evidence, proxy_tokens

FIX = Path(__file__).resolve().parent / "fixtures" / "HaluMem-Medium.jsonl"


def test_proxy_tokens_drop_pronouns_and_stopwords() -> None:
    assert proxy_tokens("I adopted a grey cat") == {"adopted", "grey", "cat"}


def test_map_evidence_threshold_and_latest_tie() -> None:
    turns = (
        Turn("t1", "s0", "user", "I teach physics now"),
        Turn("t2", "s1", "user", "Now I teach physics"),
        Turn("t3", "s1", "user", "unrelated words entirely"),
    )
    assert map_evidence("Ana now teaches physics.", turns, 0.5) == ("t2", 0.5)
    assert map_evidence("Ana now teaches physics.", turns, 0.75)[0] is None
    assert map_evidence("", turns) == (None, 0.0)


def test_halumem_proxy_fills_gold_turn_ids() -> None:
    ds = HaluMemProxyDataset(FIX, revision_id="auto")
    queries = {q.text: q for item in ds.items() for q in item.queries}
    cat = queries["What is the name of Ana's cat?"]
    assert cat.gold_turn_ids == ("synthetic-user-1:s0:t0:user",)
    assert cat.meta["proxy_gold"] and cat.meta["evidence_mapped"] == 1
    update = queries["What subject does Ana teach now?"]
    assert update.gold_turn_ids == ("synthetic-user-1:s1:t0:user",)
    boundary = queries["What is Ana's salary?"]
    assert boundary.gold_turn_ids == () and boundary.meta["abstention"] is True
    info = ds.info()
    assert info.dataset_id.endswith("_proxy") and "mapped 2/2" in info.notes
