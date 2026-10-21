"""Validate review data consistency; not runtime or backend erasure proof."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parent


def main():
    budgets = json.loads((ROOT / "operating_defaults.json").read_text())
    schema = json.loads((ROOT / "enrichment.schema.json").read_text())
    registry = json.loads((ROOT / "predicate_vocabulary.json").read_text())["predicates"]
    copies = json.loads((ROOT / "copy_erasure_checks.json").read_text())["copies"]
    validator = Draft202012Validator(schema)
    names = [x["spec"]["name"] for x in registry]
    assert len(names) == len(set(names)) == 43
    for entry in registry:
        validator.validate({"kind": "PredicateSpec", "value": entry["spec"]})
        assert set(entry["spec"]["value_kinds"]) <= {
            "entity",
            "text",
            "number",
            "boolean",
            "temporal",
        }
        assert not entry["spec"]["inference_rule_ids"] and entry["spec"]["closed_world"] is False
        assert bool(entry["attribute_keys"]) == (entry["spec"]["name"] == "has_attribute")
    assert len(copies) == len({x["id"] for x in copies}) == len({x["class"] for x in copies}) == 20
    assert budgets["assembly"]["budget_tokens"] == 4096
    assert budgets["assembly"]["deep_opt_in_budget_tokens"] >= budgets["assembly"]["budget_tokens"]
    assert budgets["retrieval"]["fused_candidates"] >= budgets["ranking"]["optional_rerank_pool"]
    assert budgets["retrieval"]["expanded_source_limit"] >= budgets["retrieval"]["fused_candidates"]
    assert budgets["workers"]["heartbeat_seconds"] < budgets["workers"]["lease_seconds"]
    assert (
        budgets["workers"]["pending_window_high_water_namespace"]
        <= budgets["workers"]["pending_window_high_water_engine"]
    )
    assert (
        len(budgets["workers"]["retry_backoff_seconds"])
        == budgets["workers"]["recovery_attempts"] - 1
    )
    assert (
        budgets["inference"]["generative_calls"] == 0
        and budgets["inference"]["auto_download"] is False
    )
    assert budgets["erasure"]["physical_completion_on_timeout"] is False
    assert budgets["erasure"]["development_backups_enabled"] is False
    assert budgets["erasure"]["backup_content_expiry_days"] == 7
    assert budgets["status"] == "ACCEPTED_DESIGN_UNMEASURED"
    assert set(budgets["accepted_decisions"]) == {"O1-A", "O2-A", "O3-A", "O4-A"}
    result = {
        "status": "review-data consistency passed",
        "predicate_specs": 43,
        "copy_classes": 20,
        "owner_selected_evidence_tokens": 4096,
        "other_numeric_defaults": "accepted starting design, unmeasured",
        "runtime_implementation": False,
        "erasure_proof": False,
    }
    (ROOT / "operating_validation.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
