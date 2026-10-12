from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from memspine.ingest.segment import SegmentConfig, Segmenter, SegmentState, Turn

T0 = datetime(2026, 3, 3, 9, 0, tzinfo=UTC)


def mk(i, minutes=0.0, session="s1", text=None, base=T0):
    return Turn(
        id=f"t{i}",
        speaker="Ilse" if i % 2 else "Bram",
        text=text if text is not None else f"Message number {i} about the garden.",
        source_at=base + timedelta(minutes=minutes),
        session=session,
    )


def new_ids(plans):
    return [i for p in plans for i in p.new_ids]


def stream(n, seed):
    rnd = random.Random(seed)
    out, m, s = [], 0.0, 1
    for i in range(n):
        m += rnd.choice([0.5, 1, 2, 45, 700])
        if rnd.random() < 0.1:
            s += 1
        txt = "word " * rnd.choice([1, 5, 40, 3000])
        out.append(mk(i, m, f"s{s}", txt.strip()))
    return out


@pytest.mark.parametrize("seed", range(12))
def test_coverage_every_unit_new_exactly_once(seed):
    seg = Segmenter(scope="ns")
    turns = stream(40, seed)
    plans = seg.plan(turns)
    got = new_ids(plans)
    assert len(got) == len(set(got))
    assert {i.split("#")[0] for i in got} == {t.id for t in turns}
    for p in plans:
        assert not set(p.new_ids) & set(p.context_ids)
        assert p.tokens_new <= seg.config.max_new_tokens
        assert [ln.no for ln in p.lines] == list(range(1, len(p.lines) + 1))
        assert [ln.kind for ln in p.lines] == ["context"] * len(p.context_ids) + ["new"] * len(
            p.new_ids
        )


def test_unsplit_turns_in_order_one_each():
    turns = [mk(i, i) for i in range(20)]
    assert new_ids(Segmenter().plan(turns)) == [t.id for t in turns]


def test_time_gap_cut():
    turns = [mk(0, 0), mk(1, 1), mk(2, 40), mk(3, 41)]
    plans = Segmenter().plan(turns)
    assert [p.new_ids for p in plans] == [("t0", "t1"), ("t2", "t3")]
    assert plans[0].reasons == ("time_gap",)
    assert plans[1].reasons == ("flush",)
    assert plans[1].context_ids == ()  # no context across a cut


def test_below_gap_not_cut():
    assert len(Segmenter().plan([mk(0, 0), mk(1, 29)])) == 1


def test_calendar_day_cut():
    base = datetime(2026, 3, 3, 23, 50, tzinfo=UTC)
    turns = [mk(0, 0, base=base), mk(1, 5, base=base), mk(2, 20, base=base)]  # crosses midnight
    plans = Segmenter().plan(turns)
    assert [p.new_ids for p in plans] == [("t0", "t1"), ("t2",)]
    assert plans[0].reasons == ("calendar_day",)
    assert len(Segmenter(SegmentConfig(calendar_day_cut=False)).plan(turns)) == 1


def test_session_cut():
    turns = [mk(0, 0, "a"), mk(1, 1, "a"), mk(2, 2, "b"), mk(3, 3, "b")]
    plans = Segmenter().plan(turns)
    assert [p.session for p in plans] == ["a", "b"]
    assert plans[0].reasons == ("session_end",)


def test_missing_source_time_never_gap_cut():
    turns = [Turn(id=f"t{i}", text="hi there") for i in range(4)]
    assert len(Segmenter().plan(turns)) == 1


def test_max_turns_and_context():
    turns = [mk(i, i * 0.1) for i in range(15)]
    plans = Segmenter(SegmentConfig(max_turns=5, min_turns=1, context_lines=3)).plan(turns)
    assert [len(p.new_ids) for p in plans] == [5, 5, 5]
    assert plans[0].context_ids == ()
    assert plans[1].context_ids == ("t2", "t3", "t4")
    assert plans[0].reasons == ("turns",)


def test_token_budget_cut():
    cfg = SegmentConfig(max_new_tokens=30, min_turns=1)
    turns = [mk(i, i * 0.1, text="x" * 48) for i in range(4)]  # 12 tokens each
    plans = Segmenter(cfg).plan(turns)
    assert [len(p.new_ids) for p in plans] == [2, 2]
    assert plans[0].reasons == ("tokens",)


def test_tiny_tail_merges_within_slack_not_across_cut():
    plans = Segmenter(SegmentConfig(max_turns=3, min_turns=2, tail_slack=2)).plan(
        [mk(i, i * 0.1) for i in range(7)]
    )
    assert [len(p.new_ids) for p in plans] == [3, 4]  # 3,3,1 -> 3,4
    turns = [mk(0, 0), mk(1, 1), mk(2, 2), mk(3, 3), mk(4, 100)]
    plans = Segmenter(SegmentConfig(max_turns=3)).plan(turns)
    assert [len(p.new_ids) for p in plans] == [4, 1]  # the gap is never merged across


def test_oversize_turn_split_on_sentences_with_stable_subids():
    sentences = [f"Sentence {i} is here." for i in range(40)]
    big = Turn(id="big", speaker="Bram", text=" ".join(sentences), source_at=T0, session="s")
    cfg = SegmentConfig(max_new_tokens=50, max_turns=2, min_turns=1)
    plans = Segmenter(cfg).plan([mk(0, 0, "s"), big, mk(2, 1, "s")])
    ids = new_ids(plans)
    assert ids[0] == "t0" and ids[-1] == "t2"
    pieces = [i for i in ids if i.startswith("big#")]
    assert pieces == [f"big#{n}" for n in range(len(pieces))] and len(pieces) > 1
    pieces_text = [
        ln.text for p in plans for ln in p.lines if ln.kind == "new" and ln.id.startswith("big#")
    ]
    assert all(t.endswith(".") for t in pieces_text)  # cut at sentence ends
    assert " ".join(pieces_text) == big.text
    assert any(p.split_turn_ids == ("big",) for p in plans)
    assert new_ids(Segmenter(cfg).plan([mk(0, 0, "s"), big, mk(2, 1, "s")])) == ids  # stable


def test_oversize_single_sentence_falls_back_to_words_and_chars():
    cfg = SegmentConfig(max_new_tokens=10)
    plans = Segmenter(cfg).plan([Turn(id="a", text="w " * 100), Turn(id="b", text="z" * 300)])
    assert all(p.tokens_new <= 10 for p in plans)
    assert (
        "".join(
            ln.text for p in plans for ln in p.lines if ln.kind == "new" and ln.id.startswith("b#")
        )
        == "z" * 300
    )


def test_context_never_new_twice_and_cited_lines_present():
    turns = [mk(i, i * 0.1) for i in range(30)]
    plans = Segmenter(SegmentConfig(max_turns=4, min_turns=1)).plan(turns)
    seen = set()
    for p in plans:
        assert not seen & set(p.new_ids)
        assert set(p.context_ids) <= seen  # context is always earlier, already-NEW lines
        seen |= set(p.new_ids)


def test_context_stops_at_session_boundary():
    turns = [mk(0, 0, "a"), mk(1, 1, "a"), mk(2, 2, "b")]
    plans = Segmenter().plan(turns)
    assert plans[1].context_ids == ()
    plans = Segmenter(SegmentConfig(context_across_cut=True)).plan(turns)
    assert plans[1].context_ids == ("t0", "t1")


def test_determinism_and_ids():
    turns = stream(30, 3)
    a, b = Segmenter(scope="x").plan(turns), Segmenter(scope="x").plan(turns)
    assert a == b
    assert len({p.window_id for p in a}) == len(a)
    other = Segmenter(scope="y").plan(turns)
    assert {p.window_id for p in a}.isdisjoint(p.window_id for p in other)


@pytest.mark.parametrize("seed", range(6))
def test_resume_from_committed_offset_is_the_suffix(seed):
    seg = Segmenter(scope="ns")
    turns = stream(30, seed)
    full = seg.plan(turns)
    for cut in range(1, len(full)):
        state = SegmentState()
        for p in full[:cut]:
            state = seg.advance(state, p)
        # (a) caller passes the whole stream, (b) only the unprocessed turns plus the tail state
        start = [t.id for t in turns].index(state.last_unit_id.split("#")[0])
        rest = turns[start:]
        for given in (turns, rest):
            resumed = seg.plan(given, state)
            assert resumed == full[cut:] or [p.new_ids for p in resumed] == [
                p.new_ids for p in full[cut:]
            ]
            assert new_ids(full[:cut]) + new_ids(resumed) == new_ids(full)


def test_resume_mid_split_turn():
    big = Turn(
        id="big",
        text=" ".join(f"Line {i} ends here." for i in range(60)),
        source_at=T0,
        session="s",
    )
    seg = Segmenter(SegmentConfig(max_new_tokens=40, max_turns=2, min_turns=1))
    full = seg.plan([big])
    state = seg.advance(SegmentState(), full[0])
    assert new_ids(full[:1]) + new_ids(seg.plan([big], state)) == new_ids(full)


def test_now_hint_withholds_open_tail_only():
    turns = [mk(0, 0), mk(1, 1)]
    seg = Segmenter()
    assert len(seg.plan(turns)) == 1
    assert seg.plan(turns, now_hint=T0 + timedelta(minutes=1, seconds=30)) == []
    assert len(seg.plan(turns, now_hint=T0 + timedelta(minutes=10))) == 1


def test_duplicate_ids_rejected():
    with pytest.raises(ValueError):
        Segmenter().plan([mk(0), mk(0)])


def test_custom_counter_used():
    seg = Segmenter(SegmentConfig(max_new_tokens=3, min_turns=1), counter=lambda s: len(s.split()))
    plans = seg.plan([Turn(id="a", text="one two"), Turn(id="b", text="three four")])
    assert [p.new_ids for p in plans] == [("a",), ("b",)]
    assert plans[0].tokens_new == 2
