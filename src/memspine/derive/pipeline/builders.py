"""Derive: builders of the model-backed callables the stages take (F-15, from the engine)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from memspine.config.schema import MemspineConfig
from memspine.core.escaping import escape_markers
from memspine.core.fact_chain import fact_projection_on
from memspine.core.rule_miner import mine_rules
from memspine.derive.pipeline.stages.context import (
    CalibrateEpisode,
    PredictEpisode,
    Summarize,
    SummarizeEntities,
    SummarizeIncremental,
)
from memspine.derive.pipeline.stages.extract_graph import resolve_mode
from memspine.derive.session_close import CALL2_SCHEMA
from memspine.exceptions import ConfigError
from memspine.memories.associative.resolution import ResolveBatch
from memspine.memories.semantic.write_pipeline import EdgeContext, ExtractEdges, extraction_rounds
from memspine.prompts.base import RenderedMessages
from memspine.prompts.models import (
    AnticipatedCue,
    AnticipatedCues,
    AssertionOut,
    AssertionsOut,
    ConflictVerdictOut,
    EntityMatches,
    EntitySummaries,
    ExtractedEdge,
    ExtractedEdges,
    ExtractedFact,
    ExtractedFacts,
    FactClasses,
    FactDates,
    Insights,
)
from memspine.services.llm.structured import structured_call
from memspine.store.events import fingerprint_payload
from memspine.workers.list_cards import LabelClasses


def _build_entity_summarizer(eng: Any) -> SummarizeEntities | None:
    """GP-6 (#17): the batched entity summariser, when a ``summarize_entity``
    (else ``summarize``) LLM role is bound and entity summaries are on."""
    if eng._llm is None or eng._prompts is None or eng._associative is None:
        return None
    policies = eng._memory_policy(eng._config(), "associative")
    if not policies.get("entity_summaries"):
        return None
    role = next((r for r in ("summarize_entity", "summarize") if r in eng._llm.roles), None)
    if role is None:
        return None
    llm = eng._llm.for_role(role)
    prompt = eng._prompts.for_role("summarize_entity")

    async def summarize(items: list[tuple[str, list[str]]]) -> dict[int, str]:
        blocks = []
        for i, (name, lines) in enumerate(items, start=1):
            body = "\n".join(f"- {escape_markers(line)}" for line in lines)
            blocks.append(f"[{i}] {escape_markers(name)}\n{body}")
        result = await structured_call(
            llm, prompt, {"entities": "\n".join(blocks)}, EntitySummaries
        )
        return {s.index: s.summary for s in result.summaries if s.summary.strip()}

    return summarize


def _build_entity_resolver(eng: Any) -> ResolveBatch | None:
    """GP-7 (#18): the batched resolver (``resolve_entity@batch``), when a
    ``resolve_entity`` LLM role is bound and ``extract_graph.resolve: llm``."""
    if eng._llm is None or eng._prompts is None or "resolve_entity" not in eng._llm.roles:
        return None
    policy = eng._memory_policy(eng._config(), "semantic").get("extract_graph")
    if not isinstance(policy, dict) or policy.get("resolve") != "llm":
        return None
    llm = eng._llm.for_role("resolve_entity")
    prompt = eng._prompts.select("resolve_entity", condition="batch")

    async def resolve(items: list[tuple[str, list[str]]]) -> dict[int, str]:
        lines = [
            f"[{i}] {escape_markers(name)} (candidates: "
            + "; ".join(escape_markers(c) for c in candidates)
            + ")"
            for i, (name, candidates) in enumerate(items, start=1)
        ]
        result = await structured_call(llm, prompt, {"names": "\n".join(lines)}, EntityMatches)
        return {m.index: m.match for m in result.matches}

    return resolve


def _build_summarize(eng: Any) -> Summarize | None:
    """The consolidation summarizer (M2): only when a ``summarize`` LLM role
    is bound; pipelines fall back to the deterministic extractive path."""
    if eng._llm is None or eng._prompts is None or "summarize" not in eng._llm.roles:
        return None
    llm = eng._llm.for_role("summarize")
    prompt = eng._prompts.for_role("summarize")

    async def summarize(content: str) -> str:
        return await llm.chat(prompt.render({"content": content}))  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    return summarize


def _build_summarize_incremental(eng: Any) -> SummarizeIncremental | None:
    """#56: the ``summarize@incremental`` update, only when
    ``consolidation.session_summary.incremental`` is on and a ``summarize``
    LLM role is bound (one call: previous summary + only the new turns)."""
    options = eng._consolidation_option("session_summary", None)
    if not (isinstance(options, dict) and options.get("incremental")):
        return None
    if eng._llm is None or eng._prompts is None or "summarize" not in eng._llm.roles:
        return None
    llm = eng._llm.for_role("summarize")
    prompt = eng._prompts.select("summarize", condition="incremental")

    async def update(previous: str, content: str) -> str:
        return await llm.chat(prompt.render({"previous": previous, "content": content}))  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    return update


def _predict_calibrate_role(eng: Any, role: str) -> str | None:
    """#62: the LLM role a predict-calibrate call runs on (falls back to
    ``extract``), only when ``consolidation.predict_calibrate`` is on."""
    if not eng._consolidation_option("predict_calibrate", False):
        return None
    if eng._llm is None or eng._prompts is None:
        return None
    return next((r for r in (role, "extract") if r in eng._llm.roles), None)


def _build_episode_predictor(eng: Any) -> PredictEpisode | None:
    """#62: predict an episode's facts from stored memory (``predict_episode``)."""
    role = eng._predict_calibrate_role("predict_episode")
    if role is None or eng._llm is None or eng._prompts is None:
        return None
    llm = eng._llm.for_role(role)
    prompt = eng._prompts.select("predict_episode")

    async def predict(cue: str, date: str, knowledge: list[str]) -> str:
        return await llm.chat(prompt.render({"cue": cue, "date": date, "knowledge": knowledge}))  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    return predict


def _build_calibrator(eng: Any) -> CalibrateEpisode | None:
    """#62: diff a prediction against the transcript (``calibrate``)."""
    role = eng._predict_calibrate_role("calibrate")
    if role is None or eng._llm is None or eng._prompts is None:
        return None
    llm = eng._llm.for_role(role)
    prompt = eng._prompts.select("calibrate")

    async def calibrate(prediction: str, content: str, knowledge: list[str]) -> list[ExtractedFact]:
        context: dict[str, object] = {
            "prediction": prediction,
            "content": content,
            "knowledge": knowledge,
        }
        result = await structured_call(llm, prompt, context, ExtractedFacts)
        return list(result.facts)

    return calibrate


def _build_reflector(eng: Any) -> Any:
    """H14: the profile reflector, when a ``reflect`` role and reflective memory exist."""
    if (
        eng._llm is None
        or eng._prompts is None
        or "reflect" not in eng._llm.roles
        or eng._reflective is None
    ):
        return None
    llm = eng._llm.for_role("reflect")
    prompt = eng._prompts.select("reflect")

    async def reflect(episodes: list[str]) -> list[tuple[str, list[int]]]:
        result = await structured_call(llm, prompt, {"episodes": episodes}, Insights)
        return [(i.insight, list(i.evidence)) for i in result.insights]

    return reflect


def _build_anticipator(eng: Any) -> Any:
    """H8: the anticipator, when an ``anticipate`` (or ``extract``) LLM role is bound."""
    if eng._llm is None or eng._prompts is None:
        return None
    role = next((r for r in ("anticipate", "extract") if r in eng._llm.roles), None)
    if role is None:
        return None
    llm = eng._llm.for_role(role)
    prompt = eng._prompts.select("anticipate")

    async def anticipate(content: str) -> list[AnticipatedCue]:
        result = await structured_call(llm, prompt, {"content": content}, AnticipatedCues)
        return list(result.cues)

    return anticipate


def _build_assertion_projector(eng: Any) -> Any:
    """E01 write-time arm: the ``extract@assertions`` caller of the ``project_facts``
    stage, only when ``memories.semantic.policies.fact_projection`` is ``on`` and an
    ``extract`` LLM role is bound. A bad value is a config error."""
    try:
        on = fact_projection_on(eng._memory_policy(eng._config(), "semantic"))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if not on or eng._llm is None or eng._prompts is None:
        return None
    if "extract" not in eng._llm.roles:
        return None
    llm = eng._llm.for_role("extract")
    prompt = eng._prompts.select("extract", condition="assertions")

    async def project(content: str) -> list[AssertionOut]:
        out = await structured_call(
            llm, prompt, {"question": "(none)", "content": content}, AssertionsOut
        )
        return list(out.assertions)

    return project


def _build_fact_miner(eng: Any) -> Any:
    """C6': the atomic-fact miner, only when an ``extract`` LLM role is bound.

    W5 (``consolidation.miner: rules``): the rule miner instead, no model."""
    if eng._consolidation_option("miner", "llm") == "rules":

        async def mine_by_rules(content: str) -> list[ExtractedFact]:
            return mine_rules(content)

        return mine_by_rules
    if eng._llm is None or eng._prompts is None or "extract" not in eng._llm.roles:
        return None
    llm = eng._llm.for_role("extract")
    # H2: the session variant (no pronouns, absolute dates, one fact each) when
    # shipped; the base extract prompt otherwise. #27: ``consolidation.mine_prompt``
    # picks another session variant, whose token budget becomes the output cap.
    variant = str(eng._consolidation_option("mine_prompt", "session"))
    if variant == "session" and eng._consolidation_option("mine_multiview", False):
        variant = "session4"  # #28: the default prompt asks for no view fields
    prompt = eng._prompts.select("extract", condition=variant)
    options: dict[str, Any] = {}
    if variant != "session" and prompt.token_budget:
        options["max_tokens"] = prompt.token_budget

    async def mine(content: str) -> list[ExtractedFact]:
        result = await structured_call(llm, prompt, {"content": content}, ExtractedFacts, **options)
        return list(result.facts)

    if not eng._consolidation_option("mining_cache", False) or eng._cache is None:
        return mine
    # E1 (plan v3.2, F7): deterministic mining. Temperature 0 and one cached reply
    # per (model, prompt, variant, transcript), so re-ingesting the same history
    # yields the same facts (LoCoMo L5: two runs of one mining arm differed).
    options["temperature"] = 0.0
    role = eng._config().model.llm.get("extract")
    from memspine.providers.engine_changes import resolved_config_fingerprint

    model = resolved_config_fingerprint(role) if role is not None else llm.provider_id
    version = str(getattr(prompt, "version", "")) + "/" + str(getattr(prompt, "name", ""))
    cache = eng._cache

    async def mine_cached(content: str) -> list[ExtractedFact]:
        key = "mine:" + fingerprint_payload(
            {"model": model, "prompt": version, "variant": variant, "content": content}
        )
        hit = await cache.get(key)
        if hit is not None:
            return ExtractedFacts.model_validate_json(hit).facts
        facts = await mine(content)
        await cache.set(key, ExtractedFacts(facts=facts).model_dump_json().encode())
        return facts

    return mine_cached


def _build_fact_dater(eng: Any) -> Any:
    """#29: the batched ``extract@dates`` call for mined facts with no date, only
    when ``consolidation.mine_event_dates_llm`` is on and an ``extract`` role is
    bound. Returns ``{fact index: date}`` for the facts the model could date."""
    if (
        not eng._consolidation_option("mine_event_dates_llm", False)
        or eng._llm is None
        or eng._prompts is None
        or "extract" not in eng._llm.roles
    ):
        return None
    llm = eng._llm.for_role("extract")
    prompt = eng._prompts.select("extract", condition="dates")

    async def date_facts(transcript: str, facts: list[str]) -> dict[int, str]:
        numbered = "\n".join(f"[{i}] {fact}" for i, fact in enumerate(facts, 1))
        result = await structured_call(
            llm, prompt, {"content": transcript, "facts": numbered}, FactDates
        )
        return {d.index: d.date for d in result.dates if d.date}

    return date_facts


def _build_class_labeller(eng: Any) -> LabelClasses | None:
    """#30: the per-person ``extract@classes`` call of the list-card step, only
    when ``consolidation.list_cards`` is on and an ``extract`` role is bound."""
    if (
        not eng._consolidation_option("list_cards", False)
        or eng._llm is None
        or eng._prompts is None
        or "extract" not in eng._llm.roles
    ):
        return None
    llm = eng._llm.for_role("extract")
    prompt = eng._prompts.select("extract", condition="classes")

    async def label_classes(person: str, facts: list[str]) -> dict[int, str]:
        numbered = "\n".join(f"[{i}] {fact}" for i, fact in enumerate(facts, 1))
        result = await structured_call(llm, prompt, {"facts": numbered}, FactClasses)
        return {c.index: c.label for c in result.classes if c.label}

    return label_classes


def _edge_extract_callable(eng: Any, max_rounds: int, condition: str | None = None) -> ExtractEdges:
    """The shared reflexion-merged ``extract_edges`` callable (C2 async +
    C3 sync). Caller guarantees the ``extract_edges`` role is bound.

    ``condition="session"`` (#20) selects ``extract_edges@session``: the content
    is a numbered session transcript, the context may carry the allowed entity
    names, and the rounds merge each edge's ``episode_indices``."""
    assert eng._llm is not None and eng._prompts is not None
    llm = eng._llm.for_role("extract_edges")
    prompt = (
        eng._prompts.for_role("extract_edges")
        if condition is None
        else eng._prompts.select("extract_edges", condition=condition)
    )
    rounds = max(1, max_rounds)
    graph_opts = eng._memory_policy(eng._config(), "semantic").get("extract_graph")
    graph_opts = graph_opts if isinstance(graph_opts, dict) else {}
    ontology_entities = [str(t) for t in graph_opts.get("entity_types") or ()]
    ontology_rels = [str(t) for t in graph_opts.get("relation_types") or ()]

    async def extract_edges(
        content: str, context: EdgeContext | None = None, /
    ) -> list[ExtractedEdge]:
        # GR-4: reference time, earlier episodes and known entity names ride
        # along; every key is always supplied (the prompt renders strictly).
        ctx = context or EdgeContext()
        variables: dict[str, object] = {
            "content": content,
            "reference_time": ctx.reference_time.isoformat() if ctx.reference_time else "",
            "previous_episodes": list(ctx.previous),
            "entities": list(ctx.entities),
            # G-8 / G-5 / G-6: the writer's hint and the declared ontology.
            "hint": ctx.hint,
            "entity_types": list(ontology_entities),
            "relation_types": list(ontology_rels),
        }
        if condition is not None:
            variables["allowed_entities"] = list(ctx.allowed_entities)
        merged: dict[tuple[str, str, str], ExtractedEdge] = {}
        for _ in range(rounds):
            result = await structured_call(llm, prompt, variables, ExtractedEdges)
            for edge in result.edges:
                key = (edge.src_entity, edge.rel, edge.dst_entity)
                if key in merged and condition is not None:
                    cited = [*merged[key].episode_indices, *edge.episode_indices]
                    edge = edge.model_copy(update={"episode_indices": list(dict.fromkeys(cited))})
                merged[key] = edge
        return list(merged.values())

    return extract_edges


def _build_edge_extractor(eng: Any, config: MemspineConfig) -> ExtractEdges | None:
    """C2 graphiti-style edge extractor for the ``extract_graph`` pipeline.

    Active only when ``memories.semantic.policies.extract_graph`` is truthy
    AND an ``extract_edges`` LLM role is bound — otherwise None, so the
    pipeline self-skips and ``profile="simple"`` is unchanged."""
    policy = eng._memory_policy(config, "semantic").get("extract_graph")
    if not policy:
        return None
    if isinstance(policy, dict):
        resolve_mode(policy)  # GP-7: an unknown ``resolve`` fails at start, not mid-sweep
    if eng._llm is None or eng._prompts is None or "extract_edges" not in eng._llm.roles:
        return None
    # #32: write.reflexion=false drops the extra rounds (one call per source).
    return eng._edge_extract_callable(extraction_rounds(eng._memory_policy(config, "semantic")))  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


def _build_session_edge_extractor(eng: Any, config: MemspineConfig) -> ExtractEdges | None:
    """#20: the session-level extractor for ``extract_graph.granularity: session``.

    Built only when that option is set on an active extract_graph policy (an
    ``extract_edges`` role bound); otherwise None and extraction stays per
    record. An unknown granularity is a config error."""
    policy = eng._memory_policy(config, "semantic").get("extract_graph")
    opts = policy if isinstance(policy, dict) else {}
    granularity = opts.get("granularity", "record")
    if granularity not in ("record", "session"):
        raise ConfigError(
            "memories.semantic.policies.extract_graph.granularity must be "
            f"record|session, got {granularity!r}"
        )
    if granularity != "session" or eng._build_edge_extractor(config) is None:
        return None
    return eng._edge_extract_callable(int(opts.get("max_rounds", 1)), condition="session")  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


def _build_edge_adjudicator(eng: Any) -> Callable[[str, str, str, str], Awaitable[str]] | None:
    """G-2: the ``invalidate_edge`` prompt as a callable, when that role is bound."""
    if eng._llm is None or eng._prompts is None or "invalidate_edge" not in eng._llm.roles:
        return None
    llm = eng._llm.for_role("invalidate_edge")
    prompt = eng._prompts.for_role("invalidate_edge")

    async def adjudicate(
        existing: str, existing_from: str, incoming: str, incoming_from: str
    ) -> str:
        result = await structured_call(
            llm,
            prompt,
            {
                "existing_fact": existing,
                "existing_valid_from": existing_from,
                "incoming_fact": incoming,
                "incoming_valid_from": incoming_from,
            },
            ConflictVerdictOut,
        )
        return str(result.verdict)

    return adjudicate


#: O4-02: LLM roles the call-2 model call may use, first bound one wins.
CLOSE_ROLES = ("session_close", "summarize", "extract")


def _build_close_call(eng: Any) -> Callable[[list[dict[str, str]]], Awaitable[str]] | None:
    """O4-02: the call-2 model call, only with ``offline.session_close_v2`` and a bound role.

    The prompt is the library's (``derive.session_close.render_prompt``: a stable prefix with no
    examples and the same-language rule, then the data body), sent as raw messages through an
    existing role's model binding, so no new prompt template exists."""
    if not eng._config().offline.session_close_v2 or eng._llm is None:
        return None
    role = next((r for r in CLOSE_ROLES if r in eng._llm.roles), None)
    if role is None:
        return None
    llm = eng._llm.for_role(role)

    async def call(messages: list[dict[str, str]]) -> str:
        tagged = RenderedMessages(messages, prompt_id="session_close", prompt_version=CALL2_SCHEMA)
        return await llm.chat(tagged)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

    return call
