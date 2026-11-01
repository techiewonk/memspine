from __future__ import annotations

from scripts.build_task_decision_dashboard import OUTPUT, _records, build
from scripts.refresh_tracker import SOURCE, tasks


def test_task_decision_dashboard_covers_canonical_register() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    records = _records(source)

    rows = tasks(source)
    assert [r["id"] for r in records] == [row[0] for row in rows]
    assert [r["status"] for r in records] == [row[1] for row in rows]
    assert all(row["impact"] and row["suggested_action"] for row in records)


def test_task_decision_dashboard_is_current_and_interactive() -> None:
    rendered = build()

    assert OUTPUT.read_text(encoding="utf-8") == rendered
    assert "Export decisions JSON" in rendered
    assert "Import decisions JSON" in rendered
    assert 'id="hide-done"' in rendered
    assert "localStorage" in rendered
    assert "memspine-task-decisions/v1" in rendered
