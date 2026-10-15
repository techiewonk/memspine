# ruff: noqa: E501
"""TS-1/3/4: the Tuning Studio MCP server (spawned as a subprocess, offline demo), the verify loop and the installer."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("memspine")

EVALS = Path(__file__).resolve().parents[1]
REPO = EVALS.parent

CONTRACT_TOOLS = {
    "studio_status": True, "studio_open": False, "studio_reset": False, "studio_load_conversation": False, "studio_stages": True,
    "studio_params": True, "studio_config": True, "studio_set": False, "studio_unset": False, "studio_prompts": True,
    "studio_prompt_show": True, "studio_prompt_set": False, "studio_prompt_reset": False, "studio_write": False,
    "studio_sleep": False, "studio_ask": False, "studio_rerun_window": False, "studio_rerun_ask": False, "studio_sweep": False,
    "studio_show": True, "studio_links": True, "studio_verify": False, "studio_history": True, "studio_export_arm": False,
    "studio_screen": False, "studio_screen_status": True,
}


class Client:
    def __init__(self, tmp: Path, *extra: str) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(REPO / "src"), str(EVALS)])
        env["PYTHONIOENCODING"] = "utf-8"
        self.tmp = tmp
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "memspine_evals.studio.server", "--out", str(tmp / "out"), "--arms-dir", str(tmp / "arms"),
             "--logs-dir", str(tmp / "logs"), *extra],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True, encoding="utf-8", cwd=str(tmp),
        )
        self._id = 0
        self.dog = threading.Timer(600, self.proc.kill)
        self.dog.start()

    def send(self, method: str, params: dict | None = None, note: bool = False) -> dict | None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not note:
            self._id += 1
            msg["id"] = self._id
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if note:
            return None
        line = self.proc.stdout.readline()
        assert line, "server closed stdout: " + (self.proc.stderr.read() if self.proc.poll() is not None else "")
        reply = json.loads(line)  # a non-JSON line on stdout would fail here
        assert reply["id"] == self._id
        return reply

    def tool(self, _name: str, **args) -> dict:
        reply = self.send("tools/call", {"name": _name, "arguments": args})
        assert "error" not in reply, reply
        res = reply["result"]
        assert res["content"][0]["type"] == "text"
        assert json.loads(res["content"][0]["text"]) is not None
        return res

    def ok(self, _name: str, **args) -> dict:
        res = self.tool(_name, **args)
        assert not res["isError"], res["content"][0]["text"]
        return res["structuredContent"]

    def close(self) -> None:
        self.dog.cancel()
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=60)
        except Exception:
            self.proc.kill()


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory):
    c = Client(tmp_path_factory.mktemp("studio"))
    yield c
    c.close()


def test_protocol_and_tool_list(client: Client) -> None:
    r = client.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    res = r["result"]
    assert res["protocolVersion"] == "2025-06-18" and res["serverInfo"]["name"] == "memspine-studio" and "tools" in res["capabilities"]
    client.send("notifications/initialized", note=True)
    assert client.send("ping")["result"] == {}
    tools = client.send("tools/list")["result"]["tools"]
    got = {t["name"]: t["annotations"]["readOnlyHint"] for t in tools}
    assert got == CONTRACT_TOOLS
    for t in tools:
        assert t["description"] and t["inputSchema"]["type"] == "object"
    assert client.send("nope")["error"]["code"] == -32601
    bad = client.tool("studio_status", bogus=1)
    assert bad["isError"]
    assert client.tool("studio_nothing")["isError"]
    assert client.tool("studio_ask")["isError"]  # missing required arguments


def test_catalogue_tools_before_open(client: Client) -> None:
    stages = client.tool("studio_stages")
    rows = json.loads(stages["content"][0]["text"])
    assert len(rows) >= 80 and {"stage_id", "name", "path", "file_function", "events", "gap", "n_params"} <= set(rows[0])
    assert {r["path"] for r in rows} == {"write", "sleep", "read"}
    st = client.ok("studio_status")
    assert st["open"] is False and st["gpu"]["allow_gpu"] is False
    assert client.tool("studio_ask", questions=["q1"])["isError"]  # no session yet


def test_tuning_loop(client: Client) -> None:
    st = client.ok("studio_open", data="demo", mode="offline")
    assert st["open"] and st["counts"]["turns_total"] == 30 and st["counts"]["turns_written"] == 0 and st["mode"] == "offline"

    w = client.ok("studio_write", mode="say", messages=[{"speaker": "Bram", "text": "Last week I moved to Zwolle and started at the bakery Brood."}])
    turn = w["turns"][0]
    assert turn["custom"] is True and turn["records"] >= 1
    assert {"stamp", "record", "index", "window", "extract", "envelopes", "deposits", "links"} <= set(turn["stages"])
    assert turn["stages"]["stamp"] and turn["stages"]["record"] and turn["stages"]["index"]

    ws = client.ok("studio_write", mode="session", n=1)
    assert ws["summary"]["turns"] == 10
    wids = sorted({e["window_id"] for e in ws["summary"]["sessions"][0]["close"]["window"] if e.get("window_id")})
    assert wids
    rw = client.ok("studio_rerun_window", window_id=wids[0])
    assert rw["committed"] is False and {"items_added", "items_removed", "items_changed", "stage_changes", "verdict_changes"} <= set(rw["diff"])
    assert client.tool("studio_rerun_window", window_id="nope")["isError"]
    client.ok("studio_write", mode="turns", ids=["D2:1", "D2:2"])
    client.ok("studio_write", mode="interaction", n=1)
    client.ok("studio_write", mode="session", n=3)
    sl = client.ok("studio_sleep")
    assert any(s["stage"] == "consolidate" for s in sl["stages"])

    ask = client.ok("studio_ask", questions=["q1", "q7"], detail="brief")
    by = {r["qid"]: r for r in ask["results"]}
    assert by["q1"]["gold_in_context"] is True and by["q1"]["first_losing_stage"] is None
    r7 = by["q7"]
    assert r7["gold_turns"] == ["D1:3", "D3:5"] and r7["gold_path"] and r7["packed"] and r7["context"]
    assert {"plan", "legs", "fused", "cuts", "pool", "rerank", "headers", "answer", "verdict"} <= set(r7)
    assert any(x["gold"] for x in r7["legs"]["vector"]) or any(x["gold"] for x in r7["legs"]["lexical"])
    assert {x["qid"] for x in ask["summary"]} == {"q1", "q7"}
    assert r7["funnel"] and {"stage", "n", "gold_present", "gold_total"} <= set(r7["funnel"][0])
    qs = client.ok("studio_status")["questions"]
    assert len(qs) == 8 and {"qid", "category", "question"} <= set(qs[0])
    cat = client.ok("studio_ask", questions="cat:list", detail="brief")
    assert cat["results"] and client.ok("studio_ask", questions="list", detail="brief")["results"][0]["qid"] == "q2"
    assert len(client.ok("studio_ask", questions="all", detail="brief")["results"]) >= 8
    adhoc = client.ok("studio_ask", questions=[{"text": "Where does Bram live?", "gold": "Utrecht", "turns": ["D1:1"]}])
    assert adhoc["results"][0]["qid"].startswith("adhoc-")

    shown = client.ok("studio_show", id="D1:3")
    assert shown["kind"] == "record" and "Korenbloem" in shown["content"]
    links = client.ok("studio_links", rid="D1:3")
    assert links["rid"] == shown["rid"] and "supersede_chain" in links

    cfg = client.tool("studio_config", diff=True)
    assert all(r["source"] != "override" for r in json.loads(cfg["content"][0]["text"]))  # diff = against the engine defaults
    sset = client.ok("studio_set", key="read.replay_window_before", value=0)
    assert sset["ok"] and sset["applied"] == "live_unverified" and sset["old"] == 1 and sset["new"] == 0 and "studio_rerun_ask" in sset["note"]
    keys = [r["key"] for r in json.loads(client.tool("studio_config", prefix="read.assembly.")["content"][0]["text"])]
    assert keys
    ver = client.ok("studio_set", key="read.assembly.relative_floor", value=0.2)
    assert ver["ok"] and ver["applied"] == "live_verified", ver
    assert client.ok("studio_unset", key="read.assembly.relative_floor")["ok"]
    rb = client.ok("studio_set", key="read.replay_window_after", value="1", probe=True)
    assert rb["ok"] and rb["applied"] in ("live_unverified", "live_verified") and rb["probe"] is not None
    assert client.ok("studio_unset", key="read.replay_window_after")["ok"]
    diff_rows = json.loads(client.tool("studio_config", diff=True)["content"][0]["text"])
    assert [r["key"] for r in diff_rows if r["source"] == "override"] == ["read.replay_window_before"]
    bad = client.ok("studio_set", key="read.no_such_key_at_all", value=1)
    assert bad["ok"] is False and bad["error"]
    after_bad = client.ok("studio_status")
    assert after_bad["open"] and after_bad["counts"]["records"] > 0  # a rejected key leaves the session usable

    rr = client.ok("studio_rerun_ask", q="q7")
    assert rr["before"] is not None and rr["after"]["qid"] == "q7" and "changed" in rr["diff"]
    assert rr["before"]["packed"] != rr["after"]["packed"] or rr["diff"]["changed"] in (True, False)

    pr = json.loads(client.tool("studio_prompts")["content"][0]["text"])
    assert pr and {"prompt_id", "role", "version", "overridden"} <= set(pr[0])
    ps = client.ok("studio_prompt_show", prompt_id=pr[0]["prompt_id"])
    assert ps["text"]
    pset = client.ok("studio_prompt_set", prompt_id=pr[0]["prompt_id"], text=ps["text"] + "\n# studio test line")
    assert pset["ok"] and "studio test line" in pset["diff"]
    assert client.ok("studio_prompt_reset", prompt_id=pr[0]["prompt_id"])["ok"]

    params = client.ok("studio_params", stage="C1")
    assert params["stage_id"] == "C1" and "keys" in params

    scr = client.ok("studio_screen", arm="studio-x")
    assert scr["queued"] is False and "allow-gpu" in scr["reason"]

    v = client.ok("studio_verify", questions=["q1", "q2", "q7"])
    assert set(v["counts"]) == {"fixed", "broken", "unchanged"} and sum(v["counts"].values()) == 3
    assert v["checks"]["errors"] == 0, v["checks"]["failing"]
    assert [f["stage"] for f in v["funnel"]] == ["legs", "fused", "pool", "rerank", "final", "packed"]
    assert v["passed"] == (v["counts"]["broken"] <= v["counts"]["fixed"])
    assert {p["qid"] for p in v["per_question"]} == {"q1", "q2", "q7"}
    assert client.ok("studio_status")["last_verify"]["passed"] == v["passed"]

    sweep = client.ok("studio_sweep", key="read.replay_window_before", values=[0, 2], questions=["q7"])
    assert [row["value"] for row in sweep["table"]] == [0, 2] and "gold_found" in sweep["table"][0]
    assert client.ok("studio_status")["overrides"] == {"read.replay_window_before": 0}  # the sweep restores the override

    exp = client.ok("studio_export_arm", name="test1")
    arm = json.loads(Path(exp["path"]).read_text(encoding="utf-8"))
    assert arm["read"]["replay_window_before"] == 0
    assert exp["diff_vs_base"] == [{"key": "read.replay_window_before", "old": 1, "new": 0}]
    assert Path(exp["path"]).parent == client.tmp / "arms"

    un = client.ok("studio_unset", key="read.replay_window_before")
    assert un["ok"] and un["new"] == 1

    reset = client.ok("studio_reset", keep_overrides=True)
    assert reset["counts"]["records"] == 0

    hist = json.loads(client.tool("studio_history", limit=500)["content"][0]["text"])
    tools = [h["tool"] for h in hist]
    for name in ("studio_open", "studio_write", "studio_ask", "studio_set", "studio_rerun_ask", "studio_verify"):
        assert name in tools
    assert all("log_id" in h and "ts" in h for h in hist)
    log = (client.tmp / "out" / "tuning.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log) >= len(hist) - 1
    res = client.tool("studio_status")
    assert res["structuredContent"]["log_id"] > 20


def test_screen_refusals_and_launch(tmp_path: Path) -> None:
    from memspine_evals.studio.api import StudioAPI

    calls: list[list[str]] = []
    logs = tmp_path / "logs"
    logs.mkdir()
    arms = tmp_path / "arms"
    arms.mkdir()
    (arms / "studio-t.json").write_text("{}", encoding="utf-8")
    api = StudioAPI("demo", tmp_path / "out", allow_gpu=False, arms_dir=arms, logs_dir=logs, launcher=calls.append)
    assert "allow-gpu" in api.studio_screen(arm="studio-t", screen="S01")["reason"]
    api.allow_gpu = True
    (logs / "GPU_RESERVED").write_text("x", encoding="utf-8")
    assert "reserved" in api.studio_screen(arm="studio-t", screen="S01")["reason"]
    (logs / "GPU_RESERVED").unlink()
    assert "no verify" in api.studio_screen(arm="studio-t", screen="S01")["reason"]
    from memspine_evals.studio.api import _digest

    cfg = _digest([{}, {}])
    api.last_verify = {"passed": False, "counts": {"broken": 2, "fixed": 0}, "checks": {"errors": 0}, "stale": False, "config_digest": cfg}
    assert "did not pass" in api.studio_screen(arm="studio-t", screen="S01")["reason"]
    api.last_verify = {"passed": True, "counts": {"broken": 0, "fixed": 1}, "checks": {"errors": 0}, "stale": False, "config_digest": cfg}
    assert "final" in api.studio_screen(arm="studio-t", screen="S01", mode="final")["reason"]
    assert "screen" in api.studio_screen(arm="studio-t")["reason"]
    assert "not exported" in api.studio_screen(arm="missing", screen="S01")["reason"]
    assert not calls
    ok = api.studio_screen(arm="studio-t", screen="S01", mode="sample")
    assert ok["queued"] is True and calls and calls[0][-2:] == ["S01", "sample"] and calls[0][1].endswith("run_v2_batch.sh")
    api.last_verify["stale"] = True
    assert "stale" in api.studio_screen(arm="studio-t", screen="S01")["reason"]
    # the status tool reads the queue log tail and the ledger
    (logs / "v2_S01_sample_1013-1200.log").write_text("line1\nline2\n", encoding="utf-8")
    (logs / "v2_results.tsv").write_text("ts\tscreen\tnet\n2026-10-14\tS01\t3\n", encoding="utf-8")
    st = api.studio_screen_status()
    assert st["tail"][-1] == "line2" and st["ledger_rows"][0]["screen"] == "S01"
    api.close()


def _installer():
    spec = importlib.util.spec_from_file_location("install_studio_mcp", REPO / "scripts" / "install_studio_mcp.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_installer_dry_run_preserves_keys(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    inst = _installer()
    cfg = tmp_path / "claude_desktop_config.json"
    original = {"theme": "dark", "mcpServers": {"other": {"command": "x", "args": ["1"], "env": {"A": "b"}}}, "numbers": [1, 2.5, None]}
    cfg.write_text(json.dumps(original, indent=2), encoding="utf-8")
    before = cfg.read_text(encoding="utf-8")
    assert inst.main(["--dry-run", "--config", str(cfg), "--apply-desktop"]) == 0
    out = capsys.readouterr().out
    assert "memspine-studio" in out and "claude mcp add memspine-studio --scope user" in out and "memspine-restructure" in out
    assert cfg.read_text(encoding="utf-8") == before and not list(tmp_path.glob("*.bak-*"))

    assert inst.main(["--apply-desktop", "--config", str(cfg)]) == 0
    new = json.loads(cfg.read_text(encoding="utf-8"))
    assert new["theme"] == "dark" and new["numbers"] == [1, 2.5, None] and new["mcpServers"]["other"] == original["mcpServers"]["other"]
    entry = new["mcpServers"]["memspine-studio"]
    assert entry["command"].endswith("memspine/.venv/Scripts/python.exe") and entry["args"] == ["-m", "memspine_evals.studio.server"]
    assert entry["env"]["PYTHONPATH"] == os.pathsep.join(["C:/Users/hembad/Documents/GitHub/memspine-restructure/src", "C:/Users/hembad/Documents/GitHub/memspine-restructure/evals"])
    backups = list(tmp_path.glob("claude_desktop_config.json.bak-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text(encoding="utf-8")) == original

    after = cfg.read_text(encoding="utf-8")
    assert inst.main(["--apply-desktop", "--config", str(cfg)]) == 0  # idempotent: no rewrite, no second backup
    assert "unchanged" in capsys.readouterr().out
    assert cfg.read_text(encoding="utf-8") == after and len(list(tmp_path.glob("*.bak-*"))) == 1


def test_installer_new_file_and_bad_json(tmp_path: Path) -> None:
    inst = _installer()
    cfg = tmp_path / "sub" / "claude_desktop_config.json"
    assert inst.main(["--apply-desktop", "--config", str(cfg)]) == 0
    assert list(json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]) == ["memspine-studio"]
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        inst.main(["--apply-desktop", "--config", str(bad)])
    assert bad.read_text(encoding="utf-8") == "{not json"
