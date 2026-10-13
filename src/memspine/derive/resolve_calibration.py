# ruff: noqa: E501
"""W2-09 calibration of the online resolver's thresholds on a synthetic homonym/alias set (RV17, W-19 gate).

Gate: "calibrated thresholds with no homonym merges above a stated rate". The set uses invented names
only. Each case is an existing entity (name + one context sentence) and a new mention (name + context)
with a ground-truth class:

* ``alias``: the same person written another way (nickname, accent, case, title, given name only, typo);
* ``homonym``: a different person with the same name or the same given name (Noor Haddad in Utrecht vs
  Noor Haddad in Delft, Noor Haddad vs Noor Visser, bare "Noor" in two cities);
* ``unrelated``: a different name.

The outcome of the full ladder (rules, then embedding) is ``merged`` (existing), ``pending`` or ``new``.
:func:`calibrate` picks ``(resolve_high, resolve_grey)`` on the dev half (even case index) subject to a
homonym-merge rate and an unrelated-merge rate at most ``max_rate``; :func:`evaluate` reports the
confusion matrix on any half. The embedder is a parameter: the shipped defaults were chosen with the
deterministic hash embedder (the only model-free one); re-run :func:`calibrate` with the production
embedder before relying on the embedding step with it (rule outcomes do not depend on the thresholds).

Result with the shipped defaults ``resolve_high=0.9``, ``resolve_grey=0.7`` and the hash embedder
(437 cases: 144 alias, 200 homonym, 93 unrelated; held-out = odd index)::

    held-out half    merged  pending     new     n        all cases     merged  pending     new     n
    alias                23       26      23    72        alias             48       52      44   144
    homonym               0       32      68   100        homonym            0       65     135   200
    unrelated             0        0      46    46        unrelated          0        0      93    93

Homonym-merge rate 0/200 (0.0%), stated gate <= 1%; unrelated-merge rate 0/93. Sensitivity: with
``resolve_high <= 0.71`` the family-name-alone cases merge (cosine 0.707) and the homonym-merge rate is
7.5%, so the gate is not vacuous. Rule-only (no embedder) gives the same merged counts: the embedding
step adds pending candidates, and merges only names the rules call ``no_match``. Known rule gap (LIB-04,
not changed here): two same-named entities whose contexts share one generic word (both "sells ...") read
as ``agree`` and merge.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from memspine.derive.resolve_stage import EntityResolveStage, ResolveSettings
from memspine.schemas.durable.common import OwnerScope

__all__ = [
    "Case",
    "Confusion",
    "calibrate",
    "evaluate",
    "synthetic_cases",
]

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]
CLASSES = ("alias", "homonym", "unrelated")
OUTCOMES = ("merged", "pending", "new")

_GIVEN = [
    "Noor",
    "Ilse",
    "Bram",
    "Tomasz",
    "Soren",
    "Mira",
    "Anouk",
    "Femke",
    "Joost",
    "Lieke",
    "Dagmar",
    "Kasimir",
    "Ottilie",
    "Reza",
    "Yara",
    "Idris",
    "Saskia",
    "Wouter",
    "Thijs",
    "Marit",
    "Okke",
    "Elif",
    "Matthias",
    "Greta",
    "Hannes",
    "Liesbeth",
    "Alexander",
    "Katharina",
    "Wilhelm",
    "Margarethe",
]
_SURNAMES = [
    "Haddad",
    "Visser",
    "Brandt",
    "Okafor",
    "Lindqvist",
    "Marek",
    "Vos",
    "Korhonen",
    "Delacroix",
    "Petrov",
    "Janssen",
    "Falk",
    "Adeyemi",
    "Koenig",
    "Sorensen",
    "Wojcik",
    "Amrani",
    "Hartmann",
]
_CITIES = [
    "Utrecht",
    "Delft",
    "Leiden",
    "Ghent",
    "Brno",
    "Tromso",
    "Bergen",
    "Aarhus",
    "Lublin",
    "Graz",
    "Porto",
    "Riga",
]
_ACTS = [
    "plays the cello",
    "bakes sourdough",
    "repairs bicycles",
    "keeps bees",
    "teaches chemistry",
    "paints murals",
    "runs a bookshop",
    "builds kayaks",
    "restores clocks",
    "grows dahlias",
]
_NICK = {
    "Tomasz": ["Tom", "Tomek"],
    "Alexander": ["Sasha", "Alex"],
    "Katharina": ["Kate", "Kaat"],
    "Wilhelm": ["Wim", "Willy"],
    "Margarethe": ["Grete", "Greta"],
    "Matthias": ["Thijs", "Matt"],
    "Liesbeth": ["Lisbeth", "Liz"],
    "Hannes": ["Hans"],
    "Bram": ["Abraham"],
}


@dataclass(frozen=True)
class Case:
    cls: str  # alias | homonym | unrelated
    kind: str  # the sub-category
    a_name: str
    a_text: str
    b_name: str
    b_text: str


def _ctx(name: str, city: str, act: str, style: int) -> str:
    if style == 0:
        return f"{name} lives in {city} and {act}."
    if style == 1:
        return f"Last spring {name} moved to {city}; {name} {act}."
    return f"{name} {act} near {city}."


def synthetic_cases(seed: int = 7) -> list[Case]:
    """Deterministic cases: ~145 alias, ~200 homonym, ~100 unrelated."""
    rnd = random.Random(seed)
    out: list[Case] = []

    def pick() -> tuple[str, str, str, str]:
        return rnd.choice(_GIVEN), rnd.choice(_SURNAMES), rnd.choice(_CITIES), rnd.choice(_ACTS)

    # --- aliases (the same person) ---
    for _ in range(20):
        g = rnd.choice(list(_NICK))
        s, c, act = rnd.choice(_SURNAMES), rnd.choice(_CITIES), rnd.choice(_ACTS)
        nick = rnd.choice(_NICK[g])
        out.append(
            Case(
                "alias",
                "nickname_full",
                f"{g} {s}",
                _ctx(f"{g} {s}", c, act, 0),
                f"{nick} {s}",
                _ctx(f"{nick} {s}", c, rnd.choice(_ACTS), 1),
            )
        )
    for _ in range(15):
        g = rnd.choice(list(_NICK))
        c = rnd.choice(_CITIES)
        nick = rnd.choice(_NICK[g])
        out.append(
            Case(
                "alias",
                "nickname_bare",
                g,
                _ctx(g, c, rnd.choice(_ACTS), 0),
                nick,
                _ctx(nick, c, rnd.choice(_ACTS), 2),
            )
        )
    for _ in range(15):
        g, s, c, act = pick()
        low = f"{g} {s}".lower()
        out.append(
            Case(
                "alias",
                "case",
                f"{g} {s}",
                _ctx(f"{g} {s}", c, act, 0),
                low,
                _ctx(low, c, rnd.choice(_ACTS), 1),
            )
        )
    for _ in range(15):
        g, s, c, act = pick()
        out.append(
            Case(
                "alias",
                "title",
                f"{g} {s}",
                _ctx(f"{g} {s}", c, act, 0),
                f"Dr. {g} {s}",
                _ctx(f"Dr. {g} {s}", c, rnd.choice(_ACTS), 2),
            )
        )
    for _ in range(15):
        g, s, c, act = pick()
        out.append(
            Case(
                "alias",
                "given_only",
                f"{g} {s}",
                _ctx(f"{g} {s}", c, act, 0),
                g,
                _ctx(g, c, rnd.choice(_ACTS), 1),
            )
        )
    for _ in range(10):
        g, s, c, act = pick()
        typo = s[:-1] + s[-1] * 2
        out.append(
            Case(
                "alias",
                "typo",
                f"{g} {s}",
                _ctx(f"{g} {s}", c, act, 0),
                f"{g} {typo}",
                _ctx(f"{g} {typo}", c, rnd.choice(_ACTS), 1),
            )
        )
    for _ in range(10):
        g, s, c, act = pick()
        out.append(
            Case(
                "alias",
                "no_shared_context",
                f"{g} {s}",
                f"{g} {s} {act}.",
                f"{g} {s}",
                f"{g} {s} was seen at the market.",
            )
        )
    for g, plain in (("Søren", "Soren"), ("Zoë", "Zoe"), ("Ayşe", "Ayse"), ("Łukasz", "Lukasz")):
        s, c, act = rnd.choice(_SURNAMES), rnd.choice(_CITIES), rnd.choice(_ACTS)
        out.append(
            Case(
                "alias",
                "accent",
                f"{g} {s}",
                _ctx(f"{g} {s}", c, act, 0),
                f"{plain} {s}",
                _ctx(f"{plain} {s}", c, rnd.choice(_ACTS), 1),
            )
        )

    for _ in range(15):  # an organisation written with and without its town
        _g, s, c, _a = pick()
        out.append(
            Case(
                "alias",
                "dropped_word",
                f"{s} Bakery {c}",
                f"{s} Bakery {c} sells rye bread.",
                f"{s} Bakery",
                f"{s} Bakery closes on mondays.",
            )
        )
    for _ in range(10):  # surname-first word order
        g, s, c, act = pick()
        out.append(
            Case(
                "alias",
                "word_order",
                f"{g} {s}",
                _ctx(f"{g} {s}", c, act, 0),
                f"{s} {g}",
                _ctx(f"{s} {g}", c, rnd.choice(_ACTS), 1),
            )
        )

    for _ in range(15):  # the family name alone, same town
        g, s, c, act = pick()
        out.append(
            Case(
                "alias",
                "surname_only",
                f"{g} {s}",
                _ctx(f"{g} {s}", c, act, 0),
                s,
                f"{s} was at the harbour in {c}.",
            )
        )

    # --- homonyms (different people) ---
    for _ in range(30):  # same full name, different city
        g, s, c1, act = pick()
        c2 = rnd.choice([c for c in _CITIES if c != c1])
        out.append(
            Case(
                "homonym",
                "same_name_other_city",
                f"{g} {s}",
                _ctx(f"{g} {s}", c1, act, 0),
                f"{g} {s}",
                _ctx(f"{g} {s}", c2, rnd.choice(_ACTS), 1),
            )
        )
    for _ in range(30):  # same given name, different surname
        g, s1, c, act = pick()
        s2 = rnd.choice([s for s in _SURNAMES if s != s1])
        out.append(
            Case(
                "homonym",
                "same_given_other_surname",
                f"{g} {s1}",
                _ctx(f"{g} {s1}", c, act, 0),
                f"{g} {s2}",
                _ctx(f"{g} {s2}", rnd.choice(_CITIES), rnd.choice(_ACTS), 1),
            )
        )
    for _ in range(30):  # bare given name, two cities
        g, _s, c1, act = pick()
        c2 = rnd.choice([c for c in _CITIES if c != c1])
        out.append(
            Case(
                "homonym",
                "bare_name_two_cities",
                g,
                _ctx(g, c1, act, 0),
                g,
                _ctx(g, c2, rnd.choice(_ACTS), 2),
            )
        )
    for _ in range(25):  # bare given name vs a full name with another surname-city context
        g, s, c1, act = pick()
        c2 = rnd.choice([c for c in _CITIES if c != c1])
        out.append(
            Case(
                "homonym",
                "bare_vs_full_other_city",
                g,
                _ctx(g, c1, act, 0),
                f"{g} {s}",
                _ctx(f"{g} {s}", c2, rnd.choice(_ACTS), 1),
            )
        )
    for _ in range(15):  # same name, same sentence shape, no discriminating capitalised word
        g, s, _c, act = pick()
        act2 = rnd.choice([a for a in _ACTS if a != act])
        out.append(
            Case(
                "homonym",
                "same_name_no_signal",
                f"{g} {s}",
                f"{g} {s} {act}.",
                f"{g} {s}",
                f"{g} {s} {act2}.",
            )
        )
    for _ in range(10):  # generational suffix
        g, s, c, act = pick()
        out.append(
            Case(
                "homonym",
                "generation",
                f"{g} {s} Sr",
                _ctx(f"{g} {s} Sr", c, act, 0),
                f"{g} {s} Jr",
                _ctx(f"{g} {s} Jr", c, rnd.choice(_ACTS), 1),
            )
        )

    for _ in range(20):  # two branches of one name, told apart only by the town in the name
        _g, s, c1, _a = pick()
        c2 = rnd.choice([c for c in _CITIES if c != c1])
        out.append(
            Case(
                "homonym",
                "branch_town_in_name",
                f"{s} Bakery {c1}",
                f"{s} Bakery {c1} sells rye bread.",
                f"{s} Bakery {c2}",
                f"{s} Bakery {c2} closes on mondays.",
            )
        )
    for _ in range(10):  # two different bakeries sharing a name, no distinguishing word at all
        _g, s, _c, _a = pick()
        out.append(
            Case(
                "homonym",
                "same_org_no_signal",
                f"{s} Bakery",
                f"{s} Bakery sells rye bread.",
                f"{s} Bakery",
                f"{s} Bakery closes on mondays.",
            )
        )

    for _ in range(15):  # the family name alone, nothing to tell two families apart
        g, s, _c, act = pick()
        out.append(
            Case(
                "homonym",
                "surname_only_no_signal",
                f"{g} {s}",
                f"{g} {s} {act}.",
                s,
                f"{s} was at the harbour.",
            )
        )
    for _ in range(15):  # the family name alone, another town
        g, s, c1, act = pick()
        c2 = rnd.choice([c for c in _CITIES if c != c1])
        out.append(
            Case(
                "homonym",
                "surname_only_other_town",
                f"{g} {s}",
                _ctx(f"{g} {s}", c1, act, 0),
                s,
                f"{s} was at the harbour in {c2}.",
            )
        )

    # --- unrelated ---
    for _ in range(60):
        g1, s1, c1, a1 = pick()
        g2, s2, c2, a2 = pick()
        if (g1, s1) == (g2, s2) or g1 == g2 or s1 == s2:
            continue
        out.append(
            Case(
                "unrelated",
                "different_names",
                f"{g1} {s1}",
                _ctx(f"{g1} {s1}", c1, a1, 0),
                f"{g2} {s2}",
                _ctx(f"{g2} {s2}", c2, a2, 1),
            )
        )
    for _ in range(40):
        g1, _s1, c1, a1 = pick()
        g2, _s2, c2, a2 = pick()
        if g1 == g2:
            continue
        out.append(
            Case("unrelated", "different_bare", g1, _ctx(g1, c1, a1, 0), g2, _ctx(g2, c2, a2, 2))
        )
    return out


@dataclass
class Confusion:
    """Counts of ``(truth class, outcome)`` plus the rates the gate states."""

    counts: Counter[tuple[str, str]]

    def n(self, cls: str) -> int:
        return sum(self.counts[(cls, o)] for o in OUTCOMES)

    def rate(self, cls: str, outcome: str) -> float:
        total = self.n(cls)
        return self.counts[(cls, outcome)] / total if total else 0.0

    @property
    def homonym_merge_rate(self) -> float:
        return self.rate("homonym", "merged")

    @property
    def unrelated_merge_rate(self) -> float:
        return self.rate("unrelated", "merged")

    @property
    def alias_recall(self) -> float:
        return self.rate("alias", "merged")

    def table(self) -> str:
        head = f"{'truth':<10}" + "".join(f"{o:>9}" for o in OUTCOMES) + f"{'n':>6}"
        rows = [
            f"{c:<10}" + "".join(f"{self.counts[(c, o)]:>9}" for o in OUTCOMES) + f"{self.n(c):>6}"
            for c in CLASSES
        ]
        return "\n".join([head, *rows])


_SCOPE = OwnerScope(tenant="calib", owner="noor-owner")
_KEY = (_SCOPE.tenant, _SCOPE.owner)


async def _outcome(
    case: Case, high: float, grey: float, embed: Embed | None, max_cache: int
) -> str:
    stage = EntityResolveStage(
        ResolveSettings(high=high, grey=grey, max_cache=max_cache, scoped=True), embed=embed
    )
    first = await stage.resolve_surface(case.a_name, scope=_SCOPE, key=_KEY, text=case.a_text)
    second = await stage.resolve_surface(case.b_name, scope=_SCOPE, key=_KEY, text=case.b_text)
    assert first["verdict"] == "new"
    return {"existing": "merged", "pending_merge": "pending", "new": "new"}[second["verdict"]]


async def evaluate(
    cases: list[Case],
    high: float,
    grey: float,
    embed: Embed | None,
    *,
    half: int | None = None,
    max_cache: int = 1024,
) -> Confusion:
    """Confusion matrix of the ladder on ``cases`` (``half`` 0 = even index = dev, 1 = odd = test)."""
    counts: Counter[tuple[str, str]] = Counter()
    for i, case in enumerate(cases):
        if half is not None and i % 2 != half:
            continue
        counts[(case.cls, await _outcome(case, high, grey, embed, max_cache))] += 1
    return Confusion(counts)


async def calibrate(
    cases: list[Case], embed: Embed | None, *, max_rate: float = 0.01
) -> tuple[float, float, Confusion]:
    """Pick ``(high, grey)`` on the dev half: homonym and unrelated merge rates <= ``max_rate``, then the
    best alias recall, then alias recall plus pending coverage, then the highest ``high`` (conservative),
    then the lowest unrelated-pending rate (the lowest ``grey`` wins remaining ties)."""
    best: tuple[tuple[float, float, float, float], float, float, Confusion] | None = None
    for high in [round(0.5 + 0.02 * i, 2) for i in range(0, 25)]:  # 0.50 .. 0.98
        for grey in [round(0.5 + 0.05 * i, 2) for i in range(0, 10)]:  # 0.50 .. 0.95
            if grey > high:
                continue
            conf = await evaluate(cases, high, grey, embed, half=0)
            if conf.homonym_merge_rate > max_rate or conf.unrelated_merge_rate > max_rate:
                continue
            covered = conf.alias_recall + conf.rate("alias", "pending")
            score = (conf.alias_recall, covered, high, -conf.rate("unrelated", "pending"))
            if best is None or score > best[0]:
                best = (score, high, grey, conf)
    if best is None:
        raise ValueError("no threshold pair satisfies the homonym-merge rate on the dev half")
    return best[1], best[2], best[3]
