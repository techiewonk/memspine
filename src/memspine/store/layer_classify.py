"""One layer classification for stored rows (LF-E: MF-05, MF-08, MF-09).

Three places used to classify a row on their own: the SQLite 0007 backfill, record hydration (a NULL
``layer`` column is left out and the record derives it) and ``layer_counts`` (``coalesce(layer, 'L0')``).
They agree here: :func:`classify_stored_layer` is the only function that turns ``(memory_type, source)`` of a
stored row into a layer, on top of :func:`memspine.store.layers.derive_layer`. The Postgres backfill
``pg_lf_e_layer_backfill``, ``layer_counts`` and the event upcaster all call it.

Also holds the producer table of the MF-05 event upcaster: channels whose layer is provable from the
record alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import orjson

from memspine.store.layers import (
    LAYERS,
    Layer,
    derive_axes,
    derive_layer,
    derive_provenance_class,
)

__all__ = [
    "LAYER_UPCAST_VERSION",
    "PRODUCER_LAYER",
    "classify_stored_layer",
    "corrected_layer",
    "envelope_backfill_values",
    "load_source",
]

#: Name stamped into the audit trail of the event upcaster (``memory.write`` v2 -> v3).
LAYER_UPCAST_VERSION = "lf-e-layer-v1"

#: Producer channel -> the layer its records belong on. Only these channels are ever corrected on replay.
#: ``session_close`` covers both its episode and its summary kinds.
PRODUCER_LAYER: dict[str, Layer] = {
    "session_close": "L2",
    "consolidation": "L2",
    "multires": "L2",
    "entity_summary": "L3",
    "community_summary": "L3",
    "reorganize": "L3",
    "observation": "L4",
    "standing": "L5",
}

#: Labels a producer bug could have left on these records (lineage on: L0, lineage off: the semantic L1
#: default). A record on any other layer is never touched.
_MISLABELLED = frozenset({"L0", "L1"})


def load_source(blob: object) -> dict[str, Any]:
    """The ``source`` column (an orjson blob, or already a mapping) as a dict; ``{}`` when unreadable."""
    if isinstance(blob, Mapping):
        return dict(blob)
    try:
        data = orjson.loads(blob) if blob else {}  # type: ignore[arg-type]
    except orjson.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def classify_stored_layer(memory_type: str, source: object) -> Layer:
    """The layer of a stored row that carries no layer: ``derive_layer`` of its type and write channel."""
    channel = str(load_source(source).get("channel") or "")
    return derive_layer(memory_type or "", channel)


def corrected_layer(stored: object, channel: object) -> Layer | None:
    """The layer a stored record should carry when its producer channel proves it, else ``None``.

    Only a record on an L0/L1 label (or none) from a channel in :data:`PRODUCER_LAYER` is corrected;
    everything else, L0 turns included, is left as stored."""
    expected = PRODUCER_LAYER.get(str(channel or ""))
    if expected is None or stored == expected:
        return None
    if stored is not None and stored not in _MISLABELLED:
        return None
    return expected


def envelope_backfill_values(
    memory_type: str, source_blob: object, tags_blob: object
) -> dict[str, Any]:
    """Envelope column values for a pre-envelope row (same mapping as SQLite 0007)."""
    source = load_source(source_blob)
    try:
        tags_raw = orjson.loads(tags_blob) if tags_blob else []  # type: ignore[arg-type]
    except orjson.JSONDecodeError:
        tags_raw = []
    tags = [t for t in tags_raw if isinstance(t, str)] if isinstance(tags_raw, list) else []
    channel = str(source.get("channel") or "")
    axes = derive_axes(tags)
    layer = classify_stored_layer(memory_type, source)
    assert layer in LAYERS
    return {
        "layer": layer,
        "type_tag": memory_type,
        "provenance_class": derive_provenance_class(
            str(source.get("role") or ""), channel, bool(source.get("parents"))
        ),
        "spk": axes["speaker"],
        "addr": axes["addressee"],
        "ask": axes["asker"],
        "sub": orjson.dumps(axes["subject"]),
    }
