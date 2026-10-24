"""TS-1: the Tuning Studio MCP server (stdio, JSON-RPC 2.0, newline-delimited, no dependency).

    python -m memspine_evals.studio.server [--data demo|PATH] [--out DIR] [--allow-gpu] [--arms-dir DIR] [--logs-dir DIR]

Handles ``initialize``, ``notifications/initialized``, ``ping``, ``tools/list`` and ``tools/call``.
Stdout carries protocol messages only; everything else (engine logs, prints) goes to stderr. The engine
is built lazily on the first ``studio_open``, so the server starts in well under a second.
Every call is appended to ``<out>/tuning.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from typing import Any

from .api import StudioAPI, StudioError

SERVER_NAME = "memspine-studio"
SERVER_VERSION = "1.0.0"
SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")

_S = {"type": "string"}
_B = {"type": "boolean"}
_I = {"type": "integer"}
_QUESTIONS = {
    "description": "A list of question ids / texts / {text, gold?, turns?}; or one string: a qid, 'all', 'cat:<n>' (category n), '<category>[:count]', or a slice name (json in evals/analysis or a path).",
    "anyOf": [
        {"type": "string"},
        {
            "type": "object",
            "properties": {
                "qid": _S,
                "text": _S,
                "gold": _S,
                "turns": {"type": "array", "items": _S},
            },
        },
        {"type": "array", "items": {"anyOf": [{"type": "string"}, {"type": "object"}]}},
    ],
}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


#: name, description, input schema, read-only (contract v1: docs/v2/TUNING_STUDIO_PLAN.md)
TOOLS: list[tuple[str, str, dict[str, Any], bool]] = [
    (
        "studio_status",
        "Session, arm, data, item, mode, cursor, counts, overrides, prompt overrides, last verify and GPU queue state.",
        _obj({}),
        True,
    ),
    (
        "studio_open",
        "Open a session on a conversation. data: 'demo', a path, or 'locomo'; mode: offline (hash embedder, stub LLM), replay (recorded call cache) or live.",
        _obj(
            {
                "data": _S,
                "item": _S,
                "arm": {
                    **_S,
                    "description": "an evals/arms name or json path ('demo' for the invented fixture)",
                },
                "mode": {"type": "string", "enum": ["offline", "replay", "live"]},
                "cache_dir": _S,
            }
        ),
        False,
    ),
    (
        "studio_reset",
        "Rebuild the engine with an empty store. keep_overrides keeps config and prompt overrides; replay rewrites the same turns afterwards.",
        _obj({"keep_overrides": _B, "replay": _B}),
        False,
    ),
    (
        "studio_load_conversation",
        "Replace the conversation with one in the demo shape (conversation_id, sessions[{session_id, date, turns[]}], questions[]).",
        _obj({"json": {"type": "object"}}, ["json"]),
        False,
    ),
    (
        "studio_stages",
        "All pipeline stages: stage_id, name, path (write/sleep/read), file_function, events, gap, n_params.",
        _obj({}),
        True,
    ),
    (
        "studio_params",
        "The knobs, prompts and models of one stage, with current values.",
        _obj({"stage": _S}, ["stage"]),
        True,
    ),
    (
        "studio_config",
        "Effective config as dotted keys: value, default (the arm's), source (arm/override).",
        _obj({"prefix": _S, "diff": _B}),
        True,
    ),
    (
        "studio_set",
        "Set a config key (dotted). applied = live_verified (the engine's live config shows it and the key is rebound per call), live_unverified (accepted but may be bound at engine start: confirm with studio_rerun_ask, probe=true, or studio_reset) or needs_reset.",
        _obj(
            {
                "key": _S,
                "value": {"description": "any JSON value; numeric/boolean strings are parsed"},
                "probe": {**_B, "description": "re-ask the last question to prove the key is live"},
            },
            ["key", "value"],
        ),
        False,
    ),
    ("studio_unset", "Remove an override.", _obj({"key": _S}, ["key"]), False),
    ("studio_prompts", "Prompt ids with role, version and whether overridden.", _obj({}), True),
    (
        "studio_prompt_show",
        "A prompt's text (and optionally a rendered example from the last extraction).",
        _obj({"prompt_id": _S, "rendered": _B}, ["prompt_id"]),
        True,
    ),
    (
        "studio_prompt_set",
        "Override a prompt's body text (applies after studio_reset).",
        _obj({"prompt_id": _S, "text": _S}, ["prompt_id", "text"]),
        False,
    ),
    (
        "studio_prompt_reset",
        "Drop a prompt override.",
        _obj({"prompt_id": _S}, ["prompt_id"]),
        False,
    ),
    (
        "studio_write",
        "Write to memory. mode=say: messages[{speaker,text,ts?,session?}]; turns: ids[] or range ('1-5', ['D1:1','D1:5']) or n; interaction: n exchanges (2 turns each); session: n (1-based) or ids[] of session ids.",
        _obj(
            {
                "mode": {"type": "string", "enum": ["say", "turns", "interaction", "session"]},
                "messages": {
                    "type": "array",
                    "items": _obj({"speaker": _S, "text": _S, "ts": _S, "session": _S}, ["text"]),
                },
                "ids": {
                    "type": "array",
                    "items": _S,
                    "description": "turn ids (mode=turns) or session ids (mode=session)",
                },
                "range": {
                    "description": "mode=turns: [from, to] as turn ids or 1-based positions, or '3-8'"
                },
                "n": {
                    **_I,
                    "description": "turns: next n turns; interaction: n exchanges (2 turns each); session: the next n sessions from the cursor",
                },
                "session": {"description": "mode=session: a session id, or a count like n"},
            },
            ["mode"],
        ),
        False,
    ),
    (
        "studio_sleep",
        "Run the sleep cycle (flush windows, close sessions, consolidate), stage by stage.",
        _obj({}),
        False,
    ),
    (
        "studio_ask",
        "Ask one or more questions: plan, legs, fusion, cuts, pool, rerank, headers, packed context, answer, gold path, first losing stage. Writes access stats.",
        _obj(
            {"questions": _QUESTIONS, "detail": {"type": "string", "enum": ["full", "brief"]}},
            ["questions"],
        ),
        False,
    ),
    (
        "studio_rerun_window",
        "Re-extract one window under the current prompt/config and diff it (needs TI-5b).",
        _obj({"window_id": _S, "commit": _B}, ["window_id"]),
        False,
    ),
    (
        "studio_rerun_ask",
        "Ask a question again after a change; returns before, after and the diff.",
        _obj({"q": _QUESTIONS}, ["q"]),
        False,
    ),
    (
        "studio_sweep",
        "Try values of a live key (numbers, booleans or strings) on a question set: gold found, first losing stage, correct per value.",
        _obj(
            {
                "key": _S,
                "values": {
                    "type": "array",
                    "items": {"type": ["number", "boolean", "string", "null"]},
                },
                "questions": _QUESTIONS,
            },
            ["key", "values", "questions"],
        ),
        False,
    ),
    (
        "studio_show",
        "One object: a record (rid or turn id), window id, or envelope occurrence.",
        _obj({"id": _S}, ["id"]),
        True,
    ),
    (
        "studio_links",
        "Lineage parents/children, supersede chain and graph edges of a record.",
        _obj({"rid": _S}, ["rid"]),
        True,
    ),
    (
        "studio_verify",
        "Run the invariant checks on a saved bundle and the target questions under current overrides vs a cached baseline without them: fixed/broken/unchanged and the gold survival funnel.",
        _obj(
            {
                "questions": _QUESTIONS,
                "baseline": {
                    "anyOf": [{"type": "boolean"}, {"type": "string"}],
                    "description": "true: cached run without overrides (default); false: skip; '<arm>': compare with that arm instead",
                },
            }
        ),
        False,
    ),
    (
        "studio_history",
        "Rows of tuning.jsonl (every tool call), newest last.",
        _obj({"limit": _I}),
        True,
    ),
    (
        "studio_export_arm",
        "Write the effective config (arm + overrides) as evals/arms/studio-<name>.json.",
        _obj({"name": _S}, ["name"]),
        False,
    ),
    (
        "studio_screen",
        "Launch a sample screen on the GPU queue. Refused unless the server has --allow-gpu, GPU_RESERVED is absent and the last verify passed.",
        _obj(
            {
                "arm": _S,
                "slice": _S,
                "mode": {"type": "string", "enum": ["sample", "focus"]},
                "screen": {**_S, "description": "queue row id or batch in evals/screen_queue.tsv"},
            },
            ["arm"],
        ),
        False,
    ),
    (
        "studio_screen_status",
        "The latest queue log tail and the newest ledger rows.",
        _obj({}),
        True,
    ),
]
BY_NAME = {t[0]: t for t in TOOLS}


def tool_list() -> list[dict[str, Any]]:
    return [
        {
            "name": n,
            "description": d,
            "inputSchema": s,
            "annotations": {"readOnlyHint": ro, "title": n},
        }
        for n, d, s, ro in TOOLS
    ]


def _err(rid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    e: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": rid, "error": e}


class Server:
    def __init__(self, api: StudioAPI) -> None:
        self.api = api

    def handle(self, msg: Any) -> dict[str, Any] | None:
        """One JSON-RPC message in, the response (or None for a notification) out."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
        method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        is_note = "id" not in msg
        if not isinstance(method, str):
            return None if is_note else _err(rid, -32600, "missing method")
        if method == "initialize":
            want = params.get("protocolVersion")
            ver = want if want in SUPPORTED else SUPPORTED[0]
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "protocolVersion": ver,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": "Local MemSpine Tuning Studio. Open a session with studio_open, write, ask, change a knob, ask again, then studio_verify before studio_screen.",
                },
            }
        if method.startswith("notifications/"):
            return None
        if is_note:
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": rid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tool_list()}}
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": rid, "result": self._call(params)}
        if method in ("resources/list", "prompts/list"):
            return {"jsonrpc": "2.0", "id": rid, "result": {method.split("/")[0]: []}}
        return _err(rid, -32601, f"method not found: {method}")

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in BY_NAME:
            return self._fail(f"unknown tool {name!r}")
        if not isinstance(args, dict):
            return self._fail("arguments must be an object")
        schema = BY_NAME[name][2]
        unknown = [k for k in args if k not in schema["properties"]]
        if unknown:
            return self._fail(f"unknown argument(s) for {name}: {unknown}")
        missing = [k for k in schema.get("required", []) if k not in args]
        if missing:
            return self._fail(f"missing required argument(s) for {name}: {missing}")
        try:
            result, log_id = self.api.call(name, args)
        except StudioError as exc:
            return self._fail(str(exc), code=exc.code)
        except Exception as exc:
            print(traceback.format_exc(), file=sys.stderr)
            return self._fail(f"{type(exc).__name__}: {exc}", code="internal")
        structured = (
            {**result, "log_id": log_id}
            if isinstance(result, dict)
            else {"result": result, "log_id": log_id}
        )
        text = json.dumps(
            structured if isinstance(result, dict) else result, ensure_ascii=False, default=str
        )
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": json.loads(json.dumps(structured, default=str)),
            "isError": False,
        }

    @staticmethod
    def _fail(message: str, code: str = "error") -> dict[str, Any]:
        body = {"error": message, "code": code}
        return {
            "content": [{"type": "text", "text": json.dumps(body)}],
            "structuredContent": body,
            "isError": True,
        }


def serve(api: StudioAPI, stdin: Any, stdout: Any) -> None:
    server = Server(api)
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError as exc:
            reply = _err(None, -32700, f"parse error: {exc}")
        else:
            try:
                reply = server.handle(msg)
            except Exception as exc:  # never die on one message
                print(traceback.format_exc(), file=sys.stderr)
                reply = _err(
                    msg.get("id") if isinstance(msg, dict) else None,
                    -32603,
                    f"internal error: {exc}",
                )
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False, default=str) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m memspine_evals.studio.server", description=__doc__.split("\n")[0]
    )
    ap.add_argument(
        "--data",
        default="demo",
        help="default dataset for studio_open: demo | path to a LoCoMo-shaped json | locomo",
    )
    ap.add_argument(
        "--out", help="run folder root (default evals/runs/_analysis/inspect/studio, gitignored)"
    )
    ap.add_argument(
        "--allow-gpu",
        action="store_true",
        help="let studio_screen launch the GPU queue (still refused while GPU_RESERVED exists)",
    )
    ap.add_argument("--arms-dir", help="where studio_export_arm writes (default evals/arms)")
    ap.add_argument("--logs-dir", help="queue logs and ledger folder (default evals/runs/_logs)")
    args = ap.parse_args(argv)
    # protocol on the real stdout only; stray prints from libraries go to stderr
    real_out = sys.stdout
    sys.stdout = sys.stderr
    stdin = sys.stdin
    for stream in (stdin, real_out, sys.stderr):
        try:
            stream.reconfigure(
                encoding="utf-8", errors="replace", newline="\n"
            ) if stream is not stdin else stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    api = StudioAPI(
        args.data,
        args.out,
        allow_gpu=args.allow_gpu,
        arms_dir=args.arms_dir,
        logs_dir=args.logs_dir,
    )
    print(
        f"{SERVER_NAME} {SERVER_VERSION} ready (allow_gpu={args.allow_gpu}, out={api.out})",
        file=sys.stderr,
    )
    try:
        serve(api, stdin, real_out)
    finally:
        api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
