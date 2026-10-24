"""TS-1/TS-3: the Tuning Studio API, structured results over the Trace Inspector step session.

``StudioAPI`` holds one :class:`~memspine_evals.inspect.step.StepSession` and exposes the tool
contract of ``docs/v2/TUNING_STUDIO_PLAN.md`` as methods named like the tools (``studio_open`` ...).
Every method takes keyword arguments and returns JSON-able data (dicts, or lists for the three list
tools); nothing is parsed from printed text: events come from the session's tap rows (``_take()``)
and its forensics file, grouped by pipeline stage.

Config overrides (``studio_set`` / ``studio_unset`` / prompt overrides) live here: the session is
rebuilt from ``arm + overrides`` and the recorded write log is replayed, so a change to a read-path key
(``read.*``) is visible to the next ``studio_ask`` and a change to a write-path key needs
``studio_reset`` (with ``replay`` to rewrite the same turns).

Dataset text is returned only to the local caller and written only under the gitignored run folder.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import csv
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

EVALS = Path(__file__).resolve().parents[2]
REPO = EVALS.parent
STAGE_REGISTRY = REPO / "docs" / "v2" / "data" / "stage_trace_registry.json"
STAGE_PARAMS = REPO / "docs" / "v2" / "data" / "stage_params.json"
INVENTORY = REPO / "docs" / "v2" / "PIPELINE_STAGE_INVENTORY.md"
DEFAULT_OUT = EVALS / "runs" / "_analysis" / "inspect" / "studio"

#: gold survival funnel, in pipeline order
FUNNEL = ("legs", "fused", "pool", "rerank", "final", "packed")
_DROP = frozenset({"run_id", "item", "query_id", "request", "_seq", "_req"})
MAX_ROWS = 200


class StudioError(Exception):
    """A tool failed in a way the caller can act on (bad argument, no open session, ...)."""

    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


class StudioNotImplemented(StudioError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "not_implemented")


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def _clean(row: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in _DROP and v is not None}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _set_path(cfg: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    node = cfg
    for p in parts[:-1]:
        nxt = node.get(p)
        if nxt is None:
            nxt = node[p] = {}
        if not isinstance(nxt, dict):
            raise StudioError(f"{key}: {p!r} is a value ({type(nxt).__name__}), not a section")
        node = nxt
    node[parts[-1]] = value


def _get_path(cfg: Mapping[str, Any], key: str, default: Any = None) -> Any:
    node: Any = cfg
    for p in key.split("."):
        if not isinstance(node, Mapping) or p not in node:
            return default
        node = node[p]
    return node


def _flatten(cfg: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        key = f"{prefix}{k}"
        if isinstance(v, Mapping) and v:
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out


def _coerce(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if (
            text[:1] in '[{"'
            or text in ("true", "false", "null")
            or re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", text)
        ):
            with contextlib.suppress(ValueError):
                return json.loads(text)
    return value


# ------------------------------------------------------------------------------------------------ the API
class StudioAPI:
    def __init__(
        self,
        data: str | Path | None = "demo",
        out: str | Path | None = None,
        *,
        allow_gpu: bool = False,
        arms_dir: str | Path | None = None,
        logs_dir: str | Path | None = None,
        launcher: Callable[[list[str]], Any] | None = None,
    ) -> None:
        self.default_data = str(data) if data else "demo"
        self.out = Path(out) if out else DEFAULT_OUT
        self.allow_gpu = allow_gpu
        self.arms_dir = Path(arms_dir) if arms_dir else None
        self.logs_dir = (
            Path(logs_dir)
            if logs_dir
            else Path(os.environ.get("SCREEN_LEDGER_DIR") or EVALS / "runs" / "_logs")
        )
        self.launcher = launcher
        self._loop: asyncio.AbstractEventLoop | None = None
        self._log_seq = 0
        # session state
        self.session: Any = None
        self.data_spec = ""
        self.ds: dict[str, dict[str, Any]] = {}
        self.item = ""
        self.arm = ""
        self.mode = "offline"
        self.cache_dir: str | None = None
        self.arm_cfg: dict[str, Any] = {}
        self.wlog: list[dict[str, Any]] = []
        self.custom_q: dict[str, dict[str, Any]] = {}
        self.stale: list[str] = []
        self.last_asks: dict[str, dict[str, Any]] = {}
        self.last_verify: dict[str, Any] | None = None
        self._baselines: dict[str, dict[str, Any]] = {}
        self._sink = io.StringIO()

    @property
    def overrides(self) -> dict[str, Any]:
        return self.session.overrides if self.session is not None else {}

    @property
    def prompt_overrides(self) -> dict[str, Any]:
        return self.session.prompt_overrides if self.session is not None else {}

    # ---- plumbing
    def run(self, coro: Any) -> Any:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(coro)

    @property
    def log_path(self) -> Path:
        return self.out / "tuning.jsonl"

    def _init_log_seq(self) -> None:
        if self._log_seq == 0 and self.log_path.exists():
            self._log_seq = sum(
                1 for line in self.log_path.read_text(encoding="utf-8").splitlines() if line.strip()
            )

    def call(self, tool: str, args: Mapping[str, Any] | None = None) -> tuple[Any, int]:
        """Dispatch ``tool`` with ``args``; returns ``(result, log_id)`` and appends a tuning.jsonl row."""
        self._init_log_seq()
        self._log_seq += 1
        log_id = self._log_seq
        args = dict(args or {})
        t0 = time.perf_counter()
        error: str | None = None
        result: Any = None
        try:
            fn = getattr(self, tool, None)
            if fn is None or not tool.startswith("studio_"):
                raise StudioError(f"unknown tool {tool!r}", "unknown_tool")
            try:
                result = fn(**args)
            except TypeError as exc:
                if "argument" in str(exc):
                    raise StudioError(f"bad arguments for {tool}: {exc}", "bad_arguments") from exc
                raise
        except StudioError as exc:
            error = f"{exc.code}: {exc}"
            self._append_log(log_id, tool, args, t0, None, error)
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._append_log(log_id, tool, args, t0, None, error)
            raise
        self._append_log(log_id, tool, args, t0, result, None)
        return result, log_id

    def _append_log(
        self,
        log_id: int,
        tool: str,
        args: Mapping[str, Any],
        t0: float,
        result: Any,
        error: str | None,
    ) -> None:
        brief: Any = None
        if isinstance(result, dict):
            brief = {
                k: (
                    v
                    if isinstance(v, str | int | float | bool) and len(str(v)) < 120
                    else f"<{type(v).__name__}>"
                )
                for k, v in result.items()
                if k
                in (
                    "ok",
                    "applied",
                    "passed",
                    "queued",
                    "refused",
                    "reason",
                    "mode",
                    "path",
                    "error",
                    "summary",
                    "counts",
                )
            }
        row = {
            "log_id": log_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": tool,
            "args": _jsonable(args),
            "ok": error is None,
            "ms": round((time.perf_counter() - t0) * 1000),
            "result": _jsonable(brief),
            "error": error,
            "session": self.session.run_id if self.session is not None else None,
        }
        try:
            self.out.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:  # a read-only folder must not break a tool
            pass

    def close(self) -> None:
        if self.session is not None:
            with contextlib.suppress(Exception):
                self.run(self.session.close())
            self.session = None
        if self._loop is not None:
            with contextlib.suppress(Exception):
                self._loop.close()
            self._loop = None

    # ---- helpers
    def _need(self) -> Any:
        if self.session is None:
            raise StudioError("no open session: call studio_open first", "no_session")
        return self.session

    def _load_data(
        self, data: str | Path | Mapping[str, Any] | None
    ) -> tuple[str, dict[str, dict[str, Any]]]:
        from ..inspect.assemble import load_dataset

        spec = data if data not in (None, "") else self.default_data
        if isinstance(spec, Mapping):
            return "inline", load_dataset(dict(spec))
        spec = str(spec)
        if spec == "demo":
            return "demo", load_dataset("demo")
        if spec == "locomo":
            for cand in (
                os.environ.get("MEMSPINE_LOCOMO"),
                EVALS / "data" / "locomo10.json",
                EVALS / "data" / "locomo" / "locomo10.json",
                REPO / "data" / "locomo10.json",
            ):
                if cand and Path(cand).is_file():
                    return str(cand), load_dataset(str(cand))
            raise StudioError(
                "locomo data not found: pass data=<path to locomo10.json> or set MEMSPINE_LOCOMO"
            )
        if not Path(spec).is_file():
            raise StudioError(f"data not found: {spec}")
        return spec, load_dataset(spec)

    async def _start_session(
        self, replay: bool, carry: tuple[Any, Any, Any] | None = None, overrides_off: bool = False
    ) -> None:
        """A fresh StepSession on the open item. ``carry`` = (overrides, prompt_overrides, base dump) of the session it replaces."""
        from ..inspect import step

        if carry is None and self.session is not None:
            s0 = self.session
            carry = (dict(s0.overrides), copy.deepcopy(s0.prompt_overrides), s0._base_dump)
        if self.session is not None:
            await self.session.close()
            self.session = None
        kw: dict[str, Any] = {}
        if self.mode == "offline":
            kw["offline"] = True
        elif self.mode == "replay":
            if not self.cache_dir:
                raise StudioError("mode=replay needs cache_dir (the recorded call cache)")
            kw.update(replay_cache=True, cache_dir=self.cache_dir)
        elif self.mode == "live":
            kw["live"] = True
            if self.cache_dir:
                kw["cache_dir"] = self.cache_dir
        else:
            raise StudioError(f"mode must be offline, replay or live (got {self.mode!r})")
        self._sink = io.StringIO()
        s = step.StepSession(
            self.ds,
            self.item,
            self.arm,
            out=self.out,
            stream=self._sink,
            config_override=copy.deepcopy(self.arm_cfg),
            run_id=f"studio-{re.sub(r'[^A-Za-z0-9_.-]', '_', self.arm)}-{self.item}",
            **kw,
        )
        s.arms_dir = self.arms_dir
        s.questions.update(copy.deepcopy(self.custom_q))
        if carry is not None and not overrides_off:
            s.overrides, s.prompt_overrides, s._base_dump = (
                dict(carry[0]),
                copy.deepcopy(carry[1]),
                carry[2],
            )
            s.cfg = s._compose_cfg()
        await s.start()
        self.session = s
        self._sink.seek(0)
        self._sink.truncate()
        if replay:
            log, self.wlog = self.wlog, []
            for op in log:
                await self._apply_op(op)
            self.wlog = log

    # ---- status
    def _gpu_state(self) -> dict[str, Any]:
        reserved = (self.logs_dir / "GPU_RESERVED").exists()
        logs = (
            sorted(self.logs_dir.glob("v2_*.log"), key=lambda p: p.stat().st_mtime)
            if self.logs_dir.is_dir()
            else []
        )
        latest = logs[-1] if logs else None
        return {
            "allow_gpu": self.allow_gpu,
            "reserved": reserved,
            "latest_log": latest.name if latest else None,
            "latest_log_age_s": round(time.time() - latest.stat().st_mtime) if latest else None,
        }

    async def _counts(self) -> dict[str, Any]:
        s = self.session
        recs = len(await s._records())
        wsess = list(OrderedDict.fromkeys(x["session"] for x in s.written))
        windows = {
            r.get("window_id")
            for r in s.sink.rows
            if r["event"] == "write.window.members" and r.get("window_id")
        }
        return {
            "turns_total": len(s.turns),
            "turns_written": len(s.written),
            "turns_custom": sum(1 for x in s.written if x.get("custom")),
            "sessions_total": len(s._session_ids()),
            "sessions_written": len(wsess),
            "sessions_closed": len(s.closed_sessions),
            "windows_open": len([x for x in wsess if x not in s.closed_sessions]),
            "windows_formed": len(windows),
            "records": recs,
            "questions": len(s.questions),
            "slept": bool(s.slept),
        }

    def studio_status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "open": self.session is not None,
            "session": self.session.run_id if self.session else None,
            "arm": self.arm or None,
            "data": self.data_spec or None,
            "item": self.item or None,
            "mode": self.mode if self.session else None,
            "overrides": dict(self.overrides),
            "prompt_overrides": sorted(self.prompt_overrides),
            "stale": list(self.stale),
            "last_verify": self.last_verify,
            "gpu": self._gpu_state(),
            "out": str(self.out),
        }
        if self.session is not None:
            s = self.session
            nxt = next((t for t in s.turns if t["turn_id"] not in s.written_ids), None)
            out["cursor"] = {
                "next_turn_id": nxt["turn_id"] if nxt else None,
                "session": nxt["session"] if nxt else None,
            }
            out["counts"] = self.run(self._counts())
            out["notes"] = list(s.notes)
            out["questions"] = [
                {"qid": q, "category": v.get("category"), "question": v.get("question")}
                for q, v in s.questions.items()
            ]
        return out

    # ---- session lifecycle
    def studio_open(
        self,
        data: Any = None,
        item: str | None = None,
        arm: str | None = None,
        mode: str = "offline",
        cache_dir: str | None = None,
    ) -> dict[str, Any]:
        spec, ds = self._load_data(data)
        if not ds:
            raise StudioError("the dataset has no items")
        item = item or next(iter(ds))
        if item not in ds:
            raise StudioError(f"item {item!r} not in dataset (items: {', '.join(list(ds)[:8])})")
        from ..inspect.step import resolve_arm

        arm = arm or ("demo" if spec == "demo" else "")
        if not arm:
            raise StudioError("arm is required (an evals/arms name or a json path)")
        if self.arms_dir is not None and not Path(arm).exists() and arm != "demo":
            cand = self.arms_dir / (arm if arm.endswith(".json") else arm + ".json")
            arm = str(cand) if cand.exists() else arm
        name, cfg = resolve_arm(arm)
        self.data_spec, self.ds, self.item, self.arm, self.mode = spec, ds, item, name, mode
        self.cache_dir = cache_dir
        self.arm_cfg = cfg
        self.wlog, self.stale, self.last_asks, self.last_verify, self._baselines, self.custom_q = (
            [],
            [],
            {},
            None,
            {},
            {},
        )
        self.run(self._start_session(replay=False, carry=({}, {}, None)))
        return self.studio_status()

    def studio_reset(self, keep_overrides: bool = False, replay: bool = False) -> dict[str, Any]:
        s = self._need()
        if not keep_overrides:
            s.overrides.clear()
            s.prompt_overrides.clear()
            s.cfg = s._compose_cfg()
        self.run(s.cmd_reset())
        s.questions.update(copy.deepcopy(self.custom_q))
        self.stale, self.last_asks, self.last_verify = [], {}, None
        if replay:
            self.run(self._replay())
        else:
            self.wlog = []
        return self.studio_status()

    def studio_load_conversation(self, json: Any = None, **kw: Any) -> dict[str, Any]:
        import json as _json

        conv = json if json is not None else kw.get("conversation")
        if isinstance(conv, str):
            conv = _json.loads(conv)
        if not isinstance(conv, Mapping):
            raise StudioError(
                "json must be a conversation object in the demo shape (conversation_id, sessions, questions)"
            )
        s = self._need()
        _, ds = self._load_data(dict(conv))
        path = self.out / "inline_conversation.json"
        self.out.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(conv, ensure_ascii=False), encoding="utf-8")
        self.run(s.cmd_load(str(path)))
        self.data_spec, self.ds, self.item = "inline", ds, s.item
        self.wlog, self.stale, self.last_asks, self.last_verify, self._baselines, self.custom_q = (
            [],
            [],
            {},
            None,
            {},
            {},
        )
        return self.studio_status()

    async def _replay(self) -> None:
        log, self.wlog = self.wlog, []
        for op in log:
            await self._apply_op(op)
        self.wlog = log

    # ---- stages and parameters
    def _inventory_rows(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if INVENTORY.is_file():
            for line in INVENTORY.read_text(encoding="utf-8").splitlines():
                m = re.match(r"^\|\s*([ABC]\d+[a-z]?)\s*\|[^|]*\|\s*([^|]*)\|", line)
                if m:
                    out[m.group(1)] = m.group(2).strip().strip("`").strip()
        return out

    def _param_catalogue(self) -> dict[str, dict[str, Any]]:
        if not STAGE_PARAMS.is_file():
            return {}
        raw = json.loads(STAGE_PARAMS.read_text(encoding="utf-8"))
        stages = raw.get("stages", raw) if isinstance(raw, dict) else raw
        if isinstance(stages, list):
            return {
                str(s.get("stage_id") or s.get("stage")): s for s in stages if isinstance(s, dict)
            }
        return {str(k): v for k, v in stages.items() if isinstance(v, dict)}

    def studio_stages(self) -> list[dict[str, Any]]:
        if not STAGE_REGISTRY.is_file():
            raise StudioError(f"stage registry not found: {STAGE_REGISTRY}")
        reg = json.loads(STAGE_REGISTRY.read_text(encoding="utf-8"))["stages"]
        inv, cat = self._inventory_rows(), self._param_catalogue()
        path = {"A": "write", "B": "sleep", "C": "read"}
        out = []
        for sid, st in reg.items():
            keys = (cat.get(sid) or {}).get("keys") or []
            out.append(
                {
                    "stage_id": sid,
                    "name": st.get("phase", ""),
                    "path": path.get(sid[:1], ""),
                    "file_function": inv.get(sid),
                    "events": list(st.get("events") or []),
                    "gap": bool(st.get("gap")),
                    "gap_ref": st.get("gap_ref"),
                    "n_params": len(keys),
                }
            )
        return out

    def studio_params(self, stage: str) -> dict[str, Any]:
        reg = (
            json.loads(STAGE_REGISTRY.read_text(encoding="utf-8"))["stages"]
            if STAGE_REGISTRY.is_file()
            else {}
        )
        if stage not in reg:
            raise StudioError(f"unknown stage {stage!r}")
        entry = self._param_catalogue().get(stage) or {}
        eff = self.session._effective() if self.session is not None else {}
        keys = []
        for k in entry.get("keys") or []:
            key = k.get("key") or ""
            row = {
                "key": key,
                "value": _get_path(eff, key, k.get("default"))
                if not key.startswith("cli:")
                else k.get("default"),
                "default": k.get("default"),
                "type": k.get("type"),
                "description": k.get("description"),
                "effect": k.get("effect"),
                "rebuild": bool(k.get("rebuild")),
                "rebuild_scope": k.get("rebuild_scope") or None,
                "screens": list(k.get("screen_ids") or []),
                "overridden": key in self.overrides,
                "settable": not key.startswith("cli:"),
            }
            for extra in ("choices", "range"):
                if extra in k:
                    row[extra] = k[extra]
            keys.append(row)
        out = {
            "stage_id": stage,
            "name": reg[stage].get("phase", ""),
            "file_function": entry.get("file_function"),
            "keys": keys,
            "prompts": entry.get("prompts") or [],
            "models": entry.get("models") or [],
        }
        if not entry:
            out["note"] = (
                "no parameter catalogue for this stage (docs/v2/data/stage_params.json is built by TI-8)"
            )
        return out

    # ---- config and overrides
    def _key_catalogue(self) -> dict[str, dict[str, Any]]:
        return {
            k["key"]: k
            for s in self._param_catalogue().values()
            for k in (s.get("keys") or [])
            if k.get("key")
        }

    def studio_config(self, prefix: str | None = None, diff: bool = False) -> list[dict[str, Any]]:
        from ..inspect.step_tune import flatten

        s = self._need()
        eff, dflt = flatten(s._effective()), flatten(s._defaults())
        rows = []
        for key in sorted(eff):
            if prefix and not key.startswith(prefix):
                continue
            value, default = eff[key], dflt.get(key)
            if diff and value == default:
                continue
            rows.append(
                {
                    "key": key,
                    "value": value,
                    "default": default,
                    "source": s._source(key, value, default),
                }
            )
        return _jsonable(rows)

    #: read-path sections the session rebinds on every set (scoring / assembly / compression policy objects)
    PER_CALL = ("read.assembly.", "read.scoring.", "read.compression.")

    def _classify(self, key: str, value: Any) -> tuple[str, str]:
        """(applied, note) after a set the session accepted. live_verified only when the running engine's config
        shows the new value AND the key is one the session rebinds per call; everything else accepted without a
        rebuild is live_unverified (it may be bound at engine start)."""
        s = self.session
        if s._needs_rebuild(key):
            return (
                "needs_reset",
                "changes what is written or indexed: run studio_reset (keep_overrides, replay) to rebuild",
            )
        seen = _get_path(s._effective(), key, "__absent__")
        shows = json.dumps(seen, sort_keys=True, default=str) == json.dumps(
            value, sort_keys=True, default=str
        )
        if shows and key.startswith(self.PER_CALL):
            return (
                "live_verified",
                "the engine's live config shows the value and the policy object was rebound",
            )
        why = (
            "the engine config shows the value"
            if shows
            else "the engine config does not show the value yet"
        )
        return (
            "live_unverified",
            f"{why}, but this key may be bound at engine start: confirm with studio_rerun_ask (or pass probe=true), else studio_reset",
        )

    def _probe(self, key: str) -> dict[str, Any] | None:
        """Re-ask the last asked question and compare with its previous answer: a change is proof the key is live."""
        if not self.last_asks:
            return None
        qid = next(reversed(self.last_asks))
        before = self.last_asks[qid]
        after = self.run(self._ask_one(qid, "full"))
        self.last_asks[qid] = after
        diff = self._diff_asks(before, after)
        return {"qid": qid, "changed": diff["changed"], "diff": _jsonable(diff)}

    def studio_set(self, key: str, value: Any, probe: bool = False) -> dict[str, Any]:
        s = self._need()
        if not isinstance(key, str) or not key or key.startswith(".") or key.endswith("."):
            raise StudioError(f"bad key {key!r}")
        if key.startswith("cli:"):
            return {
                "ok": False,
                "applied": None,
                "old": None,
                "new": value,
                "error": "cli: entries are run flags, not config keys",
            }
        value = _coerce(value)
        old = _get_path(s._effective(), key)
        spec = self._key_catalogue().get(key)
        if spec:
            err = self._validate(spec, value)
            if err:
                return {"ok": False, "applied": None, "old": old, "new": value, "error": err}
        _, err = s._validated(key, value)
        if err:
            return {"ok": False, "applied": None, "old": old, "new": value, "error": err}
        if not self.run(s._set_value(key, value, command="set", quiet=True)):
            return {
                "ok": False,
                "applied": None,
                "old": old,
                "new": value,
                "error": "refused by the session",
            }
        applied, note = self._classify(key, value)
        out: dict[str, Any] = {
            "ok": True,
            "applied": applied,
            "old": old,
            "new": value,
            "note": note,
        }
        if applied == "needs_reset":
            if key not in self.stale:
                self.stale.append(key)
        elif probe:
            pr = self._probe(key)
            out["probe"] = pr
            if pr is None:
                out["note"] += " (probe skipped: ask a question first)"
            elif pr["changed"]:
                out["applied"] = "live_verified"
                out["note"] = "an earlier answer changed after this set: the key is live"
            else:
                out["note"] += (
                    " (probe: the last question did not change; the key may not matter for it)"
                )
        if self.last_verify is not None:
            self.last_verify = {**self.last_verify, "stale": True}
        return _jsonable(out)

    @staticmethod
    def _validate(spec: Mapping[str, Any], value: Any) -> str | None:
        choices = spec.get("choices")
        vals = value if isinstance(value, list) else [value]
        if choices and any(v not in choices for v in vals):
            return f"value {value!r} not in choices {choices}"
        rng = spec.get("range")
        if (
            isinstance(rng, Mapping)
            and isinstance(value, int | float)
            and not isinstance(value, bool)
        ):
            for op, ok in (
                ("ge", lambda v, b: v >= b),
                ("gt", lambda v, b: v > b),
                ("le", lambda v, b: v <= b),
                ("lt", lambda v, b: v < b),
            ):
                if op in rng and not ok(value, rng[op]):
                    return f"value {value!r} violates {op} {rng[op]}"
        typ = str(spec.get("type") or "")
        base = {p.strip() for p in typ.replace("Optional[", "").split("|")}
        if value is None:
            return None if ("None" in base or "Optional" in typ) else f"{typ} does not accept null"
        simple = {"bool": bool, "int": int, "float": (int, float), "str": str}
        if base and base <= set(simple) | {"None"}:
            allowed = tuple(
                t
                for b in base
                if b in simple
                for t in (simple[b] if isinstance(simple[b], tuple) else (simple[b],))
            )
            if isinstance(value, bool) and bool not in allowed:
                return f"expected {typ}, got bool"
            if not isinstance(value, allowed):
                return f"expected {typ}, got {type(value).__name__}"
        return None

    def studio_unset(self, key: str) -> dict[str, Any]:
        s = self._need()
        if key not in s.overrides:
            cur = _get_path(s._effective(), key)
            return {"ok": True, "applied": "none", "old": cur, "new": cur, "error": None}
        old = s.overrides[key]
        self.run(s.cmd_unset(key))
        new = _get_path(s._base_dump or {}, key)
        applied, note = self._classify(key, new)
        if applied == "needs_reset" and key not in self.stale:
            self.stale.append(key)
        if self.last_verify is not None:
            self.last_verify = {**self.last_verify, "stale": True}
        return _jsonable({"ok": True, "applied": applied, "old": old, "new": new, "note": note})

    # ---- prompts
    def _prompt_kind(self, pid: str) -> str:
        from ..inspect.step_tune import CODE_PROMPTS, READER_PROMPT

        return "code" if pid in CODE_PROMPTS else "reader" if pid == READER_PROMPT else "registry"

    def _shipped_text(self, pid: str) -> tuple[str, str | None]:
        """(text as shipped, system) for any prompt id."""
        s = self.session
        kind = self._prompt_kind(pid)
        if kind == "code":
            return s._code_prompt_text(pid), None
        if kind == "reader":
            return s._reader_prompt(), None
        p = s._registry().get(pid)
        sh = s._shipped.get(pid)
        return (sh["body"] if sh else p.body), (sh["system"] if sh else p.system)

    def studio_prompts(self) -> list[dict[str, Any]]:
        s = self._need()
        return [
            {
                "prompt_id": pid,
                "role": role,
                "version": ver,
                "overridden": pid in s.prompt_overrides,
            }
            for pid, role, ver in s._prompt_ids()
        ]

    def studio_prompt_show(self, prompt_id: str, rendered: bool = False) -> dict[str, Any]:
        s = self._need()
        if not s._known_prompt(prompt_id):
            raise StudioError(f"unknown prompt {prompt_id!r}")
        kind = self._prompt_kind(prompt_id)
        ed = s.prompt_overrides.get(prompt_id)
        body, system = self._shipped_text(prompt_id)
        if ed:
            if kind == "registry":
                body, system = ed.get("body", body), ed.get("system", system)
            else:
                body = ed.get("system") or ed.get("body") or body
        row = next((r for r in s._prompt_ids() if r[0] == prompt_id), (prompt_id, kind, ""))
        out: dict[str, Any] = {
            "prompt_id": prompt_id,
            "role": row[1],
            "version": row[2],
            "kind": kind,
            "system": system,
            "text": body,
            "overridden": bool(ed),
        }
        if rendered:
            seen = [
                r for r in s.sink.rows if r["event"] == "write.extract.io" and r.get("prompt_text")
            ]
            out["rendered_example"] = (
                seen[-1]["prompt_text"]
                if seen and prompt_id in ("structured.window", "window_extract", "structured")
                else None
            )
        return out

    def studio_prompt_set(self, prompt_id: str, text: str) -> dict[str, Any]:
        s = self._need()
        if not s._known_prompt(prompt_id):
            return {"ok": False, "diff": "", "error": f"unknown prompt {prompt_id!r}"}
        kind = self._prompt_kind(prompt_id)
        old_body, _ = self._shipped_text(prompt_id)
        prev = s.prompt_overrides.get(prompt_id)
        if kind == "code":
            new = {"system": text.rstrip("\n")}
        elif kind == "reader":
            new = {"body": text.rstrip("\n")}
        else:
            new = s._parse_registry_edit(text)
        s.prompt_overrides[prompt_id] = new
        try:
            s._apply_prompt_overrides_live()
        except Exception as exc:
            if prev is None:
                s.prompt_overrides.pop(prompt_id, None)
            else:
                s.prompt_overrides[prompt_id] = prev
            return {"ok": False, "diff": "", "error": f"{type(exc).__name__}: {exc}"}
        s._log_tuning(
            "prompt edit",
            prompt_id,
            "(edited)" if prev else "(shipped)",
            f"{len(text)} chars",
            "applied to the running engine",
        )
        diff = "".join(
            difflib.unified_diff(
                old_body.splitlines(True), text.splitlines(True), "shipped", "override"
            )
        )
        if self.last_verify is not None:
            self.last_verify = {**self.last_verify, "stale": True}
        return {
            "ok": True,
            "diff": diff,
            "applied": "live_verified",
            "note": "used by the next model call of this role; records already written keep their old extraction (studio_rerun_window shows the effect)",
        }

    def studio_prompt_reset(self, prompt_id: str) -> dict[str, Any]:
        s = self._need()
        had = prompt_id in s.prompt_overrides
        if had:
            self.run(s._prompt_reset(prompt_id))
            if self.last_verify is not None:
                self.last_verify = {**self.last_verify, "stale": True}
        return {"ok": True, "was_overridden": had}

    # ---- write path
    def _group_write(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        from ..inspect import step

        sec = {name: pre for name, pre in step.WRITE_SECTIONS}
        names = list(sec)
        stamp, rec_idx, win_ext, env, dep, links = (sec[n] for n in names)
        extract_pre = ("write.prompt.", "prompt.render", "write.extract.call", "write.extract.io")
        derived = (
            "write.firewall.",
            "write.route",
            "write.deposit",
            "write.rejected",
            "write.quarantine",
        )
        out: dict[str, Any] = {
            "stamp": [],
            "record": [],
            "index": [],
            "window": [],
            "extract": [],
            "envelopes": [],
            "deposits": [],
            "links": [],
            "other": [],
        }
        idx_by_rid: dict[str, dict[str, Any]] = {}
        for r in rows:
            ev = r["event"]
            if ev.startswith(("model.call", "economy.")):
                continue
            c = _clean(r)
            if r.get("window_id") and ev.startswith(derived):
                out["deposits"].append(c)
            elif ev == "store.index.record":
                idx_by_rid[str(r.get("record_id") or r.get("rid"))] = c
            elif step._matches(ev, stamp):
                out["stamp"].append(c)
            elif step._matches(ev, extract_pre):
                out["extract"].append(c)
            elif step._matches(ev, win_ext):
                out["window"].append(c)
            elif step._matches(ev, env):
                out["envelopes"].append(c)
            elif step._matches(ev, dep):
                out["deposits"].append(c)
            elif step._matches(ev, links):
                out["links"].append(c)
            else:
                out["other"].append(c)
        for rid, c in idx_by_rid.items():
            out["record"].append({"rid": rid, "turn_id": c.get("turn_id")})
            out["index"].append(c)
        for k, v in out.items():
            if len(v) > MAX_ROWS:
                out[k] = v[:MAX_ROWS] + [{"event": "truncated", "n": len(v) - MAX_ROWS}]
        return _jsonable(out)

    async def _write_one(self, t: Mapping[str, Any], custom: bool) -> dict[str, Any]:
        s = self.session
        r = await s._write_turn(dict(t), "say" if custom else "turn")
        groups = self._group_write(r["rows"])
        return {
            "turn_id": t["turn_id"],
            "custom": custom,
            "session": t["session"],
            "speaker": t["speaker"],
            "text": t["text"],
            "records": r["records"],
            "record_ids": [x["rid"] for x in groups["record"]],
            "model_calls": r["calls"],
            "stages": groups,
        }

    def _pick_turns(self, spec: Mapping[str, Any]) -> list[dict[str, Any]]:
        s = self.session
        by_id = {t["turn_id"]: t for t in s.turns}
        done = s.written_ids
        mode = spec.get("mode")
        if mode == "turns":
            if spec.get("ids"):
                ids = [str(i) for i in spec["ids"]]
            elif spec.get("range") is not None:
                rng = spec["range"]
                if isinstance(rng, str) and ".." in rng:
                    a, b = rng.split("..", 1)
                    rng = [a, b]
                if isinstance(rng, str) and re.fullmatch(r"\d+-\d+", rng):
                    a, b = (int(x) for x in rng.split("-"))
                    return [t for t in s.turns[a - 1 : b] if t["turn_id"] not in done]
                if isinstance(rng, Mapping):
                    rng = [rng.get("from"), rng.get("to")]
                a, b = rng
                order = [t["turn_id"] for t in s.turns]
                ia = a - 1 if isinstance(a, int) else order.index(str(a))
                ib = b if isinstance(b, int) else order.index(str(b)) + 1
                ids = order[ia:ib]
            else:
                n = int(spec.get("n") or 1)
                return [t for t in s.turns if t["turn_id"] not in done][:n]
            missing = [i for i in ids if i not in by_id]
            if missing:
                raise StudioError(f"unknown turn id(s): {missing[:5]}")
            return [by_id[i] for i in ids if i not in done]
        if mode == "interaction":
            n = int(spec.get("n") or 1)
            return [t for t in s.turns if t["turn_id"] not in done][: 2 * n]
        raise StudioError(f"unknown write mode {mode!r} (say | turns | interaction | session)")

    async def _apply_op(self, op: dict[str, Any]) -> dict[str, Any]:
        s = self.session
        kind = op["op"]
        if kind == "say":
            turns, resolved = [], []
            for m in op["messages"]:
                tid = m.get("turn_id")
                if not tid:
                    s._custom_n += 1
                    tid = f"U{s._custom_n}"
                    while tid in s.written_ids or s._index_of(tid) is not None:
                        s._custom_n += 1
                        tid = f"U{s._custom_n}"
                sid = m.get("session") or (
                    s.written[-1]["session"]
                    if s.written
                    else (s.turns[0]["session"] if s.turns else "custom")
                )
                t = {
                    "item": self.item,
                    "session": sid,
                    "turn_id": tid,
                    "speaker": str(m.get("speaker") or "user"),
                    "ts": m.get("ts") or s._default_ts(),
                    "text": str(m["text"]),
                    "custom": True,
                }
                resolved.append(
                    {
                        "speaker": t["speaker"],
                        "text": t["text"],
                        "ts": t["ts"],
                        "session": sid,
                        "turn_id": tid,
                    }
                )
                turns.append(await self._write_one(t, True))
            op["messages"] = resolved  # a replay writes the same ids
            return {"turns": turns, "summary": self._wsummary(turns)}
        if kind == "turns":
            turns = [await self._write_one(t, bool(t.get("custom"))) for t in op["turns"]]
            return {"turns": turns, "summary": self._wsummary(turns)}
        if kind == "session":
            sid = op["session"]
            todo = [t for t in s.turns if t["session"] == sid and t["turn_id"] not in s.written_ids]
            res = await s._write_session(sid, [t for t in s.turns if t["session"] == sid])
            ids = {t["turn_id"] for t in todo}
            by_turn: dict[str, list[dict[str, Any]]] = {}
            tail = []
            for row in res["rows"]:
                (
                    by_turn.setdefault(row.get("turn_id"), [])
                    if row.get("turn_id") in ids
                    else tail
                ).append(row)
            turns = []
            for t in todo:
                groups = self._group_write(by_turn.get(t["turn_id"], []))
                turns.append(
                    {
                        "turn_id": t["turn_id"],
                        "custom": False,
                        "session": sid,
                        "speaker": t["speaker"],
                        "text": t["text"],
                        "records": len(groups["record"]),
                        "record_ids": [x["rid"] for x in groups["record"]],
                        "model_calls": None,
                        "stages": groups,
                    }
                )
            summary = self._wsummary(turns)
            summary.update({"session": sid, "session_close": self._group_write(tail)})
            return {"turns": turns, "summary": summary}
        if kind == "sleep":
            return await self._do_sleep()
        raise StudioError(f"bad op {kind!r}")

    @staticmethod
    def _wsummary(turns: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "turns": len(turns),
            "records": sum(t["records"] for t in turns),
            "model_calls": sum(t["model_calls"] or 0 for t in turns),
        }

    def studio_write(
        self,
        mode: str,
        messages: list[Mapping[str, Any]] | None = None,
        ids: list[str] | None = None,
        range: Any = None,
        n: int | None = None,
        session: Any = None,
    ) -> dict[str, Any]:
        s = self._need()
        spec = {"mode": mode, "ids": ids, "range": range, "n": n}
        if mode == "say":
            if not messages:
                raise StudioError("mode=say needs messages[{speaker, text, ts?, session?}]")
            op = {"op": "say", "messages": [dict(m) for m in messages]}
            res = self.run(self._apply_op(op))
            self.wlog.append(op)
        elif mode in ("turns", "interaction"):
            picked = self._pick_turns(spec)
            if not picked:
                raise StudioError("no turns left to write for that selection")
            op = {"op": "turns", "turns": picked}
            res = self.run(self._apply_op(op))
            self.wlog.append(op)
        elif mode == "session":
            sessions = s._session_ids()
            if ids:
                wanted = [str(i) for i in ids]
            elif isinstance(session, str) and session in sessions:
                wanted = [session]
            else:
                k = int(session if isinstance(session, int) else (n or 1))
                nxt = next(
                    (t["session"] for t in s.turns if t["turn_id"] not in s.written_ids), None
                )
                if nxt is None:
                    raise StudioError("no turns left to write")
                wanted = sessions[sessions.index(nxt) : sessions.index(nxt) + max(k, 1)]
            unknown = [w for w in wanted if w not in sessions]
            if unknown:
                raise StudioError(f"unknown session id(s): {unknown} (sessions: {sessions})")
            res = {
                "turns": [],
                "summary": {"turns": 0, "records": 0, "model_calls": 0, "sessions": []},
            }
            for sid in wanted:
                op = {"op": "session", "session": sid}
                part = self.run(self._apply_op(op))
                self.wlog.append(op)
                res["turns"] += part["turns"]
                for k2 in ("turns", "records", "model_calls"):
                    res["summary"][k2] += part["summary"][k2]
                res["summary"]["sessions"].append(
                    {"session": sid, "close": part["summary"].get("session_close")}
                )
        else:
            raise StudioError(f"unknown write mode {mode!r} (say | turns | interaction | session)")
        res["summary"]["replay_cache_misses"] = (
            len(s._cache.misses) if getattr(s, "_cache", None) is not None else 0
        )
        if self.last_verify is not None:
            self.last_verify = {**self.last_verify, "stale": True}
        return _jsonable(res)

    async def _do_sleep(self) -> dict[str, Any]:
        s = self.session
        shutil.rmtree(s.d_fx / "inspect", ignore_errors=True)
        dep = await s.system.build()
        s.slept = True
        rows = s._take()
        pre_rows = [
            r
            for r in rows
            if not r["event"].startswith(
                ("job.", "economy.", "model.", "graph.compile", "derive.graph", "trust.")
            )
        ]
        stages: list[dict[str, Any]] = []
        cur: dict[str, Any] | None = None
        for r in rows:
            ev = r["event"]
            if ev == "job.stage.start":
                cur = {
                    "stage": r.get("stage"),
                    "items": [],
                    "end": None,
                    "events": [],
                    "start": _clean(r),
                }
                stages.append(cur)
            elif ev == "job.stage.item" and cur is not None:
                cur["items"].append(_clean(r))
            elif ev == "job.stage.end":
                if cur is not None:
                    cur["end"] = _clean(r)
                cur = None
            elif ev.startswith(("graph.compile", "derive.graph", "trust.")) and cur is not None:
                cur["events"].append(_clean(r))
        return _jsonable(
            {
                "stages": stages,
                "pre": self._group_write(pre_rows),
                "model_calls": dep.model_calls,
                "stats": (dep.meta or {}).get("sleep") or {},
            }
        )

    def studio_sleep(self) -> dict[str, Any]:
        self._need()
        res = self.run(self._do_sleep())
        self.wlog.append({"op": "sleep"})
        return res

    # ---- read path
    def _origin(self) -> Mapping[str, str]:
        return getattr(self.session.system, "_origin", {}) or {}

    def _items(
        self, items: Any, gold: Sequence[str], limit: int | None = None
    ) -> list[dict[str, Any]]:
        org = self._origin()
        out = []
        for it in list(items or [])[:limit]:
            if not isinstance(it, Mapping):
                it = {"id": it}
            raw = it.get("turn") or it.get("id")
            turn = org.get(str(raw), raw)
            row = {k: v for k, v in it.items() if k not in ("turn", "id")}
            row.update({"turn": turn, "id": raw, "gold": turn in gold})
            out.append(row)
        return out

    def _question(self, spec: Any) -> tuple[str, dict[str, Any]]:
        s = self.session
        if isinstance(spec, Mapping):
            text = spec.get("text") or spec.get("question")
            if spec.get("qid") and spec["qid"] in s.questions:
                return spec["qid"], s.questions[spec["qid"]]
            if not text:
                raise StudioError("a question object needs text")
            s._adhoc += 1
            qid = f"adhoc-{s._adhoc}"
            while qid in s.questions:
                s._adhoc += 1
                qid = f"adhoc-{s._adhoc}"
            q = {
                "question": text,
                "gold": spec.get("gold"),
                "category": spec.get("category", "adhoc"),
                "custom": True,
                "gold_turns": [str(g) for g in spec.get("turns") or spec.get("gold_turns") or []],
            }
            s.questions[qid] = q
        else:
            qid, q = s._find_question(str(spec))
        if q.get("custom"):
            self.custom_q[qid] = q
        return qid, q

    def _expand(self, spec: Any) -> list[Any]:
        """A question set: qids / texts / {text, gold, turns} in a list, or one string: a qid, ``all``, ``cat:<n>``
        (LoCoMo category n), ``<category>[:count]``, or a slice name (a json in evals/analysis or a path)."""
        s = self.session
        qs = s.questions
        if spec in (None, "", "all"):
            return list(qs)
        if isinstance(spec, Mapping):
            return [spec]
        if isinstance(spec, list | tuple):
            return [x for x in spec]
        if not isinstance(spec, str):
            raise StudioError(
                "questions must be a list, a qid, a slice name, 'all', 'cat:<n>' or '<category>[:n]'"
            )
        if spec in qs:
            return [spec]
        low = spec.strip().lower()
        if low.startswith("cat:"):
            want = low[4:].strip()
            found = [
                k
                for k, q in qs.items()
                if str(q.get("category")).lower() in (want, f"cat{want}", want.removeprefix("cat"))
            ]
            if not found:
                raise StudioError(f"no question in category {want!r}")
            return found
        for cand in (Path(spec), EVALS / "analysis" / spec, EVALS / "analysis" / f"{spec}.json"):
            try:
                ok = cand.is_file()
            except OSError:
                ok = False
            if ok:
                raw = json.loads(cand.read_text(encoding="utf-8"))
                ids = list(raw.get("query_ids") or []) or list(raw.get("category") or {})
                out = [
                    i.split("/", 1)[1] if "/" in i else i
                    for i in ids
                    if "/" not in i or i.split("/", 1)[0] == self.item
                ]
                out = [i for i in out if i in qs]
                if not out:
                    raise StudioError(f"slice {spec!r} has no questions for item {self.item!r}")
                return out
        cat, _, num = spec.partition(":")
        pool = [k for k, q in qs.items() if str(q.get("category")) == cat]
        if pool:
            return pool[: int(num)] if num.isdigit() else pool
        return [spec]

    @staticmethod
    def _gold_path(
        stage_turns: Mapping[str, list[str] | None], gold: Sequence[str]
    ) -> tuple[list[dict[str, Any]], str | None]:
        path: list[dict[str, Any]] = []
        prev: set[str] | None = None
        losing: str | None = None
        for name in FUNNEL:
            turns = stage_turns.get(name)
            if turns is None:
                continue
            present = [g for g in gold if g in set(turns)]
            path.append(
                {
                    "stage": name,
                    "present": present,
                    "absent": [g for g in gold if g not in present],
                    "n": len(turns),
                }
            )
            here = set(present)
            if losing is None:
                if name == "legs" and len(here) < len(gold):
                    losing = "legs"
                elif prev is not None and here < prev:
                    losing = name
            prev = here
        return path, losing

    async def _ask_one(self, spec: Any, detail: str) -> dict[str, Any]:
        s = self.session
        qid, q = self._question(spec)
        gold = [str(g) for g in q.get("gold_turns") or []]
        s.system.set_query_id(qid)
        s.system.set_query_meta({})
        t0 = time.perf_counter()
        ctx = await s.system.query(q["question"], s.budget, s.top_k)
        ms = (time.perf_counter() - t0) * 1000
        rows = s._take()
        fx = s._last_forensics(qid) or {}
        from ..inspect import step

        brief = detail == "brief"
        lim = 10 if brief else None
        legs: dict[str, Any] = {
            "vector": self._items(fx.get("vector"), gold, lim),
            "lexical": self._items(fx.get("lexical"), gold, lim),
        }
        for name, leg in (fx.get("extra_legs") or {}).items():
            legs[name] = self._items(leg, gold, lim)
        tf = fx.get("trace_full") or {}
        org = self._origin()
        stage_turns: dict[str, list[str] | None] = {}
        leg_all = [x for key in ("vector", "lexical") for x in (fx.get(key) or [])] + [
            x for leg in (fx.get("extra_legs") or {}).values() for x in leg
        ]
        if leg_all:
            stage_turns["legs"] = [
                org.get(str(i.get("turn") or i.get("id")), i.get("turn") or i.get("id"))
                for i in leg_all
            ]
        for name, key in (
            ("fused", "fused"),
            ("pool", "pool"),
            ("rerank", "rerank_scores"),
            ("final", "final"),
        ):
            if fx.get(key):
                stage_turns[name] = [
                    org.get(str(i.get("turn") or i.get("id")), i.get("turn") or i.get("id"))
                    for i in fx[key]
                ]
        packed_turns = [e.turn_id for e in ctx.evidence]
        stage_turns["packed"] = packed_turns
        gpath, losing = self._gold_path(stage_turns, gold) if gold else ([], None)
        answer, verdict = await s._reader_and_judge(q, ctx)
        s._record_question(qid, q, ctx, fx or None, answer, verdict)
        plan = [_clean(r) for r in rows if step._matches(r["event"], step.READ_SECTIONS[0][1])]
        headers = [_clean(r) for r in rows if step._matches(r["event"], step.READ_SECTIONS[4][1])]
        in_ctx: bool | None = None
        if gold:
            in_ctx = all(g in packed_turns for g in gold)
        elif q.get("gold") not in (None, "") and q.get("category") != "cat5":
            from ..judge import normalise_answer

            in_ctx = normalise_answer(str(q["gold"])) in normalise_answer(ctx.text)
        res = {
            "qid": qid,
            "question": q["question"],
            "category": q.get("category"),
            "gold": q.get("gold"),
            "gold_turns": gold,
            "plan": [] if brief else plan,
            "legs": legs,
            "fused": self._items(fx.get("fused"), gold, 20 if brief else None),
            "cuts": [] if brief else _jsonable(tf.get("cuts") or []),
            "pool": self._items(fx.get("pool"), gold, lim),
            "rerank": self._items(fx.get("rerank_scores"), gold, lim),
            "final": self._items(fx.get("final"), gold, lim),
            "headers": headers,
            "packed": [{"turn": e.turn_id, "gold": e.turn_id in gold} for e in ctx.evidence],
            "context": ctx.text[:400] if brief else ctx.text,
            "context_tokens": ctx.tokens,
            "truncated": bool(ctx.truncated),
            "answer": answer,
            "verdict": verdict,
            "gold_in_context": in_ctx,
            "gold_path": gpath,
            "first_losing_stage": losing,
            "ms": round(ms),
            "funnel": [
                {
                    "stage": p["stage"],
                    "n": p["n"],
                    "gold_present": len(p["present"]),
                    "gold_total": len(gold),
                }
                for p in gpath
            ],
            "_stage_turns": stage_turns,
        }
        return res

    def studio_ask(self, questions: Any, detail: str = "full") -> dict[str, Any]:
        self._need()
        specs = self._expand(questions)
        if not specs:
            raise StudioError(
                "questions is empty: pass qids, question text, {text, gold, turns}, a slice name, 'all' or 'cat:<n>'"
            )
        results = []
        for spec in specs:
            res = self.run(self._ask_one(spec, detail))
            self.last_asks[res["qid"]] = res
            results.append({k: v for k, v in res.items() if k != "_stage_turns"})
        summary = [
            {
                "qid": r["qid"],
                "gold_in_context": r["gold_in_context"],
                "first_losing_stage": r["first_losing_stage"],
                "correct": self._correct(r),
            }
            for r in results
        ]
        return _jsonable({"results": results, "summary": summary})

    @staticmethod
    def _correct(res: Mapping[str, Any]) -> bool | None:
        v = res.get("verdict") or {}
        if v.get("score") is not None:
            return bool(float(v["score"]) >= 0.5)
        return res.get("gold_in_context")

    # ---- reruns
    def studio_rerun_window(self, window_id: str, commit: bool = False) -> dict[str, Any]:
        self._need()
        return self.run(self._rerun_window(window_id, bool(commit)))

    async def _rerun_window(self, window_id: str, commit: bool) -> dict[str, Any]:
        from types import SimpleNamespace

        from memspine.observability import tap
        from memspine.workers import windows as W

        s = self.session
        members = next(
            (
                r
                for r in s.sink.rows
                if r["event"] == "write.window.members"
                and r.get("window_id") == window_id
                and r.get("member_record_ids")
            ),
            None,
        )
        if members is None:
            ids = sorted(
                {
                    str(r.get("window_id"))
                    for r in s.sink.rows
                    if r["event"] == "write.window.members" and r.get("window_id")
                }
            )
            raise StudioError(
                f"no window {window_id!r} (windows so far: {', '.join(ids) or 'none'})", "not_found"
            )
        pipe = getattr(s.system._engine, "_windows", None)
        if pipe is None:
            raise StudioError("this arm has no window write (write.window_extract is off)")
        before = s._window_view(s._window_rows(s.sink.rows, window_id))
        recs = {str(r.record_id): r for r in await s._records()}
        ids = [str(x) for x in members["member_record_ids"]]
        records = [recs[i] for i in ids if i in recs]
        if not records:
            raise StudioError("the window's records are gone from the store")
        scratch = type(s.sink)()
        job = W._Job(
            namespace=self.item,
            window=SimpleNamespace(
                session=members.get("session"),
                turns=[SimpleNamespace(record_id=i) for i in ids],
                reason="rerun",
                tokens=0,
            ),
        )
        saved_cache = pipe._cache
        pipe._cache = None
        prepared = None
        try:
            with (
                s.system._scope(phase="rerun", window_id=window_id),
                tap.capture_only(scratch),  # the rerun trace goes to the scratch sink only
                tap.request("write", rerun=True),
            ):
                prepared = await pipe._prepare_records(self.item, job, records, strict=False)
                if commit and prepared is not None:
                    await pipe._commit_job(self.item, pipe._store(self.item), prepared)
        finally:
            pipe._cache = saved_cache
        after = s._window_view(scratch.rows)

        def by_text(view: dict[str, dict[str, Any]]) -> dict[str, tuple[str, dict[str, Any]]]:
            return {" ".join(str(v["text"] or k).lower().split()): (k, v) for k, v in view.items()}

        b, a = by_text(before), by_text(after)
        added = [a[t][1]["text"] for t in a if t not in b]
        removed = [b[t][1]["text"] for t in b if t not in a]
        changed, stage_changes, verdict_changes = [], [], []
        for t in a:
            if t in b:
                vb, va = b[t][1], a[t][1]
                delta = s._stage_delta(vb, va)
                if delta or vb["integrate"] != va["integrate"]:
                    changed.append(va["text"])
                for st, (x, y) in delta.items():
                    stage_changes.append({"text": va["text"], "stage": st, "before": x, "after": y})
                if vb["integrate"] != va["integrate"]:
                    verdict_changes.append(
                        {"text": va["text"], "before": vb["integrate"], "after": va["integrate"]}
                    )
        io_rows = [_clean(r) for r in scratch.rows if r["event"] == "write.extract.io"][:1]
        s._log_tuning(
            "rerun window",
            window_id,
            f"{len(before)} envelope(s)",
            f"{len(after)} envelope(s)",
            f"added {len(added)}, removed {len(removed)}, changed {len(changed)}"
            + ("; committed" if commit else ""),
        )
        return _jsonable(
            {
                "window_id": window_id,
                "turns": len(records),
                "committed": commit,
                "produced": prepared is not None,
                "diff": {
                    "items_before": len(before),
                    "items_after": len(after),
                    "items_added": added,
                    "items_removed": removed,
                    "items_changed": changed,
                    "stage_changes": stage_changes,
                    "verdict_changes": verdict_changes,
                },
                "extract_io": io_rows[0] if io_rows else None,
                "note": "integration is judged against the live store, which already holds this window's first run",
            }
        )

    def studio_rerun_ask(self, q: Any) -> dict[str, Any]:
        self._need()
        qid, _ = self._question(q)
        before = self.last_asks.get(qid)
        after = self.run(self._ask_one(qid, "full"))
        self.last_asks[qid] = after
        diff = self._diff_asks(before, after)
        strip = lambda r: None if r is None else {k: v for k, v in r.items() if k != "_stage_turns"}
        return _jsonable({"qid": qid, "before": strip(before), "after": strip(after), "diff": diff})

    @staticmethod
    def _diff_asks(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> dict[str, Any]:
        if before is None:
            return {"note": "no earlier answer to this question in this session", "changed": None}
        bp = {p["turn"] for p in before["packed"]}
        ap = {p["turn"] for p in after["packed"]}
        stage_changes = []
        for name in FUNNEL:
            b, a = before["_stage_turns"].get(name), after["_stage_turns"].get(name)
            if b is None and a is None:
                continue
            bs, as_ = set(b or []), set(a or [])
            if bs != as_ or (b or [])[:10] != (a or [])[:10]:
                stage_changes.append(
                    {
                        "stage": name,
                        "added": sorted(map(str, as_ - bs)),
                        "removed": sorted(map(str, bs - as_)),
                        "top10_before": [str(x) for x in (b or [])[:10]],
                        "top10_after": [str(x) for x in (a or [])[:10]],
                    }
                )
        return {
            "packed_added": sorted(map(str, ap - bp)),
            "packed_removed": sorted(map(str, bp - ap)),
            "stage_changes": stage_changes,
            "first_losing_stage": [before["first_losing_stage"], after["first_losing_stage"]],
            "gold_in_context": [before["gold_in_context"], after["gold_in_context"]],
            "answer_changed": before.get("answer") != after.get("answer"),
            "verdict": [before.get("verdict"), after.get("verdict")],
            "changed": bool(ap ^ bp)
            or bool(stage_changes)
            or before.get("answer") != after.get("answer"),
        }

    def studio_sweep(self, key: str, values: list[Any], questions: Any) -> dict[str, Any]:
        s = self._need()
        if not values:
            raise StudioError("values is empty")
        specs = self._expand(questions)
        original = s.overrides.get(key, "__none__")
        table = []
        try:
            for value in values:
                res = self.studio_set(key, value)
                if not res.get("ok"):
                    table.append({"value": value, "error": res.get("error")})
                    continue
                if res["applied"] == "needs_reset":
                    table.append(
                        {
                            "value": value,
                            "applied": "needs_reset",
                            "error": "a write-path key: sweep it with studio_set + studio_reset(replay=true) + studio_ask per value",
                        }
                    )
                    continue
                rows = []
                for spec in specs:
                    r = self.run(self._ask_one(spec, "brief"))
                    rows.append(
                        {
                            "qid": r["qid"],
                            "gold_found": r["gold_in_context"],
                            "first_losing_stage": r["first_losing_stage"],
                            "correct": self._correct(r),
                            "packed": [p["turn"] for p in r["packed"]],
                        }
                    )
                found = [x["gold_found"] for x in rows if x["gold_found"] is not None]
                corr = [x["correct"] for x in rows if x["correct"] is not None]
                table.append(
                    {
                        "value": value,
                        "applied": res["applied"],
                        "gold_found": sum(found),
                        "n": len(rows),
                        "first_losing_stage": [x["first_losing_stage"] for x in rows],
                        "correct": sum(corr) if corr else None,
                        "per_question": rows,
                    }
                )
            packs = {
                json.dumps([x["packed"] for x in r.get("per_question", [])])
                for r in table
                if "per_question" in r
            }
            note = None
            if len(packs) == 1 and len(table) > 1:
                note = "every value gave the same packed context: the key may be bound at engine start (applied is live_unverified) or irrelevant to these questions"
        finally:
            if original == "__none__":
                self.studio_unset(key)
            else:
                self.studio_set(key, original)
        return _jsonable({"key": key, "table": table, "note": note})

    # ---- inspection
    def studio_show(self, id: str) -> dict[str, Any]:
        s = self._need()
        return self.run(self._show(str(id)))

    async def _show(self, ident: str) -> dict[str, Any]:
        s = self.session
        rid = s._origin_rid(ident) or ident
        recs = {str(r.record_id): r for r in await s._records()}
        events = s.sink.rows
        if rid in recs:
            r = recs[rid]
            out: dict[str, Any] = {
                "kind": "record",
                "rid": rid,
                "turn_id": s.system._origin.get(rid),
            }
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
                    out[k] = getattr(v, "value", v)
            out.update(
                {
                    "channel": r.source.channel,
                    "role": r.source.role,
                    "parents": [str(p) for p in r.source.parents],
                    "content": r.content,
                }
            )
            try:
                env = await s.system._engine.get_envelope(rid, self.item)
                out["envelope"] = env.model_dump(mode="json") if env is not None else None
            except Exception:
                out["envelope"] = None
            out["events"] = [
                _clean(e) for e in events if e.get("rid") == rid or e.get("record_id") == rid
            ][:MAX_ROWS]
            return _jsonable(out)
        win = [e for e in events if e.get("window_id") == ident]
        if win:
            return _jsonable(
                {
                    "kind": "window",
                    "window_id": ident,
                    "events": [_clean(e) for e in win if e["event"] != "write.envelope.trace"],
                    "envelopes": [_clean(e) for e in win if e["event"] == "write.envelope.trace"],
                }
            )
        if ident.isdigit():
            occ = [e for e in events if str(e.get("occ")) == ident]
            if occ:
                return _jsonable(
                    {
                        "kind": "occ",
                        "occ": ident,
                        "envelopes": [
                            _clean(e) for e in occ if e["event"] == "write.envelope.trace"
                        ],
                        "events": [_clean(e) for e in occ if e["event"] != "write.envelope.trace"],
                    }
                )
        raise StudioError(f"nothing known as {ident!r}", "not_found")

    def studio_links(self, rid: str) -> dict[str, Any]:
        self._need()
        return self.run(self._links(str(rid)))

    async def _links(self, token: str) -> dict[str, Any]:
        s = self.session
        rid = s._origin_rid(token) or token
        recs = {str(r.record_id): r for r in await s._records()}
        if rid not in recs:
            raise StudioError(f"no record {token!r}", "not_found")
        r = recs[rid]
        out: dict[str, Any] = {
            "rid": rid,
            "turn_id": s.system._origin.get(rid),
            "parents": [str(p) for p in r.source.parents],
            "children": [k for k, x in recs.items() if rid in [str(p) for p in x.source.parents]],
            "superseded_by": str(r.evolve_to) if r.evolve_to else None,
            "supersedes": [k for k, x in recs.items() if x.evolve_to and str(x.evolve_to) == rid],
            "edges": [],
        }
        chain, cur, seen = [], rid, set()
        while cur and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            cur = str(recs[cur].evolve_to) if cur in recs and recs[cur].evolve_to else None
        out["supersede_chain"] = chain
        try:
            from ..inspect.snapshot import _graph_rows

            engine = s.system._engine
            edges, _ = await _graph_rows(engine, engine._require_started(), self.item)
            out["edges"] = [
                {"src": str(e.src), "rel": e.rel_type, "dst": str(e.dst)}
                for e in edges
                if rid in (e.src, e.dst)
            ][:100]
        except Exception as exc:
            out["edges_error"] = type(exc).__name__
        return _jsonable(out)

    def studio_history(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        rows = [
            json.loads(x)
            for x in self.log_path.read_text(encoding="utf-8").splitlines()
            if x.strip()
        ]
        return rows[-int(limit) :] if limit else rows

    # ---- export / screen
    def studio_export_arm(self, name: str) -> dict[str, Any]:
        s = self._need()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,60}", name or ""):
            raise StudioError("name must be letters, digits, '_', '.', '-' (max 60)")
        from ..inspect import step

        arms = self.arms_dir or step.ARMS_DIR
        s.arms_dir = arms
        self._sink.seek(0)
        self._sink.truncate()
        self.run(s._export_arm(f"studio-{name}"))
        path = arms / f"studio-{name}.json"
        if not path.is_file():
            raise StudioError(
                self._sink.getvalue().strip().splitlines()[-1]
                if self._sink.getvalue().strip()
                else "export failed"
            )
        cfg = json.loads(path.read_text(encoding="utf-8"))
        base, eff = _flatten(self.arm_cfg), _flatten(cfg)
        diff = [
            {"key": k, "old": base.get(k), "new": eff.get(k)}
            for k in sorted(set(base) | set(eff))
            if base.get(k) != eff.get(k)
        ]
        files = (
            sorted(str(p) for p in (arms / f"studio-{name}.prompts").glob("*"))
            if (arms / f"studio-{name}.prompts").is_dir()
            else []
        )
        return _jsonable(
            {
                "path": str(path),
                "arm": f"studio-{name}",
                "diff_vs_base": diff,
                "base_arm": self.arm,
                "prompt_files": files,
            }
        )

    def _verify_passed(self) -> tuple[bool, str]:
        v = self.last_verify
        if not v:
            return False, "no verify has run: call studio_verify first"
        if v.get("stale") or v.get("config_digest") != _digest(
            [self.overrides, self.prompt_overrides]
        ):
            return (
                False,
                "the last verify is stale (config or writes changed since): run studio_verify again",
            )
        if not v.get("passed"):
            return (
                False,
                f"the last verify did not pass (check errors {v['checks']['errors']}, broken {v['counts']['broken']} > fixed {v['counts']['fixed']})",
            )
        return True, ""

    def studio_screen(
        self, arm: str, slice: str | None = None, mode: str = "sample", screen: str | None = None
    ) -> dict[str, Any]:
        refuse = lambda reason: {"queued": False, "refused": True, "reason": reason}
        if not self.allow_gpu:
            return refuse("server was not started with --allow-gpu")
        if (self.logs_dir / "GPU_RESERVED").exists():
            return refuse(f"GPU is reserved ({self.logs_dir / 'GPU_RESERVED'} exists)")
        if mode not in ("sample", "focus"):
            return refuse(
                f"mode must be sample or focus (got {mode!r}); a final run is never launched from the studio"
            )
        ok, why = self._verify_passed()
        if not ok:
            return refuse(why)
        arms = self.arms_dir or (EVALS / "arms")
        arm_file = arms / (arm if arm.endswith(".json") else arm + ".json")
        if not arm_file.is_file():
            return refuse(f"arm {arm!r} not exported: call studio_export_arm first ({arm_file})")
        if not screen:
            return refuse(
                "screen (a queue row id or batch in evals/screen_queue.tsv, e.g. S01 or B1) is required: the queue runs screens, not bare arms; "
                f"add a row for {arm_file.stem} and pass its id"
            )
        if not re.fullmatch(r"[A-Za-z0-9_,.-]+", screen):
            return refuse("screen has unexpected characters")
        est = None
        q = EVALS / "screen_queue.tsv"
        if q.is_file():
            for line in q.read_text(encoding="utf-8").splitlines():
                cols = line.split("\t")
                if cols and cols[0] == screen and len(cols) > 9 and cols[9].isdigit():
                    est = int(cols[9])
        cmd = ["bash", str(EVALS / "run_screen_batch.sh"), screen, mode]
        if self.launcher is not None:
            self.launcher(cmd)
        else:
            bash = shutil.which("bash")
            if not bash:
                return refuse("bash not found on PATH")
            cmd[0] = bash
            proc = subprocess.Popen(
                cmd, cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            first = (proc.stdout.readline() if proc.stdout else "").strip()
            return {
                "queued": True,
                "eta_min": est,
                "screen": screen,
                "arm": arm_file.stem,
                "slice": slice,
                "mode": mode,
                "launcher": first,
            }
        return {
            "queued": True,
            "eta_min": est,
            "screen": screen,
            "arm": arm_file.stem,
            "slice": slice,
            "mode": mode,
        }

    def studio_screen_status(self) -> dict[str, Any]:
        logs = (
            sorted(self.logs_dir.glob("v2_*.log"), key=lambda p: p.stat().st_mtime)
            if self.logs_dir.is_dir()
            else []
        )
        tail: list[str] = []
        if logs:
            tail = logs[-1].read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        rows: list[dict[str, Any]] = []
        res = self.logs_dir / "screen_results.tsv"
        if res.is_file():
            with res.open(encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh, delimiter="\t"))[-10:]
        return {
            "log": logs[-1].name if logs else None,
            "tail": tail,
            "ledger_rows": rows,
            "gpu": self._gpu_state(),
        }

    # ---- verify (TS-3)
    def _verify_qids(self, spec: Any) -> list[str]:
        out = [self._question(sp)[0] for sp in self._expand(spec)]
        if not out:
            raise StudioError(f"no questions matched {spec!r}")
        return out

    def _funnel(
        self, results: Mapping[str, Mapping[str, Any]], questions: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for name in FUNNEL:
            n = surv = 0
            for qid, res in results.items():
                gold = (
                    questions[qid].get("gold_turns") or []
                    if qid in questions
                    else res.get("gold_turns") or []
                )
                turns = res["_stage_turns"].get(name)
                if not gold or turns is None:
                    continue
                n += 1
                surv += all(g in set(turns) for g in gold)
            out[name] = {"n": n, "survive": surv}
        return out

    async def _ask_all(self, qids: list[str]) -> dict[str, dict[str, Any]]:
        out = {}
        for qid in qids:
            out[qid] = await self._ask_one(qid, "brief")
        return out

    def _baseline_key(self, qids: list[str], arm: str | None) -> str:
        return _digest(
            [
                self.data_spec,
                self.item,
                arm or self.arm,
                self.arm_cfg,
                self.mode,
                self.wlog,
                qids,
                {q: self.custom_q[q] for q in qids if q in self.custom_q},
            ]
        )

    def _baseline(self, qids: list[str], arm: str | None = None) -> dict[str, dict[str, Any]]:
        """Answers to ``qids`` with no overrides (or under another arm): cached by writes + questions + arm."""
        key = self._baseline_key(qids, arm)
        if key in self._baselines:
            return self._baselines[key]
        disk = self.out / "baseline_cache" / f"{key}.json"
        if disk.is_file():
            self._baselines[key] = json.loads(disk.read_text(encoding="utf-8"))
            return self._baselines[key]
        s = self.session
        saved = (dict(s.overrides), copy.deepcopy(s.prompt_overrides), s._base_dump)
        in_place = (
            arm is None
            and not s.prompt_overrides
            and not any(s._needs_rebuild(k) for k in s.overrides)
        )
        if in_place:
            # every override is a live read-path key: revert them in the running engine, ask, put them back
            log = s._log_tuning
            s._log_tuning = lambda *a, **k: None
            try:
                for k in list(saved[0]):
                    self.run(s.cmd_unset(k))
                data = self.run(self._ask_all(qids))
            finally:
                for k, v in saved[0].items():
                    self.run(s._set_value(k, v, command="set", quiet=True))
                s._log_tuning = log
        else:
            # a separate session (the engine patches process-wide state, so they never overlap), then the user's again
            saved_cfg = self.arm_cfg
            if arm is not None:
                from ..inspect.step import resolve_arm

                self.arm_cfg = resolve_arm(arm)[1]
            try:
                self.run(self._start_session(replay=True, carry=({}, {}, None)))
                data = self.run(self._ask_all(qids))
            finally:
                self.arm_cfg = saved_cfg
                self.run(self._start_session(replay=True, carry=saved))
        self._baselines[key] = _jsonable(data)
        disk.parent.mkdir(parents=True, exist_ok=True)
        disk.write_text(json.dumps(self._baselines[key], ensure_ascii=False), encoding="utf-8")
        return self._baselines[key]

    def studio_verify(self, questions: Any = None, baseline: Any = True) -> dict[str, Any]:
        s = self._need()
        qids = self._verify_qids(questions)
        if self.stale:
            self.run(s.cmd_reset())
            s.questions.update(copy.deepcopy(self.custom_q))
            self.run(self._replay())
            self.stale = []
        no_overrides = not self.overrides and not self.prompt_overrides
        base: dict[str, dict[str, Any]] | None = None
        other_arm = (
            baseline
            if isinstance(baseline, str) and baseline not in ("", "true", "false")
            else None
        )
        if baseline and (not no_overrides or other_arm):
            base = self._baseline(qids, other_arm)
            s = self.session
        current = self.run(self._ask_all(qids))
        # checks on a saved bundle of what this session did
        self._sink.seek(0)
        self._sink.truncate()
        self.run(s.cmd_save())
        from ..inspect import checks

        bundle = s.out / f"{s.run_id}--bundle"
        results = checks.run_checks(bundle, write=True)
        fails = [r for r in results if r.status == "fail"]
        errors = [r for r in fails if r.level == "error"]
        warnings = [r for r in fails if r.level != "error"]
        failing = [
            {"check_id": r.check_id, "id": r.id, "level": r.level, "detail": r.detail}
            for r in fails[:50]
        ]
        counts = {"fixed": 0, "broken": 0, "unchanged": 0, "unknown": 0}
        per_q = []
        for qid in qids:
            cur = current[qid]
            c_ok = self._correct(cur)
            b = (base or {}).get(qid)
            b_ok = self._correct(b) if b else c_ok
            if c_ok is None or b_ok is None:
                state = "unknown"
            elif c_ok and not b_ok:
                state = "fixed"
            elif b_ok and not c_ok:
                state = "broken"
            else:
                state = "unchanged"
            counts[state if state != "unknown" else "unknown"] += 1
            per_q.append(
                {
                    "qid": qid,
                    "category": cur.get("category"),
                    "state": state,
                    "correct": c_ok,
                    "baseline_correct": b_ok if b else None,
                    "first_losing_stage": cur["first_losing_stage"],
                    "baseline_first_losing_stage": b["first_losing_stage"] if b else None,
                    "packed": [p["turn"] for p in cur["packed"]],
                    "baseline_packed": [p["turn"] for p in b["packed"]] if b else None,
                }
            )
        counts["unchanged"] += counts.pop("unknown")
        cur_f = self._funnel(current, s.questions)
        base_f = self._funnel(base, s.questions) if base else cur_f
        funnel = [
            {
                "stage": st,
                "n": cur_f[st]["n"],
                "survive": cur_f[st]["survive"],
                "baseline_survive": base_f[st]["survive"],
            }
            for st in FUNNEL
        ]
        passed = not errors and counts["broken"] <= counts["fixed"]
        out = {
            "passed": passed,
            "questions": qids,
            "baseline": "none (no overrides)"
            if base is None
            else (
                f"cached run under arm {other_arm}" if other_arm else "cached run without overrides"
            ),
            "checks": {
                "errors": len(errors),
                "warnings": len(warnings),
                "failing": failing,
                "n": len(results),
                "bundle": str(bundle),
            },
            "counts": counts,
            "funnel": funnel,
            "per_question": per_q,
        }
        self.last_verify = {
            "passed": passed,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "counts": counts,
            "checks": {"errors": len(errors), "warnings": len(warnings)},
            "n_questions": len(qids),
            "stale": False,
            "config_digest": _digest([self.overrides, self.prompt_overrides]),
        }
        return _jsonable(out)
