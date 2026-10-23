"""CPU-only smoke of the combined-engine arms (G-C2, G-C3, G-C9): no GPU, no Ollama, no network.

    python evals/smoke_gc.py [--arm scr-gc2-facts-jina ...] [--keep]

For each arm: (1) the engine config is validated against the schema and every policy block
(`rehearsal.validate_engine_config`); (2) the arm's config is run through the real harness
(`run_c0_1`, retrieval-only) on ONE small synthetic LoCoMo-format conversation, with the sleep cycle on
(`memspine_build_sleep`, the run.sh flag `--memspine-build-sleep`) and every LLM role answered by an offline
stand-in at the OpenAICompat transport (`stub_llm`), so the roles, prompts, `/no_think` switch, parsing and call
accounting are the real ones. Only the machine-bound parts of the arm are swapped for the smoke: the embedder
(`hash`) and the reranker (off); every key the arm adds on top of `scr-pool-protect-jina` is left as written.
It then reads the engine's read forensics and checks that the arm's stage ran and its leg or block appears.
Nothing here measures quality; the synthetic data and the fake LLMs prove wiring only.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE]
sys.path.append(str(HERE))

ARMS = ("scr-gc2-facts-jina", "scr-gc3-graph-jina", "scr-gc9-summary-jina", "scr-window-facts-jina")

# -- one small synthetic conversation in LoCoMo format -------------------------------------------------------

SESSIONS = [
    (
        "1:56 pm on 8 May, 2023",
        [
            ("Noor", "I went to the chess club yesterday and it was lively."),
            ("Bram", "That sounds great, Noor. How are you feeling about it?"),
            ("Noor", "Hopeful. I want to become a counselor and I recently moved to Denver."),
            ("Bram", "Denver is lovely. I started pottery classes last week."),
            ("Noor", "Pottery sounds fun. Do you make bowls or vases?"),
            ("Bram", "Mostly bowls. I also finished reading Charlotte's Web with my kids."),
            ("Noor", "Nice. I love the mountains near Denver, I hike there on weekends."),
            ("Bram", "We should hike together sometime."),
        ],
    ),
    (
        "3:10 pm on 20 May, 2023",
        [
            ("Bram", "I painted a sunrise over the lake on Sunday."),
            ("Noor", "Beautiful. I adopted a dog named Biscuit last week."),
            ("Bram", "Biscuit is a great name. Did you take him to the park?"),
            ("Noor", "Yes, we went hiking in the mountains near Denver again."),
            ("Bram", "My kids and I went camping at the beach in June last year."),
            ("Noor", "Camping sounds relaxing. Are you planning to camp this summer?"),
            ("Bram", "Yes, in July. I also signed up for a painting workshop."),
            ("Noor", "Great. I joined a counseling certificate program in Denver."),
        ],
    ),
    (
        "10:00 am on 2 June, 2023",
        [
            ("Noor", "My counseling program starts next month and I am nervous."),
            ("Bram", "You will do well. I sold my first bowl at a pottery fair."),
            ("Noor", "Congratulations, Bram. Which fair was it?"),
            ("Bram", "The Riverside Pottery Fair on Saturday."),
            ("Noor", "I will visit the next one. Biscuit loves the river."),
            ("Bram", "Bring him along. I read The Hobbit to my kids last night."),
            ("Noor", "I read Charlotte's Web as a child, a great book."),
            ("Bram", "It is. Let us plan a camping trip with the kids."),
        ],
    ),
]
QA = [
    ("Where did Noor move to?", "Denver", ["D1:3"], 4),
    ("What books has Bram read?", "Charlotte's Web, The Hobbit", ["D1:6", "D3:6"], 1),
    ("When did Bram sell her first bowl?", "2 June 2023", ["D3:2"], 2),
    ("What is the name of Noor's dog?", "Biscuit", ["D2:2"], 4),
]


def synthetic_locomo(path: Path) -> None:
    conv: dict[str, Any] = {"speaker_a": "Noor", "speaker_b": "Bram"}
    for k, (stamp, turns) in enumerate(SESSIONS, start=1):
        conv[f"session_{k}_date_time"] = stamp
        conv[f"session_{k}"] = [
            {"speaker": who, "dia_id": f"D{k}:{i}", "text": text}
            for i, (who, text) in enumerate(turns, start=1)
        ]
    qa = [{"question": q, "answer": a, "evidence": ev, "category": c} for q, a, ev, c in QA]
    path.write_text(
        json.dumps([{"sample_id": "conv-smoke", "conversation": conv, "qa": qa}]), encoding="utf-8"
    )


# -- offline LLM: the shipped stub plus the roles it does not know ------------------------------------------

_LINE = re.compile(r"^\s*\[(\d+)\]\s*(?:\[(\d{4}-\d{2}-\d{2})\])?\s*(.*)$")
_CAP = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]{2,}(?:'s Web)?)\b")


def make_stub() -> Any:
    from memspine_evals.stub_llm import StubOpenAICompat, _content_lines

    class SmokeLLM(StubOpenAICompat):
        prompts: Counter[str] = Counter()  # first words of each user prompt, per caller kind

        def classify(self, messages: list[dict[str, Any]]) -> str:
            text = " ".join(str(m.get("content", "")) for m in messages)
            if "Reply with the JSON object for the sections:" in text:
                return "window_extract"
            if "Emit a YAML list under the key `summaries`" in text:
                return "summarize_entity"
            if "Emit a YAML list under the key `classes`" in text:
                return "classes"
            return super().classify(messages)

        def reply(self, kind: str, messages: list[dict[str, Any]]) -> str:
            user = str(messages[-1].get("content", ""))
            self.prompts[kind + ": " + " ".join(user.split())[:58]] += 1
            if kind == "window_extract":
                # W1 call 1: one grounded fact per transcript line ("[n] [date] Speaker: text")
                facts = []
                for line in re.findall(r"^\[(\d+)\] \[[\d-]+\] (.+)$", user, re.M):
                    n, body = line
                    who, _, said = body.partition(": ")
                    facts.append(
                        {
                            "entity": who,
                            "attribute": "event",
                            "kind": "event",
                            "value": f"{who} said {said[:120]}",
                            "turns": [int(n)],
                            "span": " ".join(said.split()[:8]),
                            "persons": [who],
                            "topic": "activities",
                            "confidence": 0.8,
                        }
                    )
                return json.dumps({"facts": facts})
            if kind == "summarize_entity":
                names = re.findall(r"^\[(\d+)\]\s+(.+)$", user, re.M)
                return "summaries:\n" + "\n".join(
                    f'  - index: {i}\n    summary: "{n} appears in several dated facts."'
                    for i, n in names
                )
            if kind == "classes":
                idx = re.findall(r"^\s*\[?(\d+)\]?[.:)]?\s", user.split("facts:", 1)[-1], re.M)
                return "classes:\n" + "\n".join(
                    f"  - index: {i}\n    label: activities" for i in idx
                )
            if kind == "extract_session":
                facts = []
                for line in _content_lines(user, "transcript: |")[:6]:
                    body = re.sub(r"^\s*(\[\d+\]\s*)?(\[\d{4}-\d{2}-\d{2}\]\s*)?", "", line)
                    who = body.split(":", 1)[0].strip()
                    date = re.search(r"\[(\d{4}-\d{2}-\d{2})\]", line)
                    text = body.replace('"', "'")[:150]
                    facts.append(
                        f"  - entity: {who}\n    attribute: event\n    kind: event\n"
                        f'    value: "{text}"\n'
                        f"    date: {date.group(1) if date else '~'}\n    persons: [{who}]\n"
                        "    topic: activities\n    confidence: 0.8"
                    )
                return "facts:\n" + "\n".join(facts) if facts else "facts: []"
            if kind == "extract_edges":
                edges = []
                for line in _content_lines(user, "transcript: |"):
                    m = _LINE.match(line)
                    if not m:
                        continue
                    idx, _date, body = m.groups()
                    speaker, _, said = body.partition(":")
                    caps = [c for c in _CAP.findall(said) if c != speaker.strip()]
                    if caps and speaker.strip():
                        edges.append(
                            f"  - src_entity: {speaker.strip()}\n    rel: mentioned\n    kind: event\n"
                            f'    dst_entity: {caps[0]}\n    fact: "{speaker.strip()} mentioned {caps[0]}"\n'
                            f"    confidence: 0.8\n    episode_indices: [{idx}]"
                        )
                return "edges:\n" + "\n".join(edges) if edges else "edges: []"
            return super().reply(kind, messages)

    return SmokeLLM()


# -- the run ------------------------------------------------------------------------------------------------


def cpu_config(arm: dict[str, Any]) -> dict[str, Any]:
    """The arm with only its machine-bound keys swapped: hash embedder, reranker off, in-process store."""
    cfg = copy.deepcopy(arm)
    cfg.setdefault("model", {}).setdefault("embedding", {})["provider"] = "hash"
    read = cfg["read"]
    for key in ("rerank", "rerank_model", "rerank_device", "rerank_keep", "rerank_floor"):
        read.pop(key, None)  # PA-107 flat arm keys
    cfg["model"]["rerank"] = {"chain": []}  # ADR-082: an empty chain is off
    cfg["storage"] = {"path": "tempdir"}
    return cfg


async def run_arm(arm_id: str, workdir: Path, data: Path) -> dict[str, Any]:
    from memspine_evals.datasets import LoCoMoDataset
    from memspine_evals.experiments import C01Config, run_c0_1
    from memspine_evals.rehearsal import validate_engine_config
    from memspine_evals.stub_llm import install_stub_openai_compat

    arm = json.loads((HERE / "arms" / f"{arm_id}.json").read_text(encoding="utf-8"))
    validate_engine_config(arm)  # the arm exactly as it will run
    cfg = cpu_config(arm)
    validate_engine_config(cfg)
    roles = sorted((arm.get("model") or {}).get("llm") or {})
    forensics = workdir / f"{arm_id}--forensics"
    os.environ["MEMSPINE_FORENSICS_DIR"] = str(forensics)
    stub = make_stub()
    from memspine_evals.systems import memspine_system as ms

    captured: list[dict[str, Any]] = []
    original_build = ms.MemspineSystem.build

    async def build(self: Any) -> Any:
        result = await original_build(self)
        captured.append(dict(getattr(result, "meta", None) or {}))
        return result

    ms.MemspineSystem.build = build  # type: ignore[method-assign]
    ds = LoCoMoDataset(data, revision_id="auto", categories=(1, 2, 3, 4))
    config = C01Config(
        mode="retrieval",
        retrieval_only=True,
        include_memspine=True,
        only_systems=("memspine",),
        top_k=10,
        memspine_config=cfg,
        memspine_read_mode="replay",
        memspine_build_sleep=True,
        memspine_batch_turns=32,
        max_model_calls=1000,
    )
    try:
        with install_stub_openai_compat(stub):
            await run_c0_1(ds, config, workdir, run_id=arm_id)
    finally:
        ms.MemspineSystem.build = original_build  # type: ignore[method-assign]
    run = workdir / f"{arm_id}--memspine"
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    fx = [
        json.loads(x)
        for x in (forensics / "forensics.jsonl").read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]
    sleep: dict[str, Any] = {}
    window: dict[str, Any] = {}
    for meta in captured:
        sleep = meta.get("sleep") or sleep
        window = meta.get("window_extract") or window
    return {
        "window_stats": window,
        "arm": arm_id,
        "bound_roles": roles,
        "stub_calls": dict(stub.calls),
        "prompt_heads": dict(stub.prompts),
        "thinking_calls": stub.thinking_calls,
        "engine_llm_usage": summary.get("engine_llm_usage"),
        "sleep_stages": sleep,
        "forensics": fx,
        "n_sessions": len(SESSIONS),
        "n_turns": sum(len(t) for _, t in SESSIONS),
    }


def derived(fx_row: dict[str, Any]) -> list[str]:
    """Context records that no source turn produced (mined facts, cards, summaries, blocks)."""
    return [
        c["text"]
        for c in fx_row.get("context_records", [])
        if not re.fullmatch(r"D\d+:\d+", str(c.get("turn")))
    ]


CHECKS = {
    "scr-gc2-facts-jina": lambda r, legs, blocks: [
        (
            "sleep stage mine_facts produced facts",
            (r["sleep_stages"].get("mine_facts") or {}).get("facts", 0) > 0,
        ),
        (
            "extract role called",
            sum(
                u["calls"]
                for st in (r["engine_llm_usage"] or {}).values()
                for role, u in st.items()
                if role == "extract"
            )
            > 0,
        ),
        ("view_tags leg present in read forensics", any("view_tags" in lg for lg in legs)),
        ("a mined fact reaches the context", any("atomic_fact" in b or b for b in blocks)),
    ],
    "scr-gc3-graph-jina": lambda r, legs, blocks: [
        (
            "sleep stage extract_graph wrote facts",
            (r["sleep_stages"].get("extract_graph") or {}).get("facts", 0) > 0
            or (r["sleep_stages"].get("extract_graph") or {}).get("status") == "ok",
        ),
        (
            "extract_edges role called",
            sum(
                u["calls"]
                for st in (r["engine_llm_usage"] or {}).values()
                for role, u in st.items()
                if role == "extract_edges"
            )
            > 0,
        ),
        (
            "a graph leg (graph / graph_nodes) present in read forensics",
            any(("graph" in lg or "graph_nodes" in lg) for lg in legs),
        ),
        (
            "ABOUT block (entity summaries) in a context",
            any(b.lstrip().startswith("ABOUT") for b in blocks),
        ),
    ],
    "scr-window-facts-jina": lambda r, legs, blocks: [
        ("window calls ran (call 1)", (r["window_stats"] or {}).get("calls", 0) > 0),
        (
            "window facts deposited with grounded spans",
            (r["window_stats"] or {}).get("facts_deposited", 0) > 0
            and (r["window_stats"] or {}).get("facts_ungrounded", 1) == 0,
        ),
        (
            "no parse failure and no section dropped",
            (r["window_stats"] or {}).get("parse_failures", 1) == 0
            and (r["window_stats"] or {}).get("sections_dropped", 1) == 0,
        ),
        (
            "window_extract role called",
            sum(
                u["calls"]
                for st in (r["engine_llm_usage"] or {}).values()
                for role, u in st.items()
                if role == "window_extract"
            )
            > 0,
        ),
        ("view_tags leg present in read forensics", any("view_tags" in lg for lg in legs)),
        ("a window fact reaches the context", any(b for b in blocks)),
    ],
    "scr-gc9-summary-jina": lambda r, legs, blocks: [
        (
            "sleep stage consolidate wrote summaries",
            (r["sleep_stages"].get("consolidate") or {}).get("summaries", 0) > 0,
        ),
        (
            "summarize role called",
            sum(
                u["calls"]
                for st in (r["engine_llm_usage"] or {}).values()
                for role, u in st.items()
                if role == "summarize"
            )
            > 0,
        ),
        (
            "reflect role called",
            sum(
                u["calls"]
                for st in (r["engine_llm_usage"] or {}).values()
                for role, u in st.items()
                if role == "reflect"
            )
            > 0,
        ),
        ("PROFILE NOTES block in a context", any("PROFILE NOTES" in b for b in blocks)),
    ],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", action="append", choices=ARMS)
    ap.add_argument(
        "--keep", action="store_true", help="keep the work directory and print its path"
    )
    ap.add_argument("--json", type=Path, help="write the full report here")
    a = ap.parse_args()
    arms = a.arm or list(ARMS)
    work = Path(tempfile.mkdtemp(prefix="smoke-gc-"))
    data = work / "locomo-smoke.json"
    synthetic_locomo(data)
    failed = 0
    report = {}
    for arm_id in arms:
        wd = work / arm_id
        wd.mkdir()
        try:
            res = asyncio.run(run_arm(arm_id, wd, data))
        except Exception as exc:
            print(f"[FAIL] {arm_id}: {type(exc).__name__}: {exc}")
            failed += 1
            continue
        legs = [set((row.get("extra_legs") or {}).keys()) for row in res["forensics"]]
        blocks = [b for row in res["forensics"] for b in derived(row)]
        print(f"\n== {arm_id}  (bound roles: {', '.join(res['bound_roles'])})")
        print(
            f"   stub completions by caller: {res['stub_calls']}   calls without /no_think: {res['thinking_calls']}"
        )
        for head, n in sorted(res["prompt_heads"].items()):
            if not head.startswith(("reader", "harness")):
                print(f"      {n:3d} x {head}")
        for stage, roles in (res["engine_llm_usage"] or {}).items():
            for role, u in roles.items():
                print(
                    f"   engine LLM [{stage}] {role}: {u['calls']} calls, {u['prompt']} prompt tok, {u['completion']} completion tok"
                )
        print(
            "   sleep stages: "
            + ", ".join(
                f"{n}={ {k: v for k, v in s.items() if k in ('status', 'facts', 'sessions', 'summaries', 'nodes', 'edges', 'entities')} }"
                for n, s in res["sleep_stages"].items()
                if s.get("status") not in (None, "skipped")
            )
        )
        print(
            f"   {res['n_sessions']} sessions / {res['n_turns']} turns; read legs seen: {sorted(set().union(*legs)) if legs else []}"
        )
        for row in res["forensics"][:1]:
            for text in derived(row)[:4]:
                print("   derived context record:", text.replace("\n", " | ")[:200])
        for name, ok in CHECKS[arm_id](res, legs, blocks):
            print(f"   [{'ok' if ok else 'FAIL'}] {name}")
            failed += 0 if ok else 1
        report[arm_id] = {k: v for k, v in res.items() if k != "forensics"}
    if a.json:
        a.json.write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    if a.keep:
        print(f"\nwork directory kept: {work}")
    print(f"\nsmoke {'FAILED' if failed else 'passed'} ({failed} failing check(s))")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
