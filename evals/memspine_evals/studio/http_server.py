# ruff: noqa: E501
"""TS-LOCAL: serve the Tuning Studio page and API from this machine, for any browser on this PC.

    python -m memspine_evals.studio.http_server [--port 8765] [--host 127.0.0.1] [--data demo|PATH|locomo]
                                                [--out DIR] [--allow-gpu] [--token T] [--arms-dir DIR] [--logs-dir DIR]

Standard library only. Binds 127.0.0.1 by default; any other ``--host`` requires ``--token`` (so the
token is chosen by you, not printed into a log), and a wildcard host is refused. Every /api request must
carry ``X-Studio-Token``; requests whose Host or Origin is not the bound address / localhost are refused
(DNS-rebinding guard). Tool dispatch is the MCP server's (``Server._call``), serialised by one lock.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .api import StudioAPI
from .server import Server, tool_list

PAGE = Path(__file__).resolve().parent / "web" / "index.html"
LOCAL_NAMES = {"127.0.0.1", "localhost", "[::1]"}
MAX_BODY = 8 * 1024 * 1024


def _host_only(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):
        return value.split("]")[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def page_bytes() -> bytes:
    """The artifact page is an HTML fragment; wrap it in a document shell for a normal browser."""
    html = PAGE.read_text(encoding="utf-8")
    if "<html" not in html[:2000].lower():
        head = '<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body>\n'
        html = head + html + "\n</body></html>\n"
    return html.encode("utf-8")


class StudioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], api: StudioAPI, token: str, allowed_hosts: set[str]) -> None:
        super().__init__(addr, _Handler)
        self.mcp = Server(api)
        self.api = api
        self.token = token
        self.allowed_hosts = allowed_hosts
        self.lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    server: StudioHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet; calls are logged in tuning.jsonl
        pass

    # ---- helpers
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: dict[str, Any]) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def _fail(self, status: int, code: str, message: str) -> None:
        self._json(status, {"ok": False, "code": code, "message": message})

    def _host_ok(self) -> bool:
        if _host_only(self.headers.get("Host", "")) not in self.server.allowed_hosts:
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return _host_only(urlsplit(origin).netloc) in self.server.allowed_hosts

    def _token_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Studio-Token", "").encode(), self.server.token.encode())

    def _guard(self) -> bool:
        if not self._host_ok():
            self._fail(403, "forbidden_host", "request Host/Origin is not this machine")
            return False
        return True

    # ---- routes
    def do_GET(self) -> None:
        if not self._guard():
            return
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, page_bytes(), "text/html; charset=utf-8")
        elif path == "/api/tools":
            if not self._token_ok():
                return self._fail(401, "unauthorized", "missing or wrong X-Studio-Token")
            self._json(200, {"ok": True, "payload": tool_list()})
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._fail(404, "not_found", path)

    def do_POST(self) -> None:
        if not self._guard():
            return
        if urlsplit(self.path).path != "/api/call":
            return self._fail(404, "not_found", self.path)
        if not self._token_ok():
            return self._fail(401, "unauthorized", "missing or wrong X-Studio-Token")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_BODY:
                return self._fail(413, "too_large", "request body too large")
            body = json.loads(self.rfile.read(n) or b"{}")
            tool, args = body["tool"], body.get("args") or {}
        except (ValueError, KeyError, TypeError):
            return self._fail(400, "bad_request", "body must be JSON {tool, args}")
        with self.server.lock:  # one engine, one caller at a time
            res = self.server.mcp._call({"name": tool, "arguments": args})
        data = res.get("structuredContent")
        if res.get("isError"):
            data = data or {}
            return self._fail(400, str(data.get("code", "error")), str(data.get("error", "tool failed")))
        self._json(200, {"ok": True, "payload": data})

    def do_PUT(self) -> None:
        self._fail(405, "method_not_allowed", "GET or POST only")

    do_DELETE = do_PATCH = do_PUT


def make_server(api: StudioAPI, host: str = "127.0.0.1", port: int = 8765, token: str | None = None) -> StudioHTTPServer:
    if host in ("0.0.0.0", "::", ""):
        raise SystemExit("refusing a wildcard host: pass the specific address to --host (and --token)")
    if host.lower() not in LOCAL_NAMES and not token:
        raise SystemExit("--host other than localhost requires an explicit --token")
    allowed = set(LOCAL_NAMES) | {host.lower()}
    return StudioHTTPServer((host, port), api, token or secrets.token_urlsafe(24), allowed)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m memspine_evals.studio.http_server", description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1; anything else needs --token")
    ap.add_argument("--token", help="session token (default: random per start)")
    ap.add_argument("--data", default="demo", help="demo | path to a LoCoMo-shaped json | locomo")
    ap.add_argument("--out", help="run folder root (default evals/runs/_analysis/inspect/studio, gitignored)")
    ap.add_argument("--allow-gpu", action="store_true")
    ap.add_argument("--arms-dir")
    ap.add_argument("--logs-dir")
    args = ap.parse_args(argv)
    api = StudioAPI(args.data, args.out, allow_gpu=args.allow_gpu, arms_dir=args.arms_dir, logs_dir=args.logs_dir)
    srv = make_server(api, args.host, args.port, args.token)
    print(f"memspine-studio (local http) ready, out={api.out}, allow_gpu={args.allow_gpu}", file=sys.stderr)
    print(f"Open: http://{args.host}:{srv.server_address[1]}/?t={srv.token}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
