"""E1 / E3 small tooling: bench_http.py, --warn-localhost, ollama_env.ps1 log level. Offline: the
latency check talks to a throwaway HTTP server on 127.0.0.1 in this process."""

from __future__ import annotations

import http.server
import io
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import bench_http
import pytest
from memspine_evals.cli import build_parser
from memspine_evals.netcheck import localhost_warning, warn_localhost

HERE = Path(__file__).resolve().parents[1]


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b'{"version":"test"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[int]:
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def test_measure_counts_successes_and_failures(server: int) -> None:
    run = bench_http.measure("127.0.0.1", server, "/api/version", 5)
    assert len(run["times"]) == 5 and run["failures"] == 0 and all(t > 0 for t in run["times"])
    dead = bench_http.measure("127.0.0.1", 1, "/", 2, timeout=0.5)  # nothing listens on port 1
    assert dead["times"] == [] and dead["failures"] == 2


def test_summarise_reports_ms_and_the_slowest_to_fastest_ratio() -> None:
    runs = [
        {"host": "localhost", "times": [2.0, 2.0], "failures": 0, "addresses": ["ipv6 ::1"]},
        {"host": "127.0.0.1", "times": [0.5, 0.5], "failures": 1},
    ]
    out = bench_http.summarise(runs)
    by_host = {h["host"]: h for h in out["hosts"]}
    assert by_host["localhost"]["mean_ms"] == 2000.0 and by_host["127.0.0.1"]["failures"] == 1
    assert out["ratio_slowest_to_fastest"] == 4.0
    assert bench_http.summarise([{"host": "x", "times": [], "failures": 3}])["hosts"][0]["mean_ms"] is None


def test_main_json_and_fail_ratio(server: int, capsys: pytest.CaptureFixture[str]) -> None:
    base = f"http://127.0.0.1:{server}"
    code = bench_http.main(["--base", base, "--n", "3", "--hosts", "127.0.0.1", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["hosts"][0]["n_ok"] == 3 and out["ratio_slowest_to_fastest"] is None
    # two hosts that both resolve to the loopback server: the ratio exists and is finite
    code = bench_http.main(["--base", base, "--n", "3", "--hosts", "127.0.0.1", "127.0.0.1", "--fail-ratio", "1000"])
    assert code == 0
    assert "mean" in capsys.readouterr().out


def test_resolve_order_lists_addresses_and_survives_a_bad_name() -> None:
    assert bench_http.resolve_order("127.0.0.1", 80) == ["ipv4 127.0.0.1"]
    assert bench_http.resolve_order("no-such-host.invalid", 80)[0].startswith("unresolved")


def test_localhost_warning_only_for_the_host_name_localhost() -> None:
    assert localhost_warning("http://localhost:11434/v1") and "127.0.0.1" in localhost_warning(
        "http://LOCALHOST:11434/v1"
    )  # type: ignore[operator]
    assert localhost_warning("http://127.0.0.1:11434/v1") is None
    assert localhost_warning("https://api.example.com/v1") is None
    assert localhost_warning("not a url") is None


def test_warn_localhost_prints_to_the_given_stream() -> None:
    buf = io.StringIO()
    got = warn_localhost([("--base-url", "http://localhost:1/v1"), ("--x", "http://127.0.0.1/v1")], buf)
    assert len(got) == 1 and buf.getvalue().strip() == got[0] and "--base-url" in got[0]


def test_cli_flag_defaults_off_and_default_url_is_loopback_ip() -> None:
    parser = build_parser()
    a = parser.parse_args(["c0-1", "--dataset", "locomo"])
    assert a.warn_localhost is False and localhost_warning(a.base_url) is None
    assert parser.parse_args(["c0-1", "--dataset", "locomo", "--warn-localhost"]).warn_localhost is True


def test_ollama_env_script_lowers_the_log_level() -> None:
    text = (HERE / "ollama_env.ps1").read_text("utf-8")
    assert "OLLAMA_DEBUG           = '0'" in text and "OLLAMA_DEBUG=0" in text
    for name in ("FLASH_ATTENTION", "KV_CACHE_TYPE", "CONTEXT_LENGTH", "NUM_PARALLEL", "KEEP_ALIVE"):
        assert f"OLLAMA_{name}" in text  # the five originals are still there
