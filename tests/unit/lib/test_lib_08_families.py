"""LIB-08: source families, quarantine release, restated count (S4-2, S4-3, S4-4)."""

from __future__ import annotations

import pytest

from memspine.schemas.durable import LineageEdge
from memspine.trust.families import (
    RestateState,
    SourceDescriptor,
    Statement,
    StoredRecord,
    corroboration,
    families_of,
    family_of,
    registrable_domain,
    restate,
    support_of,
)


def person(pid: str) -> SourceDescriptor:
    return SourceDescriptor(kind="person", principal=pid)


def rec(rid: str, source: SourceDescriptor, **kw: object) -> StoredRecord:
    base: dict[str, object] = {"trust": 0.8, "content_fingerprint": "fp-tea"}
    base.update(kw)
    return StoredRecord(record_id=rid, source=source, **base)  # type: ignore[arg-type]


def edge(child: str, parent: str, family: str | None = None, role: str = "member") -> LineageEdge:
    return LineageEdge(child=child, parent=parent, role=role, source_family=family)  # type: ignore[arg-type]


def lookup_of(*edges: LineageEdge):
    def lookup(rid: str):
        return [e for e in edges if e.child == rid]

    return lookup


# ---------------------------------------------------------------- family_of


def test_person_family_is_opaque_principal() -> None:
    assert family_of(person("ilse-7")) == "person:ilse-7"
    assert family_of(SourceDescriptor(kind="person")) == "unknown:person"


def test_document_family_author_else_document() -> None:
    assert family_of(SourceDescriptor(kind="document", author="Bram", document_id="d1")) == (
        "doc:author:Bram"
    )
    assert family_of(SourceDescriptor(kind="document", document_id="d1")) == "doc:d1"


def test_tool_family_is_tool_name() -> None:
    assert (
        family_of(SourceDescriptor(kind="tool", tool_name="calendar_sync")) == "tool:calendar_sync"
    )


@pytest.mark.parametrize(
    ("url", "domain"),
    [
        ("https://www.example.com/a/b?q=1", "example.com"),
        ("http://blog.news.example.co.uk:8080/x", "example.co.uk"),
        ("https://user@shop.example.com.au/p", "example.com.au"),
        ("mirror.example.ac.uk", "example.ac.uk"),
        ("https://a.b.example.co.jp", "example.co.jp"),
        ("https://localhost/x", "localhost"),
        ("http://10.0.0.5:80/x", "10.0.0.5"),
        ("https://www.example.org.", "example.org"),
    ],
)
def test_registrable_domain(url: str, domain: str) -> None:
    assert registrable_domain(url) == domain


def test_registrable_domain_pluggable_suffixes() -> None:
    assert registrable_domain("a.b.example.test.zz", {"test.zz"}) == "example.test.zz"


def test_two_pages_same_domain_one_family() -> None:
    a = SourceDescriptor(kind="web", url="https://a.example.com/p1")
    b = SourceDescriptor(kind="web", url="https://www.example.com/p2")
    assert family_of(a) == family_of(b) == "web:example.com"


# ---------------------------------------------------------------- families_of


def test_plain_record_uses_own_family() -> None:
    r = rec("r1", person("ilse"))
    assert families_of(r, lookup_of()) == {"person:ilse"}


def test_assistant_echo_inherits_user_family() -> None:
    echo = rec("e1", SourceDescriptor(kind="assistant", principal="bot"))
    got = families_of(echo, lookup_of(edge("e1", "u1", "person:ilse")))
    assert got == {"person:ilse"}


def test_assistant_echo_unknown_parent_is_non_independent() -> None:
    echo = rec("e1", SourceDescriptor(kind="assistant", principal="bot"))
    assert families_of(echo, lookup_of()) == {"assistant:bot"}


def test_summary_inherits_through_chain_and_context_is_skipped() -> None:
    summ = rec("s2", SourceDescriptor(kind="derived"))
    lk = lookup_of(
        edge("s2", "s1"),
        edge("s1", "u1", "person:ilse"),
        edge("s2", "x", "web:spam.example", role="context"),
    )
    assert families_of(summ, lk) == {"person:ilse"}


def test_unresolved_parent_and_cycle() -> None:
    summ = rec("s", SourceDescriptor(kind="derived"))
    assert families_of(summ, lookup_of(edge("s", "p"))) == {"derived:unresolved"}
    assert families_of(summ, lookup_of(edge("s", "t"), edge("t", "s"))) == {"derived:unresolved"}


# ---------------------------------------------------------------- corroboration


def test_other_user_releases() -> None:
    held = rec("h", person("ilse"), quarantined=True)
    d = corroboration(rec("i", person("bram")), held)
    assert d.release and d.reason == "released" and d.family == "person:bram"


def test_operator_releases() -> None:
    held = rec("h", person("ilse"), quarantined=True)
    d = corroboration(rec("i", SourceDescriptor(kind="operator", principal="ops1")), held)
    assert d.release


def test_user_repeating_themselves_cannot_release() -> None:
    held = rec("h", person("ilse"), quarantined=True)
    again = rec("i", person("ilse"))
    d = corroboration(again, held)
    assert not d.release and d.reason == "same_family"


def test_web_and_tool_cannot_release() -> None:
    held = rec("h", person("ilse"), quarantined=True)
    web = rec("w", SourceDescriptor(kind="web", url="https://news.example.com/a"))
    tool = rec("t", SourceDescriptor(kind="tool", tool_name="lookup"))
    doc = rec("d", SourceDescriptor(kind="document", author="Bram"))
    for r in (web, tool, doc):
        d = corroboration(r, held)
        assert not d.release and d.reason == "external_family"


def test_two_pages_same_domain_cannot_release_each_other_nor_anyone() -> None:
    held = rec("h", SourceDescriptor(kind="web", url="https://a.example.com/1"), quarantined=True)
    other_page = rec("w", SourceDescriptor(kind="web", url="https://b.example.com/2"))
    assert corroboration(other_page, held).reason == "same_family"


def test_assistant_echo_cannot_corroborate_its_source() -> None:
    held = rec("h", person("ilse"), quarantined=True)
    echo = rec("e", SourceDescriptor(kind="assistant", principal="bot"))
    fams = families_of(echo, lookup_of(edge("e", "h", "person:ilse")))
    d = corroboration(echo, held, incoming_families=fams)
    assert not d.release and d.reason == "same_family"


def test_assistant_alone_cannot_release_even_with_unrelated_family() -> None:
    held = rec("h", person("ilse"), quarantined=True)
    echo = rec("e", SourceDescriptor(kind="assistant", principal="bot"))
    assert corroboration(echo, held).reason == "non_independent_family"
    fams = families_of(echo, lookup_of(edge("e", "u", "person:bram")))
    assert corroboration(echo, held, incoming_families=fams).reason == "non_independent_family"


def test_summary_of_ilse_cannot_vouch_for_ilse() -> None:
    held = rec("h", person("ilse"), quarantined=True)
    summ = rec("s", SourceDescriptor(kind="derived"))
    lk = lookup_of(edge("s", "t1", "person:ilse"), edge("s", "t2", "person:ilse"))
    d = corroboration(summ, held, incoming_families=families_of(summ, lk))
    assert not d.release and d.reason == "same_family"


def test_summary_mixing_ilse_and_another_still_blocked() -> None:
    held = rec("h", person("ilse"), quarantined=True)
    summ = rec("s", SourceDescriptor(kind="derived"))
    lk = lookup_of(edge("s", "t1", "person:ilse"), edge("s", "t2", "person:bram"))
    assert (
        corroboration(summ, held, incoming_families=families_of(summ, lk)).reason == "same_family"
    )


def test_guards_self_quarantined_trust() -> None:
    held = rec("h", person("ilse"), quarantined=True)
    assert corroboration(held, held).reason == "self"
    assert corroboration(rec("i", person("bram"), quarantined=True), held).reason == (
        "incoming_quarantined"
    )
    assert corroboration(rec("i", person("bram"), trust=0.3), held).reason == "below_default_trust"
    assert corroboration(rec("i", person("bram"), trust=0.5), held).release


def test_content_match_fingerprint_or_key_within_type() -> None:
    key = {"entity": "ilse", "attribute": "drink", "value": "tea"}
    held = rec("h", person("ilse"), quarantined=True, content_fingerprint="a", **key)
    same_key = rec("i", person("bram"), content_fingerprint="b", **key)
    assert corroboration(same_key, held).release
    other_type = rec("i", person("bram"), content_fingerprint="b", memory_type="procedural", **key)
    assert corroboration(other_type, held).reason == "different_content"
    other_value = rec("i", person("bram"), content_fingerprint="b", **{**key, "value": "coffee"})
    assert corroboration(other_value, held).reason == "different_content"


def test_keyless_records_match_by_fingerprint_only() -> None:
    held = rec("h", person("ilse"), quarantined=True, content_fingerprint="a", entity="ilse")
    inc = rec("i", person("bram"), content_fingerprint="b", entity="ilse")
    assert corroboration(inc, held).reason == "different_content"


# ---------------------------------------------------------------- restate


def stmt(*fams: str, fp: str = "v1", derived: bool = False) -> Statement:
    return Statement(families=frozenset(fams), content_fingerprint=fp, derived=derived)


def test_same_family_same_value_only_restates() -> None:
    s = restate(RestateState(), stmt("person:ilse"))
    assert (s.independent, s.restated) == (1, 0)
    s = restate(s, stmt("person:ilse"))
    s = restate(s, stmt("person:ilse"))
    assert (s.independent, s.restated) == (1, 2)


def test_new_family_adds_independent_support_per_distinct_family() -> None:
    s = restate(RestateState(), stmt("person:ilse"))
    s = restate(s, stmt("person:bram"))
    s = restate(s, stmt("web:example.com"))
    s = restate(s, stmt("web:example.com"))
    assert (s.independent, s.restated) == (3, 1)
    sup = support_of(s)
    assert sup.independent == 3 and sup.restated == 1 and len(sup.families) == 3


def test_derived_and_echo_change_nothing() -> None:
    s = restate(RestateState(), stmt("person:ilse"))
    assert restate(s, stmt("person:ilse", derived=True)) == s
    assert restate(s, stmt("person:bram", derived=True)) == s
    assert restate(s, stmt("assistant:bot")) == s
    assert restate(s, stmt("derived:unresolved", "unknown:person")) == s


def test_different_value_leaves_state() -> None:
    s = restate(RestateState(), stmt("person:ilse"))
    assert restate(s, stmt("person:bram", fp="v2")) == s
