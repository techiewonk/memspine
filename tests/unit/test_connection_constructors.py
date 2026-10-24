"""Connection constructors live only in ``memspine.clients`` (RM-113 enforcement).

docs/CLIENT_ADAPTER_FACTORY_STANDARD.md section 1: a connector (``clients/<tech>/``) owns the
connection, pool, file handle or weight download; adapters (``services/**``, ``models/**``)
and every other package receive it injected. This test walks ``src/memspine/**`` with
``ast`` and flags calls of the denylisted constructors below anywhere outside
``src/memspine/clients/**`` and Alembic ``env.py`` scripts. Import aliases are resolved
(``from redis.asyncio import Redis as R; R(...)`` is ``Redis(``).

Baseline: ``tests/unit/baselines/connection_constructors.txt``, one entry per line,
``<file under src/> <symbol> -- <reason / RM-113 work item>``. A call that is not in the
baseline fails the test; a baselined entry that no longer occurs fails it too (delete the
line in the same PR), so the baseline only shrinks.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
ROOT = "memspine"
BASELINE_FILE = REPO / "tests" / "unit" / "baselines" / "connection_constructors.txt"


def _last(n: str, *names: str) -> bool:
    return n.rsplit(".", 1)[-1] in names


def _tail(n: str, tail: str) -> bool:
    return n == tail or n.endswith("." + tail)


# canonical symbol -> predicate over the alias-resolved dotted callee
RULES: dict[str, Callable[[str], bool]] = {
    "create_engine": lambda n: _last(n, "create_engine"),
    "create_async_engine": lambda n: _last(n, "create_async_engine"),
    "sqlite3.connect": lambda n: n == "sqlite3.connect",
    "psycopg.connect": lambda n: (
        n in {"psycopg.connect", "psycopg.Connection.connect", "psycopg.AsyncConnection.connect"}
    ),
    "lancedb.connect": lambda n: n in {"lancedb.connect", "lancedb.connect_async"},
    "tantivy.Index": lambda n: n in {"tantivy.Index", "tantivy.Index.open"},
    "GraphDatabase.driver": lambda n: (
        _tail(n, "GraphDatabase.driver") or _tail(n, "AsyncGraphDatabase.driver")
    ),
    "AsyncOpenSearch": lambda n: _last(n, "AsyncOpenSearch", "OpenSearch"),
    "weaviate.connect": lambda n: (
        n.startswith("weaviate.")
        and n.rsplit(".", 1)[-1].startswith(("connect_to_", "use_async_with_"))
    ),
    "Redis": lambda n: _last(n, "Redis", "StrictRedis") or _tail(n, "Redis.from_url"),
    "httpx.Client": lambda n: n in {"httpx.Client", "httpx.AsyncClient"},
    "urlopen": lambda n: _last(n, "urlopen") or n == "urllib.request.build_opener",
    "temporalio.Client.connect": lambda n: n == "temporalio.client.Client.connect",
    "boto3.client": lambda n: (
        n in {"boto3.client", "boto3.resource", "boto3.Session", "boto3.session.Session"}
    ),
    "MongoClient": lambda n: _last(n, "MongoClient", "AsyncIOMotorClient", "AsyncMongoClient"),
    "snapshot_download": lambda n: _last(n, "snapshot_download", "hf_hub_download"),
}


def _allowed(rel: str) -> bool:
    return rel.startswith(f"{ROOT}/clients/") or (
        "/alembic/" in rel and rel.endswith("/alembic/env.py")
    )


def _aliases(tree: ast.AST) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    out[a.asname] = a.name
                else:
                    out.setdefault(a.name.split(".")[0], a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            for a in node.names:
                out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def scan_source(text: str, filename: str = "<src>") -> set[str]:
    tree = ast.parse(text, filename=filename)
    aliases = _aliases(tree)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted(node.func)
        if name is None:
            continue
        head, _, rest = name.partition(".")
        resolved = aliases.get(head, head) + (f".{rest}" if rest else "")
        for symbol, pred in RULES.items():
            if pred(resolved):
                hits.add(symbol)
    return hits


def scan(src: Path = SRC) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for f in sorted((src / ROOT).rglob("*.py")):
        rel = f.relative_to(src).as_posix()
        if _allowed(rel):
            continue
        for symbol in scan_source(f.read_text(encoding="utf-8"), str(f)):
            found.add((rel, symbol))
    return found


def load_baseline(path: Path = BASELINE_FILE) -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body, sep, reason = line.partition(" -- ")
        fields = body.split()
        if not sep or len(fields) != 2 or not reason.strip():
            raise ValueError(f"{path.name}:{no}: expected '<file> <symbol> -- <reason>'")
        key = (fields[0], fields[1])
        if key in out:
            raise ValueError(f"{path.name}:{no}: duplicate entry {key}")
        if fields[1] not in RULES:
            raise ValueError(f"{path.name}:{no}: unknown symbol {fields[1]!r}")
        if "RM-113" not in reason:
            raise ValueError(f"{path.name}:{no}: reason must cite an RM-113 work item")
        out[key] = reason.strip()
    return out


# ---------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------


def test_no_new_connection_constructors_and_baseline_is_honest():
    baseline = set(load_baseline())
    found = scan()
    rel = BASELINE_FILE.relative_to(REPO).as_posix()
    new = sorted(found - baseline)
    gone = sorted(baseline - found)
    assert not new, (
        "connection constructors outside src/memspine/clients (inject a connector; never "
        f"grow {rel}):\n" + "\n".join(f"  {f} {s}" for f, s in new)
    )
    assert not gone, (
        f"baselined constructor calls no longer occur; delete these lines from {rel}:\n"
        + "\n".join(f"  {f} {s}" for f, s in gone)
    )


@pytest.mark.parametrize(
    ("code", "symbol"),
    [
        ("import sqlite3\nsqlite3.connect('x')\n", "sqlite3.connect"),
        ("import sqlite3 as s\ns.connect('x')\n", "sqlite3.connect"),
        ("from sqlalchemy import create_engine\ncreate_engine('x')\n", "create_engine"),
        ("import sqlalchemy as sa\nsa.create_engine('x')\n", "create_engine"),
        (
            "from sqlalchemy.ext.asyncio import create_async_engine\ncreate_async_engine('x')\n",
            "create_async_engine",
        ),
        ("import lancedb\nlancedb.connect('x')\n", "lancedb.connect"),
        ("import tantivy\ntantivy.Index(s)\n", "tantivy.Index"),
        ("from tantivy import Index\nIndex.open('p')\n", "tantivy.Index"),
        (
            "from neo4j import AsyncGraphDatabase\nAsyncGraphDatabase.driver(u)\n",
            "GraphDatabase.driver",
        ),
        (
            "from opensearchpy import AsyncOpenSearch\nAsyncOpenSearch(hosts=[])\n",
            "AsyncOpenSearch",
        ),
        ("import weaviate\nweaviate.connect_to_local()\n", "weaviate.connect"),
        ("import weaviate\nweaviate.use_async_with_custom()\n", "weaviate.connect"),
        ("def f():\n    from redis.asyncio import Redis\n    Redis()\n", "Redis"),
        ("import redis\nredis.Redis.from_url('u')\n", "Redis"),
        ("import httpx\nhttpx.AsyncClient()\n", "httpx.Client"),
        ("from urllib.request import urlopen\nurlopen('u')\n", "urlopen"),
        ("import urllib.request\nurllib.request.urlopen('u')\n", "urlopen"),
        ("import urllib.request\nurllib.request.build_opener(h)\n", "urlopen"),
        ("import psycopg\npsycopg.connect('dsn')\n", "psycopg.connect"),
        (
            "from temporalio.client import Client\nClient.connect('h')\n",
            "temporalio.Client.connect",
        ),
        ("import boto3\nboto3.client('s3')\n", "boto3.client"),
        ("from pymongo import MongoClient\nMongoClient()\n", "MongoClient"),
        (
            "from motor.motor_asyncio import AsyncIOMotorClient\nAsyncIOMotorClient()\n",
            "MongoClient",
        ),
        (
            "from huggingface_hub import snapshot_download\nsnapshot_download('r')\n",
            "snapshot_download",
        ),
    ],
)
def test_scanner_flags(code: str, symbol: str):
    assert symbol in scan_source(code)


def test_scanner_ignores_non_calls_and_lookalikes():
    code = (
        "import sqlite3\nfrom sqlalchemy.engine import Engine\n"
        "x: sqlite3.Connection\nconn.connect()\nEngine\n"
        "import httpx\nhttpx.Timeout(1)\n"
    )
    assert scan_source(code) == set()


def test_scan_skips_clients_and_alembic_env(tmp_path: Path):
    src = tmp_path / "src"
    for rel in (
        "memspine/clients/sqlite.py",
        "memspine/services/storage/alembic/env.py",
        "memspine/services/x.py",
    ):
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("import sqlite3\nsqlite3.connect('x')\n", encoding="utf-8")
    assert scan(src) == {("memspine/services/x.py", "sqlite3.connect")}


def test_baseline_parser_rejects_bad_lines(tmp_path: Path):
    p = tmp_path / "b.txt"
    for bad in (
        "memspine/a.py sqlite3.connect\n",
        "memspine/a.py nope -- RM-113 x\n",
        "memspine/a.py sqlite3.connect -- no ref\n",
        "memspine/a.py urlopen -- RM-113 x\nmemspine/a.py urlopen -- RM-113 y\n",
    ):
        p.write_text(bad, encoding="utf-8")
        with pytest.raises(ValueError):
            load_baseline(p)
