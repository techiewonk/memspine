"""Generate accepted design defaults; no runtime configuration changes."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parent
BUDGETS = {
    "status": "ACCEPTED_DESIGN_UNMEASURED",
    "accepted_decisions": ["O1-A", "O2-A", "O3-A", "O4-A"],
    "runtime_implemented": False,
    "profile": "balanced_non_llm",
    "windows": {
        "max_turns": 6,
        "estimated_tokens": 1500,
        "idle_seconds": 120,
        "oversize_source_chunk_characters": 6000,
        "chunk_overlap_characters": 256,
    },
    "context": {"max_sources": 32, "max_characters": 24000, "reply_hops": 2},
    "retrieval": {
        "exact_alias_candidates": 16,
        "bm25_candidates": 40,
        "optional_vector_candidates": 40,
        "temporal_candidates": 16,
        "fused_candidates": 96,
        "expanded_source_limit": 128,
        "neighbours_before": 2,
        "neighbours_after": 2,
        "dependency_hops": 3,
        "max_sources_per_bundle": 8,
    },
    "ranking": {
        "optional_rerank_pool": 48,
        "reranker_batch_size": 16,
        "max_selected_bundles": 12,
        "identity_similarity_auto_accept": False,
        "uncalibrated_score_threshold": None,
    },
    "assembly": {
        "budget_tokens": 4096,
        "hard_character_limit": 16384,
        "deep_opt_in_budget_tokens": 4096,
        "deep_opt_in_hard_character_limit": 16384,
        "tokenizer_required_for_exact_token_claim": True,
        "mandatory_dependency_drop_allowed": False,
    },
    "workers": {
        "active_per_engine": 1,
        "active_per_namespace": 1,
        "pending_window_high_water_namespace": 256,
        "pending_window_high_water_engine": 1024,
        "reconcile_items_per_batch": 100,
        "cooperative_slice_seconds": 2,
        "lease_seconds": 60,
        "heartbeat_seconds": 10,
        "recovery_attempts": 3,
        "retry_backoff_seconds": [1, 5],
        "scan_interval_seconds": 5,
        "unresolved_retention_days": None,
    },
    "inference": {
        "generative_calls": 0,
        "auto_download": False,
        "logical_ner_operations_per_job": 6,
        "logical_embedding_operations_per_job": 2,
        "logical_rerank_operations_per_read": 1,
        "concurrent_inference_per_shared_model": 1,
        "required_model_deadline_seconds": 30,
    },
    "erasure": {
        "deny_reads_after_tombstone": True,
        "poll_interval_seconds": 5,
        "purge_page_size": 100,
        "foreground_wait_seconds": 2,
        "verification_attempts": 3,
        "verification_backoff_seconds": [1, 5],
        "active_copy_target_seconds": 60,
        "development_backups_enabled": False,
        "backup_content_expiry_days": 7,
        "content_logging": False,
        "manifest_retention_days": None,
        "physical_completion_on_timeout": False,
    },
}

PEOPLE = ["person", "agent", "group"]
SUBJECTS = PEOPLE + ["organisation"]


def entry(name, subject, kinds, semantics, objects=(), notes="", transition="append_only"):
    return {
        "spec": {
            "name": name,
            "version": 1,
            "subject_types": subject,
            "value_kinds": kinds,
            "semantics": semantics,
            "cardinality": "many",
            "closed_world": False,
            "transition_policy": transition,
            "inference_rule_ids": [],
            "allowed_source_origins": ["raw_message", "document", "attachment_text"],
        },
        "object_entity_types": list(objects),
        "attribute_keys": (
            [
                "colour",
                "material",
                "breed",
                "model",
                "title",
                "language",
                "genre",
                "activity_type",
                "art_form",
            ]
            if name == "has_attribute"
            else []
        ),
        "enablement": "accepted_vocabulary_extraction_gated_not_implemented",
        "notes": notes,
    }


P = []


def add(*args, **kwargs):
    P.append(entry(*args, **kwargs))


for n in ["resides_in", "works_in", "home_country", "born_in", "grew_up_in"]:
    add(
        n,
        PEOPLE,
        ["entity"],
        "state" if n in ["resides_in", "works_in"] else "relation",
        ["place"],
        "Separate roles; no nationality/residence inference. Multiple supported places allowed.",
        transition="explicit_only",
    )
for n in ["move_origin", "move_destination", "visited", "temporarily_present_in"]:
    add(
        n,
        PEOPLE,
        ["entity"],
        "event",
        ["place"],
        "Domain event/occurrence and source support required; visit does not change residence.",
    )
for n in ["parent_of", "sibling_of", "spouse_of", "friend_of", "member_of", "employed_by"]:
    subjects = (
        ["person"] if n in ["parent_of", "sibling_of", "spouse_of", "employed_by"] else PEOPLE
    )
    objects = (
        ["organisation"]
        if n == "employed_by"
        else (["organisation", "group"] if n == "member_of" else ["person"])
    )
    add(
        n,
        subjects,
        ["entity"],
        "relation",
        objects,
        "Explicit relation only; coattendance is not friendship.",
        transition="explicit_only",
    )
for n in [
    "owns",
    "adopted",
    "made",
    "gave",
    "received",
    "commissioned",
    "read",
    "listened_to",
    "watched",
    "played",
    "participated_in",
    "attended",
    "offered",
]:
    objects = {
        "owns": ["object", "work", "project", "place"],
        "adopted": ["person", "object"],
        "made": ["object", "work", "project"],
        "gave": ["object", "work", "project", "place"],
        "received": ["object", "work", "project", "place"],
        "commissioned": ["object", "work", "project"],
        "read": ["work"],
        "listened_to": ["work"],
        "watched": ["work"],
        "played": ["work", "object"],
        "participated_in": ["work", "project", "group", "organisation"],
        "attended": ["work", "organisation"],
        "offered": ["work", "project", "object"],
    }[n]
    add(
        n,
        SUBJECTS,
        ["entity", "text"],
        "state" if n == "owns" else "event",
        objects,
        "Typed role checks; maker/donor/recipient distinct. Title text stays text until identity resolved; offered does not mean attended.",
        transition="explicit_only" if n == "owns" else "append_only",
    )
for n in ["likes", "dislikes", "prefers", "interested_in", "intends", "suggested_to"]:
    add(
        n,
        PEOPLE,
        ["entity", "text"],
        "state" if n in ["likes", "dislikes", "prefers", "interested_in"] else "relation",
        ["object", "work", "project", "person", "agent", "group"],
        "No recommendation adoption or genre-to-every-item inference.",
        transition="explicit_only",
    )
for n in ["started_on", "opened_on", "completed_on", "occurred_on"]:
    add(
        n,
        SUBJECTS + ["project", "work"],
        ["temporal"],
        "event",
        (),
        "Only aware resolved times for arithmetic; civil wording remains unresolved text inside TemporalRef. Distinguish plan/start/open/finish.",
    )
for n in ["has_quantity", "has_measurement", "explicit_frequency"]:
    add(
        n,
        SUBJECTS + ["object", "project"],
        ["number", "text"],
        "attribute",
        (),
        "Preserve units/currency; observed intervals do not establish frequency.",
    )
add(
    "explicit_reason",
    SUBJECTS + ["project"],
    ["text"],
    "relation",
    (),
    "Explicit causal statement only; event links preserve the relevant subject.",
)
add(
    "has_attribute",
    SUBJECTS + ["object", "work", "project", "place"],
    ["text", "number", "boolean", "entity"],
    "attribute",
    ["object", "work", "project", "place", "species"],
    "Extension allowlist required; not a catch-all for diagnosis, protected traits or unsupported inference.",
)

COPIES = [
    (
        "raw_records",
        "Raw message/document/attachment text",
        "Hide then purge/tombstone content; retained IDs cannot reproduce text",
    ),
    (
        "claim_payloads",
        "Claims, value text, explanations and notes",
        "Purge affected text, recompute unaffected state from remaining evidence",
    ),
    (
        "bundle_excerpts",
        "Persisted bundle evidence quotes",
        "Reverse source-to-copy enumeration; invalidate full affected bundle",
    ),
    (
        "event_log_payloads",
        "Source-bearing WRITE/effect/audit payloads",
        "Purge/redact content while retaining non-content event tombstones; never replay erased text",
    ),
    (
        "sql_journal_wal",
        "SQLite WAL/journals and freed database pages",
        "Backend-specific checkpoint/compaction and storage-boundary evidence; logical DELETE is not physical proof",
    ),
    (
        "vector_index",
        "Vectors plus stored text/metadata",
        "Delete target entries and verify; compact/rebuild where backend retains old segments",
    ),
    (
        "lexical_index",
        "Tantivy/OpenSearch/Weaviate text projections",
        "Delete and verify visibility; account for refresh, segments and inactive generations",
    ),
    (
        "graph",
        "Mentions, entity labels, edges, summaries",
        "Remove source-bound occurrences; preserve shared entities only when independently supported",
    ),
    (
        "cache",
        "KV, result caches, embedding results",
        "Invalidate source/namespace generation keys; expire/evict payloads",
    ),
    (
        "queue_outbox",
        "Pending jobs and source snapshots",
        "Cancel/invalidate; no text retry payload may survive under an untracked key",
    ),
    (
        "local_step_journal",
        "Resonate/application-owned step results",
        "Invalidate stored outputs and resume markers; resumed job must honour tombstones",
    ),
    (
        "external_workflow_history",
        "Temporal activity/workflow inputs/results",
        "Use opaque IDs initially; any accidental content-bearing history is an explicit external retention boundary",
    ),
    (
        "summaries_profiles",
        "Summaries, profiles, observations, standing answers",
        "Invalidate and rederive from retained support; do not erase unrelated shared facts blindly",
    ),
    (
        "media_derivatives",
        "OCR text, captions, thumbnails, downloaded assets",
        "Delete every engine-owned derivative and object version; external originals are outside engine ownership",
    ),
    (
        "temporary_files",
        "Spills, temporary batches, worker files",
        "Track ownership, close handles, purge all tracked paths",
    ),
    (
        "telemetry",
        "Logs, traces, exceptions, crash dumps",
        "Content logging off; treat accidental dumps as tracked incidents/copies, not silently exempt",
    ),
    (
        "inactive_generations",
        "Old index generations and saved corpus snapshots",
        "Include in manifest and verification, not just active provider",
    ),
    (
        "exports",
        "Engine-owned exports and reports",
        "Track and purge copies; external recipient copies need declared outside-control state",
    ),
    (
        "backups",
        "Snapshots, object versions, replicas and scheduled backups",
        "Restore gate applies tombstones before access; configured expiry/key destruction required for completion",
    ),
    (
        "process_memory",
        "Hydrated excerpts/in-flight inference buffers",
        "Fence/cancel use, invalidate caches and bound lifetimes; do not claim arbitrary RAM/SSD forensic sanitisation",
    ),
]

if __name__ == "__main__":
    ROOT.mkdir(exist_ok=True)
    (ROOT / "operating_defaults.json").write_text(
        json.dumps(BUDGETS, indent=2) + "\n", encoding="utf-8"
    )
    (ROOT / "predicate_vocabulary.json").write_text(
        json.dumps(
            {
                "status": "ACCEPTED_VOCABULARY_EXTRACTION_GATED",
                "version": 1,
                "runtime_implemented": False,
                "predicates": P,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (ROOT / "copy_erasure_checks.json").write_text(
        json.dumps(
            {
                "status": "ACCEPTED_CHECK_SCOPE_IMPLEMENTATION_PENDING",
                "copies": [
                    {"id": i, "class": k, "payload": v, "verification": w}
                    for i, (k, v, w) in enumerate(COPIES, 1)
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    table = [
        "# Initial predicate vocabulary — accepted design, extraction gated",
        "",
        "| Predicate | Subject types | Value kinds | Semantics | Entity-object candidates |",
        "|---|---|---|---|---|",
    ]
    table += [
        "| "
        + x["spec"]["name"]
        + " | "
        + ", ".join(x["spec"]["subject_types"])
        + " | "
        + ", ".join(x["spec"]["value_kinds"])
        + " | "
        + x["spec"]["semantics"]
        + " | "
        + ", ".join(x["object_entity_types"])
        + " |"
        for x in P
    ]
    (ROOT / "predicate_vocabulary.md").write_text("\n".join(table) + "\n", encoding="utf-8")
    print(
        f"Generated accepted design budgets, {len(P)} predicates and {len(COPIES)} copy classes; runtime unchanged."
    )
