# ruff: noqa: E501
"""W2-15 gate: coverage check and targeted re-extraction on a synthetic, labelled, UNSEEN-domain set
(CPU only, stub LLM, no GPU).

    python evals/coverage_gate.py

Four domains used nowhere else in the repo (beekeeping, sailing, choir, falconry), invented names, three
windows each. Every window has labelled facts (line, text, cited span). The stub LLM plays the model:

* first call: returns every labelled fact EXCEPT the window's ``omit`` lines (an incomplete extraction);
* targeted call (the coverage stage's, recognised by its NEW block): returns the omitted facts among the
  lines it was asked about, plus the window's hallucinated extra when it names one of those lines.

Two arms run on the same first-call output: **baseline** (grounding only) and **coverage** (grounding +
the order-250 stage). Reported separately, as the gate asks:

* ``recall_gain``: omitted facts recovered (labelled facts kept after the stage minus before);
* ``unsupported_added``: hallucinated extras that survived into the kept set (and ``unsupported_dropped``,
  the ones the grounding re-check stopped);
* ``calls_per_window``: 1 normal call + the extra calls, and ``false_triggers`` (windows whose first call
  was complete but still got an extra call).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from memspine.config import constants
from memspine.derive.compile import WindowLine, compile_items
from memspine.derive.coverage_stage import make_coverage_stage
from memspine.derive.envelope_compiler import EnvelopeCompiler
from memspine.derive.envelope_stages import EnvelopeStageContext, run_envelope_stages
from memspine.derive.grounding_stage import make_grounding_stage
from memspine.schemas.durable import OwnerScope

NOW = datetime(2024, 3, 9, 12, 0, tzinfo=UTC)
SCOPE = OwnerScope(tenant="default", owner="noor")


@dataclass(frozen=True)
class Line:
    speaker: str
    text: str
    role: str = "user"
    #: the labelled fact on this line: (assertion text, span in the line); None = filler / echo
    fact: tuple[str, str] | None = None
    echo: bool = False
    mode: str = "asserted"


@dataclass(frozen=True)
class Window:
    domain: str
    lines: tuple[Line, ...]
    omit: tuple[int, ...] = ()  # 1-based line numbers the FIRST call misses
    #: (line, assertion text, kind) the targeted call adds besides the truth: kind number|name|flip|subtle
    extra: tuple[int, str, str] | None = None


def _l(
    speaker: str, text: str, fact: str | None = None, span: str | None = None, **kw: Any
) -> Line:
    return Line(speaker, text, fact=(fact, span or text) if fact else None, **kw)


WINDOWS: tuple[Window, ...] = (
    # --- beekeeping ------------------------------------------------------------------------------
    Window(
        "beekeeping",
        (
            _l(
                "Ilse",
                "I keep 6 hives behind the Kestrel Lane allotment.",
                "Ilse keeps 6 hives behind the Kestrel Lane allotment",
                "keep 6 hives behind the Kestrel Lane allotment",
            ),
            _l(
                "Bram",
                "I lost two colonies to wasps in August.",
                "Bram lost two colonies to wasps in August",
                "lost two colonies to wasps in August",
            ),
            _l("Ilse", "haha yes"),
            _l(
                "Bram",
                "I plan to buy a Langstroth extractor from Tomasz Hive Supplies next month.",
                "Bram plans to buy a Langstroth extractor from Tomasz Hive Supplies next month",
                "plan to buy a Langstroth extractor from Tomasz Hive Supplies",
                mode="planned",
            ),
            _l("Ilse", "ok, thanks"),
        ),
        omit=(2, 4),
        extra=(2, "Bram lost 9 colonies to wasps in August", "number"),
    ),
    Window(
        "beekeeping",
        (
            _l(
                "Noor",
                "My queen Marigold was marked blue at the Tuesday inspection.",
                "Noor's queen Marigold was marked blue at the Tuesday inspection",
                "queen Marigold was marked blue at the Tuesday inspection",
            ),
            _l(
                "Assistant",
                "Noted: your queen Marigold was marked blue at the Tuesday inspection.",
                role="assistant",
                echo=True,
            ),
            _l(
                "Noor",
                "I harvested 18 kilos of heather honey on the Grindle ridge.",
                "Noor harvested 18 kilos of heather honey on the Grindle ridge",
                "harvested 18 kilos of heather honey on the Grindle ridge",
            ),
            _l("Noor", "lol"),
        ),
        omit=(3,),
        extra=(
            3,
            "Noor harvested 18 kilos of heather honey with Ilse on the Grindle ridge",
            "name",
        ),
    ),
    Window(
        "beekeeping",
        (
            _l(
                "Ilse",
                "I do not treat my hives with oxalic acid in winter.",
                "Ilse does not treat her hives with oxalic acid in winter",
                "do not treat my hives with oxalic acid in winter",
            ),
            _l(
                "Bram",
                "I teach the Thursday beginners class at Wren Meadow.",
                "Bram teaches the Thursday beginners class at Wren Meadow",
                "teach the Thursday beginners class at Wren Meadow",
            ),
            _l("Ilse", "great"),
        ),
        omit=(),  # complete first call: must not trigger
    ),
    # --- sailing ---------------------------------------------------------------------------------
    Window(
        "sailing",
        (
            _l(
                "Tomasz",
                "I sail a 27 foot sloop called Curlew out of Harrow Quay.",
                "Tomasz sails a 27 foot sloop called Curlew out of Harrow Quay",
                "sail a 27 foot sloop called Curlew out of Harrow Quay",
            ),
            _l(
                "Noor",
                "I crewed in the Saltmarsh Regatta in June.",
                "Noor crewed in the Saltmarsh Regatta in June",
                "crewed in the Saltmarsh Regatta in June",
            ),
            _l(
                "Tomasz",
                "We reefed at 22 knots off Gannet Point.",
                "Tomasz reefed at 22 knots off Gannet Point",
                "reefed at 22 knots off Gannet Point",
            ),
            _l("Noor", "haha"),
            _l(
                "Tomasz",
                "I want to cross to Dunmere in September.",
                "Tomasz wants to cross to Dunmere in September",
                "want to cross to Dunmere in September",
                mode="planned",
            ),
        ),
        omit=(2, 3),
        extra=(3, "Tomasz reefed at 25 knots off Gannet Point", "number"),
    ),
    Window(
        "sailing",
        (
            _l(
                "Bram",
                "I never sail without a handheld VHF radio.",
                "Bram never sails without a handheld VHF radio",
                "never sail without a handheld VHF radio",
            ),
            _l(
                "Ilse",
                "I booked a winter berth at Cobb Marina for 400 pounds.",
                "Ilse booked a winter berth at Cobb Marina for 400 pounds",
                "booked a winter berth at Cobb Marina for 400 pounds",
            ),
            _l("Bram", "thanks!"),
        ),
        omit=(2,),
        extra=(
            2,
            "Ilse booked a winter berth at Cobb Marina for 400 pounds and paid in cash",
            "subtle",
        ),
    ),
    Window(
        "sailing",
        (
            _l(
                "Noor",
                "I passed my Day Skipper exam at Fenwick Academy.",
                "Noor passed her Day Skipper exam at Fenwick Academy",
                "passed my Day Skipper exam at Fenwick Academy",
            ),
            _l(
                "Tomasz",
                "I fixed the Curlew's bilge pump on Sunday.",
                "Tomasz fixed the Curlew's bilge pump on Sunday",
                "fixed the Curlew's bilge pump on Sunday",
            ),
            _l("Noor", "cool"),
        ),
        omit=(),
    ),
    # --- choir -----------------------------------------------------------------------------------
    Window(
        "choir",
        (
            _l(
                "Ilse",
                "I sing second alto in the Ashgrove Chamber Choir.",
                "Ilse sings second alto in the Ashgrove Chamber Choir",
                "sing second alto in the Ashgrove Chamber Choir",
            ),
            _l(
                "Bram",
                "We rehearse every Wednesday at 19:30 in St Aldric's hall.",
                "The choir rehearses every Wednesday at 19:30 in St Aldric's hall",
                "rehearse every Wednesday at 19:30 in St Aldric's hall",
            ),
            _l(
                "Noor",
                "I conduct the Easter concert on 30 March.",
                "Noor conducts the Easter concert on 30 March",
                "conduct the Easter concert on 30 March",
            ),
            _l("Ilse", "hehe"),
        ),
        omit=(2, 3),
        extra=(3, "Noor does not conduct the Easter concert on 30 March", "flip"),
    ),
    Window(
        "choir",
        (
            _l(
                "Tomasz",
                "I am learning the Brindle Mass for the autumn programme.",
                "Tomasz is learning the Brindle Mass for the autumn programme",
                "learning the Brindle Mass for the autumn programme",
            ),
            _l(
                "Assistant",
                "You are learning the Brindle Mass for the autumn programme, got it.",
                role="assistant",
                echo=True,
            ),
            _l(
                "Bram",
                "I lost my voice after the Marlow festival in May.",
                "Bram lost his voice after the Marlow festival in May",
                "lost my voice after the Marlow festival in May",
            ),
            _l("Tomasz", "ok"),
        ),
        omit=(3,),
        extra=(
            3,
            "Bram lost his voice after the Marlow festival in May and quit the choir",
            "subtle",
        ),
    ),
    Window(
        "choir",
        (
            _l(
                "Noor",
                "I prefer to stand beside the tenors at Ashgrove.",
                "Noor prefers to stand beside the tenors at Ashgrove",
                "prefer to stand beside the tenors at Ashgrove",
            ),
            _l(
                "Ilse",
                "I joined the choir in 2019.",
                "Ilse joined the choir in 2019",
                "joined the choir in 2019",
            ),
        ),
        omit=(),
    ),
    # --- falconry --------------------------------------------------------------------------------
    Window(
        "falconry",
        (
            _l(
                "Bram",
                "I fly a Harris hawk named Juniper at Thornby Down.",
                "Bram flies a Harris hawk named Juniper at Thornby Down",
                "fly a Harris hawk named Juniper at Thornby Down",
            ),
            _l(
                "Ilse",
                "Juniper weighs 850 grams in flying condition.",
                "Juniper weighs 850 grams in flying condition",
                "Juniper weighs 850 grams in flying condition",
            ),
            _l(
                "Noor",
                "I trained with Tomasz at the Wexcombe Hawking Centre.",
                "Noor trained with Tomasz at the Wexcombe Hawking Centre",
                "trained with Tomasz at the Wexcombe Hawking Centre",
            ),
            _l("Bram", "haha nice"),
        ),
        omit=(1, 2),
        extra=(1, "Bram flies a peregrine named Juniper at Thornby Down", "name"),
    ),
    Window(
        "falconry",
        (
            _l(
                "Ilse",
                "I am applying for a falconry licence in Orsley.",
                "Ilse is applying for a falconry licence in Orsley",
                "applying for a falconry licence in Orsley",
            ),
            _l(
                "Tomasz",
                "My mews has 3 perches and a weathering lawn.",
                "Tomasz's mews has 3 perches and a weathering lawn",
                "mews has 3 perches and a weathering lawn",
            ),
            _l("Ilse", "thanks"),
        ),
        omit=(2,),
    ),
    Window(
        "falconry",
        (
            _l(
                "Noor",
                "I do not hunt rabbits with Juniper before October.",
                "Noor does not hunt rabbits with Juniper before October",
                "do not hunt rabbits with Juniper before October",
            ),
            _l(
                "Bram",
                "I ring every fledgling at Thornby Down in July.",
                "Bram rings every fledgling at Thornby Down in July",
                "ring every fledgling at Thornby Down in July",
            ),
            _l("Noor", "yes"),
        ),
        omit=(),
    ),
)

_NEW = re.compile(r"NEW:\n(.*?)\n\nCONTEXT:", re.S)
_ROW = re.compile(r"^\[(\d+)\]", re.M)


def _item(n: int, speaker: str, text: str, span: str, mode: str = "asserted") -> dict[str, Any]:
    return {
        "s": speaker,
        "r": "states",
        "o": None,
        "text": text,
        "kind": "event",
        "mode": mode,
        "neg": False,
        "lines": [n],
        "span": span,
        "speaker": speaker,
    }


class StubWindowLLM:
    """The stub model for one window; records every call it gets."""

    provider_id = "fake:coverage"

    def __init__(self, w: Window) -> None:
        self.w = w
        self.calls = 0

    async def chat(self, messages: Sequence[dict[str, str]], **_: Any) -> str:
        self.calls += 1
        m = _NEW.search(messages[-1]["content"])
        asked = {int(x) for x in _ROW.findall(m.group(1))} if m else set()
        out: list[dict[str, Any]] = []
        for n, ln in enumerate(self.w.lines, 1):
            if ln.fact is None or n not in asked or n not in self.w.omit:
                continue
            out.append(_item(n, ln.speaker, *ln.fact, ln.mode))
        if self.w.extra and self.w.extra[0] in asked:
            n, text, _kind = self.w.extra
            ln = self.w.lines[n - 1]
            out.append(_item(n, ln.speaker, text, ln.fact[1] if ln.fact else ln.text))
        return json.dumps({"facts": out})


def first_call(w: Window) -> list[dict[str, Any]]:
    return [
        _item(n, ln.speaker, *ln.fact, ln.mode)
        for n, ln in enumerate(w.lines, 1)
        if ln.fact is not None and n not in w.omit
    ]


def build(w: Window) -> tuple[dict[int, WindowLine], list[Any]]:
    lines = {
        n: WindowLine(
            turn_id=f"t{n}",
            source_id=f"t{n}",
            source_version=1,
            seg_index=n,
            role=ln.role,
            speaker=ln.speaker,
            source_at=NOW,
            tz="UTC",
            content=f"{ln.speaker}: {ln.text}",
        )
        for n, ln in enumerate(w.lines, 1)
    }
    records = [SimpleNamespace(tags=[constants.ECHO_TAG] if ln.echo else []) for ln in w.lines]
    return lines, records


@dataclass
class Arm:
    facts_total: int = 0
    facts_kept: int = 0
    unsupported_kept: int = 0
    unsupported_dropped: int = 0
    other_kept: int = 0
    calls: int = 0
    windows: int = 0
    false_triggers: int = 0
    notes: Counter[str] = field(default_factory=Counter)


async def run(threshold: float = 0.8) -> dict[str, Any]:
    base, cov = Arm(), Arm()
    for w in WINDOWS:
        lines, records = build(w)
        compiler = EnvelopeCompiler(clock=lambda: NOW)
        ctx = compiler.context(SCOPE, "w", lines, source_time=NOW)
        first = list(compile_items(first_call(w), ctx).envelopes)
        truth = {ln.fact[0] for ln in w.lines if ln.fact}
        extra = w.extra[1] if w.extra else None
        for arm, with_cov in ((base, False), (cov, True)):
            llm = StubWindowLLM(w)
            grounding = make_grounding_stage()
            stages = [grounding]
            stage_obj = None
            if with_cov:
                stage_obj = make_coverage_stage(llm, earlier=[grounding], threshold=threshold)
                stages.append(stage_obj.stage())
            sctx = EnvelopeStageContext(
                namespace="a", window_id="w", lines=lines, records=records, compile_ctx=ctx
            )
            out = await run_envelope_stages(stages, list(first), sctx)
            texts = [e.text for e in out]
            arm.windows += 1
            arm.facts_total += len(truth)
            arm.facts_kept += len(truth & set(texts))
            arm.unsupported_kept += int(extra is not None and extra in texts)
            arm.other_kept += sum(1 for t in texts if t not in truth and t != extra)
            arm.calls += 1 + llm.calls
            arm.notes.update(sctx.notes)
            if with_cov:
                assert stage_obj is not None
                stats = stage_obj.stats.as_dict()
                arm.unsupported_dropped += int(stats["added_unsupported_dropped"])
                if not w.omit and llm.calls:
                    arm.false_triggers += 1
    return {
        "windows": len(WINDOWS),
        "domains": sorted({w.domain for w in WINDOWS}),
        "facts": base.facts_total,
        "recall_before": base.facts_kept / base.facts_total,
        "recall_after": cov.facts_kept / cov.facts_total,
        "recall_gain": (cov.facts_kept - base.facts_kept) / base.facts_total,
        "omitted_facts": base.facts_total - base.facts_kept,
        "omitted_recovered": cov.facts_kept - base.facts_kept,
        "hallucinated_extras_offered": sum(1 for w in WINDOWS if w.extra),
        "unsupported_added": cov.unsupported_kept,
        "unsupported_stopped": sum(1 for w in WINDOWS if w.extra) - cov.unsupported_kept,
        # grounded-only rule: also stops true facts the CPU grounding leaves "uncertain"
        "dropped_total": cov.unsupported_dropped,
        "true_facts_dropped": cov.unsupported_dropped
        - (sum(1 for w in WINDOWS if w.extra) - cov.unsupported_kept),
        "other_kept": cov.other_kept,
        "calls_per_window_baseline": base.calls / base.windows,
        "calls_per_window_coverage": cov.calls / cov.windows,
        "extra_calls": cov.calls - base.calls,
        "false_triggers": cov.false_triggers,
        "notes": dict(cov.notes),
    }


def main() -> None:
    print(json.dumps(asyncio.run(run()), indent=2, default=str))


if __name__ == "__main__":
    main()
