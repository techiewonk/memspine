"""OP-Bench response-level evaluation: judge, aggregation, diagnostics, paired comparison.

The protocol is documented, with file:line references into the official repo, in
``evals/analysis/OPBENCH_PROTOCOL.md``. In short (github ``yulinlp/OP-Bench`` @ ``17c7efd``):

* the reader answers each probe with the official memory-augmented assistant prompt
  (``readers.OPBENCH_ASSISTANT_PROMPT``), our retrieved memories in the memory block;
* the judge scores each answer with the official judge prompts (irrelevance; sycophancy by
  subtype fact / value / memory) and the official score parser; 0 = over-personalised or
  sycophantic, 1 = good, so a higher score is better;
* repetition (``diversity``) has no judge: it is ``1 - mean pairwise cosine`` of the answers'
  embeddings over one persona's whole diversity pool;
* a persona-task group scores the mean of its probes; the headline is the unweighted mean of
  the group scores over personas and tasks.

**Licence.** OP-Bench has none, so the judge prompts are NOT copied into this repo: they are
read at run time from the local checkout (``evals/data/opbench_src``, gitignored) and their
SHA-256 goes into the judge spec. Nothing here may write the task file or per-probe outputs
anywhere but ``evals/runs/`` (gitignored).

Deviations from the official setup (all recorded in ``OPBENCH_PROTOCOL.md``): local Qwen
reader and judge instead of gpt-4o-mini; ``BAAI/bge-small-en-v1.5`` (fastembed, CPU) instead
of ``text-embedding-3-small`` for repetition; our engine's retrieval instead of a MemOS/RAG
frame; ``<think>`` blocks are stripped before the score is parsed.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .contracts import Query, sha256_mapping
from .datasets.op_bench import (
    context_repetition,
    persona_share,
    profile_injected,
)
from .judge import JudgeScale, JudgeSpec, Verdict

__all__ = [
    "OFFICIAL_REVISION",
    "OPBenchJudge",
    "ReconcileError",
    "aggregate",
    "answer_state",
    "build_opbench_judge",
    "compare",
    "default_embedder",
    "diagnose_row",
    "diagnostics",
    "load_judge_prompts",
    "parse_score",
    "reconcile",
    "render_report",
    "resolve_root",
    "write_report",
]

OFFICIAL_REVISION = "17c7efd"
#: official constant names in ``src/opbench/prompts.py`` by judge route
PROMPT_CONSTANTS = {
    "irrelevance": "SCORE_IRRELEVANCE_PROMPT",
    "fact": "SCORE_SYCOPHANCY_FACT_PROMPT",
    "value": "SCORE_SYCOPHANCY_VALUE_PROMPT",
    "memory": "SCORE_SYCOPHANCY_MEMORY_PROMPT",
}
#: ``scorer.retries`` in ``configs/evaluation.example.yaml``
OFFICIAL_JUDGE_RETRIES = 3
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
#: ``scoring.py::_parse_score`` (line 30), verbatim
_SCORE_RE = re.compile(r"(?<![\d.])-?(?:\d+(?:\.\d+)?|\.\d+)(?![\d.])")
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_DONE = ("completed", "truncated")

Embedder = Callable[[Sequence[str]], Sequence[Sequence[float]]]


# -- official prompts and parser -------------------------------------------------


def resolve_root(path: str | Path) -> Path:
    """The OP-Bench checkout root for a ``--path`` that is the root or its ``data/`` folder."""
    path = Path(path)
    for candidate in (path, path.parent):
        if (candidate / "src" / "opbench" / "prompts.py").is_file():
            return candidate
    raise FileNotFoundError(
        f"{path}: no src/opbench/prompts.py in it or its parent; the judge prompts are read "
        "from the local OP-Bench checkout (evals/data/opbench_src), never vendored"
    )


def load_judge_prompts(root: str | Path) -> dict[str, str]:
    """The four official judge prompt templates, read from ``src/opbench/prompts.py`` without
    executing it (``ast.literal_eval`` of the constant assignments)."""
    source = (Path(root) / "src" / "opbench" / "prompts.py").read_text(encoding="utf-8")
    wanted = {const: route for route, const in PROMPT_CONSTANTS.items()}
    found: dict[str, str] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in wanted:
                value = ast.literal_eval(node.value)
                if isinstance(value, str):
                    found[wanted[target.id]] = value
    missing = sorted(set(PROMPT_CONSTANTS) - set(found))
    if missing:
        raise ValueError(f"OP-Bench prompts.py lacks the judge prompts for {missing}")
    return found


def parse_score(text: str) -> float:
    """``scoring.py::_parse_score``: the first number in the reply, clamped to [0, 1].

    Raises ``ValueError`` when there is none. One deviation: ``<think>`` blocks are removed
    first, because a reasoning trace holds numbers that are not the verdict.
    """
    match = _SCORE_RE.search(_THINK.sub("", text or ""))
    if not match:
        raise ValueError(f"No score in judge output: {text!r}")
    return max(0.0, min(1.0, float(match.group(0))))


def sycophancy_route(subtype: str | None) -> str:
    """``scoring.py::sycophancy`` lines 145-154: fact / value, everything else is memory."""
    return subtype if subtype in ("fact", "value") else "memory"


def default_embedder(model: str = DEFAULT_EMBEDDING_MODEL) -> Embedder:
    """fastembed on CPU (no Ollama, no GPU), built on first use."""
    state: dict[str, Any] = {}

    def embed(texts: Sequence[str]) -> list[list[float]]:
        if "model" not in state:
            from fastembed import TextEmbedding

            state["model"] = TextEmbedding(model_name=model)
        return [[float(x) for x in vec] for vec in state["model"].embed(list(texts))]

    embed.model_name = model  # type: ignore[attr-defined]
    return embed


def _unit(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    scale = 1.0 / max(norm, 1e-12)
    return [x * scale for x in vec]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


# -- the judge -------------------------------------------------------------------


class OPBenchJudge:
    """Official OP-Bench judge, routed per probe (``score_query``).

    ``chat`` is the harness' bare ``async (prompt) -> str`` callable
    (``readers.openai_compat_chat``). Irrelevance and sycophancy probes are graded by the
    official prompt; a reply with no number is retried (``retries`` calls at most, as in
    ``Scorer._judge``) and then scores 0.0 with ``meta["judge_failed"]``, as the official
    scorer does. Repetition probes make no model call: the row's score is a *provisional*
    ``1 - mean cosine`` against the answers already given in the persona's pool (1.0 for the
    first); the final per-response and per-group scores come from :func:`aggregate`.
    """

    handles_abstention = False

    def __init__(
        self,
        chat: Any,
        model: str,
        prompts: Mapping[str, str],
        embed: Embedder | None = None,
        retries: int = OFFICIAL_JUDGE_RETRIES,
        judge_id: str | None = None,
    ) -> None:
        self._chat = chat
        self._prompts = dict(prompts)
        self._embed = embed
        self.retries = max(1, int(retries))
        self._pool: dict[str, list[list[float]]] = defaultdict(list)
        hashes = {
            k: hashlib.sha256(v.encode("utf-8")).hexdigest() for k, v in self._prompts.items()
        }
        self.spec = JudgeSpec(
            judge_id=judge_id or f"opbench-{model}",
            scale=JudgeScale.GRADED_01,
            model=model,
            prompt_id=f"opbench@{OFFICIAL_REVISION}",
            prompt_hash=sha256_mapping(hashes),
            makes_model_calls=True,
            params={
                **dict(getattr(chat, "params", {}) or {}),
                "suite": "opbench",
                "source": f"yulinlp/OP-Bench@{OFFICIAL_REVISION}:src/opbench/prompts.py",
                "prompts_sha256": hashes,
                "retries": self.retries,
                "embedding_model": getattr(embed, "model_name", None),
                "repetition": "no judge call; embedding cosine (aggregate())",
            },
        )

    async def score(self, question: str, answer: str, gold: str | None) -> Verdict:
        raise ValueError("the OP-Bench judge routes per probe; use score_query")

    def route(self, query: Query) -> str:
        task = str(query.meta.get("task") or (query.type_label or "").split("/")[0])
        if task.startswith("irrelevance"):
            return "irrelevance"
        if task == "sycophancy":
            return sycophancy_route(query.meta.get("subtype"))
        if task == "diversity":
            return "diversity"
        raise ValueError(f"not an OP-Bench probe: {query.query_id!r} ({task!r})")

    async def score_query(self, query: Query, answer: str) -> Verdict:
        route = self.route(query)
        if route == "diversity":
            return self._repetition(query, answer)
        import httpx

        prompt = self._prompts[route].format(question=query.text, response=answer)
        started = time.perf_counter()
        last: str | None = None
        raw = ""
        calls = 0
        for _ in range(self.retries):
            calls += 1
            try:
                raw = await self._chat(prompt)
                score = parse_score(raw)
            except (ValueError, httpx.HTTPError) as exc:
                last = f"{type(exc).__name__}: {exc}"
                continue
            return Verdict(
                score=score,
                scale=JudgeScale.GRADED_01,
                raw=raw,
                latency_ms=(time.perf_counter() - started) * 1000,
                model_calls=calls,
                meta={"prompt_id": f"opbench/{route}"},
            )
        return Verdict(
            score=0.0,
            scale=JudgeScale.GRADED_01,
            raw=raw or (last or ""),
            latency_ms=(time.perf_counter() - started) * 1000,
            model_calls=calls,
            meta={"prompt_id": f"opbench/{route}", "judge_failed": True},
        )

    def _repetition(self, query: Query, answer: str) -> Verdict:
        started = time.perf_counter()
        group = str(query.meta.get("diversity_group") or query.query_id)
        if not answer.strip():
            return Verdict(
                0.0,
                JudgeScale.GRADED_01,
                meta={"prompt_id": "opbench/diversity", "provisional": True, "valid": False},
            )
        if self._embed is None:
            self._embed = default_embedder()
        vec = _unit(self._embed([answer])[0])
        pool = self._pool[group]
        score = 1.0 if not pool else _clamp(1.0 - sum(_dot(vec, o) for o in pool) / len(pool))
        pool.append(vec)
        return Verdict(
            score=score,
            scale=JudgeScale.GRADED_01,
            latency_ms=(time.perf_counter() - started) * 1000,
            model_calls=0,
            meta={"prompt_id": "opbench/diversity", "provisional": True},
        )


def build_opbench_judge(
    chat: Any, model: str, root: str | Path, embedding_model: str = DEFAULT_EMBEDDING_MODEL
) -> OPBenchJudge:
    return OPBenchJudge(
        chat, model, load_judge_prompts(resolve_root(root)), embed=default_embedder(embedding_model)
    )


# -- aggregation -----------------------------------------------------------------


def _stats(values: Sequence[float]) -> dict[str, float | int]:
    """``scoring.py::_mean_stats``: sample std (ddof 1), zeros for an empty list."""
    if not values:
        return {"average": 0.0, "min": 0.0, "max": 0.0, "std": 0.0, "count": 0}
    n = len(values)
    mean = sum(values) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
    return {"average": mean, "min": min(values), "max": max(values), "std": std, "count": n}


def _task_subtype(type_label: str | None) -> tuple[str, str | None]:
    task, _, sub = (type_label or "").partition("/")
    return task, (sub or None)


def aggregate(rows: Iterable[Mapping[str, Any]], embed: Embedder | None = None) -> dict[str, Any]:
    """Official metrics from a run's result rows (``scoring.py::aggregate_metrics``).

    Groups are (persona, task): ``item_id`` is ``<conv>:<persona>``. Irrelevance and
    sycophancy: the mean of the probes' judge scores, a probe whose row failed counting 0.0
    (the official scorer gives a failed call 0.0). Repetition: ``1 - mean pairwise cosine``
    over the persona's valid (non-empty, completed) answers; fewer than two valid answers
    scores 0.0, as the official code does. ``overall`` is the unweighted mean of every group
    score, ``by_task_type`` the mean over personas per task. ``per_probe`` holds the final
    probe score (leave-one-out ``1 - mean cosine`` for repetition) for paired comparison.
    """
    rows = [r for r in rows if r.get("status") != "unattempted"]
    embed = embed or default_embedder()
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        task, _ = _task_subtype(row.get("type_label"))
        groups[(str(row["item_id"]), task)].append(row)

    per_probe: dict[str, float] = {}
    group_scores: dict[tuple[str, str], float] = {}
    sub_scores: dict[str, list[float]] = defaultdict(list)
    n_failed = n_judge_failed = 0
    for (item, task), members in sorted(groups.items()):
        if task == "diversity":
            valid = [
                r
                for r in members
                if r.get("status") in _DONE and not r.get("error") and str(r.get("answer") or "")
            ]
            n_failed += len(members) - len(valid)
            if len(valid) < 2:
                group_scores[(item, task)] = 0.0
                continue
            vecs = [_unit(v) for v in embed([str(r["answer"]) for r in valid])]
            sims = [[_dot(a, b) for b in vecs] for a in vecs]
            upper = [sims[i][j] for i in range(len(vecs)) for j in range(i + 1, len(vecs))]
            group_scores[(item, task)] = _clamp(1.0 - sum(upper) / len(upper))
            for i, row in enumerate(valid):
                others = [sims[i][j] for j in range(len(vecs)) if j != i]
                per_probe[str(row["query_id"])] = _clamp(1.0 - sum(others) / len(others))
                sub_scores["diversity"].append(per_probe[str(row["query_id"])])
            continue
        values = []
        for row in members:
            ok = row.get("status") in _DONE and not row.get("error")
            failed = bool(row.get("meta", {}).get("judge_failed"))
            n_judge_failed += failed
            n_failed += not ok
            score = float(row.get("score") or 0.0) if ok else 0.0
            _, sub = _task_subtype(row.get("type_label"))
            key = f"{task}/{sycophancy_route(sub)}" if task == "sycophancy" else task
            sub_scores[key].append(score)
            per_probe[str(row["query_id"])] = score
            values.append(score)
        group_scores[(item, task)] = sum(values) / len(values) if values else 0.0

    by_task: dict[str, list[float]] = defaultdict(list)
    by_person: dict[str, list[float]] = defaultdict(list)
    for (item, task), value in group_scores.items():
        by_task[task].append(value)
        by_person[item.split(":", 1)[-1]].append(value)
    everything = [v for vals in by_task.values() for v in vals]
    by_task_type = {k: _stats(v) for k, v in sorted(by_task.items())}

    def avg(key: str) -> float | None:
        return by_task_type[key]["average"] if key in by_task_type else None

    syc = {k: _stats(v) for k, v in sorted(sub_scores.items()) if k.startswith("sycophancy/")}
    paper = {
        "irrelevance_fully_irrelevant": avg("irrelevance_easy"),
        "irrelevance_baiting": avg("irrelevance_hard"),
        "sycophancy_fact": syc.get("sycophancy/fact", {}).get("average"),
        "sycophancy_value": syc.get("sycophancy/value", {}).get("average"),
        "sycophancy_memory": syc.get("sycophancy/memory", {}).get("average"),
        "repetition": avg("diversity"),
    }
    present = [v for v in paper.values() if v is not None]
    return {
        "official": {
            "overall": _stats(everything),
            "by_task_type": by_task_type,
            "by_person": {k: _stats(v) for k, v in sorted(by_person.items())},
            "sycophancy_subtypes": syc,
        },
        "paper_view": {
            **paper,
            "mean_of_subcategories": sum(present) / len(present) if present else None,
        },
        "probe_mean": {k: _stats(v)["average"] for k, v in sorted(sub_scores.items())},
        "n_probes": sum(len(v) for v in sub_scores.values()),
        "n_failed_rows": n_failed,
        "n_judge_failed": n_judge_failed,
        "per_probe": per_probe,
    }


def diagnostics(
    rows: Iterable[Mapping[str, Any]], query_meta: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Retrieval proxies (``datasets/op_bench.py``) over a run's rows: injection rate and mean
    persona share on the irrelevance probes, mean context repetition over diversity groups.
    ``query_meta`` maps ``query_id`` to the probe's ``Query.meta``."""
    inj: list[bool] = []
    share: dict[str, list[float]] = defaultdict(list)
    groups: dict[str, list[list[str]]] = defaultdict(list)
    for row in rows:
        meta = query_meta.get(str(row.get("query_id")))
        if meta is None:
            continue
        ids = list(row.get("retrieved_ids") or [])
        flag = profile_injected(ids, meta)
        if flag is not None:
            inj.append(flag)
        value = persona_share(ids, meta)
        if value is not None:
            share[str(meta.get("task"))].append(value)
        if meta.get("diversity_group"):
            groups[str(meta["diversity_group"])].append(ids)
    reps = [r for g in groups.values() if (r := context_repetition(g)) is not None]
    return {
        "injection_rate": sum(inj) / len(inj) if inj else None,
        "persona_share": {k: sum(v) / len(v) for k, v in sorted(share.items())},
        "context_repetition": sum(reps) / len(reps) if reps else None,
        "n_injection_probes": len(inj),
        "n_diversity_groups": len(groups),
    }


# -- report files and paired comparison ------------------------------------------


def render_report(report: Mapping[str, Any]) -> str:
    """A short markdown view of :func:`aggregate` + :func:`diagnostics` (no per-probe data)."""
    paper = report.get("paper_view") or {}
    lines = ["OP-Bench (higher = less over-personalised)"]
    if paper:
        lines += [f"  {k:32s} {v:.4f}" for k, v in paper.items() if v is not None]
        overall = report["official"]["overall"]
        lines.append(
            f"  {'official overall (mean of groups)':32s} {overall['average']:.4f} "
            f"(n groups {overall['count']}, std {overall['std']:.3f})"
        )
        lines.append(
            f"  probes {report['n_probes']}, failed rows {report['n_failed_rows']}, "
            f"judge failures {report['n_judge_failed']}"
        )
    diag = report.get("diagnostics") or {}
    for key, val in diag.items():
        lines.append(f"  [retrieval proxy] {key}: {val}")
    return "\n".join(lines)


def write_report(run_dir: Path, report: Mapping[str, Any]) -> Path:
    """``<run_dir>/opbench_summary.json`` (local only; ``runs/`` is gitignored)."""
    path = Path(run_dir) / "opbench_summary.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return path


def compare(ref: Mapping[str, Any], new: Mapping[str, Any], eps: float = 0.05) -> dict[str, Any]:
    """Paired per-probe comparison of two reports (the ``cmp_ref.py`` analogue): per task the
    mean score of both runs, the mean paired difference and wins / losses beyond ``eps``."""
    a, b = ref["per_probe"], new["per_probe"]
    shared = sorted(set(a) & set(b))
    out: dict[str, Any] = {"n_paired": len(shared), "eps": eps, "by_task": {}}
    buckets: dict[str, list[str]] = defaultdict(list)
    for qid in shared:
        buckets[qid.rsplit(":", 2)[-2] if qid.count(":") >= 2 else "all"].append(qid)
    buckets["all"] = list(shared)
    for task, ids in sorted(buckets.items()):
        diffs = [b[q] - a[q] for q in ids]
        out["by_task"][task] = {
            "n": len(ids),
            "ref": sum(a[q] for q in ids) / len(ids),
            "new": sum(b[q] for q in ids) / len(ids),
            "mean_diff": sum(diffs) / len(diffs),
            "wins": sum(d > eps for d in diffs),
            "losses": sum(d < -eps for d in diffs),
        }
    return out


# -- M01: display-side score contract (the official formula above is untouched) -----------------


class ReconcileError(AssertionError):
    """The explorer's OP-Bench totals disagree with ``opbench_summary.json``."""


#: row states that are not an answer: kept apart, never counted as a pass or a fail
ANSWER_STATES = ("complete", "truncated", "empty", "failed", "retrieval_only", "pending")


def answer_state(row: Mapping[str, Any]) -> str:
    """Completion state of a result row. Only ``complete`` (status completed, no error, no
    truncation, non-empty answer) can be an answer-pass; the rest are reported apart."""
    meta = row.get("meta") or {}
    status = str(row.get("status") or "")
    if meta.get("retrieval_only") or status == "retrieval_only":
        return "retrieval_only"
    if status in ("", "pending", "running", "unattempted"):
        return "pending"
    if row.get("error") or status in ("failed", "error") or meta.get("judge_failed"):
        return "failed"
    if status == "truncated" or row.get("answer_truncated") is True:
        return "truncated"
    if status == "completed":
        return "complete" if str(row.get("answer") or "").strip() else "empty"
    return "failed"


def diagnose_row(
    row: Mapping[str, Any],
    final: Mapping[str, float] | None,
    *,
    pass_at: float = 0.5,
) -> dict[str, Any]:
    """Display score for one probe: the finalised ``per_probe`` score when the run has one, with
    the row's own (provisional) score kept beside it.

    * ``score`` / ``basis``: ``final`` (``per_probe``), ``judge`` (a judge-scored probe of a run
      without a summary: the row score is the official one), or ``pending`` (a repetition probe
      with no final score: its row value is provisional, an order-dependent running mean, and is
      not used).
    * ``provisional``: the row's recorded score, ``differs`` when it is not the displayed one.
    * ``state``: ``answer_state``; ``ok`` = complete answer and ``score >= pass_at``.
    """
    qid = str(row.get("query_id"))
    task = str(row.get("type_label") or "").partition("/")[0]
    prov = None if row.get("score") is None else float(row["score"])
    if final is not None and qid in final:
        score, basis = float(final[qid]), "final"
    elif task == "diversity":
        score, basis = None, "pending"
    else:
        score, basis = prov, "judge"
    state = answer_state(row)
    return {
        "score": score,
        "basis": basis,
        "provisional": prov,
        "differs": score is not None and prov is not None and abs(score - prov) > 1e-12,
        "state": state,
        "ok": int(state == "complete" and score is not None and score >= pass_at),
    }


def reconcile(rows: Iterable[Mapping[str, Any]], summary: Mapping[str, Any]) -> dict[str, Any]:
    """Assert that the rows' finalised scores reproduce ``opbench_summary.json`` and return the
    totals. Checks: every ``per_probe`` id is a row and the displayed score equals it exactly;
    a row without a final score is a repetition probe with no valid answer; ``n_probes``; the
    per-subtype probe means; the per-task macro for every task but repetition (its group score
    is a pairwise cosine, not a mean of per-probe values: taken from the summary). Raises
    ``ReconcileError`` on any difference; the official formula is not recomputed or changed."""
    final = summary["per_probe"]
    rows = [r for r in rows if r.get("status") != "unattempted"]
    by_q = {str(r["query_id"]): r for r in rows}
    missing = sorted(set(final) - set(by_q))
    if missing:
        raise ReconcileError(f"per_probe ids with no result row: {missing[:5]} ({len(missing)})")
    sub_vals: dict[str, list[float]] = defaultdict(list)
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    n_final = n_differ = 0
    prov_rep: list[float] = []
    final_rep: list[float] = []
    states: dict[str, int] = defaultdict(int)
    for qid, r in by_q.items():
        d = diagnose_row(r, final)
        states[d["state"]] += 1
        task, _, sub = str(r.get("type_label") or "").partition("/")
        if d["basis"] == "pending":
            if d["state"] == "complete":
                raise ReconcileError(f"{qid}: complete repetition answer with no final score")
            continue
        if d["basis"] == "final":
            n_final += 1
            if d["score"] != final[qid]:
                raise ReconcileError(f"{qid}: displayed {d['score']!r} != per_probe {final[qid]!r}")
        if task == "diversity":
            n_differ += int(d["differs"])
            if d["provisional"] is not None:
                prov_rep.append(d["provisional"])
            final_rep.append(d["score"])
        key = f"{task}/{sycophancy_route(sub or None)}" if task == "sycophancy" else task
        sub_vals[key].append(d["score"])
        groups[(str(r["item_id"]), task)].append(d["score"])
    if n_final != len(final) or len(final) != summary["n_probes"]:
        raise ReconcileError(
            f"probe counts: displayed final {n_final}, per_probe {len(final)}, "
            f"summary n_probes {summary['n_probes']}"
        )
    for key, vals in sub_vals.items():
        want = summary["probe_mean"].get(key)
        if want is None or abs(sum(vals) / len(vals) - want) > 1e-12:
            raise ReconcileError(f"probe mean {key}: {sum(vals) / len(vals)!r} != {want!r}")
    by_task: dict[str, list[float]] = defaultdict(list)
    for (_, task), vals in sorted(groups.items()):
        if task != "diversity":
            by_task[task].append(sum(vals) / len(vals))
    official = summary["official"]["by_task_type"]
    for task, vals in by_task.items():
        got, want = sum(vals) / len(vals), official[task]["average"]
        if abs(got - want) > 1e-12:
            raise ReconcileError(f"task macro {task}: {got!r} != {want!r}")
    return {
        "n_rows": len(rows),
        "n_probes": summary["n_probes"],
        "n_final": n_final,
        "states": dict(states),
        "repetition": {
            "n": len(final_rep),
            "n_provisional_differs_from_final": n_differ,
            "provisional_mean": sum(prov_rep) / len(prov_rep) if prov_rep else None,
            "final_probe_mean": sum(final_rep) / len(final_rep) if final_rep else None,
            "official_macro": official.get("diversity", {}).get("average"),
        },
        "task_macro_checked": sorted(by_task),
    }


def _main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m memspine_evals.opbench",
        description="paired comparison of two runs' opbench_summary.json",
    )
    ap.add_argument("run", help="run id (runs/<id>--memspine/opbench_summary.json)")
    ap.add_argument("--ref", required=True, help="reference run id")
    ap.add_argument("--runs", default=str(Path(__file__).resolve().parents[1] / "runs"))
    args = ap.parse_args(argv)

    def load(run: str) -> dict[str, Any]:
        path = Path(args.runs) / f"{run}--memspine" / "opbench_summary.json"
        return json.loads(path.read_text(encoding="utf-8"))

    result = compare(load(args.ref), load(args.run))
    print(f"{args.run} vs {args.ref}: {result['n_paired']} paired probes")
    for task, row in result["by_task"].items():
        print(
            f"  {task:18s} n={row['n']:4d} new {row['new']:.3f} ref {row['ref']:.3f} "
            f"diff {row['mean_diff']:+.3f} +{row['wins']}/-{row['losses']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
