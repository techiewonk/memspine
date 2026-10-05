"""Ingest speed-ups must not change what ``write_messages`` produces.

The same 60 LoCoMo-like turns (3 sessions x 20, one ``write_messages`` call per
session) are ingested on the ``base`` and ``core`` templates, on an in-memory
and on a file-backed store. The records (content, status, trust and the rest),
the event log, and the results of five searches are compared against a fixture
generated from the code before the ingest optimisations.

Record and event ids are random, so they are replaced by ordinals in order of
first appearance in the log; wall-clock timestamps (anything not in 2023, the
turns' own year) are replaced by a placeholder. Floats are compared with a
relative tolerance of 1e-9, except search scores (see ``_clusters``).

Tantivy breaks BM25 ties by segment order, which varies from run to run even
on unchanged code (its segments merge in the background), so the hybrid
search would not repeat itself. ``_stable_lexical_ties`` makes the lexical leg
break ties by indexing order for the whole observation, in the reference run
as in the checked one.

Regenerate the fixture (only from code whose behaviour is the reference) with
``python tests/integration/test_ingest_equivalence.py --regen``.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.services.lexical.base import LexicalHit
from memspine.services.lexical.tantivy import TantivyLexical

FIXTURE = Path(__file__).parent / "fixtures" / "ingest_equivalence.json"

_WORDS = [
    "caroline",
    "melanie",
    "camping",
    "guitar",
    "adoption",
    "painting",
    "lake",
    "sunrise",
    "school",
    "talk",
    "support",
    "group",
    "birthday",
    "mountains",
    "kids",
    "weekend",
    "research",
    "agency",
    "journey",
    "pottery",
    "museum",
    "dog",
    "cat",
    "hike",
    "concert",
    "festival",
    "book",
    "novel",
    "friend",
    "family",
    "dinner",
    "recipe",
    "garden",
]

QUERIES = (
    "When did Caroline go camping in the mountains?",
    "What did Melanie paint at the lake?",
    "adoption agency research",
    "Which concert or festival did they attend?",
    "pottery museum with the kids",
)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_VOLATILE_EVENT_KEYS = ("event_id", "ts", "fingerprint")


def _sessions() -> list[tuple[str, list[dict[str, str]]]]:
    rnd = random.Random(7)
    base = datetime(2023, 5, 1, 13, 56, tzinfo=UTC)
    sessions: list[tuple[str, list[dict[str, str]]]] = []
    for s in range(3):
        turns: list[dict[str, str]] = []
        for t in range(20):
            speaker = "Caroline" if t % 2 == 0 else "Melanie"
            words = " ".join(rnd.choice(_WORDS) for _ in range(rnd.randint(4, 24)))
            if t == 7:  # an exact repeat inside the session (dedup / tie handling)
                words = turns[1]["content"].split(": ", 1)[1]
            turns.append(
                {
                    "role": "user" if t % 2 == 0 else "assistant",
                    "content": f"{speaker}: {words}.",
                    "timestamp": (base + timedelta(days=s, minutes=t)).isoformat(),
                }
            )
        sessions.append((f"s{s}", turns))
    return sessions


class _Normaliser:
    """Replaces random ids by ordinals and wall-clock stamps by a placeholder."""

    def __init__(self) -> None:
        self._ids: dict[str, str] = {}

    def id(self, value: str) -> str:
        return self._ids.setdefault(value, f"id{len(self._ids)}")

    def __call__(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self(item) for key, item in sorted(value.items())}
        if isinstance(value, list | tuple):
            return [self(item) for item in value]
        if isinstance(value, str):
            if _ISO.match(value) and not value.startswith("2023-"):
                return "<now>"
            return _UUID.sub(lambda m: self.id(m.group(0)), value)
        return value


@contextmanager
def _stable_lexical_ties() -> Iterator[None]:
    """Break equal BM25 scores by first indexing order, not segment order."""
    order: dict[str, int] = {}
    add_doc, search = TantivyLexical._add_doc, TantivyLexical._search

    def _add(self: TantivyLexical, record_id: str, namespace: str, content: str) -> None:
        order.setdefault(record_id, len(order))
        add_doc(self, record_id, namespace, content)

    def _search(
        self: TantivyLexical, namespace: str, terms: list[str], top_k: int
    ) -> list[LexicalHit]:
        hits = search(self, namespace, terms, 1_000_000)  # every match: no cut on a tie
        hits.sort(key=lambda hit: (-hit.score, order.get(hit.record_id, len(order))))
        return hits[:top_k]

    TantivyLexical._add_doc = _add  # type: ignore[method-assign]
    TantivyLexical._search = _search  # type: ignore[method-assign]
    try:
        yield
    finally:
        TantivyLexical._add_doc = add_doc  # type: ignore[method-assign]
        TantivyLexical._search = search  # type: ignore[method-assign]


async def _observe(template: str, storage_path: str) -> dict[str, Any]:
    with _stable_lexical_ties():
        return await _observe_engine(template, storage_path)


async def _observe_engine(template: str, storage_path: str) -> dict[str, Any]:
    engine = Engine(
        template=template,
        dotenv_path=None,
        storage={"path": storage_path},
        embedding={"provider": "hash", "batch_size": 32},
    )
    await engine.start()
    try:
        for session_id, turns in _sessions():
            await engine.write_messages(
                turns, namespace="conv", session_id=session_id, group_id=session_id
            )
        assert engine._storage is not None
        events = await engine._storage.read_events(after_seq=0, limit=1_000_000)
        norm = _Normaliser()
        event_rows = []
        for event in events:
            row = event.model_dump(mode="json")
            for key in _VOLATILE_EVENT_KEYS:
                row.pop(key, None)
            event_rows.append(norm(row))
        records = await engine.retrieve(namespace="conv", include_held=True)
        record_rows = sorted(
            (norm(record.model_dump(mode="json")) for record in records),
            key=lambda row: row["record_id"],
        )
        searches = []
        for query in QUERIES:
            hits = await engine.search(query, namespace="conv", top_k=8)
            searches.append([[norm.id(record.record_id), score] for record, score in hits])
        return {"events": event_rows, "records": record_rows, "searches": searches}
    finally:
        await engine.stop()


def _observe_all(tmp_dir: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for template in ("base", "core"):
        out[f"{template}:memory"] = asyncio.run(_observe(template, ":memory:"))
        db = tmp_dir / f"{template}.db"
        out[f"{template}:file"] = asyncio.run(_observe(template, str(db)))
    return out


def _clusters(hits: list[list[Any]]) -> list[tuple[list[str], list[float]]]:
    """Ranked hits grouped into runs of near-equal scores, ids sorted per run.

    Scores carry a recency term that drifts by ~1e-7 between runs, so two hits
    with equal relevance (a repeated turn) may swap places run to run even on
    unchanged code. Order is compared between runs, not inside one."""
    clusters: list[tuple[list[str], list[float]]] = []
    previous: float | None = None
    for record_id, score in hits:
        if previous is None or abs(previous - score) > _TIE:
            clusters.append(([], []))
        clusters[-1][0].append(record_id)
        clusters[-1][1].append(score)
        previous = score
    return [(sorted(ids), scores) for ids, scores in clusters]


def _assert_searches(got: list[Any], want: list[Any], path: str) -> None:
    assert len(got) == len(want), path
    for i, (g, w) in enumerate(zip(got, want, strict=True)):
        got_clusters, want_clusters = _clusters(g), _clusters(w)
        assert [ids for ids, _ in got_clusters] == [ids for ids, _ in want_clusters], (
            f"{path}[{i}]: {g} != {w}"
        )
        for (_, got_scores), (_, want_scores) in zip(got_clusters, want_clusters, strict=True):
            assert got_scores == pytest.approx(want_scores, abs=_SCORE_DRIFT), f"{path}[{i}]"


_TIE = 1e-5  # scores closer than this are a tie (recency drift is ~1e-7)
_SCORE_DRIFT = 1e-4  # recency moves scores by ~1e-7 per second between runs


def _assert_same(got: Any, want: Any, path: str = "$") -> None:
    if path.endswith(".searches"):
        _assert_searches(got, want, path)
        return
    if isinstance(want, dict):
        assert isinstance(got, dict), path
        assert sorted(got) == sorted(want), f"{path}: keys {sorted(got)} != {sorted(want)}"
        for key in want:
            _assert_same(got[key], want[key], f"{path}.{key}")
    elif isinstance(want, list):
        assert isinstance(got, list), path
        assert len(got) == len(want), f"{path}: length {len(got)} != {len(want)}"
        for i, (g, w) in enumerate(zip(got, want, strict=True)):
            _assert_same(g, w, f"{path}[{i}]")
    elif isinstance(want, float) and not isinstance(want, bool):
        assert got == pytest.approx(want, rel=1e-9, abs=1e-12), f"{path}: {got} != {want}"
    else:
        assert got == want, f"{path}: {got!r} != {want!r}"


@pytest.mark.parametrize("variant", ["base:memory", "base:file", "core:memory", "core:file"])
def test_write_messages_matches_pre_optimisation_fixture(variant: str, tmp_path: Path) -> None:
    template, store = variant.split(":")
    path = ":memory:" if store == "memory" else str(tmp_path / f"{template}.db")
    got = asyncio.run(_observe(template, path))
    want = json.loads(FIXTURE.read_text(encoding="utf-8"))[variant]
    assert len(got["records"]) == 60
    _assert_same(got, want)


if __name__ == "__main__":  # pragma: no cover - fixture regeneration
    import tempfile

    if "--regen" not in sys.argv:
        raise SystemExit("usage: python test_ingest_equivalence.py --regen")
    with tempfile.TemporaryDirectory() as tmp:
        data = _observe_all(Path(tmp))
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {FIXTURE}")
