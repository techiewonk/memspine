"""MongoDB registry source predicate against the adapter's stored record shape."""

from __future__ import annotations

import pytest

from memspine.store.records import MemoryRecord, RecordStatus


def test_mongo_record_projection_materializes_guard_fields() -> None:
    pytest.importorskip("pymongo")
    from memspine.services.storage.mongodb import MongoRecordsStore

    record = MemoryRecord(
        record_id="source",
        namespace="ns/a",
        memory_type="episodic",
        content="source",
        version=3,
        status=RecordStatus.ARCHIVED,
    )
    fields = MongoRecordsStore._record_fields(record)
    assert fields["namespace"] == "ns/a"
    assert fields["memory_type"] == "episodic"
    assert fields["version"] == 3
    assert fields["status"] == "archived"
    assert fields["quarantined"] is False
