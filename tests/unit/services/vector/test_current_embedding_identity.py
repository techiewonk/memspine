"""Fresh vector storage uses the configured embedding identity directly."""

from pathlib import Path

from memspine.clients.lancedb import LanceDBClient, LanceDBSpec
from memspine.services.embedding.onnx import OnnxEmbedding
from memspine.services.vector.lancedb_store import LanceDBVectorStore


async def test_new_onnx_table_uses_current_identity(tmp_path: Path) -> None:
    embedder = OnnxEmbedding()
    client = LanceDBClient(LanceDBSpec(tmp_path / "vectors"))
    await client.open()
    store = LanceDBVectorStore(client, embedder)
    vector = [1.0] + [0.0] * (embedder.dim - 1)
    try:
        await store.upsert("record", "tenant", embedder.embedder_id, vector)
        table = await store._ensure_table()
        assert table.name == "memspine_embedder_BAAI_bge_small_en_v1_5"
        hits = await store.query("tenant", vector, embedder.embedder_id)
        assert [hit.record_id for hit in hits] == ["record"]
        assert not await store.query("other-tenant", vector, embedder.embedder_id)
        assert not embedder._weights.loaded
    finally:
        await client.close()
        await embedder.aclose()
