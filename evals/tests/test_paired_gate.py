"""G-E1: the paired adoption gate never promotes an identical or incomplete arm, and holds every category to its band."""

from __future__ import annotations

import eval_blind
from memspine_evals.paired_gate import paired, sign_test_p


def _ids(convs: int, per: int) -> list[str]:
    return [f"c{c}/q{i}" for c in range(convs) for i in range(per)]


def test_identical_arm_is_no_change_never_promote() -> None:
    ids = _ids(10, 40)
    ref = {q: i % 2 == 0 for i, q in enumerate(ids)}
    r = paired(dict(ref), ref, ids)
    assert r.verdict == "NO CHANGE"
    assert (r.gained, r.lost, r.net) == (0, 0, 0)


def test_missing_rows_make_the_verdict_incomplete() -> None:
    ids = _ids(10, 40)
    ref = {q: False for q in ids}
    arm = {q: True for q in ids[:-1]}  # one question missing in the arm, the rest all gained
    assert paired(arm, ref, ids).verdict == "INCOMPLETE"
    assert paired({q: True for q in ids}, {}, ids).verdict == "INCOMPLETE"


def test_large_spread_gain_promotes() -> None:
    ids = _ids(10, 40)
    ref = {q: False for q in ids}
    arm = {q: (int(q.split("q")[-1]) < 6) for q in ids}  # 6 gains in every conversation: net 60
    r = paired(arm, ref, ids)
    assert r.verdict == "PROMOTE", r.reasons
    assert r.ci_low > 0 and r.p_sign < 0.05


def test_gain_from_one_conversation_is_held() -> None:
    ids = _ids(10, 40)
    ref = {q: False for q in ids}
    arm = {q: q.startswith("c0/") and int(q.split("q")[-1]) < 30 for q in ids}  # 30 gains, all in one conversation
    r = paired(arm, ref, ids)
    assert r.net == 30 and r.net > r.band
    assert r.verdict == "HOLD"  # the cluster bootstrap lower bound is 0
    assert any("bootstrap" in x for x in r.reasons)


def test_a_category_beyond_its_band_rejects_despite_a_net_gain() -> None:
    ids = _ids(10, 40)
    cat = {q: ("multi" if int(q.split("q")[-1]) < 10 else "single") for q in ids}
    ref = {q: cat[q] == "multi" for q in ids}  # multi all right, single all wrong in the reference
    arm = {q: cat[q] == "single" for q in ids}  # arm flips both: +300 single, -100 multi
    r = paired(arm, ref, ids, category_of=cat.__getitem__)
    assert r.net > r.band
    assert r.verdict == "REJECT"
    assert r.by_category["multi"]["beyond_band"]


def test_sign_test_is_exact_and_symmetric() -> None:
    assert sign_test_p(0, 0) == 1.0
    assert abs(sign_test_p(5, 5) - 1.0) < 1e-12
    assert abs(sign_test_p(15, 6) - sign_test_p(6, 15)) < 1e-12
    assert sign_test_p(20, 0) < 1e-5


def test_blind_verdict_needs_every_dataset() -> None:
    full = {"n": 200, "delta": 2.0, "gained": 8, "lost": 4, "guard_ok": True, "complete": True}
    assert eval_blind.verdict([full, dict(full)]) == "CONFIRMS"
    assert eval_blind.verdict([full, {"n": 0, "complete": False}]) == "INCOMPLETE"
    assert eval_blind.verdict([full]) == "INCOMPLETE"
    assert eval_blind.verdict([full, dict(full, complete=False)]) == "INCOMPLETE"
    same = dict(full, gained=0, lost=0, delta=0.0)
    assert eval_blind.verdict([same, dict(same)]) == "NO CHANGE"
    assert eval_blind.verdict([full, dict(full, guard_ok=False)]) == "REJECT (guard)"
