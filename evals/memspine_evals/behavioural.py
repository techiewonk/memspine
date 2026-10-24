"""Behavioural suite (P0-08, G-04): dataset-free invariance and safety pairs, retrieval/compose only.

The suite builds invented conversations (``behavioural_cases``), runs them against a model-free
engine (hash embedder, reranker off, no LLM, the golden-harness configuration) and checks what the
engine *returns* (search hits, composed context), never a reader's answer. It is the sixth gate of
G-02 and the overfitting guard of G-04.

Each case has an id, a kind (``invariance`` | ``safety``), a description, an expected relation
(``equal`` | ``subset`` | ``disjoint`` | ``abstain``) and a result:

* ``PASS``: the relation held and the control (if any) was not vacuous;
* ``FAIL``: the relation did not hold, the case could not run, or its positive control returned nothing;
* ``NOT_SUPPORTED``: the engine build lacks the feature the case needs. Never counted as a pass.

``run_behavioural(engine_factory)`` takes ``engine_factory(storage_dir) -> Engine`` (unstarted). A
FAIL is information about the engine, not an error of the runner.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import tempfile
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .behavioural_cases import (
    FILLER_TURNS,
    PARAPHRASES,
    RENAME_MAP,
    STORY_A,
    Story,
    rename,
    rename_story,
)

__all__ = [
    "CASES",
    "BehaviouralReport",
    "CaseResult",
    "Status",
    "default_engine_factory",
    "run_behavioural",
]

TOP_K = 5
BUDGET_TOKENS = 400
FROZEN_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_CLOCK_MODULES = (
    "memspine.core.policies.scoring",
    "memspine.core.policies.decay",
    "memspine.api.engine",
    "memspine.memory.pipeline.procedural",
    "memspine.memory.pipeline.prospective",
    "memspine.memory.pipeline.working",
    "memspine.derive.pipeline.integrate",
    "memspine.derive.pipeline.stages.communities",
    "memspine.derive.pipeline.stages.consolidate",
    "memspine.derive.pipeline.stages.entity_summaries",
    "memspine.derive.pipeline.stages.maintenance",
    "memspine.derive.pipeline.stages.mining",
    "memspine.derive.pipeline.stages.retention",
    "memspine.derive.pipeline.stages.watches",
)
_BACK = {v: k for k, v in RENAME_MAP.items()}


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_SUPPORTED = "NOT_SUPPORTED"


class NotSupported(Exception):
    """Raised by a case when the engine build lacks the feature it needs."""


@dataclass
class CaseResult:
    id: str
    kind: str
    description: str
    relation: str
    status: Status
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d


@dataclass
class BehaviouralReport:
    results: list[CaseResult] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c = Counter(r.status.value for r in self.results)
        return {s.value: c.get(s.value, 0) for s in Status}

    def by_kind(self) -> dict[str, dict[str, int]]:
        out: dict[str, Counter] = {}
        for r in self.results:
            out.setdefault(r.kind, Counter())[r.status.value] += 1
        return {k: {s.value: v.get(s.value, 0) for s in Status} for k, v in sorted(out.items())}

    def status_of(self, case_id: str) -> Status | None:
        for r in self.results:
            if r.id == case_id:
                return r.status
        return None

    def statuses(self) -> dict[str, str]:
        return {r.id: r.status.value for r in self.results}

    def to_dict(self) -> dict[str, Any]:
        return {"counts": self.counts(), "results": [r.to_dict() for r in self.results]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BehaviouralReport:
        return cls([CaseResult(**{**r, "status": Status(r["status"])}) for r in d["results"]])


# ---------------------------------------------------------------- engine plumbing


def default_engine_factory(storage_dir: Path) -> Any:
    """The golden-harness configuration: no model, no network."""
    from memspine import Engine

    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(storage_dir / "behavioural.db")},
            }
        },
        model={"embedding": {"provider": "hash"}, "rerank": {"chain": []}},
        read={"record_access": False},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
    )


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)


@contextmanager
def _frozen_clock() -> Iterator[None]:
    saved: list[tuple[Any, Any]] = []
    for name in _CLOCK_MODULES:
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        if hasattr(mod, "datetime"):
            saved.append((mod, mod.datetime))
            mod.datetime = _FrozenDatetime
    try:
        yield
    finally:
        for mod, original in saved:
            mod.datetime = original


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class Ctx:
    """Engine plus helpers shared by the cases. One engine serves all cases; namespaces keep them apart."""

    def __init__(self, eng: Any) -> None:
        self.eng = eng

    def supports(self, method: str, param: str) -> bool:
        fn = getattr(self.eng, method, None)
        return fn is not None and param in inspect.signature(fn).parameters

    async def ingest(
        self,
        story: Story,
        ns: str,
        *,
        reverse: bool = False,
        filler: bool = False,
        facts: bool = True,
    ) -> None:
        sessions = list(reversed(story.sessions)) if reverse else list(story.sessions)
        for s in sessions:
            turns = list(s.turns)
            if filler:
                mixed: list[tuple[str, str]] = []
                for i, t in enumerate(turns):
                    mixed.append(t)
                    mixed.extend(FILLER_TURNS[(2 * i) : (2 * i) + 2])
                turns = mixed
            await self.eng.ingest_messages(
                [{"role": r, "content": t} for r, t in turns],
                namespace=ns,
                session_id=f"{ns}-{s.session_id}",
                valid_from=_dt(s.date),
            )
        if facts:
            items = list(reversed(story.facts)) if reverse else list(story.facts)
            for content, entity, attribute, valid_from in items:
                await self.eng.write(
                    content,
                    namespace=ns,
                    memory_type="semantic",
                    entity=entity,
                    attribute=attribute,
                    valid_from=_dt(valid_from),
                )

    async def search(self, ns: str, query: str, **kw: Any) -> list[Any]:
        return [rec for rec, _ in await self.eng.search(query, namespace=ns, top_k=TOP_K, **kw)]

    async def read(self, ns: str, query: str, mode: str = "compose", **kw: Any) -> Any:
        return await self.eng.read(
            query, namespace=ns, mode=mode, top_k=TOP_K, budget_tokens=BUDGET_TOKENS, **kw
        )


def _norm(text: str) -> str:
    """Map renamed entities back to the canonical names so two arms compare as text."""
    return rename(text, _BACK)


def _contents(records: list[Any]) -> list[str]:
    return [_norm(r.content) for r in records]


def _is_filler(text: str) -> bool:
    return any(text == t for _, t in FILLER_TURNS)


# ---------------------------------------------------------------- cases


@dataclass
class Case:
    id: str
    kind: str
    description: str
    relation: str
    run: Callable[[Ctx], Awaitable[tuple[bool, str]]]


async def _inv_rename_search(c: Ctx) -> tuple[bool, str]:
    await c.ingest(STORY_A, "rn-a")
    renamed = rename_story(STORY_A)
    await c.ingest(renamed, "rn-b")
    bad = []
    for key, q in STORY_A.queries.items():
        a = set(_contents(await c.search("rn-a", q)))
        b = set(_contents(await c.search("rn-b", renamed.queries[key])))
        if a != b:
            bad.append(f"{key}: top-{TOP_K} set differs ({len(a ^ b)} records)")
    return not bad, "; ".join(bad)


async def _inv_rename_compose(c: Ctx) -> tuple[bool, str]:
    renamed = rename_story(STORY_A)
    await c.ingest(STORY_A, "rc-a")
    await c.ingest(renamed, "rc-b")
    bad = []
    for key, q in STORY_A.queries.items():
        a = await c.read("rc-a", q)
        b = await c.read("rc-b", renamed.queries[key])
        if _contents(a.context.records) != _contents(b.context.records) or bool(
            a.context.abstained
        ) != bool(b.context.abstained):
            bad.append(f"{key}: composed context differs")
    return not bad, "; ".join(bad)


async def _inv_paraphrase(c: Ctx) -> tuple[bool, str]:
    await c.ingest(STORY_A, "pp")
    bad = []
    for key, (p1, p2) in PARAPHRASES.items():
        a = _contents(await c.search("pp", p1))[:1]
        b = _contents(await c.search("pp", p2))[:3]
        if not set(a) <= set(b):
            bad.append(f"{key}: top-1 evidence not within the paraphrase top-3")
    return not bad, "; ".join(bad)


async def _inv_order_swap(c: Ctx) -> tuple[bool, str]:
    await c.ingest(STORY_A, "os-fwd")
    await c.ingest(STORY_A, "os-rev", reverse=True)
    q = STORY_A.queries["city"]
    cur: dict[str, set[str]] = {}
    for ns in ("os-fwd", "os-rev"):
        # live (not superseded) city facts the search offers as evidence
        cur[ns] = {r.content for r in await c.search(ns, q) if r.memory_type == "semantic"}
    want = {"Ilse lives in Rotterdam"}
    ok = cur["os-fwd"] == cur["os-rev"] == want
    return (
        ok,
        ""
        if ok
        else f"live city facts forward={sorted(cur['os-fwd'])} reversed={sorted(cur['os-rev'])}",
    )


async def _inv_filler(c: Ctx) -> tuple[bool, str]:
    await c.ingest(STORY_A, "fl-a")
    await c.ingest(STORY_A, "fl-b", filler=True)
    bad = []
    for key, q in STORY_A.queries.items():
        a = _contents(await c.search("fl-a", q))
        b = [t for t in _contents(await c.search("fl-b", q)) if not _is_filler(t)]
        if not set(a[:2]) <= set(b):
            bad.append(f"{key}: top evidence changed by filler")
    return not bad, "; ".join(bad)


_CANARY_A = ("zorblax", "quindle")


async def _tenant_setup(c: Ctx) -> tuple[str, str]:
    a, b = "tn-a", "tn-b"
    await c.eng.ingest_messages(
        [
            {
                "role": "user",
                "content": "My vault passphrase is zorblax and my allergy is quindle pollen.",
            },
            {"role": "user", "content": "I booked a dentist appointment for next Tuesday at nine."},
        ],
        namespace=a,
        session_id="tn-a-s1",
        valid_from=_dt("2024-05-01T10:00:00+00:00"),
    )
    await c.eng.write(
        "Bram's allergy is quindle pollen and his dentist is on Tuesday",
        namespace=a,
        memory_type="semantic",
        entity="Bram",
        attribute="allergy",
    )
    await c.eng.ingest_messages(
        [
            {"role": "user", "content": "I repainted the shed green and bought new garden gloves."},
            {
                "role": "user",
                "content": "The allergy clinic is closed on Tuesday so I will walk instead.",
            },
        ],
        namespace=b,
        session_id="tn-b-s1",
        valid_from=_dt("2024-05-02T10:00:00+00:00"),
    )
    return a, b


_TENANT_QUERIES = (
    "What is my vault passphrase?",
    "Which pollen allergy do I have?",
    "dentist appointment Tuesday",
)


def _leaks(records: list[Any]) -> list[str]:
    return [r.content[:40] for r in records if any(k in r.content.lower() for k in _CANARY_A)]


async def _saf_tenant_search(c: Ctx) -> tuple[bool, str]:
    a, b = await _tenant_setup(c)
    ctrl = await c.search(a, _TENANT_QUERIES[0])
    if not _leaks(ctrl):
        return False, "control: tenant A's own query did not return its canary record (vacuous)"
    leaks: list[str] = []
    for q in _TENANT_QUERIES:
        leaks += _leaks(await c.search(b, q))
    return not leaks, f"{len(leaks)} record(s) of tenant A returned to tenant B" if leaks else ""


async def _saf_tenant_read(c: Ctx) -> tuple[bool, str]:
    a, b = await _tenant_setup(c)
    leaks: list[str] = []
    for mode in ("retrieve", "compose", "full"):
        for q in _TENANT_QUERIES:
            res = await c.read(b, q, mode=mode)
            leaks += _leaks(list(res.context.records))
    return not leaks, f"{len(leaks)} leaked record(s) across read modes" if leaks else ""


async def _saf_viewer(c: Ctx) -> tuple[bool, str]:
    if not (
        c.supports("search", "viewer")
        and c.supports("read", "viewer")
        and c.supports("write", "participants")
    ):
        raise NotSupported("engine has no viewer / participants support")
    ns = "vw"
    await c.eng.write(
        "Ilse told Bram in private that the surprise party for Marguerite is on Friday",
        namespace=ns,
        participants=["Ilse", "Bram"],
        visibility="participants",
        valid_from=_dt("2024-07-01T10:00:00+00:00"),
    )
    await c.eng.write(
        "The office plant watering rota changes next week",
        namespace=ns,
        visibility="owner_shared",
        valid_from=_dt("2024-07-01T10:05:00+00:00"),
    )
    q = "surprise party Marguerite Friday"
    mine = await c.search(ns, q, viewer="Bram")
    if not any("surprise" in r.content for r in mine):
        return False, "control: a participant did not see the record (vacuous)"
    seen = [r for r in await c.search(ns, q, viewer="Cato") if "surprise" in r.content]
    ctx = await c.read(ns, q, mode="compose", viewer="Cato")
    seen += [r for r in ctx.context.records if "surprise" in r.content]
    return not seen, f"{len(seen)} private record(s) shown to a non-party viewer" if seen else ""


_UNANSWERABLE = (
    "What is the name of the boat that Ilse's dentist owns?",
    "Which volcano did Ilse climb in 1998?",
)


async def _saf_unanswerable(c: Ctx) -> tuple[bool, str]:
    await c.ingest(STORY_A, "un")
    bad = []
    for q in _UNANSWERABLE:
        res = await c.read("un", q)
        if res.context.records and not res.context.abstained:
            bad.append(
                f"confident evidence returned ({len(res.context.records)} records) for an unanswerable question"
            )
    return not bad, "; ".join(bad)


_GENERIC = ("What is the capital of France?", "How many days are there in a leap year?")


async def _saf_generic(c: Ctx) -> tuple[bool, str]:
    await c.ingest(STORY_A, "gp")
    ctrl = await c.read("gp", STORY_A.queries["city"])
    if not ctrl.context.records:
        return False, "control: a personal question returned no memory (vacuous)"
    leaked = 0
    for q in _GENERIC:
        res = await c.read("gp", q)
        leaked += len(res.context.records)
    return (
        leaked == 0,
        f"{leaked} personal record(s) in the context of generic questions" if leaked else "",
    )


CASES: tuple[Case, ...] = (
    Case(
        "INV-RENAME-SEARCH",
        "invariance",
        "Renaming entities (Ilse->Aiko, Bram->Cato) consistently gives the same search hit structure.",
        "equal",
        _inv_rename_search,
    ),
    Case(
        "INV-RENAME-COMPOSE",
        "invariance",
        "Renaming entities gives the same composed context.",
        "equal",
        _inv_rename_compose,
    ),
    Case(
        "INV-PARAPHRASE",
        "invariance",
        "A paraphrased query keeps the original top-1 evidence within its top-3.",
        "subset",
        _inv_paraphrase,
    ),
    Case(
        "INV-ORDER-SWAP",
        "invariance",
        "Ingesting sessions in reverse order with explicit dates keeps the current value (latest city).",
        "equal",
        _inv_order_swap,
    ),
    Case(
        "INV-FILLER",
        "invariance",
        "Inserting unrelated filler turns keeps the top-2 evidence within the top-5.",
        "subset",
        _inv_filler,
    ),
    Case(
        "SAF-TENANT-SEARCH",
        "safety",
        "A query in tenant B never returns tenant A's records (search).",
        "disjoint",
        _saf_tenant_search,
    ),
    Case(
        "SAF-TENANT-READ",
        "safety",
        "A query in tenant B never returns tenant A's records (read: retrieve, compose, full).",
        "disjoint",
        _saf_tenant_read,
    ),
    Case(
        "SAF-VIEWER",
        "safety",
        "A viewer who was not a party to a record never sees it.",
        "disjoint",
        _saf_viewer,
    ),
    Case(
        "SAF-UNANSWERABLE",
        "safety",
        "An unanswerable question yields no confident evidence (abstains).",
        "abstain",
        _saf_unanswerable,
    ),
    Case(
        "SAF-GENERIC",
        "safety",
        "A generic knowledge question pulls no personal memory into the context.",
        "disjoint",
        _saf_generic,
    ),
)


# ---------------------------------------------------------------- runner


async def _run(engine_factory: Callable[[Path], Any], only: set[str] | None) -> BehaviouralReport:
    report = BehaviouralReport()
    with tempfile.TemporaryDirectory(prefix="behavioural-") as tmp, _frozen_clock():
        eng = engine_factory(Path(tmp))
        await eng.start()
        try:
            ctx = Ctx(eng)
            for case in CASES:
                if only is not None and case.id not in only:
                    continue
                try:
                    ok, detail = await case.run(ctx)
                    status = Status.PASS if ok else Status.FAIL
                except NotSupported as e:
                    status, detail = Status.NOT_SUPPORTED, str(e)
                except Exception as e:  # a crash is a FAIL, never a silent pass
                    status, detail = Status.FAIL, f"{type(e).__name__}: {e}"[:300]
                report.results.append(
                    CaseResult(case.id, case.kind, case.description, case.relation, status, detail)
                )
        finally:
            await eng.stop()
    return report


def run_behavioural(
    engine_factory: Callable[[Path], Any] = default_engine_factory,
    *,
    only: set[str] | None = None,
) -> BehaviouralReport:
    """Run the suite on an engine built by ``engine_factory(storage_dir)``; return the report."""
    return asyncio.run(_run(engine_factory, only))
