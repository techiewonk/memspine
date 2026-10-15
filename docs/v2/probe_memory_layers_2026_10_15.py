"""CPU-only forensic reproduction; creates only temporary/in-memory engine stores.

Run with PYTHONPATH=src using the project venv. No model/network calls.
This records current behaviour, not assertions that the behaviour is desirable.
"""
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from memspine import Engine
from memspine.derive.write import stamp_derived
from memspine.derive.pipeline.stages.envelope_adapter import is_fact
from memspine.derive.pipeline.stages.offline_common import is_input_fact
from memspine.read.legs.core import LegContext
from memspine.read.legs.episode import EpisodeLeg
from memspine.read.legs.graph import HopEdge, OneHopConvergent
from memspine.store.layer_registry import LayerRegistry, LayerTemplate
from memspine.store.records import MemoryRecord, SourceInfo


async def main():
    results = {"session": [], "mapping": [], "graph": {}, "fact_classification": []}
    for lineage in (False, True):
        eng = Engine(template="core", dotenv_path=None,
                     storage={"path": ":memory:"}, embedding={"provider": "hash"},
                     memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
                     write={"lineage_table": lineage})
        await eng.start()
        try:
            parent = await eng.write("Noor discussed a pottery exhibition.", namespace="audit",
                                     memory_type="episodic")
            for kind in ("episode", "summary"):
                rec = await eng._deposit_session_record(
                    "audit", kind, "Noor discussed a pottery exhibition.",
                    [parent.record_id], datetime(2025, 3, 1, tzinfo=UTC), "audit-session")
                storage = eng._require_started()
                stored = await storage.get_record(rec.record_id)
                counts = await storage.layer_counts("audit")
                leg = await EpisodeLeg().run(LegContext(
                    query="pottery exhibition", namespace="audit", fetch_k=5,
                    records=[stored], layer_registry=LayerRegistry.of("audit", LayerTemplate(), counts)))
                events = await storage.read_events(after_seq=0, limit=1000)
                payload = next(e.payload["record"] for e in events
                               if (e.payload or {}).get("record", {}).get("record_id") == rec.record_id)
                await eng.rebuild()
                replayed = await storage.get_record(rec.record_id)
                results["session"].append(dict(lineage=lineage, kind=kind, returned=rec.layer,
                    stored=stored.layer, payload_layer=payload.get("layer", "omitted"),
                    replayed=replayed.layer, episode_leg_reason=leg.empty_reason, counts=counts))
        finally:
            await eng.stop()
    for channel, route, requested in [
        ("consolidation", "summary", None), ("entity_summary", "summary", None),
        ("reorganize", "summary", None), ("observation", "semantic", "L4"),
        ("standing", "semantic", "L5"), ("community_summary", "summary", "L3"),
        ("multires", "summary", "L2")]:
        rec = MemoryRecord(namespace="audit", memory_type="semantic", content="Synthetic note",
                           source=SourceInfo(role="assistant", channel=channel, parents=["source"]))
        stamped = stamp_derived(rec, route, channel, requested)
        results["mapping"].append(dict(channel=channel, initial=rec.layer, route=route,
            requested=requested, stamped=stamped.layer,
            roundtrip=MemoryRecord.model_validate(stamped.model_dump(mode="json")).layer))
    fact = MemoryRecord(namespace="audit", memory_type="semantic", content="A supported fact")
    for channel in ("observation", "standing", "session_close", "entity_summary", "community_summary", "multires"):
        candidate = MemoryRecord(namespace="audit", memory_type="semantic", entity="Noor",
            content="Synthetic derived view", source=SourceInfo(role="assistant", channel=channel, parents=["source"]))
        results["fact_classification"].append(dict(channel=channel,
            envelope_is_fact=is_fact(candidate), offline_is_input_fact=is_input_fact(candidate)))
    ctx = LegContext(query="shared destination", namespace="audit", fetch_k=5, records=[fact],
        graph_seeds=["a", "b"], graph_edges=[HopEdge("a", "c", fact.record_id),
                                                 HopEdge("b", "c", fact.record_id)])
    blocked = await OneHopConvergent().run(ctx)
    ctx.layer_registry = LayerRegistry.of("audit", LayerTemplate(), {"L1": 1, "L3": 1})
    admitted = await OneHopConvergent().run(ctx)
    results["graph"] = dict(l1_only_reason=blocked.empty_reason,
        with_l3_count_hits=len(admitted.hits), supporting_fact_layer=fact.layer)
    out = Path(__file__).with_suffix(".json")
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
