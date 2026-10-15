"""W2-08 (W-17, W-08): grounding stage over a window's assertion envelopes (``write.grounding_v2``).

An envelope stage at order 200 (see ``derive/envelope_stages.py``). For each compiled assertion:

1. **Spans.** Locate the supporting span in the cited lines (LIB-03 ``check_grounding``) and write the
   character offsets onto the envelope's evidence (a support item without a span gets one; an existing
   span is kept).
2. **Rule checks** (no model): the cited turns exist; numbers, units, dates and names appear in the
   span; polarity and modality are preserved; the speaker matches the asserter (W2-02
   ``AsserterSubjectCheck``).
3. **Adjudication** (``Adjudicator``), only for what the rules could not decide (label ``undecided``):
   the decider (``grounded`` task, local entailment, CPU) when configured, otherwise a lexical-overlap
   fallback. A decider that is unavailable or fails falls back to lexical overlap.
4. **Assent** (W2-02 ``AssentChecker``): an ``assistant_proposed`` assertion is promoted to
   ``user_confirmed`` only when the user line right after the proposal is a real, binding yes. "ok,
   thanks" is not.
5. **Action.** ``supported`` is kept (``grounding="grounded"``). ``unsupported`` and ``partly`` are
   dropped or marked ``tentative`` (``write.ground_action``), with a reason code counted in
   ``ctx.notes``. A still-``undecided`` assertion is kept with ``grounding="uncertain"``.

Envelope fields read: ``text``, ``subject``, ``object``, ``value``, ``speaker``, ``src_class``,
``assessment.modality``, ``evidence`` (turn refs, spans). ``ctx.lines`` (alias -> ``WindowLine``).
Envelope fields written: ``grounding``, ``checks``, ``status`` (``tentative`` when so configured),
``src_class`` / ``evidence`` (assent promotion: a binding evidence on the user line), ``evidence[].span``
(offsets in the line's content). Reason codes go to ``ctx.notes`` as ``ground.<outcome>.<code>``
(extras stay model-only, so nothing is written there).

Reads nothing from the other stages; stages after it may rely on ``grounding`` being set.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Protocol

from memspine.derive.asserter import AsserterSubjectCheck, CitedLine
from memspine.derive.envelope_stages import EnvelopeStage, EnvelopeStageContext
from memspine.derive.ground import (
    CitedSpan,
    Claim,
    GroundingLabel,
    GroundingResult,
    WindowTurn,
    check_grounding,
    claim_from_envelope,
    normalise,
    turn_key,
)
from memspine.ingest.provenance import AssentChecker, AssentVerdict
from memspine.observability import tap
from memspine.schemas.durable.assertion import AssertionEnvelope
from memspine.schemas.durable.evidence import Evidence, Span, TurnRef

__all__ = [
    "GROUND_STAGE_ORDER",
    "AdjudicationVerdict",
    "Adjudicator",
    "GroundingChecker",
    "GroundingVerdict",
    "build_grounding_stage",
    "lexical_support",
    "make_grounding_stage",
]

GROUND_STAGE_ORDER = 200
_PREFIX = re.compile(r"^(\S[^:\n]{0,40}):\s+")
_WORD = re.compile(r"\w+", re.UNICODE)
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "with",
        "by",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "he",
        "she",
        "they",
        "we",
        "i",
        "you",
        "his",
        "her",
        "their",
        "our",
        "my",
        "your",
        "do",
        "does",
        "did",
        "not",
        "no",
    ]
)


# --------------------------------------------------------------------------------------------------
# Adjudicator
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AdjudicationVerdict:
    label: GroundingLabel  # supported | unsupported | undecided
    source: str  # decider | lexical
    score: float | None = None


class _DeciderLike(Protocol):
    async def decide(self, task: str, question: str, context: str | None = None) -> Any: ...


def _content_tokens(text: str) -> list[str]:
    return [t for t in _WORD.findall(normalise(text)) if t not in _STOP and len(t) > 1]


def _english_like(text: str) -> bool:
    """Enough English function words to trust a word-overlap *rejection* (cross-language text
    cannot be rejected by overlap: it shares no words with an English claim)."""
    toks = _WORD.findall(normalise(text))
    return bool(toks) and sum(1 for t in toks if t in _STOP) / len(toks) >= 0.15


def lexical_support(claim: str, evidence: str) -> float:
    """Share of the claim's content words (or word stems) found in the evidence, 0..1."""
    want = _content_tokens(claim)
    if not want:
        return 0.0
    have = set(_content_tokens(evidence))
    stems = {t[:5] for t in have}
    hit = sum(1 for t in want if t in have or t[:5] in stems)
    return hit / len(want)


class Adjudicator:
    """Settles what the rules left ``undecided`` (W-17): local entailment through the decider, else
    lexical overlap. CPU only. The result is ``supported`` / ``unsupported`` / ``undecided``.

    ``decider`` is any object with ``async decide(task, question, context)`` whose ``grounded`` task
    answers the label ``grounded`` or ``ungrounded`` (the OpenDecider adapter). Without one, or when it
    raises (no model, unknown task), the lexical fallback decides: coverage at or above ``accept`` is
    ``supported``, at or below ``reject`` is ``unsupported``, in between stays ``undecided``.
    """

    def __init__(
        self,
        decider: _DeciderLike | None = None,
        *,
        accept: float = 0.85,
        reject: float = 0.34,
        min_confidence: float = 0.6,
    ) -> None:
        self.decider = decider
        self.accept = accept
        self.reject = reject
        self.min_confidence = min_confidence
        self.decider_failures = 0

    async def adjudicate(self, claim: str, evidence: str) -> AdjudicationVerdict:
        if self.decider is not None:
            try:
                d = await self.decider.decide("grounded", claim, evidence)
            except Exception:  # unknown task, no model cached, runtime error: fall back to lexical
                self.decider_failures += 1
            else:
                conf = d.confidence
                if conf is None or conf >= self.min_confidence:
                    if d.label == "grounded":
                        return AdjudicationVerdict("supported", "decider", conf)
                    if d.label == "ungrounded":
                        return AdjudicationVerdict("unsupported", "decider", conf)
        return self.lexical(claim, evidence)

    def lexical(self, claim: str, evidence: str) -> AdjudicationVerdict:
        s = lexical_support(claim, evidence)
        if s >= self.accept:
            return AdjudicationVerdict("supported", "lexical", s)
        if s <= self.reject and _english_like(evidence):
            return AdjudicationVerdict("unsupported", "lexical", s)
        return AdjudicationVerdict("undecided", "lexical", s)


# --------------------------------------------------------------------------------------------------
# Checker
# --------------------------------------------------------------------------------------------------


@dataclass
class GroundingVerdict:
    label: GroundingLabel  # final: supported | partly | unsupported | undecided
    rules: GroundingResult
    reasons: tuple[str, ...] = ()
    adjudicated: AdjudicationVerdict | None = None
    #: found spans as ``(turn key, start, end, text)`` in the coordinates of the turn text passed in
    spans: tuple[tuple[str, int, int, str], ...] = ()


def _strip_speaker(content: str, speaker: str | None) -> tuple[str, int]:
    """The line text without a leading ``Name: `` tag, and the offset that was removed."""
    m = _PREFIX.match(content)
    if m and (
        speaker is None or normalise(m.group(1)) == normalise(speaker) or len(m.group(1)) <= 40
    ):
        return content[m.end() :], m.end()
    return content, 0


def _shown_speaker(line: Any) -> str | None:
    """The name the model saw on the line: the ``Name:`` tag of the content when there is one (the
    transcript builder keeps a name the turn already carried), else the line's own speaker."""
    m = _PREFIX.match(line.content or "")
    return m.group(1) if m else line.speaker


class GroundingChecker:
    """Spans per assertion and endpoint plus rule checks (LIB-03, W2-02 asserter), then adjudication."""

    def __init__(
        self,
        *,
        adjudicator: Adjudicator | None = None,
        asserter_check: AsserterSubjectCheck | None = None,
    ) -> None:
        self.adjudicator = adjudicator
        self.asserter_check = asserter_check or AsserterSubjectCheck()

    def window(self, lines: Mapping[int, Any]) -> tuple[dict[str, WindowTurn], dict[str, int]]:
        """``ctx.lines`` -> LIB-03 window (speaker tag stripped) and the stripped offset per turn key."""
        out: dict[str, WindowTurn] = {}
        offsets: dict[str, int] = {}
        for line in lines.values():
            key = turn_key(
                TurnRef(
                    source_id=line.source_id,
                    source_version=line.source_version,
                    seg_index=line.seg_index,
                )
            )
            text, off = _strip_speaker(line.content or "", line.speaker)
            out[key] = WindowTurn(key=key, text=text, speaker=_shown_speaker(line))
            offsets[key] = off
        return out, offsets

    async def check_claim(
        self,
        claim: Claim,
        window: Mapping[str, WindowTurn],
        *,
        asserter: str | None = None,
        subjects: Sequence[str] = (),
    ) -> GroundingVerdict:
        rules = check_grounding(claim, window)
        reasons = list(rules.failing)
        label: GroundingLabel = rules.label
        # the speaker must match the asserter (W2-02): a hard flag is a wrong-speaker failure
        support = [c.turn for c in claim.cites if c.role == "support" and c.turn in window]
        if asserter is not None and support:
            verdict = self.asserter_check.check(
                asserter,
                list(subjects),
                [
                    CitedLine(speaker=window[k].speaker, text=window[k].text)
                    for k in dict.fromkeys(support)
                ],
            )
            if not verdict.ok:
                reasons.extend(f"asserter:{f}" for f in verdict.flags if f != "attributed")
                label = "unsupported"
        adjudicated: AdjudicationVerdict | None = None
        if label == "undecided" and self.adjudicator is not None:
            cited = [
                c.turn for c in claim.cites if c.role in ("support", "binding") and c.turn in window
            ]
            evidence = " ".join(window[k].text for k in dict.fromkeys(cited))
            adjudicated = await self.adjudicator.adjudicate(claim.text, evidence)
            label = adjudicated.label
            if label == "unsupported":
                reasons.append(f"adjudicator:{adjudicated.source}")
        if label == "undecided":
            reasons.extend(f"undecided:{u}" for u in rules.undecided)
        spans = tuple((s.turn, s.start, s.end, s.text) for s in rules.spans)
        return GroundingVerdict(label, rules, tuple(dict.fromkeys(reasons)), adjudicated, spans)


# --------------------------------------------------------------------------------------------------
# The stage
# --------------------------------------------------------------------------------------------------


def _code(reason: str) -> str:
    return re.sub(r"[^a-z0-9_.]+", "_", reason.casefold()).strip("_")[:40] or "unknown"


@dataclass
class _Stage:
    checker: GroundingChecker
    action: Literal["drop", "tentative"] = "drop"
    assent: AssentChecker = field(default_factory=AssentChecker)
    check_assent: bool = True
    confirmed_trust: float = 0.8

    async def run(
        self, envelopes: list[AssertionEnvelope], ctx: EnvelopeStageContext
    ) -> list[AssertionEnvelope]:
        window, offsets = self.checker.window(ctx.lines)
        by_key = {
            turn_key(
                TurnRef(
                    source_id=ln.source_id, source_version=ln.source_version, seg_index=ln.seg_index
                )
            ): (n, ln)
            for n, ln in ctx.lines.items()
        }
        source_time = _source_date(ctx)
        kept: list[AssertionEnvelope] = []
        for env in envelopes:
            claim = _shift_spans(claim_from_envelope(env, source_time=source_time), offsets)
            subjects = [env.subject.surface] if env.subject is not None else []
            v = await self.checker.check_claim(
                claim, window, asserter=_asserter(env, claim, window, ctx.lines), subjects=subjects
            )
            first = _code(v.reasons[0]) if v.reasons else "none"
            tap.event(
                "write.ground.checked",
                window=ctx.window_id,
                write_id=env.write_id,
                label=v.label,
                rules=v.rules.label,
                reasons=list(v.reasons),
                adjudicated=v.adjudicated.source if v.adjudicated else None,
            )
            if v.adjudicated is not None:
                ctx.notes[f"ground.adjudicated.{v.adjudicated.source}"] += 1
            if v.label in ("unsupported", "partly"):
                if self.action == "drop":
                    ctx.notes[f"ground.dropped.{first}"] += 1
                    ctx.notes["ground.dropped"] += 1
                    tap.event(
                        "write.ground.dropped",
                        window=ctx.window_id,
                        write_id=env.write_id,
                        label=v.label,
                        reasons=list(v.reasons),
                    )
                    continue
                ctx.notes[f"ground.tentative.{first}"] += 1
                ctx.notes["ground.tentative"] += 1
                env = self._mark(env, v, status="tentative", grounding="rejected")
            elif v.label == "undecided":
                ctx.notes["ground.uncertain"] += 1
                env = self._mark(env, v, grounding="uncertain")
            else:
                ctx.notes["ground.grounded"] += 1
                env = self._mark(env, v, grounding="grounded")
            env = self._put_spans(env, v, by_key, offsets)
            if self.check_assent:
                env = self._assent(env, ctx, by_key, ctx.notes)
            kept.append(env)
        tap.event(
            "write.ground.summary",
            window=ctx.window_id,
            before=len(envelopes),
            after=len(kept),
        )
        return kept

    # -- helpers -----------------------------------------------------------------------------------

    @staticmethod
    def _mark(
        env: AssertionEnvelope,
        v: GroundingVerdict,
        *,
        grounding: str,
        status: str | None = None,
    ) -> AssertionEnvelope:
        upd: dict[str, Any] = {"grounding": grounding, "checks": v.rules.to_checks()}
        if status is not None:
            upd["status"] = status
        return env.model_copy(update=upd)

    @staticmethod
    def _put_spans(
        env: AssertionEnvelope,
        v: GroundingVerdict,
        by_key: Mapping[str, tuple[int, Any]],
        offsets: Mapping[str, int],
    ) -> AssertionEnvelope:
        if not v.spans:
            return env
        found = {k: (s, e, t) for k, s, e, t in v.spans}
        new: list[Evidence] = []
        changed = False
        for ev in env.evidence:
            if ev.role == "support" and ev.span is None and ev.turn is not None:
                k = turn_key(ev.turn)
                if k in found:
                    s, e, t = found[k]
                    off = offsets.get(k, 0)
                    ev = ev.model_copy(update={"span": Span(start=s + off, end=e + off, quote=t)})
                    changed = True
            new.append(ev)
        return env.model_copy(update={"evidence": new}) if changed else env

    def _assent(
        self,
        env: AssertionEnvelope,
        ctx: EnvelopeStageContext,
        by_key: Mapping[str, tuple[int, Any]],
        notes: Counter[str],
    ) -> AssertionEnvelope:
        """An assistant proposal becomes ``user_confirmed`` only on a real user yes on the next line."""
        if env.src_class != "assistant_proposed":
            return env
        sup = next((e for e in env.evidence if e.role == "support" and e.turn is not None), None)
        if sup is None or sup.turn is None:
            return env
        got = by_key.get(turn_key(sup.turn))
        if got is None:
            return env
        n, line = got
        nxt = ctx.lines.get(n + 1)
        if nxt is None or (nxt.role or "user") not in ("user", "operator"):
            notes["ground.assent.none"] += 1
            return env
        proposal, _ = _strip_speaker(line.content or "", line.speaker)
        reply, _ = _strip_speaker(nxt.content or "", nxt.speaker)
        verdict: AssentVerdict = self.assent.check(reply, proposal)
        tap.event(
            "write.ground.assent",
            window=ctx.window_id,
            write_id=env.write_id,
            kind=verdict.kind,
            binds=verdict.binds,
        )
        if not verdict.confirms:
            notes[f"ground.assent.{verdict.kind}"] += 1
            return env
        notes["ground.assent.confirmed"] += 1
        bind = Evidence(
            role="binding",
            turn=TurnRef(
                source_id=nxt.source_id, source_version=nxt.source_version, seg_index=nxt.seg_index
            ),
            source_family=nxt.speaker or nxt.role or "user",
        )
        assessment = env.assessment.model_copy(
            update={"source_trust": max(env.assessment.source_trust, self.confirmed_trust)}
        )
        return env.model_copy(
            update={
                "src_class": "user_confirmed",
                "evidence": [*env.evidence, bind],
                "assessment": assessment,
            }
        )


def _asserter(
    env: AssertionEnvelope, claim: Claim, window: Mapping[str, WindowTurn], lines: Mapping[int, Any]
) -> str | None:
    """The envelope's asserter in the names the window shows: when it is the cited line's own speaker
    label (the compiler's), use the name printed on that line."""
    if env.speaker is None:
        return None
    raw = {
        turn_key(
            TurnRef(
                source_id=ln.source_id, source_version=ln.source_version, seg_index=ln.seg_index
            )
        ): ln.speaker
        for ln in lines.values()
    }
    for c in claim.cites:
        if (
            c.role == "support"
            and c.turn in window
            and normalise(raw.get(c.turn) or "") == normalise(env.speaker)
        ):
            return window[c.turn].speaker or env.speaker
    return env.speaker


def _shift_spans(claim: Claim, offsets: Mapping[str, int]) -> Claim:
    """The compiler's spans count from the start of the full line (``Name: text``); the rules see the
    line without its speaker tag, so shift each cited span by the tag length."""
    cites = []
    for c in claim.cites:
        off = offsets.get(c.turn, 0)
        if c.span is not None and off:
            sp = CitedSpan(
                start=max(0, c.span.start - off), end=max(0, c.span.end - off), quote=c.span.quote
            )
            c = c.model_copy(update={"span": sp})
        cites.append(c)
    return claim.model_copy(update={"cites": tuple(cites)})


def _source_date(ctx: EnvelopeStageContext) -> date | None:
    t = getattr(ctx.compile_ctx, "source_time", None)
    return t.date() if t is not None and hasattr(t, "date") else None


def make_grounding_stage(
    *,
    action: Literal["drop", "tentative"] = "drop",
    decider: _DeciderLike | None = None,
    adjudicate: bool = True,
    check_assent: bool = True,
    accept: float = 0.85,
    reject: float = 0.34,
    aliases: Mapping[str, str] | None = None,
) -> EnvelopeStage:
    """The order-200 stage. ``decider`` None with ``adjudicate`` True = lexical-overlap adjudication."""
    checker = GroundingChecker(
        adjudicator=Adjudicator(decider, accept=accept, reject=reject) if adjudicate else None,
        asserter_check=AsserterSubjectCheck(aliases),
    )
    stage = _Stage(checker, action=action, check_assent=check_assent)
    return EnvelopeStage("grounding", GROUND_STAGE_ORDER, stage.run)


class _EngineDecider:
    """The engine's decider, looked up at call time (built lazily on first use, on CPU by default)."""

    def __init__(self, eng: Any) -> None:
        self._eng = eng

    async def decide(self, task: str, question: str, context: str | None = None) -> Any:
        return await self._eng._decider_adapter().decide(task, question, context)


def build_grounding_stage(eng: Any, config: Any) -> EnvelopeStage:
    """``write.grounding_v2`` -> the stage, from the ``write.ground_*`` keys."""
    w = config.write
    return make_grounding_stage(
        action=w.ground_action,
        decider=_EngineDecider(eng) if w.ground_adjudicator == "decider" else None,
        adjudicate=w.ground_adjudicator != "off",
    )
