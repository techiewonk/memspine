"""RM-66: the read-only learned.alias diagnostic over a real memory_events table."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import zstandard
from sqlalchemy import create_engine, insert

from memspine.services.storage.sqlite.schema import memory_events
from memspine.store.events import EventKind, MemoryEvent, canonical_payload

_SPEC = importlib.util.spec_from_file_location(
    "audit_learned_alias_events",
    Path(__file__).resolve().parents[2] / "scripts/audit_learned_alias_events.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_AUDIT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _AUDIT  # dataclasses resolve their module through sys.modules
_SPEC.loader.exec_module(_AUDIT)


def _log(path: Path, events: list[tuple[EventKind, str, dict[str, Any]]], *, zstd: bool = False):
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    memory_events.create(engine)
    cctx = zstandard.ZstdCompressor()
    with engine.begin() as conn:
        for kind, namespace, payload in events:
            ev = MemoryEvent(kind=kind, namespace=namespace, actor="test", payload=payload)
            raw = canonical_payload(ev.payload)
            conn.execute(
                insert(memory_events).values(
                    event_id=ev.event_id,
                    kind=ev.kind.value,
                    namespace=ev.namespace,
                    ts=ev.ts.isoformat(),
                    actor=ev.actor,
                    schema_version=ev.schema_version,
                    payload=cctx.compress(raw) if zstd else raw,
                    compressed=zstd,
                    fingerprint=ev.fingerprint,
                )
            )
    engine.dispose()
    return path


def _alias(ns: str, *names: str) -> tuple[EventKind, str, dict[str, Any]]:
    return (EventKind.MARKER, ns, {"marker": "learned.alias", "names": list(names)})


def _write(ns: str, content: str) -> tuple[EventKind, str, dict[str, Any]]:
    return (EventKind.WRITE, ns, {"content": content})


def test_names_from_another_namespace_are_hits(tmp_path: Path, capsys: Any) -> None:
    db = _log(
        tmp_path / "log.sqlite",
        [
            _write("alice", "Met Dr. Okafor at the clinic"),
            _write("bob", "Bob talked about Zanzibar"),
            # pre-fix flush: alice's flush wrote bob's alias as well
            _alias("alice", "Dr. Okafor", "Zanzibar"),
            _alias("bob", "Nowhere Person"),
        ],
        zstd=True,
    )
    report = _AUDIT.scan(db).as_dict()
    assert report["alias_events"] == 2 and report["namespaces"] == 2
    assert (report["own"], report["foreign_only"], report["unseen"]) == (1, 1, 1)
    assert report["hits"] == [
        {"namespace": "alice", "name": "Zanzibar", "seq": 3, "found_in": ["bob"]}
    ]
    assert _AUDIT.main([f"sqlite:///{db.as_posix()}"]) == 1
    assert "foreign_only=1" in capsys.readouterr().out


def test_reset_drops_earlier_names_and_clean_log_exits_zero(tmp_path: Path) -> None:
    db = _log(
        tmp_path / "log.sqlite",
        [
            _write("bob", "Zanzibar trip"),
            _alias("alice", "Zanzibar"),
            (EventKind.MARKER, "alice", {"marker": "learned.alias.reset"}),
            _write("alice", "Okafor again"),
            _alias("alice", "Okafor"),
        ],
    )
    report = _AUDIT.scan(db).as_dict()
    assert report["names_dropped_by_reset"] == 1 and report["reset_events"] == 1
    assert (report["own"], report["foreign_only"]) == (1, 0)
    assert _AUDIT.main([str(db), "--json"]) == 0


def test_the_log_is_opened_read_only(tmp_path: Path) -> None:
    db = _log(tmp_path / "log.sqlite", [_alias("a", "X")])
    before = db.read_bytes()
    _AUDIT.scan(db)
    assert db.read_bytes() == before


def test_bad_targets_are_usage_errors(tmp_path: Path) -> None:
    assert _AUDIT.main([str(tmp_path / "missing.sqlite")]) == 2
    assert _AUDIT.main(["postgresql://h/db"]) == 2
    assert _AUDIT.main(["sqlite:///:memory:"]) == 2
