"""SpineTune: a guarded self-tuner for memspine's read configuration (G-23).

SimpleMem's EvolveMem tunes its retrieval settings in a loop: evaluate the benchmark
questions, let an LLM read the worst failures *with their gold answers*, change the
settings, and keep the change if F1 rose on the *same* questions. That is test-set
tuning: the reported score is optimistic by construction (research repo,
``sota/S8_...`` G-23 and the explanation in the 2026-10-09 session notes).

SpineTune keeps the useful part (automatic search over settings) and removes the leaks:

* **No answers, no LLM proposer.** Candidates come only from a declared search space of
  existing memspine config keys. The objective is retrieval coverage (``ev_all``: every
  gold evidence turn reached the context), computed without a reader or a judge.
* **Split by conversation.** Search runs on a *dev* split; the final configuration is
  confirmed on a *held-out* split it never saw, and optionally on a second dataset
  (*guard*), where it must not be significantly worse.
* **Statistics, not "it went up".** A change is accepted only when a paired exact sign
  test on dev is significant at ``alpha / max_trials`` (Bonferroni over every try) and
  the coverage gain is at least ``min_delta``.
* **Search algorithms.** ``coordinate`` (one key at a time, repeated passes),
  ``random`` (sampled configurations against the incumbent) and ``halving``
  (successive halving: many candidates on a small dev subset, the best fraction on
  larger subsets, the survivor tested on all of dev).
* **Controls.** ``max_trials``, ``max_seconds``, ``seed``, a disk cache of every
  evaluation (re-runs and resumes are free), ``--dry-run`` (prints the plan, runs
  nothing), and a report that labels the result "auto-tuned" next to the baseline.

The evaluator is pluggable: :class:`RunnerEvaluator` runs the harness's own
retrieval-only protocol (``c0-1 --retrieval-only --max-model-calls 0``: $0, local
embedder) on a set of conversations; tests pass a synthetic evaluator.

    python -m memspine_evals.spinetune --space arms/spinetune_space.json \\
        --base arms/local-temporal-leg.json --dataset locomo --path data/locomo10.json \\
        --algo coordinate --max-trials 12 --dev-fraction 0.5 --dry-run
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import math
import os
import random
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

__all__ = [
    "Knob",
    "RunnerEvaluator",
    "SearchSpace",
    "TuneSettings",
    "Tuner",
    "apply_knobs",
    "sign_test",
    "split_items",
]

#: question key -> did every gold evidence turn reach the context (None: no gold).
Outcomes = dict[str, bool]
#: (config, item ids) -> outcomes for those items' questions.
Evaluate = Callable[[dict[str, Any], Sequence[str]], Outcomes]


# ── search space ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Knob:
    """One tunable config key (dotted path, e.g. ``read.temporal_rank``) and its values.

    The first value is the one tried last-resort as the incumbent's value is kept
    unchanged; values are tried in order."""

    path: str
    values: tuple[Any, ...]


@dataclass(frozen=True)
class SearchSpace:
    knobs: tuple[Knob, ...]

    @classmethod
    def load(cls, path: str | Path) -> SearchSpace:
        """``{"knobs": [{"path": "read.temporal_rank", "values": ["midpoint", "overlap"]}]}``."""
        raw = json.loads(Path(path).read_text(encoding="utf8"))
        knobs = tuple(Knob(k["path"], tuple(k["values"])) for k in raw["knobs"])
        if not knobs:
            raise ValueError("search space has no knobs")
        return cls(knobs)

    def size(self) -> int:
        return math.prod(len(k.values) for k in self.knobs)


def apply_knobs(base: Mapping[str, Any], settings: Mapping[str, Any]) -> dict[str, Any]:
    """``base`` with each dotted-path setting written in (nested dicts created)."""
    out = copy.deepcopy(dict(base))
    for path, value in settings.items():
        node = out
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return out


def config_hash(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]


# ── data split and statistics ────────────────────────────────────────────────


def split_items(
    items: Sequence[str], dev_fraction: float, seed: int
) -> tuple[list[str], list[str]]:
    """Deterministic dev / held-out split of item (conversation) ids."""
    if not 0.0 < dev_fraction < 1.0:
        raise ValueError("dev_fraction must be in (0, 1)")
    shuffled = sorted(items)
    random.Random(seed).shuffle(shuffled)
    cut = max(1, min(len(shuffled) - 1, round(len(shuffled) * dev_fraction)))
    return sorted(shuffled[:cut]), sorted(shuffled[cut:])


def sign_test(base: Outcomes, arm: Outcomes) -> dict[str, Any]:
    """Paired exact sign test (McNemar's exact form) on questions present in both.

    ``won``: arm covers, base does not; ``lost``: the reverse. Two-sided p with p=1/2.
    ``delta``: coverage difference in points over the shared questions."""
    keys = sorted(set(base) & set(arm))
    won = sum(1 for k in keys if arm[k] and not base[k])
    lost = sum(1 for k in keys if base[k] and not arm[k])
    n = won + lost
    if n == 0:
        p = 1.0
    else:
        tail = sum(math.comb(n, i) for i in range(0, min(won, lost) + 1))
        p = float(min(Fraction(1), Fraction(2 * tail, 2**n)))
    cov_base = 100.0 * sum(base[k] for k in keys) / len(keys) if keys else 0.0
    cov_arm = 100.0 * sum(arm[k] for k in keys) / len(keys) if keys else 0.0
    return {
        "n": len(keys),
        "won": won,
        "lost": lost,
        "p": p,
        "base": round(cov_base, 2),
        "arm": round(cov_arm, 2),
        "delta": round(cov_arm - cov_base, 2),
    }


# ── tuner ────────────────────────────────────────────────────────────────────


@dataclass
class TuneSettings:
    algo: str = "coordinate"  # coordinate | random | halving
    max_trials: int = 20
    max_seconds: float = 6 * 3600.0
    alpha: float = 0.05
    min_delta: float = 0.3  # coverage points
    passes: int = 2  # coordinate: full passes over the knobs
    halving_eta: int = 3  # halving: keep 1/eta per rung
    halving_rungs: int = 3
    seed: int = 11


@dataclass
class Trial:
    step: int
    settings: dict[str, Any]
    split: str
    test: dict[str, Any]
    accepted: bool
    note: str = ""


@dataclass
class TuneResult:
    algo: str
    dev_items: list[str]
    heldout_items: list[str]
    baseline_settings: dict[str, Any]
    best_settings: dict[str, Any]
    trials: list[Trial] = field(default_factory=list)
    heldout: dict[str, Any] | None = None
    guard: dict[str, Any] | None = None
    verdict: str = "no change"
    seconds: float = 0.0


class Tuner:
    """Search the space on dev, confirm on held-out (and the guard), with Bonferroni."""

    def __init__(
        self,
        space: SearchSpace,
        base_config: Mapping[str, Any],
        evaluate: Evaluate,
        items: Sequence[str],
        settings: TuneSettings,
        *,
        dev_fraction: float = 0.5,
        guard: tuple[Evaluate, Sequence[str]] | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.space = space
        self.base = dict(base_config)
        self.evaluate = evaluate
        self.s = settings
        self.dev, self.heldout = split_items(items, dev_fraction, settings.seed)
        self.guard = guard
        self.log = log
        self._trials = 0
        self._start = time.monotonic()
        #: per-try significance level: Bonferroni over every try the budget allows.
        self.alpha_try = settings.alpha / max(1, settings.max_trials)

    # -- helpers --
    def _budget_left(self) -> bool:
        return (
            self._trials < self.s.max_trials and time.monotonic() - self._start < self.s.max_seconds
        )

    def _run(self, settings: Mapping[str, Any], items: Sequence[str]) -> Outcomes:
        return self.evaluate(apply_knobs(self.base, settings), items)

    def _better(self, test: Mapping[str, Any]) -> bool:
        return (
            test["won"] > test["lost"]
            and test["p"] < self.alpha_try
            and test["delta"] >= self.s.min_delta
        )

    def _baseline_values(self) -> dict[str, Any]:
        """The base config's current value of every knob (its incumbent)."""
        out: dict[str, Any] = {}
        for knob in self.space.knobs:
            node: Any = self.base
            for part in knob.path.split("."):
                node = node.get(part) if isinstance(node, Mapping) else None
            out[knob.path] = node
        return out

    # -- algorithms --
    def _coordinate(self, result: TuneResult) -> dict[str, Any]:
        incumbent: dict[str, Any] = {}
        inc_out = self._run(incumbent, self.dev)
        for _ in range(self.s.passes):
            changed = False
            for knob in self.space.knobs:
                for value in knob.values:
                    if not self._budget_left():
                        return incumbent
                    current = {**self._baseline_values(), **incumbent}.get(knob.path)
                    if value == current:
                        continue
                    trial = {**incumbent, knob.path: value}
                    out = self._run(trial, self.dev)
                    self._trials += 1
                    test = sign_test(inc_out, out)
                    ok = self._better(test)
                    result.trials.append(Trial(self._trials, trial, "dev", test, ok))
                    self.log(f"[spinetune] {knob.path}={value!r}: {test} {'ACCEPT' if ok else ''}")
                    if ok:
                        incumbent, inc_out, changed = trial, out, True
            if not changed:
                break
        return incumbent

    def _sample(self, rng: random.Random) -> dict[str, Any]:
        return {k.path: rng.choice(k.values) for k in self.space.knobs}

    def _random(self, result: TuneResult) -> dict[str, Any]:
        rng = random.Random(self.s.seed)
        incumbent: dict[str, Any] = {}
        inc_out = self._run(incumbent, self.dev)
        seen: set[str] = set()
        while self._budget_left() and len(seen) < self.space.size():
            trial = self._sample(rng)
            key = json.dumps(trial, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            out = self._run(trial, self.dev)
            self._trials += 1
            test = sign_test(inc_out, out)
            ok = self._better(test)
            result.trials.append(Trial(self._trials, trial, "dev", test, ok))
            self.log(f"[spinetune] random {trial}: {test} {'ACCEPT' if ok else ''}")
            if ok:
                incumbent, inc_out = trial, out
        return incumbent

    def _halving(self, result: TuneResult) -> dict[str, Any]:
        """Successive halving: rung r evaluates the survivors on the first
        ``len(dev) * eta**(r - rungs + 1)`` dev items; the best 1/eta go on."""
        rng = random.Random(self.s.seed)
        pool = min(self.space.size(), max(self.s.halving_eta, self.s.max_trials // 2))
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        all_combos = (
            [
                dict(zip([k.path for k in self.space.knobs], combo, strict=True))
                for combo in itertools.product(*[k.values for k in self.space.knobs])
            ]
            if self.space.size() <= pool
            else []
        )
        candidates = all_combos[:pool] if all_combos else []
        while len(candidates) < pool:
            c = self._sample(rng)
            key = json.dumps(c, sort_keys=True)
            if key not in seen:
                seen.add(key)
                candidates.append(c)
        dev = list(self.dev)
        for rung in range(self.s.halving_rungs):
            size = max(1, round(len(dev) * self.s.halving_eta ** (rung - self.s.halving_rungs + 1)))
            items = dev[:size]
            base_out = self._run({}, items)
            scored = []
            for c in candidates:
                if not self._budget_left():
                    break
                out = self._run(c, items)
                self._trials += 1
                test = sign_test(base_out, out)
                result.trials.append(Trial(self._trials, c, f"dev[{size}]", test, False, "rung"))
                scored.append((test["delta"], test["won"] - test["lost"], c))
            if not scored:
                break
            scored.sort(key=lambda t: (-t[0], -t[1], json.dumps(t[2], sort_keys=True)))
            keep = max(1, len(scored) // self.s.halving_eta)
            candidates = [c for _, _, c in scored[:keep]]
            if len(candidates) == 1:
                break
        winner = candidates[0]
        out = self._run(winner, self.dev)
        test = sign_test(self._run({}, self.dev), out)
        ok = self._better(test)
        result.trials.append(Trial(self._trials, winner, "dev", test, ok, "survivor"))
        return winner if ok else {}

    # -- entry point --
    def run(self) -> TuneResult:
        result = TuneResult(
            algo=self.s.algo,
            dev_items=list(self.dev),
            heldout_items=list(self.heldout),
            baseline_settings=self._baseline_values(),
            best_settings={},
        )
        algo = {"coordinate": self._coordinate, "random": self._random, "halving": self._halving}
        if self.s.algo not in algo:
            raise ValueError(f"unknown algo {self.s.algo!r}")
        best = algo[self.s.algo](result)
        result.best_settings = best
        if best:
            # Confirmation on data the search never saw (one test, full alpha).
            held = sign_test(self._run({}, self.heldout), self._run(best, self.heldout))
            result.heldout = held
            confirmed = held["won"] > held["lost"] and held["p"] < self.s.alpha
            if self.guard is not None:
                g_eval, g_items = self.guard
                g = sign_test(
                    g_eval(self.base, g_items), g_eval(apply_knobs(self.base, best), g_items)
                )
                result.guard = g
                # Non-inferiority: the guard must not lose significantly.
                if g["lost"] > g["won"] and g["p"] < self.s.alpha:
                    confirmed = False
            result.verdict = "confirmed (auto-tuned)" if confirmed else "not confirmed on held-out"
        result.seconds = round(time.monotonic() - self._start, 1)
        return result


# ── the real evaluator: the harness's retrieval-only protocol ────────────────


class RunnerEvaluator:
    """Runs ``c0-1 --retrieval-only --max-model-calls 0`` for a config on some items and
    returns per-question ``ev_all``. Every evaluation is cached on disk by
    (config, items, dataset), so a re-run or a resumed tune costs nothing."""

    def __init__(
        self,
        dataset: str,
        path: str,
        cache_dir: str | Path,
        *,
        budget: int = 4096,
        top_k: int = 10,
        read_mode: str = "replay",
        extra: Sequence[str] = (),
        pythonpath: str | None = None,
    ) -> None:
        self.dataset, self.path = dataset, path
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.budget, self.top_k, self.read_mode = budget, top_k, read_mode
        self.extra = list(extra)
        self.pythonpath = pythonpath

    def __call__(self, config: dict[str, Any], items: Sequence[str]) -> Outcomes:
        key = config_hash({"c": config, "i": sorted(items), "d": [self.dataset, self.path]})
        hit = self.cache / f"{key}.json"
        if hit.exists():
            return {k: bool(v) for k, v in json.loads(hit.read_text(encoding="utf8")).items()}
        run_id = f"spinetune-{key}"
        cmd = [
            sys.executable,
            "-m",
            "memspine_evals",
            "c0-1",
            "--dataset",
            self.dataset,
            "--path",
            self.path,
            "--with-memspine",
            "--only-systems",
            "memspine",
            "--memspine-read-mode",
            self.read_mode,
            "--memspine-config",
            json.dumps(config),
            "--retrieval-only",
            "--max-model-calls",
            "0",
            "--budget",
            str(self.budget),
            "--top-k",
            str(self.top_k),
            "--item-ids",
            ",".join(sorted(items)),
            "--run-id",
            run_id,
            *self.extra,
        ]
        if self.dataset == "locomo":
            cmd += ["--categories", "all"]
        env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
        env["HF_HUB_OFFLINE"] = "1"
        if self.pythonpath:
            env["PYTHONPATH"] = self.pythonpath
        subprocess.run(
            cmd, check=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
        )
        rows = Path("runs") / f"{run_id}--memspine" / "results.jsonl"
        outcomes: Outcomes = {}
        for line in rows.read_text(encoding="utf8").splitlines():
            row = json.loads(line)
            if row.get("kind") != "result":
                continue
            ev = (row.get("meta") or {}).get("ev_all")
            if ev is not None:
                outcomes[f"{row['item_id']}|{row['query_id']}"] = bool(ev)
        hit.write_text(json.dumps(outcomes), encoding="utf8")
        return outcomes


def _dataset_items(dataset: str, path: str) -> list[str]:
    if dataset == "locomo":
        data = json.loads(Path(path).read_text(encoding="utf8"))
        return [str(conv.get("sample_id")) for conv in data]
    raise ValueError(f"item listing for {dataset!r} not supported; pass --items")


def _report_md(result: TuneResult) -> str:
    lines = [
        f"# SpineTune report ({result.algo})",
        "",
        f"- dev items: {', '.join(result.dev_items)}",
        f"- held-out items: {', '.join(result.heldout_items)}",
        f"- verdict: **{result.verdict}** ({result.seconds}s, {len(result.trials)} trials)",
        f"- baseline: `{json.dumps(result.baseline_settings)}`",
        f"- best: `{json.dumps(result.best_settings)}`",
        "",
        "| # | split | settings | won | lost | p | Δ coverage | accepted |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in result.trials:
        lines.append(
            f"| {t.step} | {t.split} | `{json.dumps(t.settings)}` | {t.test['won']} | "
            f"{t.test['lost']} | {t.test['p']:.4f} | {t.test['delta']:+.2f} | "
            f"{'yes' if t.accepted else ''} |"
        )
    for label, t in (("Held-out", result.heldout), ("Guard", result.guard)):
        if t:
            lines += [
                "",
                f"{label}: {t['base']} → {t['arm']} ({t['delta']:+.2f}), "
                f"won {t['won']} / lost {t['lost']}, p = {t['p']:.4f}",
            ]
    lines += [
        "",
        "Results from this run are labelled **auto-tuned** and are reported next to "
        "the hand-built baseline, never in place of it.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="memspine_evals.spinetune", description=__doc__)
    ap.add_argument("--space", required=True, type=Path)
    ap.add_argument("--base", required=True, type=Path, help="base memspine config (JSON)")
    ap.add_argument("--dataset", default="locomo")
    ap.add_argument("--path", required=True)
    ap.add_argument("--items", default=None, help="comma list of item ids (default: all)")
    ap.add_argument("--algo", default="coordinate", choices=("coordinate", "random", "halving"))
    ap.add_argument("--max-trials", type=int, default=20)
    ap.add_argument("--max-hours", type=float, default=6.0)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--min-delta", type=float, default=0.3)
    ap.add_argument("--dev-fraction", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--guard-dataset", default=None)
    ap.add_argument("--guard-path", default=None)
    ap.add_argument("--guard-items", default=None)
    ap.add_argument("--cache-dir", type=Path, default=Path("runs/_spinetune_cache"))
    ap.add_argument("--out-dir", type=Path, default=Path("runs/spinetune"))
    ap.add_argument("--pythonpath", default=None, help="frozen engine src (as the screens use)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    space = SearchSpace.load(args.space)
    base = json.loads(args.base.read_text(encoding="utf8"))
    items = args.items.split(",") if args.items else _dataset_items(args.dataset, args.path)
    settings = TuneSettings(
        algo=args.algo,
        max_trials=args.max_trials,
        max_seconds=args.max_hours * 3600,
        alpha=args.alpha,
        min_delta=args.min_delta,
        seed=args.seed,
    )
    dev, held = split_items(items, args.dev_fraction, args.seed)
    print(
        f"[spinetune] {args.algo}: {len(space.knobs)} knobs, space size {space.size()}, "
        f"dev {dev}, held-out {held}, max {args.max_trials} trials, "
        f"per-try alpha {args.alpha / max(1, args.max_trials):.4g}"
    )
    if args.dry_run:
        for k in space.knobs:
            print(f"  {k.path}: {list(k.values)}")
        return 0
    evaluator = RunnerEvaluator(args.dataset, args.path, args.cache_dir, pythonpath=args.pythonpath)
    guard = None
    if args.guard_dataset:
        g_eval = RunnerEvaluator(
            args.guard_dataset, args.guard_path, args.cache_dir, pythonpath=args.pythonpath
        )
        guard = (
            g_eval,
            args.guard_items.split(",")
            if args.guard_items
            else _dataset_items(args.guard_dataset, args.guard_path),
        )
    result = Tuner(
        space, base, evaluator, items, settings, dev_fraction=args.dev_fraction, guard=guard
    ).run()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "spinetune.json").write_text(
        json.dumps(asdict(result), indent=2, default=str), encoding="utf8"
    )
    (args.out_dir / "SPINETUNE.md").write_text(_report_md(result), encoding="utf8")
    print(f"[spinetune] verdict: {result.verdict}; best {result.best_settings}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
