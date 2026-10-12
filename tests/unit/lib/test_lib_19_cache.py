"""LIB-19: read-cache keys, generation invalidation, POL-1 revalidation. Invented names only."""

from __future__ import annotations

from memspine.read.cache import (
    InMemoryBackend,
    ItemsResult,
    ReadCache,
    ReadCaches,
    embedding_key,
    extraction_key,
    read_key,
    rerank_key,
    revalidate,
)
from memspine.schemas.durable.common import OwnerScope
from memspine.trust.erasure_plan import ErasureState, visible

SCOPE = OwnerScope(tenant="acme", owner="u1")


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def rk(viewer="alice", store=1, erasure=0, consent=None, q="where does Dana live"):
    return read_key(
        scope=SCOPE,
        viewer=viewer,
        query=q,
        config_hash="cfg1",
        model_ids=["m1"],
        store_generation=store,
        erasure_generation=erasure,
        consent_generation=consent,
    )


def mk(**kw) -> ReadCache:
    return ReadCache("read", enabled=True, **kw)


def test_hit_and_normalised_query():
    c = mk()
    c.put(rk(), ItemsResult(("a", "b")))
    assert c.get(rk(q="  Where does  DANA live ")) == ItemsResult(("a", "b"))


def test_store_generation_bump_invalidates_and_evicts():
    c = mk()
    c.put(rk(store=1), ItemsResult(("a",)))
    assert c.get(rk(store=2)) is None
    assert len(c) == 0
    assert c.get(rk(store=1)) is None  # evicted, not resurrected


def test_erasure_and_consent_generation_bump_invalidate():
    c = mk()
    c.put(rk(erasure=0), ItemsResult(("a",)))
    assert c.get(rk(erasure=1)) is None
    c.put(rk(consent=3), ItemsResult(("a",)))
    assert c.get(rk(consent=3)) is not None
    assert c.get(rk(consent=4)) is None
    c.put(rk(consent=None), ItemsResult(("a",)))
    assert c.get(rk(consent=1)) is None


def test_erasure_between_put_and_get_never_leaks():
    c = mk()
    c.put(rk(), ItemsResult(("rec1", "rec2", "rec3")))
    # Same generations (stale caller or missed bump): POL-1 still removes the erased item.
    state = ErasureState(erased=frozenset({"rec2"}), generation=0)
    out = c.get(rk(), lambda r: revalidate(r, lambda i: visible(i, state)))
    assert out is not None
    assert out.items == ("rec1", "rec3")
    assert out.revalidated_partial and out.dropped == 1
    # the polluted entry is gone, so it cannot come back
    assert c.get(rk()) is None


def test_revalidate_clean_result_untouched_and_hidden_dropped():
    r = ItemsResult(("a", "b"))
    assert revalidate(r, lambda i: True) is r
    out = revalidate(r, lambda i: False)
    assert out.items == () and out.revalidated_partial


def test_viewer_isolation():
    c = mk()
    c.put(rk(viewer="alice"), ItemsResult(("secret-for-alice",)))
    assert c.get(rk(viewer="bob")) is None
    assert c.get(rk(viewer=None)) is None
    assert c.get(rk(viewer="alice")) is not None


def test_disabled_by_default():
    c: ReadCache = ReadCache("read")
    assert c.enabled is False
    c.put(rk(), ItemsResult(("a",)))
    assert len(c) == 0 and c.get(rk()) is None
    caches = ReadCaches.build()
    caches.read.put(rk(), ItemsResult(("a",)))
    assert caches.read.get(rk()) is None


def test_ttl_expiry_with_injected_clock():
    clk = Clock()
    c = mk(ttl_seconds=10, clock=clk)
    c.put(rk(), ItemsResult(("a",)))
    clk.t = 9.9
    assert c.get(rk()) is not None
    clk.t = 10.0
    assert c.get(rk()) is None
    assert len(c) == 0


def test_size_bound_lru():
    c = mk(backend=InMemoryBackend(2))
    c.put(rk(q="one"), ItemsResult(("1",)))
    c.put(rk(q="two"), ItemsResult(("2",)))
    assert c.get(rk(q="one")) is not None  # refresh one
    c.put(rk(q="three"), ItemsResult(("3",)))
    assert len(c) == 2
    assert c.get(rk(q="two")) is None
    assert c.get(rk(q="one")) is not None


def test_extraction_key_changes_with_model_id_and_prompt():
    a = extraction_key(scope=SCOPE, text="Dana moved", model_id="m-a", prompt_hash="p1")
    b = extraction_key(scope=SCOPE, text="Dana moved", model_id="m-b", prompt_hash="p1")
    p = extraction_key(scope=SCOPE, text="Dana moved", model_id="m-a", prompt_hash="p2")
    assert len({a.slot(), b.slot(), p.slot()}) == 3
    c = ReadCache("extraction", enabled=True)
    c.put(a, {"facts": 1})
    assert c.get(b) is None and c.get(p) is None and c.get(a) == {"facts": 1}


def test_embedding_key_components_and_case_sensitivity():
    base = dict(scope=SCOPE, text="Apple", embedder_id="e1", vector_space_version="v1")
    k = embedding_key(**base)
    assert k.slot() != embedding_key(**{**base, "embedder_id": "e2"}).slot()
    assert k.slot() != embedding_key(**{**base, "vector_space_version": "v2"}).slot()
    assert k.slot() != embedding_key(**{**base, "text": "apple"}).slot()


def test_rerank_key_candidate_set_and_reranker():
    def k(ids, rid="r1"):
        return rerank_key(
            scope=SCOPE, viewer="alice", query="q", reranker_id=rid, candidate_ids=ids
        )

    assert k(["a", "b"]).slot() == k(["b", "a", "a"]).slot()
    assert k(["a", "b"]).slot() != k(["a", "c"]).slot()
    assert k(["a"]).slot() != k(["a"], rid="r2").slot()


def test_kind_mismatch_rejected():
    c = ReadCache("embedding", enabled=True)
    try:
        c.put(rk(), 1)
    except ValueError:
        return
    raise AssertionError("expected ValueError")
