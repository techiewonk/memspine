"""TI-5: the Trace Inspector step mode, a terminal REPL over an in-process engine.

    python -m memspine_evals.inspect step --data <locomo json | demo> --item <id> --arm <name | path.json>
        [--replay-cache --cache-dir DIR [--live]] [--offline] [--out DIR] [--script FILE]
        [--reader-model M [--judge-model M] [--base-url URL]] [--pause] [--wide]

It builds the arm's engine through the harness ``MemspineSystem`` (same config, models and write
path as an eval run) with ``observability.inspect`` and ``inspect_content`` on, and an in-memory tap
sink next to the run's jsonl sink, so each step prints the events it produced, grouped by pipeline
stage. Every step also appends to a run folder (``<out>/<run_id>--memspine|--forensics|--trace``)
that ``assemble.py`` reads; ``save`` builds the inspect bundle from it, which the checks and the HTML
viewer open.

Commands (Enter = ``turn``)::

  write
    turn [n] | turn <turn_id> | turns D1:1-D1:6 | turns D1:3,D2:5   dataset turns (out of order allowed, with a warning)
    skip [n] | goto <turn_id>        move the cursor without writing
    say <Speaker>: <text> [@YYYY-MM-DD[ HH:MM]] [#session]   a custom message (turn id U<n>); `say <<` ... `.` = several
    interaction [n]                  a turn plus the reply (the next speaker's turn[s]), n times
    session [n|id[,id]] | sessions D1-D3   sessions (rest of the current one, the next n, or named), windows flushed and closed
    sleep                            the sleep cycle, stage by stage
  read
    ask <qid|text>                   one read; `ask q1,q3,q7` | `ask all` | `ask cat:<n>` | `asks <file>` add a SUMMARY TABLE
    ask <text> | gold: <answer> | turns: D1:3,D2:5   an ad hoc question with a gold to check against
    brief on|off                     one line per question instead of the full detail (or `--brief` on one ask)
  inspect
    status | show <rid|window_id|occ|turn_id> | links <rid> | diff
    save                             write the inspect bundle (custom turns and questions flagged `custom`)
    reset | load <file.json>         start over with the same arm | load a custom conversation
  tuning
    config [prefix|diff] | set <key>=<value> | unset <key> | params <stage-id|name>
    prompt list|show <id>|edit <id> <file>|reset <id>
    rerun window <window_id> [--commit] | rerun ask <qid|text> | sweep <key> <v1,v2> ask <targets> | export arm <name>
  quit

Models. ``--offline``: hash embedder, no reranker, a stub LLM (no network, no GPU). ``--replay-cache``:
engine-role completions and embeddings are served from the recorded ``call_cache.py`` entries
(``--cache-dir``); a miss is listed and answered by the stub (and never stored) unless ``--live``,
which sends it to the arm's real endpoint. An arm that embeds with a local model (``st``) cannot be
replayed from the cache, so replay and offline use the hash embedder and say so.

This module prints dataset text only to the terminal; bundles with real text belong under the
gitignored ``evals/runs/_analysis/inspect/``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import hashlib
import json
import os
import shutil
import sys
import time
from collections import OrderedDict, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

from ..contracts import Turn
from .assemble import assemble, gold_path, load_dataset
from .step_tune import TuneMixin

__all__ = ["StepSession", "main", "resolve_arm"]

ARMS_DIR = Path(__file__).resolve().parents[2] / "arms"
DEMO_ARM = Path(__file__).with_name("demo_arm.json")
DEFAULT_OUT = Path("runs/_analysis/inspect/step")

#: events of the write path, in the order a turn's stages print
WRITE_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "stamp / role",
        (
            "write.received",
            "write.messages.received",
            "write.intake.",
            "write.turn.",
            "write.stamp.",
            "write.role.",
            "write.echo.",
            "write.time.",
            "write.index_view.",
            "write.firewall.",
            "write.quarantine",
            "write.route",
            "write.deposit",
            "write.rejected",
            "write.asset",
        ),
    ),
    ("record / index (store.index.record)", ("store.index.record",)),
    (
        "window membership / extraction",
        (
            "write.segment.",
            "write.window.",
            "write.prepass.window",
            "write.prompt.",
            "prompt.render",
            "write.extract.call",
            "write.extract.io",
            "write.batch.",
        ),
    ),
    (
        "envelope stages (write.envelope.trace)",
        (
            "write.item.",
            "write.envelope.",
            "write.temporal",
            "write.ground.",
            "write.coverage.",
            "write.resolve.",
            "write.integrate.decision",
            "write.integrate.stage",
            "write.prepass.unseen",
        ),
    ),
    (
        "deposits / ladder",
        (
            "write.extract.deposit",
            "write.integrate.ladder",
            "write.extract.facts",
            "write.extract.edges",
            "write.extract.episode",
            "write.extract.rejected",
            "derive.",
            "trust.gate",
            "write.learned.",
        ),
    ),
    ("links", ("write.edges.", "graph.", "write.integrate.peer")),
)

READ_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "plan",
        (
            "read.request.",
            "read.contract.",
            "read.tier.",
            "read.router.",
            "read.planner.",
            "read.list_trigger",
            "read.decompose",
            "read.cache",
            "read.legs.parallel",
            "scope.",
        ),
    ),
    ("legs", ("read.leg", "read.graph_hop", "read.lift", "read.freshness", "read.external")),
    (
        "fusion / gates",
        ("read.fusion.", "read.date_prior", "read.gate.", "read.deep.", "read.obligations"),
    ),
    (
        "rerank",
        ("read.rerank", "models.rerank", "read.coverage_select"),
    ),
    ("headers / pack", ("compose.", "read.retrieve.writeback", "read.contract.header")),
)

RUN_CAP = 3

#: keys every inspect event carries; not repeated on each printed line
_CORRELATION = frozenset(
    {
        "run_id",
        "item",
        "query_id",
        "request",
        "_seq",
        "_req",
        "event",
        "turn_id",
        "session",
        "phase",
    }
)


# ------------------------------------------------------------------------------------------------ arms
def resolve_arm(arm: str | Path) -> tuple[str, dict[str, Any]]:
    """``scr-b8-write-v2`` (an ``evals/arms`` name) or a path to a json file -> ``(name, config)``."""
    if str(arm) == "demo":
        return "demo", json.loads(DEMO_ARM.read_text(encoding="utf-8"))
    p = Path(str(arm))
    if not p.suffix:
        p = ARMS_DIR / f"{p.name}.json"
    if not p.exists() and (ARMS_DIR / p.name).exists():
        p = ARMS_DIR / p.name
    if not p.exists():
        raise FileNotFoundError(f"arm not found: {arm} (looked in {ARMS_DIR})")
    return p.stem, json.loads(p.read_text(encoding="utf-8"))


def offline_config(
    cfg: Mapping[str, Any], *, keep_models: bool = False
) -> tuple[dict[str, Any], list[str]]:
    """The arm config with the heavy parts swapped for local ones; returns ``(config, notes)``."""
    out = copy.deepcopy(dict(cfg))
    notes: list[str] = []
    emb = out.get("embedding") or {}
    if not keep_models and emb.get("provider") not in (None, "hash"):
        notes.append(f"embedder: hash (arm uses {emb.get('provider')}:{emb.get('model')})")
        out["embedding"] = {"provider": "hash"}
    read = dict(out.get("read") or {})
    if not keep_models and read.get("rerank") not in (None, "off"):
        notes.append(f"reranker: off (arm uses {read.get('rerank')})")
        read["rerank"] = "off"
        read.pop("rerank_model", None)
        read.pop("rerank_device", None)
    out["read"] = read
    if (out.get("storage") or {}).get("path") not in (None, "tempdir", ":memory:"):
        out["storage"] = {"path": "tempdir"}
        notes.append("storage: tempdir")
    return out, notes


# ------------------------------------------------------------------------------------------ tap sink
class _Collect:
    """A tap sink that keeps every finished trace as flat event rows."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def write(self, trace: dict[str, Any]) -> None:
        self._walk(trace)

    def _walk(self, trace: dict[str, Any]) -> None:
        for e in trace.get("events", []):
            row = dict(e.get("fields") or {})
            row["event"] = e["stage"]
            row["_req"] = trace.get("kind")
            row["_seq"] = len(self.rows)
            self.rows.append(row)
        for child in trace.get("children", []):
            self._walk(child)

    def close(self) -> None:  # the tap calls it at shutdown
        pass


# ------------------------------------------------------------------------------------- the replay cache
def _make_replay(cache_dir: Path, live: bool) -> Any:
    from ..call_cache import CallCache, chat_key, embed_key

    class ReplayCache(CallCache):
        """Lookup-only view of a recorded call cache. A miss is listed; it is answered by the
        fallback transport and not stored (with ``live`` it is stored, like any paid screen)."""

        def __init__(self) -> None:
            super().__init__(cache_dir)
            self.live = live
            self.misses: list[dict[str, Any]] = []

        async def acompletion(self, **kwargs: Any) -> Any:
            if not self.cacheable(kwargs):
                self.misses.append(
                    {"kind": "chat", "why": "uncacheable", "model": kwargs.get("model")}
                )
                return await self._inner_completion(**kwargs)
            hit = self._db.execute(
                "SELECT 1 FROM chat WHERE key = ?", (chat_key(kwargs),)
            ).fetchone()
            if hit is not None or self.live:
                if hit is None:
                    self.misses.append(
                        {"kind": "chat", "why": "miss (live)", "model": kwargs.get("model")}
                    )
                return await super().acompletion(**kwargs)
            self.stats.chat_misses += 1
            last = (kwargs.get("messages") or [{}])[-1].get("content", "")
            self.misses.append(
                {
                    "kind": "chat",
                    "why": "miss (stubbed)",
                    "model": kwargs.get("model"),
                    "tail": str(last)[:80],
                }
            )
            return await self._inner_completion(**kwargs)

        async def aembedding(self, **kwargs: Any) -> Any:
            inputs = kwargs.get("input") or []
            texts = [inputs] if isinstance(inputs, str) else [str(t) for t in inputs]
            model = str(kwargs.get("model") or "")
            absent = [
                t
                for t in texts
                if self._db.execute(
                    "SELECT 1 FROM embed WHERE key = ?",
                    (embed_key(model, t, kwargs.get("input_type"), kwargs.get("dimensions")),),
                ).fetchone()
                is None
            ]
            if absent:
                self.misses.append(
                    {
                        "kind": "embed",
                        "why": "miss (live)" if self.live else "miss (hashed)",
                        "model": model,
                        "n": len(absent),
                    }
                )
            if absent and not self.live:
                # serve the hits and hash the rest, without storing either
                return await _hash_embedding(self, kwargs, texts, model)
            return await super().aembedding(**kwargs)

    return ReplayCache()


async def _hash_embedding(
    cache: Any, kwargs: Mapping[str, Any], texts: list[str], model: str
) -> Any:
    from types import SimpleNamespace

    from ..call_cache import _bytes_vector, embed_key

    dim = int(kwargs.get("dimensions") or 0)
    vectors: list[list[float]] = []
    for t in texts:
        row = cache._db.execute(
            "SELECT vector FROM embed WHERE key = ?",
            (embed_key(model, t, kwargs.get("input_type"), kwargs.get("dimensions")),),
        ).fetchone()
        if row is not None:
            vectors.append(_bytes_vector(row[0]))
            dim = dim or len(vectors[-1])
        else:
            vectors.append([])
    dim = dim or 256
    for i, t in enumerate(texts):
        if not vectors[i]:
            seed = hashlib.sha256(t.encode("utf-8")).digest()
            raw = [(seed[j % 32] - 127.5) / 127.5 for j in range(dim)]
            norm = sum(x * x for x in raw) ** 0.5 or 1.0
            vectors[i] = [x / norm for x in raw]
    data = [{"embedding": v, "index": i, "object": "embedding"} for i, v in enumerate(vectors)]
    return SimpleNamespace(
        data=data, usage=SimpleNamespace(prompt_tokens=0, total_tokens=0), model=model
    )


# ------------------------------------------------------------------------------------------ formatting
def _short(value: Any, n: int = 90) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3] + "..."


def _kv(row: Mapping[str, Any], skip: Iterable[str] = (), n: int = 70) -> str:
    skip = set(skip) | _CORRELATION
    parts = []
    for k, v in row.items():
        if k in skip or v is None or v == [] or v == {}:
            continue
        parts.append(f"{k}={_short(v, n)}")
    return " ".join(parts)


def _matches(name: str, prefixes: Sequence[str]) -> bool:
    return any(name == p or name.startswith(p) for p in prefixes)


def _ok(flag: Any) -> str:
    return "yes" if flag else "no"


# ------------------------------------------------------------------------------------------ the session
class StepSession(TuneMixin):
    """One item stepped through an engine; every command returns after printing."""

    def __init__(
        self,
        dataset: Mapping[str, Any],
        item: str,
        arm: str | Path,
        *,
        out: str | Path = DEFAULT_OUT,
        offline: bool = False,
        replay_cache: bool = False,
        cache_dir: str | Path | None = None,
        live: bool = False,
        reader_model: str | None = None,
        judge_model: str | None = None,
        base_url: str = "http://127.0.0.1:11434/v1",
        budget: int = 4096,
        top_k: int = 10,
        pause: bool = False,
        wide: bool = False,
        stream: TextIO | None = None,
        config_override: Mapping[str, Any] | None = None,
        run_id: str | None = None,
    ) -> None:
        if item not in dataset:
            raise KeyError(f"item {item!r} not in dataset (items: {', '.join(list(dataset)[:8])})")
        self.item = item
        self.turns: list[dict[str, Any]] = list(dataset[item]["turns"])
        self.questions: dict[str, dict[str, Any]] = dict(dataset[item]["questions"])
        self.arm_name, cfg = (
            resolve_arm(arm) if config_override is None else (str(arm), dict(config_override))
        )
        self.mode = (
            "live" if live else "replay" if replay_cache else "offline" if offline else "arm"
        )
        self.notes: list[str] = []
        arm_cfg_orig = copy.deepcopy(cfg)
        if self.mode in ("offline", "replay"):
            cfg, self.notes = offline_config(cfg)
        self.cfg = cfg
        self.run_id = run_id or f"step-{self.arm_name}-{item}"
        self.out = Path(out)
        self.pause = pause
        self.wide = wide
        self.budget = budget
        self.top_k = top_k
        self.reader_model = reader_model
        self.judge_model = judge_model or reader_model
        self.base_url = base_url
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.stream = stream or sys.stdout
        self.sink = _Collect()
        self.mark = 0
        self.system: Any = None
        self._stack = contextlib.ExitStack()
        self._env: dict[str, str | None] = {}
        self._cache: Any = None
        self._adhoc = 0
        self.saved = False
        self.check_errors: list[str] = []
        self.base_dataset = dataset
        self._arm_cfg_orig = copy.deepcopy(arm_cfg_orig)
        self._reset_state()
        self.brief = False
        self._mute = False
        self._block: list[str] | None = None
        self.custom_questions: dict[str, dict[str, Any]] = {}
        self._init_tuning(cfg)

    def _reset_state(self) -> None:
        """Everything a fresh engine starts without: what was written, closed and asked."""
        self.next_turn = 0
        self.slept = False
        self.written: list[dict[str, Any]] = []
        self.written_ids: set[str] = set()
        self.closed_sessions: set[str] = set()
        self._custom_n = 0
        self._asked = []
        self._seen = {}
        self._edges_seen = 0
        self._last_read: dict[str, dict[str, Any]] = {}

    # ---- paths
    @property
    def d_mem(self) -> Path:
        return self.out / f"{self.run_id}--memspine"

    @property
    def d_fx(self) -> Path:
        return self.out / f"{self.run_id}--forensics"

    @property
    def d_trace(self) -> Path:
        return self.out / f"{self.run_id}--trace"

    # ---- output
    def w(self, text: str = "") -> None:
        if self._mute:
            return
        data = text + "\n"
        try:
            self.stream.write(data)
        except UnicodeEncodeError:
            self.stream.write(data.encode("ascii", "replace").decode("ascii"))
        self.stream.flush()

    def head(self, title: str) -> None:
        self.w(f"--- {title} " + "-" * max(3, 74 - len(title)))

    def _pause(self) -> None:
        if self.pause and self.stream is sys.stdout and sys.stdin and sys.stdin.isatty():
            with contextlib.suppress(EOFError):
                input("   [Enter] ")

    # ---- lifecycle
    async def start(self) -> None:
        self._stack.callback(_quiet_logs())
        self.out.mkdir(parents=True, exist_ok=True)
        for key, val in (
            ("MEMSPINE_FORENSICS_DIR", str(self.d_fx)),
            ("MEMSPINE_INSPECT", "1"),
            ("MEMSPINE_INSPECT_CONTENT", "1"),
        ):
            self._env[key] = os.environ.get(key)
            os.environ[key] = val
        self._install_transport()
        self._install_prompt_hook()
        await self._boot()
        self.head(f"step: item {self.item} | arm {self.arm_name} | mode {self.mode}")
        self.w(
            f"turns {len(self.turns)} in {len({t['session'] for t in self.turns})} session(s), "
            f"{len(self.questions)} question(s); run folder {self.out / self.run_id}--*"
        )
        for n in self.notes:
            self.w(f"note: {n}")
        if self.mode == "replay":
            self.w(
                f"replay cache: {self.cache_dir}"
                + (
                    " (misses go live)"
                    if self.mode == "replay" and self._cache.live
                    else " (misses are stubbed and listed)"
                )
            )

    async def _boot(self) -> None:
        """A fresh engine, run folders and tap sink for the current item and config."""
        for d in (self.d_mem, self.d_fx, self.d_trace):
            if d.exists():
                shutil.rmtree(d)
        from ..systems.memspine_system import MemspineSystem

        self.system = MemspineSystem(
            template=str(self.cfg.get("template", "base")) if "template" in self.cfg else "base",
            namespace=self.item,
            config={k: v for k, v in self.cfg.items() if k != "template"},
            system_id=f"step-{self.arm_name}",
            build_sleep=True,
            inspect=True,
            inspect_content=True,
        )
        self.system.begin_run(self.run_id)
        self.system.set_trace_dir(str(self.d_trace))
        await self.system.reset(self.item)
        from memspine.observability import tap

        if not tap.enabled():
            raise RuntimeError(
                "the tap did not start; observability.inspect needs observability.tap"
            )
        self.sink = _Collect()
        self.mark = 0
        tap.add_sink(self.sink)
        self._write_manifest()
        self._shipped = {}
        self._apply_prompt_overrides_live()

    async def close(self) -> None:
        if self.system is not None:
            with contextlib.suppress(Exception):
                await self.system.close()
            self.system = None
        self._stack.close()
        for key, old in self._env.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self._env = {}

    def _install_transport(self) -> None:
        if self.mode == "arm":
            return
        import litellm

        from ..stub_llm import install_stub_litellm

        if self.mode == "live":
            if self.cache_dir:
                self._install_replay(live=True)
            return
        stub = self._stack.enter_context(install_stub_litellm())
        self.stub = stub
        inner = litellm.acompletion

        async def window_aware(**kwargs: Any) -> Any:
            reply = _offline_window_reply(kwargs.get("messages") or [])
            if reply is None:
                return await inner(**kwargs)
            from types import SimpleNamespace

            msg = SimpleNamespace(content=reply, role="assistant")
            usage = SimpleNamespace(
                prompt_tokens=len(json.dumps(kwargs.get("messages"))) // 4,
                completion_tokens=len(reply) // 4,
                total_tokens=0,
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=msg, finish_reason="stop", index=0)],
                usage=usage,
                model=kwargs.get("model"),
            )

        litellm.acompletion = window_aware
        if self.mode == "replay":
            if not self.cache_dir:
                raise SystemExit("--replay-cache needs --cache-dir (the recorded call cache)")
            self._install_replay(live=False)

    def _install_replay(self, live: bool) -> None:
        import litellm

        cache = _make_replay(self.cache_dir or Path("."), live)
        saved = (litellm.acompletion, litellm.aembedding)
        cache._inner_completion, cache._inner_embedding = saved
        litellm.acompletion = cache.acompletion
        litellm.aembedding = cache.aembedding

        def restore() -> None:
            litellm.acompletion, litellm.aembedding = saved
            cache.close()

        self._stack.callback(restore)
        self._cache = cache

    def _write_manifest(self) -> None:
        self.d_mem.mkdir(parents=True, exist_ok=True)
        cfg_hash = hashlib.sha256(
            json.dumps(self.cfg, sort_keys=True, default=str).encode()
        ).hexdigest()[:12]
        row = {
            "kind": "manifest",
            "run_id": self.run_id,
            "system": {"system_id": f"step-{self.arm_name}", "config_hash": cfg_hash},
            "reader": {"model": self.reader_model},
            "judge": {"model": self.judge_model},
            "labels": {"arms": [self.arm_name], "mode": self.mode, "step": True},
            "code": {},
            "dataset": {"revision_id": self.item},
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with (self.d_mem / "results.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    # ---- event slices
    def _take(self) -> list[dict[str, Any]]:
        rows = self.sink.rows[self.mark :]
        self.mark = len(self.sink.rows)
        return rows

    def _sections(
        self, rows: list[dict[str, Any]], sections: Sequence[tuple[str, tuple[str, ...]]]
    ) -> None:
        used: set[int] = set()
        derived = (
            "write.firewall.",
            "write.route",
            "write.deposit",
            "write.rejected",
            "write.quarantine",
        )
        for title, prefixes in sections:
            part = [
                r
                for r in rows
                if r["_seq"] not in used
                and (
                    # a derived record's door events (they carry a window id) belong with the deposits
                    (
                        title.startswith("deposits")
                        and r.get("window_id")
                        and r["event"].startswith(derived)
                    )
                    or (
                        not (
                            r.get("window_id")
                            and r["event"].startswith(derived)
                            and not title.startswith("deposits")
                        )
                        and _matches(r["event"], prefixes)
                    )
                )
            ]
            if not part:
                continue
            used.update(r["_seq"] for r in part)
            self.head(title)
            self._print_group(part, compact=title.startswith("stamp"))
            self._pause()
        rest = [
            r
            for r in rows
            if r["_seq"] not in used and not r["event"].startswith(("model.call", "economy."))
        ]
        if rest:
            self.head("other events")
            self._print_group(rest)

    def _print_group(self, rows: list[dict[str, Any]], compact: bool = False) -> None:
        turn_ids = {r.get("turn_id") for r in rows if r.get("turn_id")}
        if compact and len(turn_ids) > 1 and not self.wide:
            rows = self._compact_turns(rows)
        rows = self._thin(rows)
        env_rows = [r for r in rows if r["event"] == "write.envelope.trace"]
        done_env = False
        for r in rows:
            ev = r["event"]
            if ev == "write.envelope.trace":
                if not done_env:
                    self._print_envelopes(env_rows)
                    done_env = True
                continue
            handler = getattr(self, "_p_" + ev.replace(".", "_"), None)
            if handler:
                handler(r)
            else:
                self.w(f"  {ev}  {_kv(r)}")

    def _thin(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """One index line per record, and at most ``RUN_CAP`` lines of one event name (``--wide``: all)."""
        if self.wide:
            return rows
        out: list[dict[str, Any]] = []
        seen_idx: dict[str, int] = {}
        count: dict[str, int] = defaultdict(int)
        skipped: dict[str, int] = defaultdict(int)
        for r in rows:
            ev = r["event"]
            if ev == "store.index.record":
                rid = str(r.get("record_id") or r.get("rid"))
                if rid in seen_idx:
                    out[seen_idx[rid]] = r
                    continue
                seen_idx[rid] = len(out)
            elif ev not in ("write.envelope.trace", "write.turn.summary"):
                count[ev] += 1
                if count[ev] > RUN_CAP:
                    skipped[ev] += 1
                    continue
            out.append(r)
        for ev, n in skipped.items():
            out.append({"event": "write.more", "_seq": -1, "name": ev, "n": n})
        return out

    def _p_write_more(self, r: dict[str, Any]) -> None:
        self.w(f"  ... {r['n']} more {r['name']} (--wide prints all)")

    def _compact_turns(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Many turns in one step: one line per turn instead of every stamp/role/firewall event."""
        per: OrderedDict[str, dict[str, Any]] = OrderedDict()
        keep: list[dict[str, Any]] = []
        for r in rows:
            tid = r.get("turn_id")
            if not tid or not r["event"].startswith(
                (
                    "write.received",
                    "write.messages",
                    "write.role.turn",
                    "write.stamp.",
                    "write.turn.bound",
                    "write.firewall.",
                    "write.route",
                    "write.deposit",
                )
            ):
                keep.append(r)
                continue
            d = per.setdefault(
                str(tid), {"event": "write.turn.summary", "turn_id": tid, "_seq": r["_seq"]}
            )
            for k in ("role", "src_class", "trust", "quarantine", "action", "precision", "route"):
                if r.get(k) is not None:
                    d[k] = r[k]
            if r.get("record_id") or r.get("id"):
                d["rid"] = r.get("record_id") or r.get("id")
        return list(per.values()) + keep

    # ---- specific printers
    def _p_write_turn_summary(self, r: dict[str, Any]) -> None:
        self.w(
            f"  turn {r.get('turn_id')} rid={r.get('rid')} role={r.get('role')} src={r.get('src_class')} "
            f"trust={r.get('trust')} quarantine={r.get('quarantine')} route={r.get('route')} action={r.get('action')}"
        )

    def _p_store_index_record(self, r: dict[str, Any]) -> None:
        edges = r.get("edges") or []
        n_edges = edges if isinstance(edges, int) else len(edges)
        edges = edges if isinstance(edges, list) else []
        self.w(
            f"  index  rid={r.get('record_id') or r.get('rid')} turn={r.get('turn_id')} vector={_ok(r.get('vector'))} "
            f"lexical={_ok(r.get('lexical'))} graph_node={_ok(r.get('graph_node'))} "
            f"projectors={r.get('projectors')} edges={n_edges} text_len={r.get('index_text_len')}"
        )
        for e in edges[:8]:
            if isinstance(e, dict):
                self.w(
                    f"    edge {e.get('kind')}: {e.get('src')} -> {e.get('dst')} ({e.get('rel')})"
                )

    def _p_write_window_members(self, r: dict[str, Any]) -> None:
        pre = r.get("prepass") or {}
        self.w(
            f"  window {r.get('window_id')} session={r.get('session')} cut={r.get('cut_reason')} "
            f"prepass={pre.get('verdict') if isinstance(pre, dict) else pre}"
        )
        rids = r.get("member_record_ids") or []
        turns = r.get("member_turns") or [self.system._origin.get(str(x)) for x in rids]
        self.w(f"    members: turns {[t for t in turns if t]}  records {len(rids)}")

    def _p_write_extract_io(self, r: dict[str, Any]) -> None:
        pr = r.get("prompt") if isinstance(r.get("prompt"), dict) else {}
        self.w(
            f"  extract.io window={r.get('window_id')} prompt={pr.get('id') or r.get('prompt_id')} "
            f"hash={pr.get('hash') or r.get('prompt_hash')} "
            f"model={r.get('model')} cached={r.get('cached')} tokens={r.get('tokens')} ms={r.get('ms')}"
        )
        self.w(f"    sections: {r.get('sections')}")
        limit = 1200 if self.wide else 240
        for label, key in (("prompt", "prompt_text"), ("reply ", "reply_text")):
            text = r.get(key)
            if text:
                self.w(f"    {label}: {_short(text, limit)}")

    def _p_job_stage_item(self, r: dict[str, Any]) -> None:
        ins, outs = r.get("input_ids") or [], r.get("output_ids") or []
        self.w(
            f"    item stage={r.get('stage')} cycle={r.get('cycle')} session={r.get('session_key')} "
            f"in={len(ins)} out={len(outs)} calls={r.get('calls')} tokens={r.get('tokens')} "
            f"status={r.get('status')}" + (f" out_ids={_short(outs, 60)}" if outs else "")
        )

    def _p_write_integrate_ladder(self, r: dict[str, Any]) -> None:
        self.w(f"  ladder  {_kv(r)}")

    def _print_envelopes(self, rows: list[dict[str, Any]]) -> None:
        by_occ: OrderedDict[Any, list[dict[str, Any]]] = OrderedDict()
        for r in rows:
            by_occ.setdefault(r.get("occ"), []).append(r)
        for occ, chain in by_occ.items():
            steps = []
            for r in chain:
                tag = r.get("status") or "?"
                if tag != "kept":
                    extra = r.get("reason") or ",".join(r.get("fields_changed") or []) or ""
                    tag += f"({extra})" if extra else ""
                steps.append(f"{r.get('stage')}:{tag}")
            first = chain[0]
            self.w(f"  occ {occ} [{first.get('window_id')}] " + " ".join(steps))
            shown: dict[str, str] = {}
            for r in chain:
                for key in ("grounding", "resolve", "integrate", "time"):
                    val = r.get(key)
                    if not val:
                        continue
                    if isinstance(val, dict) and not any(
                        v not in (None, "", [], {}) for v in val.values()
                    ):
                        continue
                    text = json.dumps(val, sort_keys=True, default=str)
                    if shown.get(key) != text:
                        shown[key] = text
                        self.w(f"      {r.get('stage')} {key}: {_short(val, 100)}")
            last = chain[-1]
            if last.get("record_id"):
                self.w(f"      -> record {last['record_id']}")

    # ---- commands: write
    def _advance(self) -> None:
        """Move the cursor past turns already written."""
        while (
            self.next_turn < len(self.turns)
            and self.turns[self.next_turn]["turn_id"] in self.written_ids
        ):
            self.next_turn += 1

    def _index_of(self, turn_id: str) -> int | None:
        for i, t in enumerate(self.turns):
            if t["turn_id"] == turn_id:
                return i
        return None

    async def _insert(self, t: dict[str, Any]) -> Any:
        """Write one turn dict through the engine and remember it."""
        turn = Turn(
            turn_id=t["turn_id"],
            session_id=t["session"],
            speaker=t["speaker"],
            text=t["text"],
            timestamp=t.get("ts"),
        )
        dep = await self.system.insert(turn)
        self.written.append(t)
        self.written_ids.add(t["turn_id"])
        self.slept = False
        self._advance()
        return dep

    async def _write_turn(self, t: dict[str, Any], label: str) -> dict[str, Any]:
        """One turn with its stages printed; returns ``{turn_id, records, calls, rows}``."""
        self.head(
            f"{label}: {t['turn_id']}  session {t['session']}  {t['speaker']}  [{t.get('ts')}]"
            + ("  (custom)" if t.get("custom") else "")
        )
        self.w(f"  text: {_short(t['text'], 160)}")
        dep = await self._insert(t)
        dep2 = await self.system._flush_windows(final=False)
        rows = self._take()
        calls = dep.model_calls + (dep2.model_calls if dep2 else 0)
        self.w(
            f"  deposit: records={dep.n_records} model_calls={calls} ids={[str(i) for i in dep.record_ids]}"
        )
        self._sections(rows, WRITE_SECTIONS)
        self._drift_note()
        return {
            "turn_id": t["turn_id"],
            "speaker": t["speaker"],
            "records": dep.n_records,
            "calls": calls,
            "rows": rows,
        }

    def _drift_note(self) -> None:
        if self._cache is not None and self._cache.misses:
            self.w(f"  replay cache misses so far: {len(self._cache.misses)}")

    def _summary(self, title: str, parts: list[dict[str, Any]]) -> None:
        counts: dict[str, int] = {}
        for p in parts:
            for r in p["rows"]:
                for name, prefixes in WRITE_SECTIONS:
                    if _matches(r["event"], prefixes):
                        key = name.split(" (")[0]
                        counts[key] = counts.get(key, 0) + 1
                        break
        self.head(f"summary: {title}")
        self.w(
            f"  turns={len(parts)} records={sum(p['records'] for p in parts)} "
            f"model_calls={sum(p['calls'] for p in parts)}"
        )
        self.w(
            "  events by stage: " + (", ".join(f"{k}={v}" for k, v in counts.items()) or "(none)")
        )

    def _order_warning(self, picked: list[int]) -> None:
        if picked != sorted(picked):
            self.w("  warning: the selection is not in dataset order; writing it as given")
        done = [i for t in self.written if (i := self._index_of(t["turn_id"])) is not None]
        late = [self.turns[i]["turn_id"] for i in picked if done and i < max(done)]
        if late:
            self.w(
                f"  warning: {late} come before turns already written (order differs from the dataset)"
            )

    async def cmd_turn(self, count: int = 1) -> None:
        self._advance()
        parts = []
        for _ in range(count):
            self._advance()
            if self.next_turn >= len(self.turns):
                self.w("no more turns")
                break
            t = self.turns[self.next_turn]
            parts.append(await self._write_turn(t, f"turn {self.next_turn + 1}/{len(self.turns)}"))
        if len(parts) > 1:
            self._summary(f"{len(parts)} turns", parts)

    def _resolve_turns(self, spec: str) -> list[int] | None:
        """``D1:3``, ``D1:3-D1:6``, ``D1:3,D2:5`` or a mix -> dataset indices (None = a bad token)."""
        out: list[int] = []
        for part in [p.strip() for p in spec.split(",") if p.strip()]:
            i = self._index_of(part)
            if i is not None:
                out.append(i)
                continue
            for pos in [k for k, c in enumerate(part) if c == "-"]:
                lo, hi = self._index_of(part[:pos]), self._index_of(part[pos + 1 :])
                if lo is not None and hi is not None:
                    if lo > hi:
                        self.w(
                            f"  warning: {part} runs backwards; writing {self.turns[hi]['turn_id']}..{self.turns[lo]['turn_id']}"
                        )
                        lo, hi = hi, lo
                    out.extend(range(lo, hi + 1))
                    break
            else:
                self.w(f"unknown turn {part!r} (turn ids look like {self.turns[0]['turn_id']})")
                return None
        return out

    async def cmd_turns(self, spec: str) -> None:
        if not spec:
            self.w("usage: turns D1:1-D1:6 | turns D1:3,D2:5 | turn <turn_id>")
            return
        picked = self._resolve_turns(spec)
        if picked is None:
            return
        fresh: list[int] = []
        for i in picked:
            tid = self.turns[i]["turn_id"]
            if tid in self.written_ids or i in fresh:
                self.w(f"  {tid} is already written (skipped; `reset` starts over)")
            else:
                fresh.append(i)
        self._order_warning(fresh)
        parts = [
            await self._write_turn(self.turns[i], f"turn {i + 1}/{len(self.turns)}") for i in fresh
        ]
        if len(parts) > 1:
            self._summary(f"{len(parts)} selected turns", parts)

    async def cmd_skip(self, n: int = 1) -> None:
        moved = []
        for _ in range(n):
            self._advance()
            if self.next_turn >= len(self.turns):
                break
            moved.append(self.turns[self.next_turn]["turn_id"])
            self.next_turn += 1
        self.w(f"skipped (not written): {moved}; cursor at " + self._cursor_text())

    def _cursor_text(self) -> str:
        self._advance()
        return (
            f"{self.turns[self.next_turn]['turn_id']} ({self.next_turn}/{len(self.turns)})"
            if self.next_turn < len(self.turns)
            else f"end ({len(self.turns)}/{len(self.turns)})"
        )

    async def cmd_goto(self, arg: str) -> None:
        i = self._index_of(arg)
        if i is None:
            self.w(f"unknown turn {arg!r}")
            return
        self.next_turn = i
        self.w(
            f"cursor moved to {arg} (nothing written)"
            + ("; note: it is already written" if arg in self.written_ids else "")
        )

    async def cmd_interaction(self, n: int = 1) -> None:
        """One interaction = a turn plus the run of turns right after it by the next (different) speaker."""
        for k in range(n):
            self._advance()
            if self.next_turn >= len(self.turns):
                self.w("no more turns")
                return
            i = self.next_turn
            group = [self.turns[i]]
            j = i + 1
            replier = None
            while (
                j < len(self.turns)
                and self.turns[j]["session"] == group[0]["session"]
                and self.turns[j]["turn_id"] not in self.written_ids
                and self.turns[j]["speaker"] != group[0]["speaker"]
                and replier in (None, self.turns[j]["speaker"])
            ):
                replier = self.turns[j]["speaker"]
                group.append(self.turns[j])
                j += 1
            if len(group) == 1:
                self.w(
                    "  note: no reply follows this turn in its session (the interaction is one turn)"
                )
            parts = [
                await self._write_turn(
                    t, f"interaction {k + 1}/{n} turn {(self._index_of(t['turn_id']) or 0) + 1}"
                )
                for t in group
            ]
            self._summary(
                "interaction " + " -> ".join(f"{t['turn_id']} ({t['speaker']})" for t in group),
                parts,
            )

    # ---- custom messages
    @staticmethod
    def _parse_ts(text: str | None) -> Any:
        from datetime import datetime

        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(str(text), fmt)
            except ValueError:
                continue
        return None

    def _default_ts(self) -> str:
        from datetime import timedelta

        last = (
            self.written[-1].get("ts")
            if self.written
            else (self.turns[0].get("ts") if self.turns else None)
        )
        dt = self._parse_ts(last)
        if dt is None:
            return str(last) if last else "2024-01-01 00:00"
        return (dt + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M")

    def parse_say(self, line: str) -> dict[str, Any] | None:
        """``Speaker: text [@YYYY-MM-DD[ HH:MM]] [#session]`` -> a custom turn spec (id assigned at write)."""
        import re

        text = line.strip()
        ts = sess = None
        for _ in range(2):
            m = re.search(r"\s+@(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?)\s*$", text)
            if m and ts is None:
                ts, text = m.group(1).replace("T", " "), text[: m.start()]
                continue
            m = re.search(r"\s+#(\S+)\s*$", text)
            if m and sess is None:
                sess, text = m.group(1), text[: m.start()]
        speaker, sep, said = text.partition(":")
        if not sep or not speaker.strip() or not said.strip():
            self.w("usage: say <Speaker>: <text> [@YYYY-MM-DD[ HH:MM]] [#session]")
            return None
        return {"speaker": speaker.strip(), "text": said.strip(), "ts": ts, "session": sess}

    async def cmd_say(self, lines: list[str]) -> None:
        parts = []
        for line in lines:
            spec = self.parse_say(line)
            if spec is None:
                continue
            self._custom_n += 1
            tid = f"U{self._custom_n}"
            while tid in self.written_ids or self._index_of(tid) is not None:
                self._custom_n += 1
                tid = f"U{self._custom_n}"
            last_session = (
                self.written[-1]["session"]
                if self.written
                else (self.turns[0]["session"] if self.turns else "custom")
            )
            t = {
                "item": self.item,
                "session": spec["session"] or last_session,
                "turn_id": tid,
                "speaker": spec["speaker"],
                "ts": spec["ts"] or self._default_ts(),
                "text": spec["text"],
                "custom": True,
            }
            parts.append(await self._write_turn(t, "say"))
        if len(parts) > 1:
            self._summary(f"{len(parts)} custom messages", parts)

    # ---- sessions
    def _session_ids(self) -> list[str]:
        return list(OrderedDict.fromkeys(t["session"] for t in self.turns))

    async def _write_session(self, session: str, turns: list[dict[str, Any]]) -> dict[str, Any]:
        n = 0
        for t in turns:
            if t["turn_id"] in self.written_ids:
                continue
            await self._insert(t)
            n += 1
        self.head(f"session {session}: wrote {n} turn(s); closing windows and the session")
        await self.system._flush_windows(final=False)
        if self.system._windows_on():
            await self.system._flush_windows(final=True)
        else:
            closed = await self.system._close_sessions()
            if closed:
                self.w(f"  session close: {_short(closed, 200)}")
        self.closed_sessions.add(session)
        rows = self._take()
        self._sections(rows, WRITE_SECTIONS)
        self._drift_note()
        return {
            "session": session,
            "turns": n,
            "records": sum(1 for r in rows if r["event"] == "store.index.record"),
            "calls": 0,
            "rows": rows,
        }

    async def cmd_session(self, arg: str = "") -> None:
        """No argument: the rest of the current session. ``N``: that many sessions from the cursor.
        An id (or ``id1,id2``): those sessions, in the order given."""
        self._advance()
        ids = self._session_ids()
        done: list[dict[str, Any]] = []
        known = set(ids) | {t["session"] for t in self.written}
        if arg and all(x.strip() in known for x in arg.split(",")):
            for sid in [x.strip() for x in arg.split(",")]:
                done.append(
                    await self._write_session(sid, [t for t in self.turns if t["session"] == sid])
                )
        else:
            if arg and not arg.isdigit():
                self.w(f"unknown session {arg!r} (known: {', '.join(ids)})")
                return
            for _ in range(int(arg) if arg else 1):
                self._advance()
                if self.next_turn >= len(self.turns):
                    self.w("no more turns")
                    break
                sid = self.turns[self.next_turn]["session"]
                done.append(
                    await self._write_session(
                        sid, [t for t in self.turns[self.next_turn :] if t["session"] == sid]
                    )
                )
        if len(done) > 1:
            self.head("summary: sessions")
            for d in done:
                self.w(f"  {d['session']}: turns={d['turns']} records={d['records']}")

    async def cmd_sessions(self, spec: str) -> None:
        ids = self._session_ids()
        picked: list[str] = []
        for part in [p.strip() for p in spec.split(",") if p.strip()]:
            if part in ids:
                picked.append(part)
                continue
            for pos in [k for k, c in enumerate(part) if c == "-"]:
                lo, hi = part[:pos], part[pos + 1 :]
                if lo in ids and hi in ids:
                    a, b = sorted((ids.index(lo), ids.index(hi)))
                    picked.extend(ids[a : b + 1])
                    break
            else:
                self.w(f"unknown session {part!r} (known: {', '.join(ids)})")
                return
        if not picked:
            self.w("usage: sessions D1-D3 | sessions D1,D3")
            return
        await self.cmd_session(",".join(picked))

    async def cmd_status(self) -> None:
        recs = await self._records()
        sessions: OrderedDict[str, int] = OrderedDict()
        for t in self.written:
            sessions[t["session"]] = sessions.get(t["session"], 0) + 1
        windows = {
            r.get("window_id")
            for r in self.sink.rows
            if r["event"] == "write.window.members" and r.get("window_id")
        }
        self.head("status")
        self.w(f"  item {self.item} | arm {self.arm_name} | mode {self.mode}")
        self.w(
            f"  written: {len(self.written)} turn(s) ({sum(1 for t in self.written if t.get('custom'))} custom) of "
            f"{len(self.turns)} in the dataset; cursor at {self._cursor_text()}"
        )
        for sid, n in sessions.items():
            self.w(
                f"  session {sid}: {n} turn(s) written, "
                + ("closed" if sid in self.closed_sessions else "open")
            )
        self.w(
            f"  windows formed: {len(windows)}; open sessions: "
            f"{[s for s in sessions if s not in self.closed_sessions] or 'none'}; slept: {_ok(self.slept)}"
        )
        self.w(
            f"  records in the store: {len(recs)}; questions asked: {len({r['query_id'] for r in self._asked})} "
            f"of {len(self.questions)}"
        )
        if self.overrides or self.prompt_overrides:
            self.w(
                f"  tuning: {len(self.overrides)} override(s), {len(self.prompt_overrides)} prompt override(s)"
            )

    async def cmd_reset(self) -> None:
        if self.system is not None:
            with contextlib.suppress(Exception):
                await self.system.close()
        self._reset_state()
        await self._boot()
        self.head(
            "reset: fresh engine, same arm"
            + (f" with {len(self.overrides)} override(s) kept" if self.overrides else "")
        )
        self.w(f"  nothing written; cursor at {self._cursor_text()}")

    async def cmd_load(self, path: str) -> None:
        if not path or not Path(path).exists():
            self.w(
                f"usage: load <file.json> (same shape as demo_conversation.json); not found: {path!r}"
            )
            return
        ds = load_dataset(path)
        item = next(iter(ds))
        self.item = item
        self.turns = list(ds[item]["turns"])
        self.questions = dict(ds[item]["questions"])
        self.custom_questions = {}
        self.run_id = f"step-{self.arm_name}-{item}"
        if self.system is not None:
            with contextlib.suppress(Exception):
                await self.system.close()
        self._reset_state()
        await self._boot()
        self.head(f"load: item {item}")
        self.w(
            f"  turns {len(self.turns)} in {len(self._session_ids())} session(s), {len(self.questions)} question(s)"
        )

    async def cmd_sleep(self) -> None:
        for d in (self.d_fx / "inspect",):
            if d.exists():
                shutil.rmtree(d)  # build() writes the post_ingest and post_sleep snapshots afresh
        while self.next_turn < len(self.turns):
            self.w(f"(writing the {len(self.turns) - self.next_turn} remaining turn(s) first)")
            await self.cmd_session()
        self.head("sleep: flush, session close, then the sleep cycle")
        deposit = await self.system.build()
        self.slept = True
        rows = self._take()
        self.w(f"  model_calls={deposit.model_calls}")
        pre = [r for r in rows if not r["event"].startswith(("job.", "economy.", "model."))]
        if pre:
            self._sections(pre, WRITE_SECTIONS)
        jobs = [
            r
            for r in rows
            if r["event"].startswith(
                ("job.", "economy.", "model.call", "graph.compile", "derive.graph")
            )
            or r["event"].startswith("trust.")
        ]
        current: str | None = None
        for r in jobs:
            ev = r["event"]
            if ev == "job.stage.start":
                current = r.get("stage")
                self.head(f"sleep stage {current}")
                self._pause()
            elif ev == "job.stage.end":
                self.w(f"  end {_kv(r, skip=('stage',))}")
                current = None
            elif ev == "job.stage.item":
                self._p_job_stage_item(r)
            elif ev.startswith("model.call"):
                continue
            else:
                self.w(f"  {ev}  {_kv(r)}")
        if not any(r["event"] == "job.stage.start" for r in jobs):
            self.w("  (no sleep stage events)")
        stats = (deposit.meta or {}).get("sleep") or {}
        for name, st in stats.items():
            self.w(f"  stage {name}: {_short(st, 120)}")
        self._drift_note()

    # ---- commands: read
    def _find_question(self, arg: str) -> tuple[str, dict[str, Any]]:
        """A question id, the text of a known question, or free text (``text | gold: A | turns: D1:3,D2:5``).
        Free text becomes a custom question, kept for the bundle."""
        if arg in self.questions:
            return arg, self.questions[arg]
        text, gold, turns = arg, None, []
        if "|" in arg:
            text, *tail = [x.strip() for x in arg.split("|")]
            for part in tail:
                key, _, val = part.partition(":")
                if key.strip().lower() == "gold":
                    gold = val.strip() or None
                elif key.strip().lower() in ("turns", "gold_turns"):
                    turns = [x.strip() for x in val.split(",") if x.strip()]
        for qid, q in self.questions.items():
            if q.get("question", "").strip().lower() == text.strip().lower():
                if gold is not None or turns:
                    q.update(
                        {
                            "gold": gold if gold is not None else q.get("gold"),
                            "gold_turns": turns or q.get("gold_turns"),
                        }
                    )
                return qid, q
        self._adhoc += 1
        qid = f"adhoc-{self._adhoc}"
        while qid in self.questions:
            self._adhoc += 1
            qid = f"adhoc-{self._adhoc}"
        q = {
            "question": text,
            "gold": gold,
            "category": "adhoc",
            "gold_turns": turns,
            "custom": True,
        }
        self.questions[qid] = q
        return qid, q

    def _targets(self, arg: str) -> list[tuple[str, dict[str, Any]]] | None:
        """``q1,q3`` | ``all`` | ``cat:<n>`` | free text -> the questions to ask (None = nothing matched)."""
        low = arg.strip().lower()
        if low == "all":
            return list(self.questions.items())
        if low.startswith("cat:"):
            want = low[4:].strip()
            found = [
                (k, q)
                for k, q in self.questions.items()
                if str(q.get("category")).lower() in (want, f"cat{want}", want.removeprefix("cat"))
            ]
            if not found:
                cats = sorted({str(q.get("category")) for q in self.questions.values()})
                self.w(f"no question in category {want!r} (categories: {', '.join(cats)})")
                return None
            return found
        toks = [x.strip() for x in arg.split(",") if x.strip()]
        if len(toks) > 1 and all(x in self.questions for x in toks):
            return [(x, self.questions[x]) for x in toks]
        return [self._find_question(arg)]

    async def cmd_ask(self, arg: str) -> None:
        brief = self.brief
        for flag in ("--brief", "--full"):
            if arg.endswith(flag):
                arg, brief = arg[: -len(flag)].strip(), flag == "--brief"
        if not arg:
            for qid, q in self.questions.items():
                self.w(f"  {qid}  [{q.get('category')}] {_short(q.get('question'), 100)}")
            return
        targets = self._targets(arg)
        if not targets:
            return
        await self._ask_many(targets, brief)

    async def cmd_asks(self, path: str) -> None:
        brief = self.brief
        if path.endswith("--brief"):
            path, brief = path[:-7].strip(), True
        if not path or not Path(path).exists():
            self.w(
                f"usage: asks <file> (one question per line: an id, free text, or `text | gold: A | turns: D1:3`); not found: {path!r}"
            )
            return
        targets = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.strip().startswith("#"):
                targets.append(self._find_question(line.strip()))
        await self._ask_many(targets, brief)

    async def _ask_many(self, targets: list[tuple[str, dict[str, Any]]], brief: bool) -> None:
        results = []
        for qid, q in targets:
            if brief:
                self._mute = True
                try:
                    res = await self._ask_one(qid, q)
                finally:
                    self._mute = False
                self.w("  " + self._brief_line(res))
            else:
                res = await self._ask_one(qid, q)
            results.append(res)
        if len(results) > 1:
            self._ask_table(results)

    def _brief_line(self, r: dict[str, Any]) -> str:
        return (
            f"{r['qid']} [{r['category']}] gold {r['found']}/{r['total']} lost_at={r['lost']} answer={r['correct']} "
            f"| {_short(r['question'], 60)}"
        )

    def _ask_table(self, results: list[dict[str, Any]]) -> None:
        self.head(f"SUMMARY TABLE ({len(results)} question(s))")
        rows = [("qid", "category", "gold found", "first losing stage", "answer correct")]
        rows += [
            (
                r["qid"],
                str(r["category"]),
                f"{r['found']}/{r['total']}",
                str(r["lost"]),
                str(r["correct"]),
            )
            for r in results
        ]
        widths = [max(len(row[i]) for row in rows) for i in range(5)]
        for k, row in enumerate(rows):
            self.w("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(row)))
            if k == 0:
                self.w("  " + "  ".join("-" * w for w in widths))
        with_gold = [r for r in results if r["total"]]
        if with_gold:
            full = sum(1 for r in with_gold if r["found"] == r["total"])
            self.w(f"  gold fully in context: {full}/{len(with_gold)}")

    def _lost_stage(
        self, gold_turns: Sequence[str], fx: Mapping[str, Any] | None, got: set[str]
    ) -> str:
        if not gold_turns:
            return "-"
        unwritten = [g for g in gold_turns if g not in self.written_ids]
        if unwritten:
            return f"not written ({len(unwritten)})"
        _rows, loss = gold_path(list(gold_turns), dict(fx) if fx else None, {}, got)
        return loss or "-"

    async def _ask_one(self, qid: str, q: dict[str, Any]) -> dict[str, Any]:
        gold_turns = list(q.get("gold_turns") or [])
        self.head(f"ask {qid} [{q.get('category')}]: {q['question']}")
        self.w(f"  gold: {_short(q.get('gold'), 100)}   gold turns: {gold_turns}")
        self.system.set_query_id(qid)
        self.system.set_query_meta({})
        t0 = time.perf_counter()
        ctx = await self.system.query(q["question"], self.budget, self.top_k)
        ms = (time.perf_counter() - t0) * 1000
        rows = self._take()
        fx = self._last_forensics(qid)
        self._print_read(rows, fx, gold_turns)
        self.head("packed context")
        self.w(
            f"  tokens={ctx.tokens} truncated={ctx.truncated} evidence={len(ctx.evidence)} ({ms:.0f} ms)"
        )
        got = [e.turn_id for e in ctx.evidence]
        for line in ctx.text.splitlines()[: (200 if self.wide else 40)]:
            self.w("  | " + _short(line, 200))
        if gold_turns:
            hit = [g for g in gold_turns if g in got]
            self.w(f"  gold turns in context: {hit} of {gold_turns}")
        gold_text = q.get("gold")
        in_ctx = None
        if gold_text not in (None, "") and q.get("category") not in ("cat5",):
            from ..judge import normalise_answer

            in_ctx = normalise_answer(str(gold_text)) in normalise_answer(ctx.text)
            self.w(f"  gold answer text appears in context: {_ok(in_ctx)}")
        answer, verdict = await self._reader_and_judge(q, ctx)
        self._record_question(qid, q, ctx, fx, answer, verdict)
        self._drift_note()
        score = (verdict or {}).get("score")
        if score is not None:
            correct = "yes" if score >= 1 else "no"
        elif in_ctx is not None:
            correct = f"gold text in ctx: {_ok(in_ctx)}"
        else:
            correct = "-"
        res = {
            "qid": qid,
            "category": q.get("category"),
            "question": q["question"],
            "gold": q.get("gold"),
            "gold_turns": gold_turns,
            "found": sum(1 for g in gold_turns if g in got),
            "total": len(gold_turns),
            "lost": self._lost_stage(gold_turns, fx, set(got)),
            "correct": correct,
            "got": got,
            "fx": fx,
            "context": ctx.text,
            "answer": answer,
            "verdict": verdict,
            "tokens": ctx.tokens,
        }
        self._last_read[qid] = res
        return res

    def _last_forensics(self, qid: str) -> dict[str, Any] | None:
        path = self.d_fx / "forensics.jsonl"
        if not path.exists():
            return None
        last = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                last = json.loads(line)
        return last

    def _ranked(self, name: str, items: Any, gold: Sequence[str], limit: int = 10) -> None:
        if not items:
            self.w(f"  {name}: (none)")
            return
        pieces = []
        for it in list(items)[:limit]:
            turn = it.get("turn") or it.get("id")
            mark = "*" if turn in gold else ""
            sc = it.get("score")
            pieces.append(
                f"{it.get('r', '')}:{turn}{mark}"
                + (f"({sc:.3f})" if isinstance(sc, int | float) else "")
            )
        miss = [g for g in gold if g not in {(i.get("turn") or i.get("id")) for i in items}]
        self.w(
            f"  {name} top{min(limit, len(items))}/{len(items)}: "
            + " ".join(pieces)
            + (f"   gold absent: {miss}" if gold and miss else "")
        )

    def _print_read(
        self, rows: list[dict[str, Any]], fx: dict[str, Any] | None, gold: Sequence[str]
    ) -> None:
        plan = [r for r in rows if _matches(r["event"], READ_SECTIONS[0][1])]
        if plan:
            self.head("plan")
            self._print_group(plan)
        if fx:
            self.head("legs (gold marked *)")
            self._ranked("vector ", fx.get("vector"), gold)
            self._ranked("lexical", fx.get("lexical"), gold)
            for name, leg in (fx.get("extra_legs") or {}).items():
                self._ranked(name, leg, gold)
        leg_rows = [r for r in rows if _matches(r["event"], READ_SECTIONS[1][1])]
        if leg_rows:
            self._print_group(leg_rows)
        if fx:
            self.head("fusion / cuts / pool")
            self._ranked("fused  ", fx.get("fused"), gold)
            tf = fx.get("trace_full") or {}
            for cut in (tf.get("cuts") or [])[:20]:
                self.w(
                    f"  cut {cut.get('turn') or cut.get('id')}: {cut.get('reason')}"
                    + (" <== GOLD" if (cut.get("turn") or cut.get("id")) in gold else "")
                )
            self._ranked("pool   ", fx.get("pool"), gold, 12)
        mid = [r for r in rows if _matches(r["event"], READ_SECTIONS[2][1])]
        if mid:
            self._print_group(mid)
        rer = [r for r in rows if _matches(r["event"], READ_SECTIONS[3][1])]
        if fx or rer:
            self.head("rerank")
            if fx:
                self._ranked("rerank ", fx.get("rerank_scores"), gold, 12)
                self._ranked("final  ", fx.get("final"), gold)
            comps = [r for r in rer if r["event"] == "read.rerank.components"]
            for c in comps:
                self.w(
                    f"  components reranked={c.get('reranked')} mode={c.get('mode')} n={c.get('n')}"
                )
                for it in (c.get("items") or [])[:12]:
                    turn = self.system._origin.get(str(it.get("rid")))
                    self.w("    " + (f"turn={turn} " if turn else "") + _kv(it, n=40))
            self._print_group([r for r in rer if r["event"] != "read.rerank.components"])
        hdr = [r for r in rows if _matches(r["event"], READ_SECTIONS[4][1])]
        if hdr:
            self.head("headers / pack")
            self._print_group(hdr)
        if fx and (fx.get("trace_full") or {}).get("window"):
            self.head("window (neighbours)")
            for wnd in fx["trace_full"]["window"][:10]:
                self.w(f"  anchor {wnd.get('anchor')} neighbours {wnd.get('neighbours')}")

    async def _reader_and_judge(
        self, q: Mapping[str, Any], ctx: Any
    ) -> tuple[str | None, dict[str, Any] | None]:
        self.head("reader / judge")
        if not self.reader_model:
            self.w("  (no reader: pass --reader-model [--judge-model] [--base-url])")
            return None, None
        from ..judge import JudgeScale, LLMJudge
        from ..readers import OpenAICompatReader

        edited = (self.prompt_overrides.get("reader.qa") or {}).get("body")
        reader = OpenAICompatReader(
            self.reader_model, base_url=self.base_url, **({"prompt": edited} if edited else {})
        )
        try:
            ans = await reader.answer(q["question"], ctx.text)
        except Exception as exc:
            self.w(f"  reader unavailable: {type(exc).__name__}: {_short(str(exc), 120)}")
            return None, None
        answer = getattr(ans, "text", None) or getattr(ans, "answer", None) or str(ans)
        self.w(f"  answer: {_short(answer, 200)}")
        verdict = None
        if q.get("gold") not in (None, ""):
            judge_reader = OpenAICompatReader(
                self.judge_model or self.reader_model, base_url=self.base_url
            )
            judge = LLMJudge(
                chat=judge_reader.chat,
                model=self.judge_model or self.reader_model,
                scale=JudgeScale.BINARY,
            )
            try:
                v = await judge.score(q["question"], answer, str(q["gold"]))
                verdict = {"score": v.score, "raw": v.raw}
                self.w(f"  judge: score={v.score} raw={_short(v.raw, 80)}")
            except Exception as exc:
                self.w(f"  judge unavailable: {type(exc).__name__}: {_short(str(exc), 120)}")
        return answer, verdict

    def _record_question(
        self,
        qid: str,
        q: Mapping[str, Any],
        ctx: Any,
        fx: Mapping[str, Any] | None,
        answer: str | None,
        verdict: Mapping[str, Any] | None,
    ) -> None:
        score = (verdict or {}).get("score")
        row = {
            "kind": "result",
            "run_id": self.run_id,
            "item_id": self.item,
            "query_id": qid,
            "question": q["question"],
            "gold": q.get("gold"),
            "answer": answer,
            "score": score,
            "status": "completed" if answer is not None else "retrieval_only",
            "type_label": q.get("category"),
            "context_tokens": ctx.tokens,
            "retrieved_ids": [e.turn_id for e in ctx.evidence],
            "meta": {"judge_raw": (verdict or {}).get("raw")},
        }
        if q.get("custom"):
            row["custom"] = True
        reads = {
            "run_id": self.run_id,
            "item_id": self.item,
            "query_id": qid,
            "question": q["question"],
            "gold": q.get("gold"),
            "context_text": ctx.text,
            "context_tokens": ctx.tokens,
            "answer": answer,
            "reader_raw": answer,
            "reader_calls": [],
            "judge_calls": [],
            "verdict": dict(verdict or {}),
        }
        self.d_mem.mkdir(parents=True, exist_ok=True)
        self.d_trace.mkdir(parents=True, exist_ok=True)
        with (self.d_mem / "results.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        with (self.d_trace / "reads.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(reads, default=str) + "\n")
        self._asked.append(row)

    # ---- inspection commands
    async def _records(self) -> list[Any]:
        storage = self.system._engine._require_started()
        return list(await storage.list_records(self.item))

    async def cmd_diff(self) -> None:
        recs = await self._records()
        now = {str(r.record_id): (getattr(r.status, "value", r.status), r.content) for r in recs}
        by_id = {str(r.record_id): r for r in recs}
        new = [k for k in now if k not in self._seen]
        changed = [k for k in now if k in self._seen and self._seen[k][0] != now[k][0]]
        self.head(f"diff: {len(new)} new, {len(changed)} status change(s), {len(now)} total")
        for k in new[: (len(new) if self.wide else 25)]:
            r = by_id[k]
            self.w(
                f"  + {k} {r.memory_type}/{r.layer} {r.source.channel} status={now[k][0]} "
                f"trust={r.trust} {_short(r.content, 90)}"
            )
        if len(new) > 25 and not self.wide:
            self.w(f"  ... {len(new) - 25} more new records (--wide prints all)")
        for k in changed:
            r = by_id[k]
            self.w(
                f"  ~ {k} {self._seen[k][0]} -> {now[k][0]} evolve_to={r.evolve_to} valid_to={r.valid_to}"
            )
        edges = await self._edge_count()
        if edges is not None:
            self.w(f"  graph edges: {edges} ({edges - self._edges_seen:+d})")
            self._edges_seen = edges
        self._seen = now

    async def _edge_count(self) -> int | None:
        try:
            from .snapshot import _graph_rows

            engine = self.system._engine
            edges, _nodes = await _graph_rows(engine, engine._require_started(), self.item)
            return len(edges)
        except Exception:
            return None

    def _origin_rid(self, token: str) -> str | None:
        for rid, turn in self.system._origin.items():
            if turn == token:
                return rid
        return None

    async def cmd_show(self, arg: str) -> None:
        if not arg:
            self.w("usage: show <rid|window_id|occ|turn_id>")
            return
        rid = self._origin_rid(arg) or arg
        recs = {str(r.record_id): r for r in await self._records()}
        events = self.sink.rows
        if rid in recs:
            r = recs[rid]
            self.head(
                f"record {rid}"
                + (f"  (turn {self.system._origin[rid]})" if rid in self.system._origin else "")
            )
            for k in (
                "memory_type",
                "layer",
                "status",
                "trust",
                "quarantined",
                "valid_from",
                "valid_to",
                "evolve_to",
                "tags",
                "entity",
                "attribute",
                "group_id",
            ):
                v = getattr(r, k, None)
                if v not in (None, [], ()):
                    self.w(f"  {k}: {_short(v, 120)}")
            self.w(
                f"  channel: {r.source.channel}  role: {r.source.role}  parents: {[str(p) for p in r.source.parents]}"
            )
            self.w(f"  content: {_short(r.content, 400)}")
            try:
                env = await self.system._engine.get_envelope(rid, self.item)
            except Exception:
                env = None
            if env is not None:
                self.w(f"  envelope: {_short(env.model_dump(mode='json'), 600)}")
            for e in events:
                if e.get("rid") == rid or e.get("record_id") == rid:
                    self.w(f"  event {e['event']}: {_kv(e, n=60)}")
            return
        win = [e for e in events if e.get("window_id") == arg]
        if win:
            self.head(f"window {arg}")
            self._print_group([e for e in win if e["event"] != "write.envelope.trace"])
            self._print_envelopes([e for e in win if e["event"] == "write.envelope.trace"])
            return
        if arg.isdigit():
            occ = [e for e in events if str(e.get("occ")) == arg]
            if occ:
                self.head(f"occ {arg}")
                self._print_envelopes([e for e in occ if e["event"] == "write.envelope.trace"])
                for e in occ:
                    if e["event"] != "write.envelope.trace":
                        self.w(f"  {e['event']}: {_kv(e)}")
                return
        self.w(f"nothing known as {arg!r}")

    async def cmd_links(self, arg: str) -> None:
        if not arg:
            self.w("usage: links <rid|turn_id>")
            return
        rid = self._origin_rid(arg) or arg
        recs = {str(r.record_id): r for r in await self._records()}
        if rid not in recs:
            self.w(f"no record {arg!r}")
            return
        r = recs[rid]
        self.head(f"links of {rid}")
        parents = [str(p) for p in r.source.parents]
        children = [k for k, x in recs.items() if rid in [str(p) for p in x.source.parents]]
        self.w(f"  parents ({len(parents)}): {parents}")
        self.w(f"  children ({len(children)}): {children}")
        if r.evolve_to:
            self.w(f"  superseded by: {r.evolve_to}")
        prev = [k for k, x in recs.items() if x.evolve_to and str(x.evolve_to) == rid]
        if prev:
            self.w(f"  supersedes: {prev}")
        try:
            from .snapshot import _graph_rows

            engine = self.system._engine
            edges, _ = await _graph_rows(engine, engine._require_started(), self.item)
            mine = [e for e in edges if rid in (e.src, e.dst)]
            self.w(f"  graph edges ({len(mine)}):")
            for e in mine[:20]:
                self.w(f"    {e.src} -[{e.rel_type}]-> {e.dst}")
        except Exception as exc:
            self.w(f"  graph edges unavailable: {type(exc).__name__}")

    async def cmd_save(self) -> None:
        from .snapshot import dump_snapshot

        inspect_dir = self.d_fx / "inspect"
        if not self.slept:  # sleep() writes its own two snapshots; otherwise take one now
            if inspect_dir.exists():
                shutil.rmtree(inspect_dir)
            await dump_snapshot(
                self.system._engine,
                self.item,
                inspect_dir,
                "post_ingest",
                origin_map=self.system._origin,
                content=True,
                run_id=self.run_id,
                item=self.item,
            )
        bundle = self.out / f"{self.run_id}--bundle"
        if bundle.exists():
            shutil.rmtree(bundle)
        self._dedupe_results()
        raw = _as_dataset_json(self.item, self.written, self.questions)
        man = assemble(self.d_mem, bundle, raw, content=True)
        self.saved = True
        if self.tuning_log:  # the tuning history rides in the bundle for the viewer
            (bundle / "tuning.jsonl").write_text(
                "".join(
                    json.dumps(r, default=str, ensure_ascii=False) + chr(10)
                    for r in self.tuning_log
                ),
                encoding="utf-8",
            )
            man.setdefault("counts", {})["tuning"] = len(self.tuning_log)
        self.head("saved")
        self.w(f"  bundle: {bundle}")
        self.w(f"  counts: {man.get('counts')}")
        try:
            from . import checks

            results = checks.run_checks(bundle)
            nerr = sum(1 for r in results if r.status == "fail" and r.level == "error")
            nwarn = sum(1 for r in results if r.status == "fail" and r.level == "warn")
            self.check_errors = [
                f"{r.check_id}:{r.id}" for r in results if r.status == "fail" and r.level == "error"
            ]
            self.w(f"  checks: {len(results)} rows, {nerr} error(s), {nwarn} warning(s)")
        except Exception as exc:  # the checks API is TI-6's; a mismatch must not fail a save
            self.w(f"  checks not run: {type(exc).__name__}: {_short(str(exc), 100)}")
        if self._cache is not None and self._cache.misses:
            self.w(f"  replay cache misses: {len(self._cache.misses)}")
            for m in self._cache.misses[:10]:
                self.w(f"    {m}")

    def _dedupe_results(self) -> None:
        """A question asked twice keeps its last row, so the bundle has one row per question."""
        for path in (self.d_mem / "results.jsonl", self.d_trace / "reads.jsonl"):
            if not path.exists():
                continue
            keep: dict[str, str] = {}
            order: list[str] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                key = (
                    f"{row.get('query_id')}"
                    if row.get("query_id") is not None and row.get("kind") != "manifest"
                    else f"_{len(order)}"
                )
                if key not in keep:
                    order.append(key)
                keep[key] = line
            path.write_text("\n".join(keep[k] for k in order) + "\n", encoding="utf-8")

    # ---- the loop
    async def execute(self, line: str) -> bool:
        """Run one command line; False = quit."""
        if self._block is not None:  # inside a `say <<` block
            if line.strip() == ".":
                lines, self._block = self._block, None
                await self.cmd_say(lines)
            elif line.strip():
                self._block.append(line)
            return True
        line = line.strip()
        if line.startswith("#"):
            return True
        cmd, _, arg = line.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd in ("", "turn", "t", "n", "next"):
            if arg.isdigit() or not arg:
                await self.cmd_turn(int(arg) if arg else 1)
            else:
                await self.cmd_turns(arg)
        elif cmd == "turns":
            await self.cmd_turns(arg)
        elif cmd == "skip":
            await self.cmd_skip(int(arg) if arg.isdigit() else 1)
        elif cmd == "goto":
            await self.cmd_goto(arg)
        elif cmd == "say":
            if arg.replace(" ", "") == "<<":
                self._block = []
            else:
                await self.cmd_say([arg])
        elif cmd == "interaction":
            await self.cmd_interaction(int(arg) if arg.isdigit() else 1)
        elif cmd == "session":
            await self.cmd_session(arg)
        elif cmd == "sessions":
            await self.cmd_sessions(arg)
        elif cmd == "sleep":
            await self.cmd_sleep()
        elif cmd == "ask":
            await self.cmd_ask(arg)
        elif cmd == "asks":
            await self.cmd_asks(arg)
        elif cmd == "brief":
            self.brief = arg.lower() not in ("off", "0", "no")
            self.w(f"brief {'on' if self.brief else 'off'}: asks print one line per question")
        elif cmd == "status":
            await self.cmd_status()
        elif cmd == "reset":
            await self.cmd_reset()
        elif cmd == "load":
            await self.cmd_load(arg)
        elif cmd in self.TUNE_COMMANDS:
            await self.tune_command(cmd, arg)
        elif cmd == "show":
            await self.cmd_show(arg)
        elif cmd == "links":
            await self.cmd_links(arg)
        elif cmd == "diff":
            await self.cmd_diff()
        elif cmd == "save":
            await self.cmd_save()
        elif cmd in ("quit", "q", "exit"):
            return False
        elif cmd in ("help", "?"):
            self.w(__doc__.split("Commands", 1)[1].split("Models.", 1)[0])
        else:
            self.w(f"unknown command {cmd!r}; try help")
        return True

    async def run(self, script: Iterable[str] | None = None) -> None:
        await self.start()
        try:
            if script is not None:
                for line in script:
                    self.w(f"\nstep> {line.strip()}")
                    if not await self.execute(line):
                        break
            else:
                while True:
                    try:
                        line = await asyncio.get_running_loop().run_in_executor(
                            None, input, "\nstep> "
                        )
                    except EOFError:
                        break
                    if not await self.execute(line):
                        break
        finally:
            await self.close()


def _quiet_logs() -> Any:
    """The engine logs every write at info level; step mode prints the tap events instead.
    Returns a callable that restores the previous structlog setup."""
    import logging

    try:
        import structlog
    except ImportError:  # pragma: no cover
        return lambda: None
    saved = dict(structlog.get_config())
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
    return lambda: structlog.configure(**saved)


# ------------------------------------------------------------------------------------ offline stubs
def _offline_window_reply(messages: Sequence[Mapping[str, Any]]) -> str | None:
    """A deterministic stand-in for the window-extraction call when running offline: one fact per
    transcript line that is long enough and carries a capital or a digit after its first letter.
    None = not a window call (the stub answers it)."""
    import re

    system = str(messages[0].get("content", "")) if messages else ""
    user = str(messages[-1].get("content", "")) if messages else ""
    if not system.startswith("You turn numbered conversation lines into memory items"):
        return None
    # offline tuning hook: a `[stub: min_len=N]` line in an edited system prompt moves the length threshold
    directive = re.search(r"\[stub: min_len=(\d+)\]", system)
    min_len = int(directive.group(1)) if directive else 24
    items = []
    for m in re.finditer(r"^\[(\d+)\] (?:\[[\d-]+\] )?([^:\n]{1,40}): (.+)$", user, re.M):
        n, who, said = int(m.group(1)), m.group(2).strip(), m.group(3).strip()
        if len(said) < min_len or not re.search(r"[A-Z0-9]", said[1:]):
            continue
        items.append(
            {
                "s": who,
                "r": "said",
                "o": said[:60],
                "text": f"{who}: {said}",
                "kind": "state",
                "mode": "asserted",
                "neg": False,
                "when": None,
                "until": None,
                "lines": [n],
                "span": said,
                "speaker": who,
            }
        )
    return json.dumps({"facts": items, "edges": [], "episode": None})


def _as_dataset_json(
    item: str, turns: Sequence[Mapping[str, Any]], questions: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """The demo-shaped json (sessions + questions) that ``load_dataset`` reads back."""
    sessions: dict[str, dict[str, Any]] = OrderedDict()
    for t in turns:
        s = sessions.setdefault(
            t["session"], {"session_id": t["session"], "date": t.get("ts"), "turns": []}
        )
        row = {
            "turn_id": t["turn_id"],
            "speaker": t["speaker"],
            "text": t["text"],
            "ts": t.get("ts"),
        }
        if t.get("custom"):
            row["custom"] = True
        s["turns"].append(row)
    qs = [
        {
            "query_id": qid,
            "category": q.get("category"),
            "question": q.get("question"),
            "gold": q.get("gold"),
            "gold_turns": list(q.get("gold_turns") or []),
            **({"custom": True} if q.get("custom") else {}),
        }
        for qid, q in questions.items()
    ]
    return {"conversation_id": item, "sessions": list(sessions.values()), "questions": qs}


# ------------------------------------------------------------------------------------------------ CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m memspine_evals.inspect step", description=__doc__.split("\n")[0]
    )
    p.add_argument(
        "--data", required=True, help="LoCoMo-shaped json, or 'demo' (the invented fixture)"
    )
    p.add_argument("--item", help="conversation id (default: the first item)")
    p.add_argument(
        "--arm",
        required=True,
        help="evals/arms name (scr-gc3-graph-jina, scr-b8-write-v2) or a json path",
    )
    p.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help="run folder root (gitignored under evals/runs/_analysis/)",
    )
    p.add_argument(
        "--offline", action="store_true", help="hash embedder, no reranker, stub LLM (no network)"
    )
    p.add_argument(
        "--replay-cache", action="store_true", help="serve model calls from a recorded call cache"
    )
    p.add_argument(
        "--cache-dir", help="the call cache directory (llm_cache.sqlite) for --replay-cache"
    )
    p.add_argument(
        "--live", action="store_true", help="send what the cache lacks to the arm's real endpoints"
    )
    p.add_argument("--script", help="run the commands of this file (one per line) and exit")
    p.add_argument(
        "--reader-model", help="answer asked questions with this model (OpenAI-compatible)"
    )
    p.add_argument("--judge-model", help="judge model (default: the reader model)")
    p.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    p.add_argument("--budget", type=int, default=4096)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--pause", action="store_true", help="wait for Enter between printed stages")
    p.add_argument("--wide", action="store_true", help="print full prompts, replies and contexts")
    p.add_argument("--run-id")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ds = load_dataset("demo" if args.data == "demo" else args.data)
    item = args.item or next(iter(ds))
    script = None
    if args.script:
        script = Path(args.script).read_text(encoding="utf-8").splitlines()
    session = StepSession(
        ds,
        item,
        args.arm,
        out=args.out,
        offline=args.offline,
        replay_cache=args.replay_cache,
        cache_dir=args.cache_dir,
        live=args.live,
        reader_model=args.reader_model,
        judge_model=args.judge_model,
        base_url=args.base_url,
        budget=args.budget,
        top_k=args.top_k,
        pause=args.pause,
        wide=args.wide,
        run_id=args.run_id,
    )
    asyncio.run(session.run(script))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
