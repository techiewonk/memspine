"""W2-15 (W-17, W-15; LIB-17): coverage check and ONE targeted re-extraction (``write.coverage``).

An envelope stage at order 250 (see ``derive/envelope_stages.py``), after grounding (200). After the
window's normal extraction it asks LIB-17 :func:`~memspine.derive.coverage.coverage` which informative
lines no kept assertion cites. *Informative* means the line has a salience signal (a named entity,
number, date, first-person state / plan / preference, list shape); greetings and filler ("ok",
"haha") carry none, and system / echo turns (W2-03 tags, or a ``system`` role) are context only: none of
them is in the denominator.

    coverage = (informative lines cited as support or binding by a kept assertion) / (informative lines)

When ``coverage < write.coverage_threshold`` and at least one informative line is uncited, the stage
makes **at most one** extra model call for the window, covering only the uncited lines (plus a few
neighbours as CONTEXT, never cited). The call uses the W2-06b window prompt (NEW / CONTEXT / KNOWN, the
``facts`` section) on the ``window_extract`` LLM. The reply goes through the compiler (validate +
compile; no repair re-ask, so the call count stays one), then through the stages that ran before this
one (temporal, grounding, ...). If no grounding stage is configured, a grounding stage is built for
this purpose: a re-extracted assertion is added **only** when it comes out ``grounded``. Everything
else is dropped and counted as ``added_unsupported_dropped``; an assertion that cites none of the
targeted lines, or repeats one already kept, is dropped too (counted apart).

Envelope fields read: ``evidence`` (role, turn), ``write_id`` / ``occurrence_id`` (dedupe), ``subject`` /
``object`` surfaces (KNOWN), ``grounding`` (re-extracted items). Written: nothing on existing envelopes;
new ones carry what the earlier stages set. ``ctx.lines`` / ``ctx.records`` give the window.

``ctx.notes`` (summed over windows; ratios as per-mille so they stay integers):
``coverage.windows``, ``coverage.before_permille``, ``coverage.after_permille``,
``coverage.reextract_calls``, ``coverage.added``, ``coverage.added_unsupported_dropped``,
``coverage.added_offtarget_dropped``, ``coverage.added_duplicate_dropped``,
``coverage.rejected`` (compiler), ``coverage.skipped.<reason>``, ``coverage.call_failed``.
The extra call is also counted by the W2-12 cost ledger under stage ``coverage_reextract`` (not
``window_extract``, so the P-14 one-call-per-window target keeps measuring the normal call).
Per-window numbers are on the stage object (:attr:`CoverageStage.stats`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any

from memspine.derive import role_filter
from memspine.derive.compile import compile_items
from memspine.derive.coverage import (
    CoverageBudget,
    WindowLine,
    coverage,
    plan_reextraction,
)
from memspine.derive.envelope_stages import EnvelopeStage, EnvelopeStageContext, run_envelope_stages
from memspine.derive.grounding_stage import _strip_speaker, make_grounding_stage
from memspine.derive.items import parse_reply
from memspine.observability import tap
from memspine.prompts.layout_aliases import render_known
from memspine.prompts.structured import window_messages
from memspine.schemas.durable.assertion import AssertionEnvelope
from memspine.services.llm.base import lenient_json

__all__ = [
    "COVERAGE_COST_STAGE",
    "COVERAGE_STAGE_ORDER",
    "CoverageStage",
    "CoverageStats",
    "WindowCoverage",
    "make_coverage_stage",
]

COVERAGE_STAGE_ORDER = 250
COVERAGE_COST_STAGE = "coverage_reextract"

#: ``(namespace, text) -> [(name, aliases)]``: the store's known entities (W2-06b KNOWN source)
KnownFn = Callable[[str, str], Any]
ScopeFn = Callable[[str], AbstractContextManager[None]]


@dataclass(frozen=True)
class WindowCoverage:
    window_id: str
    coverage_before: float
    coverage_after: float
    reextract_calls: int = 0
    added: int = 0
    added_unsupported_dropped: int = 0


@dataclass
class CoverageStats:
    """Per-window numbers, in window order (the notes carry the sums)."""

    windows: list[WindowCoverage] = field(default_factory=list)

    @property
    def reextract_calls(self) -> int:
        return sum(w.reextract_calls for w in self.windows)

    @property
    def added(self) -> int:
        return sum(w.added for w in self.windows)

    @property
    def added_unsupported_dropped(self) -> int:
        return sum(w.added_unsupported_dropped for w in self.windows)

    def mean_before(self) -> float:
        return (
            sum(w.coverage_before for w in self.windows) / len(self.windows)
            if self.windows
            else 1.0
        )

    def mean_after(self) -> float:
        return (
            sum(w.coverage_after for w in self.windows) / len(self.windows) if self.windows else 1.0
        )

    def as_dict(self) -> dict[str, float | int]:
        n = len(self.windows)
        return {
            "windows": n,
            "coverage_before": self.mean_before(),
            "coverage_after": self.mean_after(),
            "reextract_calls": self.reextract_calls,
            "added": self.added,
            "added_unsupported_dropped": self.added_unsupported_dropped,
        }


def _parse_facts(reply: str) -> list[Any] | None:
    """The ``facts`` list of a window reply: ``{"facts": [...]}`` or a bare list; None when unreadable."""
    got = parse_reply(reply)
    if got is not None:
        return got
    text = reply.strip()
    try:
        data: Any = json.loads(text)
    except ValueError:
        try:
            data = lenient_json(text)
        except Exception:
            return None
    if isinstance(data, dict):
        data = data.get("facts")
    return list(data) if isinstance(data, list) else None


def _window_lines(ctx: EnvelopeStageContext) -> tuple[list[WindowLine], dict[int, int]]:
    """LIB-17 window from ``ctx.lines``; system / echo / excluded turns are context only (``is_new``
    False). Also ``seg_index -> line number`` for the transcript numbers the model cites."""
    out: list[WindowLine] = []
    number: dict[int, int] = {}
    for n, ln in sorted(ctx.lines.items()):
        rec = ctx.records[n - 1] if 0 < n <= len(ctx.records) else None
        tags = getattr(rec, "tags", None) or ()
        skip = role_filter.block_reason(tags) is not None or (ln.role or "") == "system"
        text, _ = _strip_speaker(ln.content or "", ln.speaker)
        out.append(
            WindowLine(
                seg_index=ln.seg_index,
                text=text,
                is_new=not skip,
                source_id=ln.source_id,
                source_version=ln.source_version,
            )
        )
        number[ln.seg_index] = n
    return out, number


def _line_date(ln: Any) -> str:
    at = getattr(ln, "source_at", None)
    return f"{at:%Y-%m-%d}" if at is not None and hasattr(at, "strftime") else "unknown"


def _names(envelopes: Sequence[AssertionEnvelope]) -> list[tuple[str, tuple[str, ...]]]:
    seen: dict[str, None] = {}
    for env in envelopes:
        for ref in (env.subject, env.object):
            surface = getattr(ref, "surface", None)
            if surface:
                seen.setdefault(surface, None)
    return [(s, ()) for s in seen]


@dataclass
class CoverageStage:
    llm: Any  # LLMService: ``await chat(messages, **options) -> str``
    earlier: tuple[EnvelopeStage, ...] = ()
    threshold: float = 0.8
    max_targets: int = 6
    context_lines: int = 2
    max_tokens: int = 4000
    known: KnownFn | None = None
    known_cap: int = 40
    scope: ScopeFn | None = None
    partial: Any = None
    stats: CoverageStats = field(default_factory=CoverageStats)

    def stage(self) -> EnvelopeStage:
        return EnvelopeStage("coverage", COVERAGE_STAGE_ORDER, self.run)

    async def run(
        self, envelopes: list[AssertionEnvelope], ctx: EnvelopeStageContext
    ) -> list[AssertionEnvelope]:
        wlines, number = _window_lines(ctx)
        report = coverage(wlines, envelopes)
        before = report.ratio
        ctx.notes["coverage.windows"] += 1
        ctx.notes["coverage.before_permille"] += round(before * 1000)

        def done(
            out: list[AssertionEnvelope],
            after: float,
            *,
            calls: int = 0,
            added: int = 0,
            bad: int = 0,
        ) -> list[AssertionEnvelope]:
            ctx.notes["coverage.after_permille"] += round(after * 1000)
            self.stats.windows.append(
                WindowCoverage(ctx.window_id, before, after, calls, added, bad)
            )
            tap.event(
                "write.coverage.window",
                window=ctx.window_id,
                before=before,
                after=after,
                calls=calls,
                added=added,
                unsupported_dropped=bad,
            )
            return out

        uncovered = report.uncovered
        if before >= self.threshold or not uncovered:
            return done(envelopes, before)
        plan = plan_reextraction(
            report,
            CoverageBudget(
                max_calls=1,
                max_tokens=self.max_tokens,
                max_targets_per_window=self.max_targets,
                context_lines=self.context_lines,
            ),
            include_omissions=False,
        )
        if not plan.windows:
            for s in plan.skipped:
                ctx.notes[f"coverage.skipped.{s.reason}"] += 1
            return done(envelopes, before)
        win = plan.windows[0]  # max_calls=1: at most one
        targets = set(win.target_indices)
        reply = await self._ask(ctx, envelopes, win, number)
        ctx.notes["coverage.reextract_calls"] += 1
        calls = 1
        facts = _parse_facts(reply) if reply is not None else None
        if facts is None:
            ctx.notes["coverage.call_failed"] += 1
            return done(envelopes, before, calls=calls)
        compiled = compile_items(facts, ctx.compile_ctx)
        ctx.notes["coverage.rejected"] += len(compiled.rejections)
        have = {(e.write_id, e.occurrence_id) for e in envelopes}
        fresh: list[AssertionEnvelope] = []
        for env in compiled.envelopes:
            key = (env.write_id, env.occurrence_id)
            if key in have:
                ctx.notes["coverage.added_duplicate_dropped"] += 1
                continue
            cited = {
                e.turn.seg_index
                for e in env.evidence
                if e.role in ("support", "binding") and e.turn
            }
            if not cited & targets:
                ctx.notes["coverage.added_offtarget_dropped"] += 1
                continue
            have.add(key)
            fresh.append(env)
        kept = await self._ground(fresh, ctx)
        bad = len(fresh) - len(kept)
        ctx.notes["coverage.added"] += len(kept)
        ctx.notes["coverage.added_unsupported_dropped"] += bad
        out = [*envelopes, *kept]
        after = coverage(wlines, out).ratio
        return done(out, after, calls=calls, added=len(kept), bad=bad)

    # -- helpers -----------------------------------------------------------------------------------

    async def _ask(
        self,
        ctx: EnvelopeStageContext,
        envelopes: Sequence[AssertionEnvelope],
        win: Any,
        number: Mapping[int, int],
    ) -> str | None:
        def row(seg: int) -> str:
            n = number[seg]
            ln = ctx.lines[n]
            return f"[{n}] [{_line_date(ln)}] {' '.join((ln.content or '').split())}"

        new = "\n".join(row(s) for s in win.target_indices)
        context = "\n".join(
            " ".join((ctx.lines[number[s]].content or "").split()) for s in win.context_indices
        )
        entries = _names(envelopes)
        if self.known is not None:
            try:
                got = await self.known(ctx.namespace, new)
                known_entries = [(k.name, tuple(k.aliases)) for k in got]
            except Exception:
                known_entries = []
            seen = {n.casefold() for n, _ in known_entries}
            entries = known_entries + [e for e in entries if e[0].casefold() not in seen]
        people: list[str] = []
        for s in win.target_indices:
            who = ctx.lines[number[s]].speaker
            if who and who not in people:
                people.append(who)
        kwargs: dict[str, Any] = {} if self.partial is None else {"partial": self.partial}
        messages = window_messages(
            ("facts",),
            new,
            ", ".join(people),
            len(win.target_indices),
            context=context,
            known=render_known(entries[: self.known_cap]),
            **kwargs,
        )
        scope = self.scope(ctx.namespace) if self.scope is not None else nullcontext()
        try:
            with scope:
                return str(await self.llm.chat(messages, temperature=0.0, max_tokens=2048))
        except Exception:  # a failed call is counted by the caller; the window keeps what it has
            return None

    async def _ground(
        self, fresh: list[AssertionEnvelope], ctx: EnvelopeStageContext
    ) -> list[AssertionEnvelope]:
        """The earlier stages (temporal, grounding) over the new envelopes only; keep ``grounded``."""
        if not fresh:
            return []
        sub = EnvelopeStageContext(
            namespace=ctx.namespace,
            window_id=ctx.window_id,
            lines=ctx.lines,
            records=ctx.records,
            compile_ctx=ctx.compile_ctx,
            services=ctx.services,
        )
        out = await run_envelope_stages(self.earlier, fresh, sub)
        for k, v in sub.notes.items():
            ctx.notes[f"coverage.reground.{k}"] += v
        return [e for e in out if e.grounding == "grounded" and e.status != "tentative"]


def make_coverage_stage(
    llm: Any,
    *,
    earlier: Sequence[EnvelopeStage] = (),
    threshold: float = 0.8,
    max_targets: int = 6,
    context_lines: int = 2,
    max_tokens: int = 4000,
    known: KnownFn | None = None,
    known_cap: int = 40,
    scope: ScopeFn | None = None,
    partial: Any = None,
) -> CoverageStage:
    """The order-250 stage object (``.stage()`` is the :class:`EnvelopeStage`, ``.stats`` the numbers).

    ``earlier`` are the stages the re-extracted items go through again (those with an order below 250).
    A grounding stage is added when none is among them: an item is added only if grounded."""
    chain = tuple(s for s in earlier if s.order < COVERAGE_STAGE_ORDER)
    if not any(s.name == "grounding" for s in chain):
        chain = (*chain, make_grounding_stage())
    return CoverageStage(
        llm=llm,
        earlier=chain,
        threshold=threshold,
        max_targets=max_targets,
        context_lines=context_lines,
        max_tokens=max_tokens,
        known=known,
        known_cap=known_cap,
        scope=scope,
        partial=partial,
    )
