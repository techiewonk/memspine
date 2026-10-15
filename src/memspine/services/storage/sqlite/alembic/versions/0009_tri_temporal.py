"""F-02b: tri-temporal times with precision on memory_records (M-10).

Adds ``source_at`` (when it was said) and ``source_precision``, plus ``event_precision`` /
``event_approximate`` / ``event_basis`` for the event interval (``valid_from``..``valid_to``). All
nullable: NULL = not known. Guarded add-column like 0007: a fresh database built from the live
``schema.py`` already has them.

Existing rows are backfilled only where a date is derivable (nothing is invented):

* a turn (layer ``L0`` episodic) not tagged ``ts_defaulted``: ``source_at`` = its ``valid_from``, precision
  ``instant``; event precision ``instant``, basis ``explicit``;
* a derived record (has ``source.parents``): ``source_at`` of its earliest dated parent, resolved in
  up to four passes so a chain of derivations is followed;
* a window episode (channel ``window_episode``), or a window fact whose ``valid_from`` equals the date
  of its first parent turn: event basis ``inferred`` at precision ``day`` (the window start); any other
  window fact: ``explicit`` / ``day``.

Downgrade drops the columns; the mapping above restores them, so no data is lost.

Revision ID: f02b_tri_temporal
Revises: f04_outbox
Create Date: 2026-10-13
"""

from __future__ import annotations

import orjson
import sqlalchemy as sa
from alembic import op

revision = "f02b_tri_temporal"
down_revision = "f04_outbox"
branch_labels = None
depends_on = None

_TABLE = "memory_records"
_COLUMNS = (
    ("source_at", sa.String),
    ("source_precision", sa.String),
    ("event_precision", sa.String),
    ("event_approximate", sa.Integer),
    ("event_basis", sa.String),
)
_UNDATED_TAG = "ts_defaulted"  # constants.UNDATED_TAG, copied so the revision stays frozen
_WINDOW_EPISODE = "window_episode"


def _columns() -> set[str]:
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _loads(blob: object, default: object) -> object:
    try:
        return orjson.loads(blob) if blob else default  # type: ignore[arg-type]
    except orjson.JSONDecodeError:
        return default


def upgrade() -> None:
    columns = _columns()
    added = False
    for name, kind in _COLUMNS:
        if name not in columns:
            op.add_column(_TABLE, sa.Column(name, kind(), nullable=True))
            added = True
    if not added:
        return
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(f"SELECT record_id, memory_type, valid_from, source, tags FROM {_TABLE}")
    ).all()
    valid_from: dict[str, str] = {}
    info: dict[str, tuple[str, list[str], list[str], str]] = {}
    for record_id, memory_type, vfrom, source_blob, tags_blob in rows:
        source = _loads(source_blob, {})
        source = source if isinstance(source, dict) else {}
        tags = _loads(tags_blob, [])
        tags = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
        parents = [p for p in (source.get("parents") or []) if isinstance(p, str)]
        valid_from[record_id] = vfrom
        info[record_id] = (memory_type, parents, tags, str(source.get("channel") or ""))
    source_at: dict[str, tuple[str, str]] = {}
    for record_id, (memory_type, parents, tags, _channel) in info.items():
        if memory_type == "episodic" and not parents and _UNDATED_TAG not in tags:
            source_at[record_id] = (valid_from[record_id], "instant")
    for _ in range(4):  # follow derivation chains
        grew = False
        for record_id, (_t, parents, _tags, _channel) in info.items():
            if record_id in source_at or not parents:
                continue
            dated = [source_at[p] for p in parents if p in source_at]
            if dated:
                source_at[record_id] = min(dated)
                grew = True
        if not grew:
            break
    for record_id, (memory_type, parents, tags, channel) in info.items():
        values: dict[str, object] = {"id": record_id}
        event: tuple[str, str] | None = None
        if record_id in source_at:
            values["sa"], values["sp"] = source_at[record_id]
            if memory_type == "episodic" and not parents:
                event = ("instant", "explicit")
        if parents and (channel == _WINDOW_EPISODE or "window_fact" in tags):
            starts = [valid_from[p][:10] for p in parents if p in valid_from]
            if channel == _WINDOW_EPISODE or (starts and valid_from[record_id][:10] == min(starts)):
                event = ("day", "inferred")
            else:
                event = ("day", "explicit")
        if event is not None:
            values["ep"], values["eb"] = event
        if len(values) == 1:
            continue
        bind.execute(
            sa.text(
                f"UPDATE {_TABLE} SET source_at = :sa, source_precision = :sp, "
                "event_precision = :ep, event_basis = :eb WHERE record_id = :id"
            ),
            {"sa": None, "sp": None, "ep": None, "eb": None, **values},
        )


def downgrade() -> None:
    columns = _columns()
    for name, _kind in reversed(_COLUMNS):
        if name in columns:
            op.drop_column(_TABLE, name)
