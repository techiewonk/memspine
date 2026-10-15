"""LIB-04: rule entity resolver. Unit tests plus the X-05 entity-pair scorecard."""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import pytest

from memspine.derive.resolve import (
    Candidate,
    MentionView,
    OnlineResolver,
    PairContext,
    normalise_name,
    resolve_pair,
)
from memspine.schemas.durable.common import OwnerScope

_EVALS = Path(__file__).resolve().parents[3] / "evals"
if str(_EVALS) not in sys.path:
    sys.path.insert(0, str(_EVALS))

from labelled.loader import load_entity_pairs


def _v(
    text: str, ctx: str = "", type_: str = "", lang: str | None = None, speaker: str | None = None
):
    return MentionView(text, type_, ctx, lang, speaker)


# --- normalisation ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "norm"),
    [
        ("Jürgen", "juergen"),
        ("Søren", "soeren"),
        ("Straße", "strasse"),
        ("Åse", "aase"),
        ("Dr. Hans  Vos", "hans vos"),
        ("Mevrouw de Vries", "de vries"),
        ("Frau Lindner", "lindner"),
        ("O'Brien", "obrien"),
        ("Version 3.1.", "version 3.1"),
        ("ＫÖＬＮ", "koeln"),
    ],
)
def test_normalise(raw, norm):
    assert normalise_name(raw) == norm


# --- pair decisions ----------------------------------------------------------------------------


def test_exact_match_same_without_context():
    assert resolve_pair("Ilse", "ilse").verdict == "same"


def test_homonym_by_context_and_explicit_attrs_and_speakers():
    a = _v("Ilse", "Nurse who works nights in Bremen.", lang="en")
    b = _v("Ilse", "Student of linguistics in Utrecht.", lang="en")
    assert resolve_pair(a, b).verdict == "different"
    ctx = PairContext(a_attrs={"city": "Kiel"}, b_attrs={"city": "Bonn"})
    assert resolve_pair("Anna", "Anna", ctx).reason.startswith("explicit_disambiguator")
    sp = PairContext(same_conversation=True)
    d = resolve_pair(_v("Anna", speaker="alice"), _v("Anna", speaker="bob"), sp)
    assert d.verdict == "pending_merge"


def test_numeric_tokens_exact():
    assert resolve_pair("Flat 12", "Flat 21").verdict == "different"
    assert resolve_pair("Version 3.1", "Version 3.10").verdict == "different"
    assert resolve_pair("Release 2.4", "release 2.4.0").verdict == "same"
    assert resolve_pair("Hausnummer 14a", "Hausnummer 14b").verdict == "different"
    assert resolve_pair("Bus 7", "Tram 7").verdict == "different"


def test_alias_rules():
    assert resolve_pair("Bram van Dijk", "Abraham van Dijk").verdict == "same"
    assert resolve_pair("J. W. Brandt", "Jan Willem Brandt").verdict == "same"
    assert resolve_pair("Søren Madsen", "Soren Madsen").verdict == "same"
    assert resolve_pair("Jürgen Maier", "Juergen Maier").verdict == "same"
    assert resolve_pair("Bram", "Abraham").verdict == "pending_merge"  # nickname without surname
    assert resolve_pair("Bram de Vries", "Bart de Vries").verdict != "same"
    assert resolve_pair("Tomasz Kowal", "Tomasz Kowalski").verdict != "same"
    # a different given name with a shared surname is never merged by fuzzy similarity alone
    assert resolve_pair("Hans Vos", "Hanne Vos").verdict != "same"


def test_alias_with_conflicting_context_is_pending():
    a = _v("Anna Bos", "Librarian in Gent.", lang="nl")
    b = _v("Anneke Bos", "Librarian in Antwerp.", lang="nl")
    assert resolve_pair(a, b).verdict == "pending_merge"


def test_exonyms():
    assert resolve_pair("Köln", "Cologne").verdict == "same"
    assert resolve_pair("Den Haag", "The Hague").verdict == "same"
    assert resolve_pair("東京", "Tokyo").verdict == "same"
    assert resolve_pair("Wien", "Vienne").verdict != "same"


def test_type_mismatch_is_different():
    assert (
        resolve_pair(_v("Aurora", type_="animal"), _v("Aurora", type_="company")).verdict
        == "different"
    )


# --- online resolver ---------------------------------------------------------------------------

S1 = OwnerScope(tenant="t1", owner="u1")
S2 = OwnerScope(tenant="t1", owner="u2")
S3 = OwnerScope(tenant="t2", owner="u1")


def test_online_resolver_existing_new_pending():
    r = OnlineResolver()
    cands = [
        Candidate("e1", "Abraham van Dijk", S1, "person", "Plumber in Utrecht."),
        Candidate("e2", "Ilse", S1, "person", "Nurse in Bremen."),
    ]
    d = r.resolve(_v("Bram van Dijk", "Plumber in Utrecht.", "person"), S1, cands)
    assert (d.kind, d.entity_id) == ("existing", "e1")
    d = r.resolve(_v("Mercator", "Sailing ship in Ostend.", "ship"), S1, cands)
    assert d.kind == "new" and d.entity_id.startswith("ent_")
    d = r.resolve(_v("Bram", "", "person"), S1, [Candidate("e3", "Abraham", S1, "person")])
    assert d.kind == "pending_merge" and d.candidates == ("e3",)


def test_online_resolver_scope_isolation():
    r = OnlineResolver()
    cand = Candidate("e1", "Ilse", S2, "person")
    assert r.resolve(_v("Ilse", type_="person"), S1, [cand]).kind == "new"
    d1 = r.resolve(_v("Ilse", type_="person"), S1, [])
    d2 = r.resolve(_v("Ilse", type_="person"), S2, [])
    d3 = r.resolve(_v("Ilse", type_="person"), S3, [])
    assert len({d1.entity_id, d2.entity_id, d3.entity_id}) == 3


def test_online_resolver_homonym_gets_distinct_id_and_is_deterministic():
    r = OnlineResolver()
    existing = Candidate("e1", "Ilse", S1, "person", "Nurse who works nights in Bremen.")
    mention = _v("Ilse", "Student of linguistics in Utrecht.", "person", lang="en")
    d = r.resolve(mention, S1, [existing])
    assert d.kind == "new" and d.entity_id != "e1"
    assert OnlineResolver().resolve(mention, S1, [existing]) == d


def test_online_resolver_ambiguous_same_is_pending_and_cache_is_bounded():
    r = OnlineResolver(max_cache=2)
    cands = [Candidate("a", "Köln", S1), Candidate("b", "Cologne", S1)]
    d = r.resolve("Koeln", S1, cands)
    assert d.kind == "pending_merge" and set(d.candidates) == {"a", "b"}
    for i in range(5):
        r.resolve(f"Name{i}", S1, [])
    assert len(r) == 2
    with pytest.raises(ValueError):
        OnlineResolver(max_cache=0)


# --- X-05 scorecard ----------------------------------------------------------------------------


def _score():
    pairs = load_entity_pairs().items
    stats = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0, "pending": 0, "n": 0})
    misses = []
    for p in pairs:
        a = MentionView(p.a.mention, "", p.a.context, p.languages[0])
        b = MentionView(p.b.mention, "", p.b.context, p.languages[1])
        d = resolve_pair(a, b)
        for key in (p.category, "all"):
            s = stats[key]
            s["n"] += 1
            s["pending"] += d.verdict == "pending_merge"
            if d.verdict == "same" and p.label == "same":
                s["tp"] += 1
            elif d.verdict == "same":
                s["fp"] += 1
            elif p.label == "same":
                s["fn"] += 1
        if (d.verdict == "same") != (p.label == "same"):
            misses.append((p.id, p.label, d.verdict, d.reason))
    return stats, misses


def test_entity_pair_scorecard(capsys):
    stats, misses = _score()
    lines = []
    for key in ("alias", "homonym", "numeric", "cross_language", "all"):
        s = stats[key]
        prec = s["tp"] / (s["tp"] + s["fp"]) if s["tp"] + s["fp"] else 1.0
        rec = s["tp"] / (s["tp"] + s["fn"]) if s["tp"] + s["fn"] else 1.0
        lines.append(
            f"{key:15s} n={s['n']:2d} precision(same)={prec:.3f} recall(same)={rec:.3f} "
            f"pending={s['pending'] / s['n']:.3f} fp={s['fp']} fn={s['fn']}"
        )
    with capsys.disabled():
        print("\n" + "\n".join(lines))
        for m in misses:
            print("MISS", *m)
    for key in ("alias", "homonym", "numeric", "cross_language", "all"):
        s = stats[key]
        if s["tp"] + s["fp"]:
            assert s["tp"] / (s["tp"] + s["fp"]) >= 0.95, key
