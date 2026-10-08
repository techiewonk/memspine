"""G-3: event-edge identity keys on the event day and numerals (opt-in, ``dated``)."""

from __future__ import annotations

from memspine.prompts.models import ExtractedEdge
from memspine.workers.pipelines import _edge_key


def _edge(fact: str, day: str | None, kind: str = "event") -> ExtractedEdge:
    return ExtractedEdge(
        src_entity="ana", rel="visited", dst_entity="paris", fact=fact, valid_from=day, kind=kind
    )


def test_plain_identity_is_unchanged() -> None:
    a = _edge("Ana visited Paris", "2022-05-01")
    b = _edge("Ana visited Paris again", "2023-07-09")
    assert _edge_key("u", a) == _edge_key("u", b)


def test_dated_events_on_different_days_stay_separate() -> None:
    a = _edge("Ana visited Paris", "2022-05-01")
    b = _edge("Ana visited Paris", "2023-07-09")
    assert _edge_key("u", a, dated=True) != _edge_key("u", b, dated=True)


def test_dated_events_with_different_numbers_stay_separate() -> None:
    a = _edge("Ana ran 5 km in Paris", None)
    b = _edge("Ana ran 10 km in Paris", None)
    assert _edge_key("u", a, dated=True) != _edge_key("u", b, dated=True)


def test_same_event_restated_is_still_one() -> None:
    a = _edge("Ana visited Paris on 1 May 2022", "2022-05-01T10:00:00")
    b = _edge("On 1 May 2022 Ana went to Paris", "2022-05-01")
    assert _edge_key("u", a, dated=True) == _edge_key("u", b, dated=True)


def test_state_edges_ignore_dates() -> None:
    a = _edge("Ana lives in Paris", "2022-05-01", kind="state")
    b = _edge("Ana lives in Paris", "2023-07-09", kind="state")
    assert _edge_key("u", a, dated=True) == _edge_key("u", b, dated=True)
