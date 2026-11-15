"""Interrupted correction recovery must not trust an older erased-source identity."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from memspine.store.events import EventKind, MemoryEvent
from memspine.trust.pipeline.erasure_state import _erased_correction_witness, load


def _write(seq: int, *, sender: str | None = "Asha", redacted: bool = False) -> MemoryEvent:
    snapshot: dict[str, Any] = {
        "record_id": "correction",
        "namespace": "a",
        "status": "activated",
        "source": {"message": {"sender": {"id": sender}}},
    }
    return MemoryEvent(
        seq=seq,
        kind=EventKind.WRITE,
        namespace="a",
        payload={"record": snapshot, **({"redacted": True} if redacted else {})},
    )


def _manifest(seq: int) -> MemoryEvent:
    return MemoryEvent(
        seq=seq,
        kind=EventKind.MARKER,
        namespace="a",
        payload={
            "marker": "erasure_manifest",
            "manifest": {"ids": {"record_ids": ["correction"]}},
        },
    )


class _Events:
    def __init__(self, events: list[MemoryEvent]) -> None:
        self.events = events
        self.read_calls = 0

    async def read_events(self, *, after_seq: int, limit: int) -> list[MemoryEvent]:
        self.read_calls += 1
        return [e for e in self.events if e.seq is not None and e.seq > after_seq][:limit]

    async def get_record(self, record_id: str) -> None:
        return None


@pytest.mark.parametrize(
    "later",
    [
        _write(2, sender=None),
        _write(2, redacted=True),
        MemoryEvent(
            seq=2,
            kind=EventKind.DECAY_TRANSITION,
            namespace="a",
            payload={"record_id": "correction", "set": {"status": "archived"}},
        ),
        MemoryEvent(
            seq=2,
            kind=EventKind.FORGET,
            namespace="a",
            payload={"record_id": "correction", "hard": False},
        ),
    ],
)
async def test_erased_correction_witness_rejects_stale_sender_or_lifecycle(
    later: MemoryEvent,
) -> None:
    assert (
        await _erased_correction_witness(
            _Events([_write(1), later, _manifest(3)]), "a", "correction"
        )
        is None
    )


async def test_erased_correction_witness_uses_latest_pre_manifest_write_only() -> None:
    witness = await _erased_correction_witness(
        _Events([_write(1), _write(2, sender="Mira"), _manifest(3), _write(4)]),
        "a",
        "correction",
    )
    assert witness is not None and witness.sender_id == "Mira"


async def test_startup_witnesses_use_existing_log_pass_for_many_erased_ids() -> None:
    ids = [f"r-{i}" for i in range(300)]
    events = [
        MemoryEvent(
            seq=i + 1,
            kind=EventKind.WRITE,
            namespace="a",
            payload={"record": {"record_id": rid, "namespace": "a"}},
        )
        for i, rid in enumerate(ids)
    ]
    events.append(
        MemoryEvent(
            seq=301,
            kind=EventKind.MARKER,
            namespace="a",
            payload={
                "marker": "erasure_manifest",
                "manifest": {"ids": {"record_ids": ids}},
            },
        )
    )
    storage = _Events(events)
    eng = SimpleNamespace(
        _storage=storage,
        _config=lambda: SimpleNamespace(event_log=SimpleNamespace(mode="full")),
    )
    await load(eng)
    assert storage.read_calls == 2  # one complete page and the empty terminator
