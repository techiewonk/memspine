"""O4-01b: read-side freshness of the derived layers (O-06).

Every derived record carries ``covers_seq``, the highest source event seq its derivation covered
(``RecordFingerprint.covers_seq``). For a namespace and a derived layer the *gap* is the latest
source seq minus the minimum ``covers_seq`` of the layer's stamped records. A gap above the
configured threshold marks the layer ``stale``; the read path then reports it and, under
``read.freshness_fallback``, adds the raw turns newer than the layer to the candidate pool.

Pure and deterministic: no I/O, no model call. A record with no ``covers_seq`` (a v1 record, or one
derived with ``offline.budget`` off) is *unstamped*: it is counted but never enters the minimum,
and a layer with only unstamped records has no gap (``None``, never stale), because a missing stamp
is not evidence of lag.

:class:`SourceSeqTracker` remembers, per namespace, the seq of every raw turn written, so the latest
source seq is known without scanning the event log at read time.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

__all__ = [
    "FreshnessReport",
    "LayerFreshness",
    "SourceSeqTracker",
    "assess_layer",
    "build_report",
]


@dataclass(frozen=True)
class LayerFreshness:
    layer: str
    records: int
    unstamped: int
    min_covers_seq: int | None
    gap: int | None  # None: no stamped record, so no evidence either way
    stale: bool


@dataclass(frozen=True)
class FreshnessReport:
    namespace: str
    latest_seq: int
    threshold: int
    layers: tuple[LayerFreshness, ...] = ()

    @property
    def stale(self) -> bool:
        return any(layer.stale for layer in self.layers)

    @property
    def stale_layers(self) -> tuple[str, ...]:
        return tuple(layer.layer for layer in self.layers if layer.stale)

    @property
    def stale_from(self) -> int | None:
        """The lowest ``covers_seq`` among the stale layers: raw turns after it are not in them."""
        marks = [x.min_covers_seq for x in self.layers if x.stale and x.min_covers_seq is not None]
        return min(marks) if marks else None

    def as_dict(self) -> dict[str, object]:
        return {
            "namespace": self.namespace,
            "latest_seq": self.latest_seq,
            "threshold": self.threshold,
            "stale": self.stale,
            "stale_layers": list(self.stale_layers),
            "layers": {
                x.layer: {
                    "records": x.records,
                    "unstamped": x.unstamped,
                    "min_covers_seq": x.min_covers_seq,
                    "gap": x.gap,
                    "stale": x.stale,
                }
                for x in self.layers
            },
        }


def assess_layer(
    layer: str, covers: Iterable[int | None], latest_seq: int, threshold: int
) -> LayerFreshness:
    """One layer: ``covers`` is the ``covers_seq`` of each live record (None = unstamped)."""
    values = list(covers)
    stamped = [c for c in values if c is not None]
    if not stamped:
        return LayerFreshness(layer, len(values), len(values), None, None, False)
    low = min(stamped)
    gap = max(0, latest_seq - low)
    return LayerFreshness(layer, len(values), len(values) - len(stamped), low, gap, gap > threshold)


def build_report(
    namespace: str,
    latest_seq: int,
    covers_by_layer: Mapping[str, Iterable[int | None]],
    threshold: int,
) -> FreshnessReport:
    layers = tuple(
        assess_layer(name, covers_by_layer[name], latest_seq, threshold)
        for name in sorted(covers_by_layer)
    )
    return FreshnessReport(namespace, latest_seq, threshold, layers)


@dataclass
class SourceSeqTracker:
    """Per namespace, the ascending seqs (and record ids) of the raw turns written. Bounded: the
    oldest entries beyond ``cap`` are dropped, which can only shorten the fallback list, never
    change ``latest``."""

    cap: int = 50_000
    _seqs: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    _ids: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    _latest: dict[str, int] = field(default_factory=dict)

    def note(self, ns: str, seq: int, record_id: str) -> None:
        seqs, ids = self._seqs[ns], self._ids[ns]
        if seqs and seq <= seqs[-1]:  # replayed or out of order: keep ascending and unique
            if seq in seqs:
                return
            at = bisect_right(seqs, seq)
            seqs.insert(at, seq)
            ids.insert(at, record_id)
        else:
            seqs.append(seq)
            ids.append(record_id)
        self._latest[ns] = max(self._latest.get(ns, 0), seq)
        if len(seqs) > self.cap:
            del seqs[: len(seqs) - self.cap]
            del ids[: len(ids) - self.cap]

    def latest(self, ns: str) -> int:
        return self._latest.get(ns, 0)

    def snapshot(self) -> dict[str, int]:
        """The latest raw seq per namespace right now (what a derivation starting now covers)."""
        return dict(self._latest)

    def beyond(self, ns: str, seq: int, limit: int) -> list[tuple[int, str]]:
        """Raw turns with a seq above ``seq``, newest first, at most ``limit``."""
        seqs, ids = self._seqs.get(ns, []), self._ids.get(ns, [])
        at = bisect_right(seqs, seq)
        pairs = list(zip(seqs[at:], ids[at:], strict=True))
        return pairs[::-1][:limit]

    def count_beyond(self, ns: str, seq: int) -> int:
        seqs = self._seqs.get(ns, [])
        return len(seqs) - bisect_right(seqs, seq)
