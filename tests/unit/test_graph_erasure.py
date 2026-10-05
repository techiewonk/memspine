"""M7 x GP-8a: a hard forget erases the extract_graph watermark of its source."""

from __future__ import annotations

from memspine import Engine
from memspine.core.events import EventKind
from memspine.prompts.models import ExtractedEdge
from memspine.workers.pipelines import GRAPH_EXTRACTED_MARKER, extract_graph


async def test_hard_forget_redacts_the_graph_extracted_fingerprint() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
    )
    await eng.start()
    try:

        async def fake_extract(_content: str, _context: object = None) -> list[ExtractedEdge]:
            return []

        eng._extract_edges = fake_extract
        source = await eng.write("Alice works at Acme.", namespace="a", memory_type="episodic")
        await extract_graph(eng._pipeline_ctx())
        storage = eng._require_started()

        async def markers() -> list[dict[str, object]]:
            return [
                e.payload
                for e in await storage.read_events()
                if e.kind is EventKind.MARKER and e.payload.get("marker") == GRAPH_EXTRACTED_MARKER
            ]

        [before] = await markers()
        assert before["sources"] == [
            {"record_id": source.record_id, "content_fingerprint": source.content_fingerprint}
        ]
        await eng.forget(source.record_id, namespace="a", hard=True)
        [after] = await markers()
        assert after["sources"] == [{"record_id": source.record_id, "content_fingerprint": ""}]
        proof = await eng.verify_forget(source.record_id, namespace="a")
        assert "content_fingerprint" not in proof["log_retained_fields"]
        assert proof["log_redacted"] is True
    finally:
        await eng.stop()
