"""MemoryAgentBench Conflict_Resolution: gold-fact mapping and the supersession-order metric.

Fixtures are hand-written facts in the release's surface forms, not release data.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from memspine_evals.datasets.mab_gold import (
    Fact,
    map_gold,
    parse_fact,
    supersession_order,
    supersession_order_rate,
)

LINES = [
    "Alice Smith is a citizen of France.",
    "The capital of France is Paris.",
    "Alice Smith is a citizen of Spain.",  # supersedes 0
    "The capital of Spain is Madrid.",
    "The capital of Spain is Toledo.",  # supersedes 3
    "The Prime Minister of Ruritania is Rudolf.",  # generic fallback form
    "The capital of Portugal is Lisbon.",
]
CHAIN_Q = "What is the capital of the country Alice Smith is a citizen of?"


def _facts() -> list[Fact]:
    out = []
    for serial, text in enumerate(LINES):
        parsed = parse_fact(text)
        assert parsed is not None, text
        out.append(Fact(serial, f"0:{serial}", *parsed))
    return out


def test_parse_fact_templates_and_fallback() -> None:
    assert parse_fact("The capital of Tang Empire is Chang'an.") == (
        "Tang Empire",
        "capital",
        "Chang'an",
    )
    assert parse_fact("Terry Pratchett's child is Rhianna Pratchett.")[1] == "child"
    assert parse_fact("Pedro Pierluisi worked in the city of Washington, D.C..")[2] == (
        "Washington, D.C."
    )
    assert parse_fact("The Prime Minister of Sweden is Stefan Löfven.") == (
        "Sweden",
        "role:prime minister",
        "Stefan Löfven",
    )
    assert parse_fact("no template here") is None


def test_single_hop_gold_is_newest_fact_with_stale_versions() -> None:
    m = map_gold(_facts(), "What is the country of citizenship of Alice Smith?", ["Spain"], False)
    assert m.status == "mapped"
    assert m.gold == ("0:2",)
    assert dict(m.stale_by_gold) == {"0:2": ("0:0",)}


def test_multi_hop_chain_follows_current_facts() -> None:
    m = map_gold(_facts(), CHAIN_Q, ["Toledo"], True)
    assert m.status == "mapped" and m.hops == 2
    assert m.gold == ("0:2", "0:4")
    assert set(m.stale) == {"0:0", "0:3"}


def test_inconsistent_release_answer_falls_back_to_all_versions() -> None:
    # The answer needs the superseded capital (Madrid): kept as gold, flagged, and the
    # non-current hop is left out of the order metric.
    m = map_gold(_facts(), CHAIN_Q, ["Madrid"], True)
    assert m.status == "inconsistent"
    assert m.gold == ("0:2", "0:3")
    assert dict(m.stale_by_gold) == {"0:2": ("0:0",)}


def test_unmapped_keeps_empty_gold() -> None:
    m = map_gold(_facts(), "Who is Bob?", ["Nobody"], False)
    assert m.status == "unmapped" and m.gold == ()


def test_supersession_order_cases() -> None:
    stale = {"g": ["s1", "s2"]}
    assert supersession_order(["g", "x", "s1"], ["g"], stale) is True
    assert supersession_order(["s2", "g"], ["g"], stale) is False
    assert supersession_order(["s1", "x"], ["g"], stale) is False  # current missing
    assert supersession_order(["g", "x"], ["g"], stale) is None  # no stale retrieved
    assert supersession_order(["g", "x", "s1"], ["g"], stale, k=2) is None  # cut at k


def test_supersession_order_rate_pools_applicable_queries() -> None:
    meta = {
        "a": {"gold_facts": ["g"], "stale_by_gold": {"g": ["s"]}},
        "b": {"gold_facts": ["h"], "stale_by_gold": {"h": ["t"]}},
        "c": {"gold_facts": ["i"], "stale_by_gold": {"i": ["u"]}},
        "d": {"gold_facts": [], "stale_by_gold": {}},
    }
    rows = [
        {"query_id": "a", "retrieved_ids": ["g", "s"]},
        {"query_id": "b", "retrieved_ids": ["t", "h"]},
        {"query_id": "c", "retrieved_ids": ["i"]},
        {"query_id": "d", "retrieved_ids": ["z"]},
    ]
    out = supersession_order_rate(rows, meta)
    assert out == {"supersession_order": 0.5, "n_applicable": 2, "n_with_gold": 3}


def test_adapter_fills_gold_turn_ids(tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq
    from memspine_evals.datasets import MemoryAgentBenchDataset

    context = "Here is a list of facts:\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(LINES))
    table = pa.Table.from_pylist(
        [
            {
                "context": context,
                "questions": [
                    "What is the country of citizenship of Alice Smith?",
                    "What is the capital of the country Alice Smith is a citizen of?",
                ],
                "answers": [["Spain"], ["Toledo"]],
                "metadata": {"qa_pair_ids": ["factconsolidation_mh_6k_no0", "x_mh_6k_no1"]},
            }
        ]
    )
    path = tmp_path / "Conflict_Resolution.parquet"
    pq.write_table(table, path)
    ds = MemoryAgentBenchDataset(path, revision_id="auto")
    (item,) = list(ds.items())
    q0, q1 = item.queries
    assert q0.gold_turn_ids == ("0:2",)
    assert q0.meta["stale_by_gold"] == {"0:2": ["0:0"]}
    assert q1.gold_turn_ids == ("0:2", "0:4")
    assert ds.info().notes.startswith("gold=mab_gold-v1")
    plain = MemoryAgentBenchDataset(path, revision_id="auto", map_gold=False)
    assert all(not q.gold_turn_ids for q in next(plain.items()).queries)
