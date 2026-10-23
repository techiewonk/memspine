"""Bounded experimental comparison of native Weaviate hybrid and portable RRF.

This evaluation creates one disposable, digest-pinned local Weaviate service,
uses the live-provider runner's loopback-only provisioning helpers, and always
removes that owned container and its volumes. It does not alter Engine provider
capabilities or defaults.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import inspect
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WEAVIATE_IMAGE = (
    "cr.weaviate.io/semitechnologies/weaviate@sha256:"
    "920463cf9a9b2b8d38e5b925d63f6928fa703277c93df11d5a75fc75e1fff758"
)
NAMESPACE_A = "pa089/team-a"
NAMESPACE_B = "pa089/team-b"
EMBEDDER_ID = "hash:64"
DIMENSION = 64
CANDIDATE_K_PER_PORTABLE_LEG = 5
CANDIDATE_UNION_CAP = 2 * CANDIDATE_K_PER_PORTABLE_LEG
FINAL_TOP_K = 5
NATIVE_LIMIT = CANDIDATE_UNION_CAP

FIXTURES = (
    ("entity", NAMESPACE_A, "Mira owns the cobalt observability playbook for project A17."),
    ("renewal", NAMESPACE_A, "Acme renewal was signed on 2025-04-02 in Utrecht."),
    ("literal", NAMESPACE_A, "Archive token COBALT-77/AX9 belongs to Mira."),
    ("scope", NAMESPACE_A, "Project ember scope sentinel assigned to Mira."),
    ("tombstone", NAMESPACE_A, "Retire marker ORBIT-REVOKED-999 belongs to a withdrawn watch."),
    ("foreign-scope", NAMESPACE_B, "Project ember scope sentinel assigned to Mira."),
    ("distractor-00", NAMESPACE_A, "Mira reviewed a project plan for the annual design meeting."),
    ("distractor-01", NAMESPACE_A, "Acme shared a public office calendar for the Utrecht team."),
    ("distractor-02", NAMESPACE_A, "The cobalt team maintains an observability dashboard."),
    ("distractor-03", NAMESPACE_A, "A17 is the label for the archived rollout checklist."),
    ("distractor-04", NAMESPACE_A, "Finance will review the renewal packet next quarter."),
    ("distractor-05", NAMESPACE_A, "The meeting moved to a different room in Utrecht."),
    ("distractor-06", NAMESPACE_A, "A calibration token was issued to the operations team."),
    ("distractor-07", NAMESPACE_A, "The archive has a new retention policy and access owner."),
    ("distractor-08", NAMESPACE_A, "Project ember records are kept in the team index."),
    ("distractor-09", NAMESPACE_A, "A watch was withdrawn after the deadline changed."),
    ("distractor-10", NAMESPACE_A, "Mira wrote notes about the new search interface."),
    ("distractor-11", NAMESPACE_A, "Utrecht has a public transport and visitor guide."),
    ("distractor-12", NAMESPACE_A, "The observability review covered service uptime and cost."),
    ("distractor-13", NAMESPACE_A, "A signed agreement was copied to the legal archive."),
    ("distractor-14", NAMESPACE_A, "The identifier AX9 appears in an unrelated inventory."),
    ("distractor-15", NAMESPACE_A, "The team assigned a Mira follow-up to the next sprint."),
    ("distractor-16", NAMESPACE_A, "An old project key is no longer active or supported."),
    ("distractor-17", NAMESPACE_A, "Renewal dates are tracked separately from event times."),
    ("distractor-18", NAMESPACE_A, "A cobalt-colored badge was seen near the office."),
    ("distractor-19", NAMESPACE_A, "The 2025-04-02 export contains unrelated test rows."),
)

QUERIES = (
    ("entity", NAMESPACE_A, "Mira cobalt observability playbook project A17", ("entity",)),
    ("renewal", NAMESPACE_A, "Acme renewal signed 2025-04-02 Utrecht", ("renewal",)),
    ("literal", NAMESPACE_A, "COBALT-77/AX9", ("literal",)),
    ("scope", NAMESPACE_A, "Project ember scope sentinel", ("scope",)),
    ("tombstone", NAMESPACE_A, "Retire marker ORBIT-REVOKED-999", ()),
)


def _live_runner() -> Any:
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import run_provider_live_contracts

    return run_provider_live_contracts


def _metrics(ranked_ids: list[str], relevant_ids: set[str], k: int) -> dict[str, Any]:
    top = ranked_ids[:k]
    reciprocal_rank = next(
        (1.0 / index for index, record_id in enumerate(top, 1) if record_id in relevant_ids), 0.0
    )
    dcg = sum(
        1.0 / math.log2(index + 1)
        for index, record_id in enumerate(top, 1)
        if record_id in relevant_ids
    )
    ideal = sum(1.0 / math.log2(index + 1) for index in range(1, min(len(relevant_ids), k) + 1))
    return {
        "relevant_in_top_k": sum(record_id in relevant_ids for record_id in top),
        "mrr_at_k": reciprocal_rank,
        "ndcg_at_k": dcg / ideal if ideal else None,
    }


async def evaluate() -> dict[str, Any]:
    live = _live_runner()
    if not live.image_present(WEAVIATE_IMAGE):
        raise RuntimeError(
            "digest-pinned Weaviate image is not cached; evaluation will not pull images"
        )

    suffix = f"pa089-native-hybrid-{uuid.uuid4().hex[:8]}"
    container = live.start_service(
        "weaviate",
        WEAVIATE_IMAGE,
        8080,
        {
            "AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED": "true",
            "PERSISTENCE_DATA_PATH": "/var/lib/weaviate",
            "CLUSTER_HOSTNAME": "node1",
            "DEFAULT_VECTORIZER_MODULE": "none",
        },
        suffix,
        extra_ports=(50051,),
    )
    client = None
    engine = None
    collection = None
    collection_id: str | None = None
    temporary_path: Path | None = None
    url_env_name = "MEMSPINE_PA089_WEAVIATE_URL"
    previous_url_env = os.environ.get(url_env_name)
    try:
        http_port = live.container_port(container.container_name, 8080)
        grpc_port = live.container_port(container.container_name, 50051)
        live.wait_tcp(http_port, timeout_s=90)
        live.wait_tcp(grpc_port, timeout_s=90)
        live.wait_http(http_port, "/v1/.well-known/ready", timeout_s=90)
        runtime_version = live.service_version(container)

        from weaviate.classes.query import Filter

        from memspine import Engine
        from memspine.clients.weaviate import WeaviateClient, WeaviateSpec
        from memspine.services._weaviate import (
            LEXICAL_PROPERTIES,
            WeaviateCollectionBinding,
            collection_name,
            ensure_collection,
            properties,
            record_uuid,
            upsert_object,
        )
        from memspine.services.lexical.base import rrf_fuse
        from memspine.services.remote_search import owner_scope

        with tempfile.TemporaryDirectory(
            prefix="memspine-pa089-native-hybrid-", ignore_cleanup_errors=True
        ) as temporary:
            temporary_path = Path(temporary).resolve()
            work = Path(temporary)
            engine = Engine(
                template="core",
                dotenv_path=None,
                deployment={"id": f"pa089-portable-{uuid.uuid4().hex}"},
                storage={
                    "records": {
                        "provider": "sqlite",
                        "settings": {"path": str(work / "events.sqlite"), "busy_timeout_s": 5.0},
                    },
                    "vector": {"provider": "lancedb", "settings": {"isolation": "shared"}},
                    "cache": {"provider": "memory", "settings": {}},
                    "lexical": {"provider": "tantivy", "settings": {}},
                },
                model={"embedding": {"provider": "hash", "settings": {"dim": DIMENSION}}},
                read={"hybrid": True},
            )
            await engine.start()

            os.environ[url_env_name] = f"http://127.0.0.1:{http_port}"
            client = WeaviateClient(
                WeaviateSpec(
                    url_env=url_env_name,
                    grpc_port=grpc_port,
                    connect_timeout_s=20.0,
                    request_timeout_s=20.0,
                )
            )
            await client.open()
            native = client.handle

            owner_id = f"pa089-native-{uuid.uuid4().hex}"
            collection_id = collection_name("Memspine", "PA089", owner_id, "active")
            collection = await ensure_collection(
                native, WeaviateCollectionBinding(collection_id, LEXICAL_PROPERTIES)
            )
            records: dict[str, Any] = {}
            namespace_by_id: dict[str, str] = {}
            for key, namespace, content in FIXTURES:
                record = await engine.write(content, namespace=namespace, memory_type="semantic")
                records[key] = record
                namespace_by_id[record.record_id] = namespace
                vector = (await engine._embedder.embed([content]))[0]
                await upsert_object(
                    collection,
                    owner_id=owner_id,
                    record_id=record.record_id,
                    props=properties(
                        record.record_id,
                        namespace,
                        owner_id,
                        content=content,
                        embedder_id=EMBEDDER_ID,
                    ),
                    vector=vector,
                )

            tombstone_id = records["tombstone"].record_id
            tombstone_query = next(query for name, _, query, _ in QUERIES if name == "tombstone")
            tombstone_vector = (await engine._embedder.embed([tombstone_query]))[0]
            pre_delete_vector = await engine._vector.query(
                NAMESPACE_A, tombstone_vector, EMBEDDER_ID, CANDIDATE_K_PER_PORTABLE_LEG
            )
            pre_delete_lexical = await engine._lexical.search(
                NAMESPACE_A, tombstone_query, CANDIDATE_K_PER_PORTABLE_LEG
            )
            tombstone_filter = Filter.all_of(
                [
                    Filter.by_property("namespace").equal(NAMESPACE_A),
                    Filter.by_property("owner_scope").equal(owner_scope(owner_id)),
                ]
            )
            pre_delete_native = await collection.query.hybrid(
                query=tombstone_query,
                vector=tombstone_vector,
                alpha=0.5,
                query_properties=["content"],
                filters=tombstone_filter,
                limit=NATIVE_LIMIT,
                return_properties=["record_id", "namespace"],
            )
            pre_native_ids = [str(obj.properties["record_id"]) for obj in pre_delete_native.objects]
            if tombstone_id not in {hit.record_id for hit in pre_delete_vector}:
                raise AssertionError("portable vector pre-delete control did not find tombstone")
            if tombstone_id not in {hit.record_id for hit in pre_delete_lexical}:
                raise AssertionError("portable lexical pre-delete control did not find tombstone")
            if tombstone_id not in pre_native_ids:
                raise AssertionError("native hybrid pre-delete control did not find tombstone")

            await engine.forget(tombstone_id, namespace=NAMESPACE_A, hard=True, cascade=False)
            await collection.data.delete_by_id(record_uuid(owner_id, tombstone_id))
            namespace_by_id.pop(tombstone_id)
            if await engine._vector.exists(tombstone_id) or await engine._lexical.exists(
                tombstone_id
            ):
                raise AssertionError("portable projections retained the hard-erased tombstone")
            if (
                await collection.query.fetch_object_by_id(record_uuid(owner_id, tombstone_id))
                is not None
            ):
                raise AssertionError("native Weaviate collection retained the erased tombstone")

            query_vectors = await engine._embedder.embed([query for _, _, query, _ in QUERIES])
            native_filter_by_namespace = {
                namespace: Filter.all_of(
                    [
                        Filter.by_property("namespace").equal(namespace),
                        Filter.by_property("owner_scope").equal(owner_scope(owner_id)),
                    ]
                )
                for namespace in {namespace for _, namespace, _, _ in QUERIES}
            }
            comparisons: list[dict[str, Any]] = []
            search_calls = 3  # the native + two portable tombstone pre-delete controls
            for (case_id, namespace, query, relevant_keys), query_vector in zip(
                QUERIES, query_vectors, strict=True
            ):
                lexical_hits = await engine._lexical.search(
                    namespace, query, CANDIDATE_K_PER_PORTABLE_LEG
                )
                vector_hits = await engine._vector.query(
                    namespace, query_vector, EMBEDDER_ID, CANDIDATE_K_PER_PORTABLE_LEG
                )
                portable_candidates = rrf_fuse(vector_hits, lexical_hits)
                portable_ids = [record_id for record_id, _ in portable_candidates[:FINAL_TOP_K]]
                if len(portable_ids) != len(set(portable_ids)):
                    raise AssertionError(f"portable RRF duplicated a record ID in {case_id}")

                native_result = await collection.query.hybrid(
                    query=query,
                    vector=query_vector,
                    alpha=0.5,
                    query_properties=["content"],
                    filters=native_filter_by_namespace[namespace],
                    limit=NATIVE_LIMIT,
                    return_properties=["record_id", "namespace"],
                )
                native_candidate_ids = [
                    str(obj.properties["record_id"]) for obj in native_result.objects
                ]
                if len(native_candidate_ids) != len(set(native_candidate_ids)):
                    raise AssertionError(f"native hybrid duplicated a record ID in {case_id}")
                native_ids = native_candidate_ids[:FINAL_TOP_K]

                relevant_ids = {records[key].record_id for key in relevant_keys}
                for method, result_ids in (("native", native_ids), ("portable_rrf", portable_ids)):
                    wrong_scope = [
                        record_id
                        for record_id in result_ids
                        if namespace_by_id.get(record_id) != namespace
                    ]
                    if wrong_scope:
                        raise AssertionError(f"{method} returned out-of-scope IDs in {case_id}")
                    if tombstone_id in result_ids:
                        raise AssertionError(f"{method} returned erased ID in {case_id}")

                comparisons.append(
                    {
                        "case_id": case_id,
                        "namespace": namespace,
                        "query": query,
                        "expected_relevant_record_ids": sorted(relevant_ids),
                        "native_candidate_count": len(native_candidate_ids),
                        "native_ranked_record_ids": native_ids,
                        "portable_vector_candidate_ids": [hit.record_id for hit in vector_hits],
                        "portable_lexical_candidate_ids": [hit.record_id for hit in lexical_hits],
                        "portable_ranked_record_ids": portable_ids,
                        "native_diagnostic_metrics": _metrics(
                            native_ids, relevant_ids, FINAL_TOP_K
                        ),
                        "portable_diagnostic_metrics": _metrics(
                            portable_ids, relevant_ids, FINAL_TOP_K
                        ),
                        "scope_and_tombstone_assertions_passed": True,
                    }
                )
                search_calls += 3

            if search_calls > 30:
                raise AssertionError(f"search-call cap exceeded: {search_calls}")

            from weaviate.collections.queries.hybrid.query import _HybridQueryAsync

            native_method = _HybridQueryAsync.hybrid
            source_file = inspect.getsourcefile(native_method)
            report = {
                "schema_version": 1,
                "date": "2026-10-17",
                "task": "PA-089 bounded experimental native Weaviate hybrid versus portable RRF",
                "status": "completed",
                "source_revision": subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip(),
                "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "host": {"python": platform.python_version(), "platform": platform.platform()},
                "image": {
                    "reference": WEAVIATE_IMAGE,
                    "inspection": live.digest(WEAVIATE_IMAGE),
                    "runtime_version": runtime_version,
                },
                "container": {
                    "owned_name": container.container_name,
                    "http_port": http_port,
                    "grpc_port": grpc_port,
                    "loopback_only": True,
                    "cleanup": "docker rm --force --volumes in finally",
                },
                "sdk": {
                    "distribution": "weaviate-client",
                    "version": importlib.metadata.version("weaviate-client"),
                    "native_method": "CollectionAsync.query.hybrid",
                    "signature": str(inspect.signature(native_method)),
                    "installed_source_file": str(source_file) if source_file else None,
                    "evidence_basis": "installed official SDK implementation source, not an Engine capability descriptor",
                },
                "corpus": {
                    "fixture_count": len(FIXTURES),
                    "namespace_a_count": sum(
                        namespace == NAMESPACE_A for _, namespace, _ in FIXTURES
                    ),
                    "namespace_b_count": sum(
                        namespace == NAMESPACE_B for _, namespace, _ in FIXTURES
                    ),
                    "fixed_fixture_content_sha256": hashlib.sha256(
                        json.dumps(FIXTURES, ensure_ascii=False, separators=(",", ":")).encode(
                            "utf-8"
                        )
                    ).hexdigest(),
                    "fixture_names_to_record_ids": {
                        key: record.record_id for key, record in records.items()
                    },
                    "embedding": {
                        "provider": "hash",
                        "dimension": DIMENSION,
                        "embedder_id": EMBEDDER_ID,
                    },
                    "model_calls": 0,
                },
                "portable_stack": {
                    "lexical": {
                        "provider": "Tantivy",
                        "version": importlib.metadata.version("tantivy"),
                    },
                    "vector": {
                        "provider": "LanceDB",
                        "version": importlib.metadata.version("lancedb"),
                    },
                    "fusion": "memspine.services.lexical.base.rrf_fuse",
                },
                "budgets": {
                    "query_count": len(QUERIES),
                    "search_calls_including_tombstone_control": search_calls,
                    "call_cap": 30,
                    "portable_candidates_per_leg": CANDIDATE_K_PER_PORTABLE_LEG,
                    "portable_union_candidate_cap": CANDIDATE_UNION_CAP,
                    "native_exposed_candidate_limit": NATIVE_LIMIT,
                    "final_top_k": FINAL_TOP_K,
                    "native_alpha": 0.5,
                    "scope_filter": "exact namespace and owner_scope on all native/portable query legs",
                },
                "tombstone_control": {
                    "record_id": tombstone_id,
                    "native_pre_delete_found": tombstone_id in pre_native_ids,
                    "portable_vector_pre_delete_found": tombstone_id
                    in {hit.record_id for hit in pre_delete_vector},
                    "portable_lexical_pre_delete_found": tombstone_id
                    in {hit.record_id for hit in pre_delete_lexical},
                    "hard_delete_verified_in_native_and_both_portable_projections": True,
                    "post_delete_searches_excluded_record": True,
                },
                "results": comparisons,
                "ranking_interpretation": {
                    "metrics": "synthetic fixture diagnostics (MRR@5, nDCG@5), not semantic/model quality",
                    "same_queries_embeddings_data_namespace_filters_and_final_top_k": True,
                    "same_max_candidate_return_budget": True,
                    "internal_candidate_equivalence": "not available: Weaviate's native hybrid API exposes one fused-result limit and does not expose a separate BM25/vector candidate-pool cap. Portable RRF requests five from each leg (at most ten unique union candidates); native requests ten fused candidates, then both report top five. Server-internal native candidate behavior is opaque.",
                    "native_fusion": "Weaviate server hybrid with caller-supplied hash vector and alpha=0.5",
                    "portable_fusion": "independent local Tantivy BM25 and LanceDB vector legs, each top-5, fused once by memspine rrf_fuse",
                },
                "engine_capability": {
                    "advertised_native_hybrid": False,
                    "default_or_engine_routing_changed": False,
                    "claim": "experimental external backend capability only; not a shipped Memspine feature or production approval",
                },
                "limitations": [
                    "Hash dim=64 is deterministic plumbing and fixture material, not semantic embedding quality.",
                    "The synthetic labels are authored from the fixed fixture and metrics are diagnostic only.",
                    "The native backend has no exposed internal candidate-pool budget equivalent to two portable legs.",
                    "One Weaviate server version, one local run, five queries, and 26 records do not establish production relevance or latency.",
                    "No native results were merged into portable RRF; each backend contributed one unique-record ranking per query.",
                ],
            }
            await client.close()
            client = None
            await engine.stop()
            engine = None
            return report
    finally:
        if collection is not None and client is not None:
            try:
                await client.handle.collections.delete(collection_id)
            except Exception:
                pass
        if client is not None:
            await client.close()
        if engine is not None:
            await engine.stop()
        if (
            temporary_path is not None
            and temporary_path.parent == Path(tempfile.gettempdir()).resolve()
            and temporary_path.name.startswith("memspine-pa089-native-hybrid-")
        ):
            shutil.rmtree(temporary_path, ignore_errors=True)
        if previous_url_env is None:
            os.environ.pop(url_env_name, None)
        else:
            os.environ[url_env_name] = previous_url_env
        subprocess.run(
            ["docker", "rm", "--force", "--volumes", container.container_name],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = asyncio.run(evaluate())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "search_calls": report["budgets"]["search_calls_including_tombstone_control"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
