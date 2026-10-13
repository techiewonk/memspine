# ruff: noqa: E501
"""F-02a: the record envelope on memory_records (layer, type tag, provenance class, axes, fingerprint).

Adds ``layer`` (M-02), ``type_tag`` (M-03), ``provenance_class`` (M-07), the ``spk`` / ``addr`` / ``ask``
axis columns plus the ``sub`` subject blob (M-08), the ``fingerprint`` blob (P-12) and the record's
``schema_version`` (M-26, default 1). Guarded add-column like 0002 to 0005: a fresh database built from
the live ``schema.py`` already has them.

Existing rows are backfilled from their own data with the same mapping new records use
(``memspine.store.layers``): ``layer`` from memory type and write channel, ``type_tag`` = the memory type,
``provenance_class`` from role, channel and parents, the axes from the ``spk:``/``addr:``/``ask:``/``sub:``
tags. ``fingerprint`` stays NULL: the record derives it from its content fingerprint and prompt version.
Downgrade drops the columns; nothing else reads them, so no data is lost that the mapping cannot restore.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-13
"""

from __future__ import annotations

import orjson
import sqlalchemy as sa
from alembic import op

from memspine.store.layers import derive_axes, derive_layer, derive_provenance_class

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_TABLE = "memory_records"
_INDEX = "ix_memory_records_ns_layer"
_TEXT = ("layer", "type_tag", "provenance_class", "spk", "addr", "ask")
_BLOB = ("sub", "fingerprint")


def _columns() -> set[str]:
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def _indexes() -> set[str]:
    return {str(ix["name"]) for ix in sa.inspect(op.get_bind()).get_indexes(_TABLE) if ix["name"]}


def _loads(blob: object, default: object) -> object:
    try:
        return orjson.loads(blob) if blob else default  # type: ignore[arg-type]
    except orjson.JSONDecodeError:
        return default


def upgrade() -> None:
    columns = _columns()
    added = False
    for name in _TEXT:
        if name not in columns:
            op.add_column(_TABLE, sa.Column(name, sa.String(), nullable=True))
            added = True
    for name in _BLOB:
        if name not in columns:
            op.add_column(_TABLE, sa.Column(name, sa.LargeBinary(), nullable=True))
            added = True
    if "schema_version" not in columns:
        op.add_column(
            _TABLE,
            sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        )
        added = True
    if _INDEX not in _indexes():
        op.create_index(_INDEX, _TABLE, ["namespace", "layer"])
    if not added:
        return
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT record_id, memory_type, source, tags FROM {_TABLE}")).all()
    for record_id, memory_type, source_blob, tags_blob in rows:
        source = _loads(source_blob, {})
        tags = _loads(tags_blob, [])
        source = source if isinstance(source, dict) else {}
        tags = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
        channel = str(source.get("channel") or "")
        axes = derive_axes(tags)
        bind.execute(
            sa.text(
                f"UPDATE {_TABLE} SET layer = :layer, type_tag = :type_tag, "
                "provenance_class = :pc, spk = :spk, addr = :addr, ask = :ask, sub = :sub "
                "WHERE record_id = :id"
            ),
            {
                "layer": derive_layer(memory_type, channel),
                "type_tag": memory_type,
                "pc": derive_provenance_class(
                    str(source.get("role") or ""), channel, bool(source.get("parents"))
                ),
                "spk": axes["speaker"],
                "addr": axes["addressee"],
                "ask": axes["asker"],
                "sub": orjson.dumps(axes["subject"]),
                "id": record_id,
            },
        )


def downgrade() -> None:
    if _INDEX in _indexes():
        op.drop_index(_INDEX, table_name=_TABLE)
    columns = _columns()
    for name in ("schema_version", *_BLOB, *reversed(_TEXT)):
        if name in columns:
            op.drop_column(_TABLE, name)
