"""I19: multi-user isolation probe (cross-namespace leakage rate), offline.

Two synthetic users get disjoint facts in separate namespaces of ONE engine. The probe then
queries as user A under a set of read configurations (list mode, bridge hop, session leg,
rerank, ...) and checks that no user-B record, and no user-B canary token, ever appears in
what the engine returns (the ranked records of ``search`` and the records plus rendered text
of ``read``). A leak is a P0 bug.

The two users deliberately share names, topics and session ids (``group_id``), so a filter
that is keyed on anything other than the namespace would leak. User A's own canaries must
come back for at least one probe per config, otherwise the probe would pass vacuously.

No model is called: the embedder is the engine's ``hash`` provider and the reranker a
constant fake. Run it from a test (``evals/tests/test_leakage_probe.py``) or:

    python -m memspine_evals.leakage          # prints the leakage table, exit 1 on any leak
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = ["CONFIGS", "LeakReport", "ProbeResult", "main", "probe", "run_all"]

NS_A, NS_B = "user-a", "user-b"
T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)

#: Shared surface wording; the canary token is what tells the users apart.
_TEMPLATES = (
    "Sam: I love hiking and pottery, last summer I went camping at the lake {c}1",
    "Pat: Sounds great, I moved from my home country years ago {c}2",
    "Sam: My hobbies are painting, running and swimming {c}3",
    "Sam: I adopted a dog named Rex and a cat named Mia {c}4",
    "Pat: Life in my home country was very different {c}5",
    "Sam: I visited Paris, Rome and Berlin on my trips {c}6",
    "Sam: My favourite books are novels about camping and trips {c}7",
    "Pat: The pottery class on Friday was fun {c}8",
)

#: Queries a user-A caller sends; they hit list mode, the bridge cue and plain lookups.
QUERIES = (
    "What hobbies does Sam enjoy?",
    "Where did Sam go camping last summer?",
    "Where did Pat move from?",
    "Which cities has Sam visited?",
    "What pets does Sam have?",
    "When did Sam go to the pottery class?",
)

READ_MODES = ("auto", "replay", "retrieve", "full")

#: name -> ReadConfig overrides. Each one turns a retrieval path on; ``rerank`` registers a
#: constant fake reranker (no weights) so the rerank pool path runs too.
CONFIGS: dict[str, dict[str, Any]] = {
    "baseline": {},
    "list_mode": {"list_mode": True},
    "list_mode_wide": {"list_mode": True, "list_trigger": "set_question_wide"},
    "bridge_hop": {"bridge_hop": True},
    "bridge_hop_cue_gate": {"bridge_hop": True, "bridge_hop_gate": "cue"},
    "session_leg": {"session_leg": True},
    "session_digest": {"session_digest": True},
    "cohesion_entity_word": {
        "cohesion_leg": True,
        "entity_expand_leg": True,
        "word_vector_leg": True,
    },
    "temporal_leg": {"temporal_leg": True, "temporal_leg_mentions": True},
    "rerank": {"rerank": "leak_probe_const", "candidate_pool": 3, "rerank_keep": 5},
    "rerank_bridge_list": {
        "rerank": "leak_probe_const",
        "candidate_pool": 3,
        "bridge_hop": True,
        "list_mode": True,
        "session_leg": True,
    },
    "session_cap_mmr": {"session_cap": 2, "mmr_lambda": 0.5},
}


class _ConstReranker:
    reranker_id = "leak_probe_const"

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        return [1.0 / (i + 1) for i in range(len(documents))]


def _register_reranker() -> None:
    from memspine.services.rerank.factory import register_reranker

    register_reranker("leak_probe_const", lambda settings: _ConstReranker())


@dataclass(slots=True)
class ProbeResult:
    config: str
    queries: int = 0
    records_returned: int = 0
    own_records_returned: int = 0
    leaked_records: int = 0
    leaked_canaries: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def leak_rate(self) -> float:
        return self.leaked_records / self.records_returned if self.records_returned else 0.0


@dataclass(slots=True)
class LeakReport:
    results: list[ProbeResult]

    @property
    def leaked(self) -> bool:
        return any(r.leaked_records or r.leaked_canaries for r in self.results)

    @property
    def vacuous(self) -> list[str]:
        """Configs where user A never got its own records back: the pass proves nothing."""
        return [r.config for r in self.results if r.own_records_returned == 0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespaces": [NS_A, NS_B],
            "leaked": self.leaked,
            "severity": "P0" if self.leaked else None,
            "results": [{**asdict(r), "leak_rate": r.leak_rate} for r in self.results],
        }

    def markdown(self) -> str:
        rows = [
            "| config | queries | records | own | leaked | leak rate |",
            "|---|---|---|---|---|---|",
        ]
        for r in self.results:
            rows.append(
                f"| {r.config} | {r.queries} | {r.records_returned} | {r.own_records_returned} | "
                f"{r.leaked_records} | {r.leak_rate:.1%} |"
            )
        if self.leaked:
            rows.append("\nP0: cross-user leak detected.")
        return "\n".join(rows)


def _canaries(tag: str) -> list[str]:
    return [t.format(c=tag).split()[-1] for t in _TEMPLATES]


async def _seed(eng: Any, ns: str, tag: str) -> None:
    for i, tpl in enumerate(_TEMPLATES):
        await eng.write(
            tpl.format(c=tag),
            namespace=ns,
            memory_type="episodic",
            group_id=f"session-{i // 3}",  # the same session ids in both namespaces
            valid_from=T0 + timedelta(minutes=i),
        )


def _make_engine(read: Mapping[str, Any]) -> Any:
    from memspine import Engine

    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )


async def probe(
    name: str,
    read: Mapping[str, Any],
    *,
    engine_factory: Callable[[Mapping[str, Any]], Any] = _make_engine,
    queries: Sequence[str] = QUERIES,
    modes: Sequence[str] = READ_MODES,
) -> ProbeResult:
    """Seed both users, query as A, and count every user-B record or canary that comes back."""
    _register_reranker()
    res = ProbeResult(name)
    eng = engine_factory(read)
    await eng.start()
    try:
        await _seed(eng, NS_A, "zqa")
        await _seed(eng, NS_B, "zqb")
        b_marks = tuple(c.lower() for c in _canaries("zqb"))
        a_marks = tuple(c.lower() for c in _canaries("zqa"))

        def audit(records: Sequence[Any], rendered: str = "") -> None:
            for rec in records:
                res.records_returned += 1
                text = str(rec.content).lower()
                if getattr(rec, "namespace", NS_A) != NS_A or any(m in text for m in b_marks):
                    res.leaked_records += 1
                    res.leaked_canaries.append(str(rec.content)[:80])
                elif any(m in text for m in a_marks):
                    res.own_records_returned += 1
            if any(m in rendered.lower() for m in b_marks):
                res.leaked_canaries.append("rendered:" + rendered[:80])

        for q in queries:
            res.queries += 1
            try:
                hits = await eng.search(q, namespace=NS_A, top_k=8)
                audit([r for r, _ in hits])
                for mode in modes:
                    out = await eng.read(q, namespace=NS_A, mode=mode, top_k=8)
                    ctx = out.context
                    audit(list(ctx.records), str(getattr(ctx, "text", "") or ""))
            except Exception as exc:  # a crash is a finding, not a pass
                res.errors.append(f"{q!r}: {type(exc).__name__}: {exc}")
    finally:
        await eng.stop()
    return res


async def run_all(configs: Mapping[str, Mapping[str, Any]] | None = None) -> LeakReport:
    out = [await probe(n, c) for n, c in (configs or CONFIGS).items()]
    return LeakReport(out)


def main(argv: list[str] | None = None) -> int:
    report = asyncio.run(run_all())
    print(report.markdown())
    if "--json" in (argv or sys.argv[1:]):
        print(json.dumps(report.to_dict(), indent=1))
    return 1 if report.leaked else 0


if __name__ == "__main__":
    raise SystemExit(main())
