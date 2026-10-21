"""Focused structural review checks; not an implementation of C01-C22."""

from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).parent
checker = FormatChecker()


@checker.checks("date-time")
def date_time(value):
    if not isinstance(value, str):
        return True
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})", value):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


@checker.checks("date")
def civil_date(value):
    if not isinstance(value, str):
        return True
    try:
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)) and bool(date.fromisoformat(value))
    except ValueError:
        return False


def main():
    schema = json.loads((ROOT / "enrichment.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=checker)
    examples = json.loads((ROOT / "examples.json").read_text())
    for i, item in enumerate(examples):
        errors = list(validator.iter_errors(item))
        assert not errors, (i, item["kind"], [e.message for e in errors])
    negative = []

    def reject(label, item):
        assert not validator.is_valid(item), label
        negative.append(label)

    x = copy.deepcopy(examples[0])
    x["value"]["unexpected"] = True
    reject("unknown field", x)
    x = copy.deepcopy(examples[2])
    x["value"]["value"] = None
    reject("resolved time without value", x)
    x = copy.deepcopy(examples[6])
    x["value"]["entity_id"] = "guessed"
    reject("ambiguous mention with canonical ID", x)
    x = copy.deepcopy(examples[3])
    x["value"]["subject"] = {"entity_id": None, "mention_id": None}
    reject("empty entity reference", x)
    x = copy.deepcopy(examples[3])
    x["value"]["assessment"]["negated"] = True
    reject("duplicate asserted negation encoding", x)
    x = copy.deepcopy(examples[-1])
    x["value"]["coverage"]["kind"] = "complete_scope"
    reject("complete scope without snapshot", x)
    x = copy.deepcopy(examples[-1])
    x["value"]["result_kind"] = "unresolved"
    reject("unresolved computation with answer", x)
    x = copy.deepcopy(examples[1])
    x["value"]["source"]["clock"]["created_at"] = "yesterday"
    reject("invalid timestamp", x)
    x = copy.deepcopy(examples[1])
    x["value"]["source"]["clock"]["created_at"] = "2026-10-18T10:00:00"
    reject("instant without offset", x)
    x = copy.deepcopy(examples[2])
    x["value"]["value"]["at"] = "2026-09-01T00:00:00"
    reject("resolved temporal value without offset", x)
    x = copy.deepcopy(examples[2])
    x["value"]["value"] = {"kind": "date", "value": "2023-05-07"}
    reject("rejected civil date value shape", x)
    x = copy.deepcopy(examples[5])
    x["value"]["copy_manifest"] = []
    reject("persisted bundle without copy manifest", x)
    mapping = json.loads((ROOT / "gap_coverage.json").read_text())
    assert {r["gap_id"] for r in mapping} == {f"NL-{i:02d}" for i in range(1, 53)} and len(
        mapping
    ) == 52
    for row in mapping:
        assert all(name in schema["$defs"] for name in row["schemas"])
        assert all(re.fullmatch(r"C(0[1-9]|1[0-9]|2[0-2])", x) for x in row["contract_checks"])
    result = dict(
        schema_draft="2020-12",
        definitions=len(schema["$defs"]),
        positive_examples=len(examples),
        negative_shape_checks=negative,
        gap_mapping=52,
        behavioural_contracts="C01-C22 specified, not implemented or runtime validated",
        engine_code_changed=False,
    )
    (ROOT / "validation.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
