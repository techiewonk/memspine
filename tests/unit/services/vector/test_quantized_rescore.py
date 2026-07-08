"""E4 quantized rescore (ADR-020): int8/binary prefilter + float32 rescore,
Matryoshka truncation, oversample gating, and the quantization=None backward-
compat guard (search_rescore == query, byte-identical).

All vectors are fixed and hand-chosen so recall assertions are deterministic —
no randomness, no time. The store normalizes at upsert, so inputs are written
un-normalized where convenient and compared against the store's own query().
"""

from __future__ import annotations

from memspine.clients.sqlite import SQLiteClient
from memspine.services.storage.sqlite.schema import metadata
from memspine.services.vector import quantize
from memspine.services.vector.sqlite_store import SQLiteVectorStore


async def make_client() -> SQLiteClient:
    client = SQLiteClient(":memory:")
    await client.connect()
    async with client.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return client


async def make_store(**kwargs: object) -> SQLiteVectorStore:
    return SQLiteVectorStore(await make_client(), **kwargs)  # type: ignore[arg-type]


# ── quantize primitives (deterministic math) ─────────────────────────────────


def test_int8_quantize_and_dot_are_deterministic() -> None:
    a = quantize.quantize_int8([1.0, 0.0, -1.0, 0.5])
    assert a == quantize.quantize_int8([1.0, 0.0, -1.0, 0.5])  # stable
    # 1.0 -> 127, 0.0 -> 0, -1.0 -> -127, 0.5 -> 64 (round-half-to-even of 63.5)
    assert quantize.int8_dot(a, a) == 127 * 127 + 0 + 127 * 127 + 64 * 64


def test_binary_sign_pack_and_hamming() -> None:
    # signs: +, -, +, - over 4 dims -> bits 1010 0000 = 0xA0
    packed = quantize.quantize_binary([0.3, -0.1, 0.9, -0.4])
    assert packed == bytes([0b1010_0000])
    same = quantize.quantize_binary([0.9, -0.9, 0.1, -0.1])
    assert quantize.hamming(packed, same) == 0  # identical sign pattern
    flipped = quantize.quantize_binary([-0.3, 0.1, -0.9, 0.4])
    assert quantize.hamming(packed, flipped) == 4  # every sign differs


def test_truncate_normalize_prefix_and_noop() -> None:
    truncated = quantize.truncate_normalize([0.6, 0.8, 3.0, 4.0], 2)
    assert len(truncated) == 2
    assert abs(sum(x * x for x in truncated) - 1.0) < 1e-9  # re-normalized
    # dim >= length is a no-op (never pads)
    assert quantize.truncate_normalize([1.0, 0.0], 5) == [1.0, 0.0]


# ── int8: quantized+rescore returns the exact float top-1 (~95% quality) ──────


async def test_int8_rescore_top1_matches_exact_float() -> None:
    store = await make_store(quantization="int8", oversample=4)
    await store.upsert("r1", "ns/a", "e:4", [1.0, 0.0, 0.0, 0.0])  # cosine 1.0
    await store.upsert("r2", "ns/a", "e:4", [0.8, 0.6, 0.0, 0.0])  # cosine 0.8
    await store.upsert("r3", "ns/a", "e:4", [0.6, 0.8, 0.0, 0.0])  # cosine 0.6
    await store.upsert("r4", "ns/a", "e:4", [0.0, 1.0, 0.0, 0.0])  # cosine 0.0
    await store.upsert("r5", "ns/a", "e:4", [0.0, 0.0, 1.0, 0.0])  # cosine 0.0

    exact = await store.query("ns/a", [1.0, 0.0, 0.0, 0.0], embedder_id="e:4", top_k=1)
    rescored = await store.search_rescore("ns/a", [1.0, 0.0, 0.0, 0.0], embedder_id="e:4", top_k=1)
    assert [h.record_id for h in rescored] == [h.record_id for h in exact] == ["r1"]
    # rescore returns the EXACT float cosine, not the int8 approximation.
    assert abs(rescored[0].score - 1.0) < 1e-6


async def test_binary_prefilter_ties_then_float_rescore_corrects() -> None:
    """Binary sign bits cannot separate two all-positive candidates (Hamming 0
    to the query for both); the float32 rescore breaks the tie exactly."""
    store = await make_store(quantization="binary", oversample=4)
    await store.upsert("hi", "ns/a", "e:4", [0.9, 0.2, 0.1, 0.1])  # closer to query
    await store.upsert("lo", "ns/a", "e:4", [0.5, 0.5, 0.5, 0.5])  # same signs, farther

    query = [1.0, 0.1, 0.1, 0.1]
    # both candidates share the query's all-positive sign pattern (prefilter tie)
    assert (
        quantize.hamming(
            quantize.quantize_binary(query), quantize.quantize_binary([0.9, 0.2, 0.1, 0.1])
        )
        == 0
    )
    assert (
        quantize.hamming(
            quantize.quantize_binary(query), quantize.quantize_binary([0.5, 0.5, 0.5, 0.5])
        )
        == 0
    )
    exact = await store.query("ns/a", query, embedder_id="e:4", top_k=1)
    rescored = await store.search_rescore("ns/a", query, embedder_id="e:4", top_k=1)
    assert [h.record_id for h in rescored] == [h.record_id for h in exact] == ["hi"]


# ── oversample factor is respected: a narrow window drops a badly-prefiltered
#    but truly-closest vector; a wide window recovers it ──────────────────────


async def test_oversample_window_gates_recall() -> None:
    async def build(oversample: int) -> SQLiteVectorStore:
        store = await make_store(quantization="binary", oversample=oversample)
        # Winner: high float cosine but a flipped last sign -> Hamming 1 (bad rank).
        await store.upsert("winner", "ns/a", "e:4", [0.99, 0.05, 0.05, -0.02])
        # Decoys: all-positive -> Hamming 0 (best prefilter rank) but low cosine.
        for i in range(3):
            await store.upsert(f"d{i}", "ns/a", "e:4", [0.5, 0.5, 0.5, 0.5])
        return store

    query = [0.9, 0.1, 0.1, 0.1]  # all-positive; winner's last sign differs
    # Exact float always finds the winner.
    exact = await (await build(1)).query("ns/a", query, embedder_id="e:4", top_k=1)
    assert exact[0].record_id == "winner"

    # oversample=1 -> window=1: prefilter ranks the Hamming-0 decoys first, the
    # winner is excluded from the window, so rescore never sees it.
    narrow = await (await build(1)).search_rescore("ns/a", query, embedder_id="e:4", top_k=1)
    assert narrow[0].record_id.startswith("d")

    # oversample=10 -> the winner is inside the window and the rescore recovers it.
    wide = await (await build(10)).search_rescore("ns/a", query, embedder_id="e:4", top_k=1)
    assert wide[0].record_id == "winner"


# ── Matryoshka truncation: prefilter runs at the reduced prefix dim ───────────


async def test_matryoshka_truncates_prefilter_dim_rescore_stays_full() -> None:
    store = await make_store(matryoshka_dim=2, oversample=4)
    # First-2-dims prefix identical to the query; the tail differs. The truncated
    # prefilter keeps it; the full-precision rescore ranks by the whole vector.
    await store.upsert("prefix_match", "ns/a", "e:4", [1.0, 0.0, 0.0, 0.0])
    await store.upsert("prefix_far", "ns/a", "e:4", [0.0, 1.0, 0.0, 0.0])

    rescored = await store.search_rescore("ns/a", [1.0, 0.0, 0.0, 0.0], embedder_id="e:4", top_k=2)
    assert rescored[0].record_id == "prefix_match"
    assert abs(rescored[0].score - 1.0) < 1e-6  # rescore is exact full-dim cosine


# ── backward-compat guard: no quantization/truncation => query() exactly ──────


async def test_no_quantization_rescore_equals_query_byte_identical() -> None:
    store = await make_store()  # quantization=None, matryoshka_dim=None (default)
    await store.upsert("r1", "ns/a", "e:4", [1.0, 0.0, 0.0, 0.0])
    await store.upsert("r2", "ns/a", "e:4", [0.7, 0.7, 0.0, 0.0])
    await store.upsert("r3", "ns/b", "e:4", [1.0, 0.0, 0.0, 0.0])

    q = [1.0, 0.0, 0.0, 0.0]
    plain = await store.query("ns/a", q, embedder_id="e:4", top_k=5)
    rescored = await store.search_rescore("ns/a", q, embedder_id="e:4", top_k=5)
    assert plain == rescored  # identical VectorHit lists — the simple-profile guard


# ── mixed table: a NULL-code row at the same dim/ns/embedder still surfaces ───


async def test_mixed_table_null_code_row_not_squeezed_out() -> None:
    """The normal state right after enabling quantization (before a rebuild
    re-encodes): an active int8 store over a table holding a legacy row whose
    ``quantized_vec`` is NULL. The legacy row is the TRUE nearest — the float
    fallback must score it on the same scale so it still wins, not get dropped."""
    client = await make_client()
    coded = SQLiteVectorStore(client, quantization="int8", oversample=4)
    legacy = SQLiteVectorStore(client)  # inactive → upserts NULL codes

    await legacy.upsert("legacy_near", "ns/a", "e:4", [1.0, 0.0, 0.0, 0.0])  # cosine 1.0
    for i, vec in enumerate(([0.6, 0.8, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0])):
        await coded.upsert(f"c{i}", "ns/a", "e:4", vec)

    hits = await coded.search_rescore("ns/a", [1.0, 0.0, 0.0, 0.0], embedder_id="e:4", top_k=1)
    assert hits[0].record_id == "legacy_near"  # fallback works — NULL row not squeezed
    assert abs(hits[0].score - 1.0) < 1e-6


# ── scheme switch: int8 codes read by a binary store must not crash ───────────


async def test_scheme_switch_falls_back_without_crash() -> None:
    """Rows coded int8, store flipped to binary WITHOUT a re-upsert: the stored
    scheme != the active scheme, so ``int8`` bytes are never fed to the binary
    comparator (would zip mismatched lengths → ValueError). They fall back to an
    exact float cosine and ``search_rescore`` returns sensible results."""
    client = await make_client()
    int8_store = SQLiteVectorStore(client, quantization="int8", oversample=4)
    await int8_store.upsert("r1", "ns/a", "e:4", [1.0, 0.0, 0.0, 0.0])
    await int8_store.upsert("r2", "ns/a", "e:4", [0.0, 1.0, 0.0, 0.0])

    binary_store = SQLiteVectorStore(client, quantization="binary", oversample=4)
    hits = await binary_store.search_rescore(
        "ns/a", [1.0, 0.0, 0.0, 0.0], embedder_id="e:4", top_k=2
    )
    assert [h.record_id for h in hits] == ["r1", "r2"]  # mismatch → float fallback, no crash


async def test_matryoshka_dim_change_same_scheme_length_guard() -> None:
    """A changed ``matryoshka_dim`` keeps the scheme string ``int8`` but shifts
    the code byte length — the byte-length guard routes such rows to the float
    fallback instead of zipping mismatched code lengths."""
    client = await make_client()
    dim4 = SQLiteVectorStore(client, quantization="int8", matryoshka_dim=4, oversample=4)
    await dim4.upsert("r1", "ns/a", "e:8", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])  # code len 4

    dim2 = SQLiteVectorStore(client, quantization="int8", matryoshka_dim=2, oversample=4)
    # dim2's query code is len 2; r1's stored code is len 4 → length mismatch → fallback.
    hits = await dim2.search_rescore(
        "ns/a", [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], embedder_id="e:8", top_k=1
    )
    assert hits[0].record_id == "r1"  # no ValueError from zipping mismatched code bytes


# ── stage 1 reads ONLY codes for coded rows (float unpacked for the window) ───


async def test_stage1_does_not_unpack_float_for_coded_rows(monkeypatch: object) -> None:
    """White-box proof of the read split: with every row coded, stage 1 ranks on
    the int8 codes alone — the float ``vector`` is unpacked ONLY for the stage-2
    window, not for the whole corpus (that reduction is E4's entire point)."""
    from memspine.services.vector import sqlite_store as store_mod

    store = await make_store(quantization="int8", oversample=1)
    for i in range(5):
        await store.upsert(f"r{i}", "ns/a", "e:4", [1.0 - i * 0.1, i * 0.1, 0.0, 0.0])

    calls = {"n": 0}
    real_unpack = store_mod._unpack

    def counting_unpack(raw: bytes) -> list[float]:
        calls["n"] += 1
        return real_unpack(raw)

    monkeypatch.setattr(store_mod, "_unpack", counting_unpack)  # type: ignore[attr-defined]
    hits = await store.search_rescore("ns/a", [1.0, 0.0, 0.0, 0.0], embedder_id="e:4", top_k=1)
    assert hits[0].record_id == "r0"
    # oversample=1, top_k=1 → window size 1; coded ranking touched NO float vector.
    assert calls["n"] == 1


# ── Matryoshka + quantization combined: encode truncates THEN quantizes ───────


async def test_matryoshka_and_quantization_combined_roundtrip() -> None:
    """Manifest declares BOTH: ``_encode`` truncates to the prefix dim then int8-
    quantizes, and the query does the same — round-trip top-1 matches exact."""
    store = await make_store(quantization="int8", matryoshka_dim=2, oversample=4)
    await store.upsert("match", "ns/a", "e:4", [1.0, 0.0, 0.0, 0.0])
    await store.upsert("far", "ns/a", "e:4", [0.0, 1.0, 0.0, 0.0])

    exact = await store.query("ns/a", [1.0, 0.0, 0.0, 0.0], embedder_id="e:4", top_k=1)
    rescored = await store.search_rescore("ns/a", [1.0, 0.0, 0.0, 0.0], embedder_id="e:4", top_k=1)
    assert [h.record_id for h in rescored] == [h.record_id for h in exact] == ["match"]
    assert abs(rescored[0].score - 1.0) < 1e-6  # rescore is exact full-dim cosine


async def test_inactive_store_writes_null_prefilter_columns() -> None:
    from sqlalchemy import select

    from memspine.services.storage.sqlite.schema import memory_embeddings

    store = await make_store()  # inactive
    await store.upsert("r1", "ns/a", "e:4", [1.0, 0.0, 0.0, 0.0])
    async with store._client.engine.connect() as conn:
        row = (
            await conn.execute(
                select(memory_embeddings.c.quantized_vec, memory_embeddings.c.quantization)
            )
        ).one()
    assert row == (None, None)  # nothing quantized on the default path
