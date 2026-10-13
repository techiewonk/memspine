"""Config area: read (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from memspine.config import constants
from memspine.exceptions import ConfigError


class ReadCacheConfig(BaseModel):
    """R3-15 read-result cache (``read.cache``)."""

    enabled: bool = False
    max_entries: int = Field(default=256, ge=1)
    ttl_seconds: float | None = Field(default=None, gt=0)


class ReadConfig(BaseModel):
    """ReadPolicy bindings (M12) + the E8/E5 opt-in stages (D-51, default OFF).

    ``scoring``/``assembly`` flow into ``ScoringPolicy.bind`` /
    ``AssemblyPolicy.bind``; per-namespace overrides ride the D-14
    policy-override channel later.

    - ``rerank``: ``off`` (default) | ``fastembed`` (ONNX cross-encoder) |
      ``flashrank`` (``[rerank]`` extra) — E8 rerank stage over the candidate
      set, fed concat_background text (D-42 §5).
    - ``static_prefilter``: cheap lexical-overlap gate before rerank/score (E8).
    - ``static_embedding_prefilter``: E4 model2vec static-embedding gate
      (``[static]``) that narrows the candidate set with cheap static cosine
      before rerank/score. Default OFF; when on but the extra is missing the
      engine skip-logs and the stage is a no-op (retrieval never fails).
    - ``hybrid``: fuse the vector leg with a lexical BM25 leg via RRF (D-25).
      **Default ON (v0.2 A3)** — the lexical BM25 index is built and fused into
      every search so records that only exact keywords surface can still land.
      Set ``hybrid: false`` for the pre-v0.2 vector-only pipeline: bit-identical
      to vector-only results and no lexical index is built. This is D-25's
      core-default intent (see ADR-019); the default ``tantivy`` provider is a
      **core** dependency and is backend-independent, so the default works on
      every storage backend with no extra to install.
    - ``lexical_provider``: which lexical store backs the hybrid leg —
      ``tantivy`` (default, standalone Tantivy BM25 index — **core**, no extra,
      independent of the storage backend; an on-disk index dir beside the db, or
      in-RAM for a ``:memory:`` log); the ``opensearch`` stub was removed (G-22). A
      provider satisfies the ``LexicalStore`` port and shares ``LexicalProjector``. The
      transactional-DB
      ``sqlite_fts5`` provider was removed (v0.2): the lexical leg is a dedicated
      search index, never bolted onto the system-of-record.
      Only consulted when ``hybrid`` is on (the default); with ``hybrid: false``
      no store is built. There is intentionally **no lexical-only reranking** — the fused
      RRF result is reranked (when enabled) by the E8 cross-encoder stage
      (``rerank`` above), which scores the query against fused candidates from
      *both* legs, so a second BM25 pass would add nothing.
    - ``compression``: options for the E5 assembly-stage ``CompressionPolicy``
      binding (``{"assembly": true, "assembly_stage": [...]}``); the master
      switch defaults off so ``profile="simple"`` behavior never changes.
    """

    model_config = ConfigDict(extra="forbid")

    scoring: dict[str, Any] = Field(default_factory=dict)
    assembly: dict[str, Any] = Field(default_factory=dict)
    rerank: str = "off"  # off | fastembed | flashrank | litellm
    #: LiteLLM rerank model id, required when rerank=litellm
    #: (e.g. cohere/rerank-english-v3.0, bedrock/amazon.rerank-v1:0).
    rerank_model: str | None = None
    static_prefilter: bool = False
    static_embedding_prefilter: bool = False
    hybrid: bool = True  # v0.2 A3: default-on hybrid retrieval (D-25, ADR-019)
    lexical_provider: str = "tantivy"  # tantivy (core, default) (D-25)
    #: N58 (gaps plan 2026-10-08): the Tantivy analyzer for the BM25 body. ``default``
    #: (unchanged: alphanumeric runs, lower-cased) or ``english`` (plus English stop
    #: words and the Snowball English stemmer, so "camping" matches "camp"). The
    #: ``english`` index lives in its own directory under its own projector name, so
    #: switching rebuilds it from the event log and never touches the default index.
    lexical_analyzer: Literal["default", "english"] = "default"
    #: N30 (write side; Hindsight indexes dates with the text): the BM25 index also
    #: holds each record's date words (the day it was said and the days it names:
    #: "2023-05-07 7 May 2023 Sunday"), so a question naming a date matches lexically.
    #: Own index and projector name (``lexical:dates``): turning it on rebuilds.
    lexical_dates: bool = False
    compression: dict[str, Any] = Field(default_factory=dict)
    #: Append a RETRIEVE event per search (access stats feed reinforcement, M1).
    #: ``false`` makes reads side-effect free, e.g. so benchmark questions cannot
    #: change the store that later questions see.
    record_access: bool = True
    #: C4': render each retrieved keyed fact as ``CURRENT (since date)`` plus its
    #: superseded ``HISTORY`` from the bi-temporal chain (deterministic, no LLM).
    current_state_view: bool = False
    #: C3': fuse a temporal leg (records whose event time lies in an absolute
    #: date span named in the query) and a metadata leg (records whose entity is
    #: named in the query) into the RRF ranking. Off: bit-identical ranking.
    temporal_leg: bool = False
    #: F2 (plan v3.2): with ``temporal_leg``, a question with no absolute date but a
    #: relative phrase ("what did we discuss last week?") gets its span resolved
    #: against the read time (the engine clock) by the H1 rules, using
    #: ``relative_week``. Off: relative phrases name no span (unchanged).
    temporal_relative: bool = False
    #: N45 (gaps plan 2026-10-08): with ``temporal_leg``, a date with no year ("on
    #: 7 May", "in March") takes the latest year that puts it on or before the
    #: namespace's newest record (or the as-of time). Off: such dates name no span.
    temporal_infer_year: bool = False
    #: N61: order of in-span records in the temporal leg. ``midpoint`` (unchanged:
    #: closest to the span's middle) or ``overlap`` (most content words shared with
    #: the question first, then midpoint).
    temporal_rank: Literal["midpoint", "overlap"] = "midpoint"
    #: N44: when fewer than ``fetch_k`` records fall in the span, fill the temporal leg
    #: with the nearest records outside it (within one span length, at least
    #: ``TEMPORAL_SOFT_MARGIN_DAYS``). Off: hard window (unchanged).
    temporal_soft: bool = False
    #: N30 (Hindsight: dates indexed with the text): with ``temporal_leg``, a turn
    #: whose text names a date ("last weekend", "on 7 May"), resolved against the
    #: turn's own time, also enters the leg when that date overlaps the question's
    #: span. Read-side only: nothing stored changes. Off: unchanged.
    temporal_leg_mentions: bool = False
    metadata_leg: bool = False
    #: W8 (plan v3.2): an RRF leg of the turns of every speaker the question names
    #: (``speaker:<name>`` tags from ``memories.episodic.policies.subject_tagging``),
    #: ranked by content-word overlap with the question. A boost, never a filter.
    #: Off: unchanged.
    subject_leg: bool = False
    #: W11 (plan v3.2): assistant-side memory. Assistant turns that recommend something
    #: are tagged ``recommendation`` at ``write_messages``; a question about what the
    #: assistant said ("what did you recommend", "your suggestion") gets an RRF leg of
    #: the assistant's own turns, recommendations first. Off: unchanged.
    role_aware: bool = False
    #: N05 (plan v3.2, EverMemOS ``amaxsim``, lexical variant): an RRF leg ranking records
    #: by their best single sentence (shared content words / sqrt(sentence length)), so a
    #: strong sentence inside a long turn is not diluted. No model. Off: unchanged.
    sentence_leg: bool = False
    #: H13: an extra BM25 leg over the question's core terms (interrogative and
    #: function words removed), fused by RRF. Needs the lexical store (hybrid).
    core_terms_leg: bool = False
    #: H18: run the reranker only when ``top_k`` is at most this (reranking helps
    #: most when few of many candidates are kept, and can hurt abstention when
    #: many are). None = always rerank when a reranker is configured.
    rerank_max_top_k: int | None = Field(default=None, ge=1)
    #: G5b: with a reranker and ``candidate_pool > 1``, keep only the best
    #: ``rerank_keep`` candidates after reranking, before assembly fills the budget,
    #: so a wider pool sharpens the ranking instead of growing the context.
    #: None = keep the whole pool (unchanged).
    rerank_keep: int | None = Field(default=None, ge=1)
    #: H15: replay windows stay inside the hit's topic segment (lexical-cohesion
    #: boundaries within a session), so neighbours from another topic are not replayed.
    replay_topic_segments: bool = False
    #: H17: label search candidates relevant / related / irrelevant with the
    #: ``relevance`` LLM role and drop only "irrelevant" ones, always keeping the
    #: ``relevance_safety_net`` best-scored candidates (a yes/no filter loses gold).
    relevance_filter: bool = False
    #: RRF rank constant (k in 1/(k+rank)); None = the default 60. Graphiti uses 1.
    rrf_k: int | None = Field(default=None, ge=1)
    #: ContextPipe: tokens kept free for the reply inside the assembly budget.
    reply_reserve_tokens: int = Field(default=0, ge=0)
    #: R3-09a (ER-24, LIB-06): count every budgeted piece (selected records, headers, lead blocks,
    #: the rendered text) with the reader's own tokenizer instead of ``len // 4 + 1``, and when a
    #: record does not fit skip it and try smaller later ones (early break after
    #: ``exact_budget_max_skips`` skips) instead of stopping at the first overflow. Off: unchanged.
    exact_budget: bool = False
    #: R3-09a: the reader's tokenizer, resolved OFFLINE only (never downloads): a Hugging Face
    #: tokenizer already in the local cache (``hf:<name>`` or a bare name) or a tiktoken encoding /
    #: model whose files are cached (``tiktoken:<name>``). Unset or unresolvable: the chars/4
    #: estimate, and ``compose.budget`` reports ``accounting=estimated``.
    reader_tokenizer: str | None = None
    #: R3-09a: how many over-budget records may be skipped before the rest are dropped.
    exact_budget_max_skips: int = Field(default=8, ge=1)
    #: R3-09b (ER-24 C+A, LIB-06): selected mined facts enter the context as complete evidence
    #: bundles (the fact plus its source turn(s), cited, whole or not at all) packed under the
    #: budget. Today's records carry no evidence lists, so a fact's bundle is the fact plus its
    #: eligible parent turns (``source.parents``); other records are their own evidence.
    #: Off: unchanged.
    evidence_bundles: bool = False
    #: R3-09b: with bundles on, the selection pool is this many times the budget, so bundles that
    #: do not fit whole can give way to smaller later ones.
    bundle_pool_factor: float = Field(default=2.0, ge=1.0)
    #: R3-09b: share of the retrieved-evidence budget fact bundles may take while other records
    #: still wait; with no other records they may use all of it.
    bundle_fact_share: float = Field(default=0.6, ge=0.0, le=1.0)
    #: T5-02 (G-09, LIB-25): low-trust, tentative and contested records are rendered in labelled
    #: sections as quoted, neutralised data (injection-like text defanged), quarantined ones
    #: withheld. Replaces the B6 untrusted-note wrapper while on. Off: unchanged.
    trust_rendering: bool = False
    #: The mode ``Engine.read()`` uses when the caller passes none. ``auto`` keeps the
    #: routed behaviour; the ``assistant`` template pins ``replay`` (combo-A, LoCoMo:
    #: compose routing measured -2.1, replay is the base of every winning arm).
    default_mode: Literal["auto", "full", "replay", "retrieve", "compose"] = "auto"
    #: Hindsight: prefix reranker inputs with ``[Date: YYYY-MM-DD]``.
    rerank_date_prefix: bool = False
    #: N41 (Dakera): None (unchanged) = the reranker's relevance replaces the retrieval
    #: score; a weight in [0, 1] blends them: ``w * rerank + (1 - w) * retrieval``,
    #: each min-max normalised over the candidates.
    rerank_blend: float | None = Field(default=None, ge=0.0, le=1.0)
    #: N41 confidence gate: when the reranker's best raw score is below this, it is
    #: not confident any candidate answers, and the retrieval order is kept. None = off.
    rerank_gate: float | None = None
    #: N42 (MemMachine): the reranker sees each candidate with this many neighbouring
    #: turns of its session on each side, so a short reply is judged with its question.
    #: 0 = the candidate alone (unchanged).
    rerank_context: int = Field(default=0, ge=0, le=3)
    #: GR-15 (Graphiti balanced shortlist): with a reranker on, the pool cut before
    #: reranking takes each leg's best hits in turn, so one leg cannot fill it.
    rerank_balanced: bool = False
    #: I75a (leg-protected pool): each leg's top-N hits are guaranteed a slot in the pool sent to
    #: the reranker even when their fused (RRF) rank falls outside the pool size; the rest is
    #: filled by RRF. The pool grows by at most ``legs * N``. 0 = off (unchanged).
    pool_protect_per_leg: int = Field(default=0, ge=0, le=20)
    #: R02 / I75 v2: how ``pool_protect_per_leg`` counts. ``per_leg`` (default) = I75a: every leg
    #: protects its own top-N, so the pool grows. ``source_family`` groups legs into independent
    #: families (lexical; semantic = vector + perspective + other vector-derived legs; temporal;
    #: other), protects the top-N per FAMILY with correlated votes counted once, and keeps the
    #: pool at its normal size by evicting the lowest-RRF unprotected candidates.
    pool_protect_mode: Literal["per_leg", "source_family"] = "per_leg"
    #: R3-06 (LIB-10, R-11/R-12/R-13): fuse by source FAMILY (a family's legs merge by best
    #: rank, so a
    #: second leg of a family never adds a vote), protect each family's top ``pool_protect_per_leg``
    #: hits (3 when that is 0), apply ``leg_min_scores`` as floors that never drop a
    #: protected hit, and
    #: cap the fused pool. Off (default) = the per-leg RRF of ``rrf_fuse`` unchanged. Equal
    #: leg weights
    #: (``leg_weights`` and ``fusion: minmax`` do not apply while on).
    family_fusion: bool = False
    #: R3-08 (LIB-11, ER-21 B, POL-2): lift derived hits (facts, summaries) to their source
    #: turns BEFORE
    #: the rerank: lift, re-admit through the read gates, dedupe, cap to the incoming pool
    #: size, then
    #: rerank once, so lifted turns are scored. A derived hit with a hidden or missing source
    #: turn is
    #: dropped (strict). Off (default) = the pool reaches the reranker as it is.
    lift_before_rerank: bool = False
    #: R3-08: at most this many source turns replace one derived hit.
    lift_max_turns: int = Field(default=2, ge=1, le=8)
    #: R3-07 (LIB-42, Q5/Q6): rerank every read through a licence fallback chain
    #: (``rerank_v2_chain``,
    #: first buildable and working wins; none = unscored, never 0) over a pool capped at
    #: ``rerank_v2_pool`` (64). A candidate the reranker did not score stays unscored: it
    #: follows every
    #: scored one and is never given 0.0. Bypasses the ``rerank_max_top_k`` gate; ``read.rerank`` is
    #: ignored while on. Off (default) = the single configured reranker, unchanged.
    rerank_v2: bool = False
    #: R3-07: reranker modes in preference order (Jina v3.5, Qwen3-Reranker-0.6B, MiniLM on CPU).
    #: Drop ``jina`` to avoid its CC-BY-NC weights.
    rerank_v2_chain: list[str] = Field(default_factory=lambda: ["jina", "qwen3", "fastembed"])
    rerank_v2_pool: int = Field(default=64, ge=1, le=256)
    #: G-10 (Graphiti MMR): None = off; a lambda in [0, 1] reorders the final hits by
    #: maximal marginal relevance on embeddings (1 = relevance only, 0 = diversity only).
    mmr_lambda: float | None = Field(default=None, ge=0.0, le=1.0)
    #: N12 (plan v3.2, EverMemOS): the task instruction an instruction-conditioned
    #: reranker (``rerank: qwen3``) judges with; None keeps its memory default.
    rerank_instruction: str | None = None
    #: ``rerank: qwen3`` only: torch device (None = CPU float32) and weight quantisation
    #: ("4bit" | "8bit" via bitsandbytes, GPU only; None = full precision).
    rerank_device: str | None = None
    rerank_quant: Literal["4bit", "8bit"] | None = None
    #: I9: None (default, unchanged) = a long record is cut by the reranker's own context
    #: limit (the tail is dropped). N = records longer than N characters are scored in
    #: overlapping windows of N characters and take the max window score, so a long assistant
    #: turn is judged on its best passage. ``rerank_chunk_overlap`` = characters shared by
    #: neighbouring windows.
    rerank_chunk_chars: int | None = Field(default=None, ge=64)
    rerank_chunk_overlap: int = Field(default=0, ge=0)
    #: I7: a source with no timestamps (ConvoMem, PrefEval, LaMP shapes) gets
    #: ``valid_from`` = the write clock, so every rendered line would show today's date. On:
    #: a write whose event time was not supplied is tagged `ts_defaulted`, and the date
    #: prefix / relative-date annotation / rerank date prefix / timeline date skip it. Only
    #: records written with this key on are affected. Off: byte-identical.
    skip_defaulted_dates: bool = False
    #: F5: the zone a NAIVE datetime (a ``valid_from`` / message ``timestamp`` without an
    #: offset) is read in, an IANA name. Default ``UTC`` (as always); a warning is logged once
    #: per engine whenever a naive value is seen, because labelling dataset-local wall time as
    #: UTC shifts events near midnight onto the wrong day. Aware values are never touched.
    naive_timezone: str = "UTC"
    #: F5: when several turns of one session carry the SAME event time (a day-level or
    #: session-level stamp), each later one is moved by one more microsecond so the event-time
    #: order within the session is the write order everywhere (not only where ``recorded_at``
    #: breaks the tie). Off: stamps are stored as given.
    session_sequence: bool = False
    #: I23: `on` = the regex features that only know English (query shape, temporal phrases,
    #: month names, set nouns) do not fire on text a cheap script/stopword check judges
    #: non-English; they fail closed. Off: unchanged.
    language_guard: Literal["off", "on"] = "off"
    #: Agent Zero: skip the reranker for ordering questions (first / latest / ...).
    skip_rerank_for_ordering: bool = False
    #: H22 (Mastra): with ``render: dated``, mark long gaps ("[3 weeks later]").
    gap_markers: bool = False
    #: P4 (JustMem COMPOSE): the compose read adds up to two answer-free rewrites
    #: from the ``query_rewrite`` LLM role (``@compose`` prompt). Needs the role bound.
    compose_rewrites: bool = False
    #: H24: how ``read(mode="auto")`` picks a mode once full context does not fit:
    #: ``rules`` (deterministic cues), ``decision`` (the decision provider chooses
    #: among compose / replay / retrieve) or ``llm`` (G2a: one ``plan`` role call
    #: returns a ReadPlan; lookup/replay read by replay, aggregate by compose with
    #: the plan's subqueries as extra probes). Rules on any failure.
    planner: Literal["rules", "decision", "llm"] = "rules"
    #: G2b: with ``planner: decision``, a choice whose confidence is below this
    #: does not route the read: it keeps the default ``replay`` (retrieve when no
    #: hit is episodic). 0.0 = every choice routes (unchanged).
    planner_min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    #: #35: with ``planner: llm``, ``v2`` selects the ``plan@v2`` prompt, which also
    #: writes one or two evidence-seeking subqueries for lookup questions ("Is X
    #: religious?" -> "church", "faith"); the lookup read then fuses them by RRF as
    #: extra search legs. ``v3`` (#36) selects ``plan@v3``: v2 plus the people the
    #: question is about (``persons``) and its time expression (``time_expr``), which
    #: feed the persons / time leg (``person_time_leg_k``). ``v1``: unchanged.
    planner_version: Literal["v1", "v2", "v3"] = "v1"
    #: G2c: compose results get the same +-``replay_window`` neighbour expansion as
    #: replay mode (nearest first, within the budget), so routing an aggregation
    #: question to compose no longer loses the turns around each hit.
    compose_replay: bool = False
    #: G11: the ``top_k`` of a read that ``read(mode="auto")`` routes to compose (the
    #: LLM planner's ``aggregate``, the decision planner's or the rules' compose), so
    #: list and count questions whose evidence spans sessions pool more candidates.
    #: The budget still caps the context. None = the caller's ``top_k`` (unchanged).
    aggregate_top_k: int | None = Field(default=None, ge=1)
    relevance_safety_net: int = Field(default=10, ge=0)
    #: C8': resolve search hits on anticipatory cues (``Engine.add_cues``) to
    #: their target records. A cue below ``cue_min_trust`` is ignored, so cues
    #: from low-trust sources cannot redirect retrieval. Off: cues are invisible.
    anticipatory_cues: bool = False
    #: H1: annotate relative-time phrases in assembled/read records with the absolute
    #: date they denote, resolved against each record's event time (``valid_from``):
    #: "last Friday" -> "last Friday [= Fri 2023-07-14]". Deterministic rules, no model;
    #: stored content is never changed. Off: byte-identical.
    resolve_relative_dates: bool = False
    #: G13: with ``resolve_relative_dates``, state week-level phrases relative to the
    #: record's own day, LoCoMo's convention: "last week [= the week before 2023-06-09
    #: (2023-06-02..2023-06-08)]" instead of the previous calendar week; "last weekend",
    #: "last Friday", "N weeks ago" name the relation too, and "a few days ago" gets
    #: "[= a few days before <day>]". Months, years and days are unchanged. Off:
    #: byte-identical.
    relative_dates_anchored: bool = False
    #: #58: with ``resolve_relative_dates``, the span of "last/past week" and "next
    #: week": ``calendar`` (the previous / next Monday-to-Sunday week, unchanged) or
    #: ``preceding_7_days`` (the seven days before / after the record's own day,
    #: LoCoMo's "the week before <session date>"). Also used by
    #: ``consolidation.mine_event_dates``.
    relative_week: Literal["calendar", "preceding_7_days"] = "calendar"
    #: C5: with ``resolve_relative_dates``, also annotate unambiguous durations against
    #: the record's own day: "for 3 years" -> "[= since about 2020]", "a month now" ->
    #: "[= since about 2023-04]", "since 2019" -> "[= about 4 years as of 2023-05-20]".
    #: Always ``about``; vague spans ("for years", "for a while") are left alone.
    #: Off: byte-identical.
    resolve_durations: bool = False
    #: I79: with ``resolve_relative_dates``, also annotate a bare ``on <weekday>`` against the
    #: record's own day, by the sentence's tense: "won it on Friday" -> "[= the Friday before
    #: 2022-07-10 (Fri 2022-07-08)]" (anchored form with ``relative_dates_anchored``). No clear
    #: tense, the plural and the anchor's own weekday are left alone. Off: byte-identical.
    relative_dates_weekdays: bool = False
    #: I79: with ``resolve_relative_dates``, write each date label as "[= event <date>]" so the
    #: date the event happened reads distinct from the line's leading said-date. Duration
    #: labels are unchanged. Off: byte-identical.
    relative_dates_happened: bool = False
    #: H11: assembly draws from ``candidate_pool x top_k`` search candidates, so
    #: the token budget, not a fixed K, decides how much evidence enters (LoCoMo:
    #: top-10 filled ~400 of 4,096 tokens). 1 = unchanged. Pair with
    #: ``assembly.relative_floor`` to keep precision.
    candidate_pool: int = Field(default=1, ge=1, le=10)
    #: RETRIEVAL_GAPS finding 4: the replay / compose neighbour window is a fixed
    #: +-2 turns (the ``window`` argument of ``Engine.read``), but 63% of the gold the
    #: window supplies lies AFTER the hit, so a 2-before / 4-after window beats a
    #: symmetric 3 at equal cost. These two override the window per side (turns of the
    #: hit's session). ``None`` = that side keeps the ``read`` argument (default 2),
    #: byte-identical; ``0`` = no neighbours on that side. ``read(replay_window=0)``
    #: still switches the compose expansion off.
    replay_window_before: int | None = Field(default=None, ge=0)
    replay_window_after: int | None = Field(default=None, ge=0)
    #: I6 / I20: the window counted in TURNS suits short chat turns; with long assistant
    #: turns or a very long history one neighbour can be thousands of tokens. ``tokens``
    #: expands each hit's neighbours nearest first, per side, until that side's token
    #: allowance (``replay_window_tokens_before`` / ``_after``, per hit, 1:2 like the
    #: turn window) is used; a neighbour that would pass it closes that side. ``turns`` =
    #: today's behaviour, byte-identical.
    replay_window_unit: Literal["turns", "tokens"] = "turns"
    replay_window_tokens_before: int = Field(default=256, ge=0)
    replay_window_tokens_after: int = Field(default=512, ge=0)
    #: I20: scale the window and the candidate pool to the budget the routed read has
    #: left. With ``f = min(1, budget / replay_budget_reference)`` (floored at 0.25) the
    #: turn window, the token allowances and ``top_k`` shrink by ``f`` (a side that had a
    #: neighbour keeps at least one turn; ``top_k`` keeps at least 1). Off: unchanged.
    replay_budget_scaling: bool = False
    replay_budget_reference: int = Field(default=4096, ge=1)
    #: I20: replay admits every hit first (best score first) and only then the neighbours,
    #: so a neighbour of the first hit can never push a lower-ranked hit out of the
    #: budget. Off: each hit is followed at once by its own window (today's order).
    replay_hits_first: bool = False
    #: I31: near-duplicate removal among the candidates before assembly. ``exact`` =
    #: equal text; ``jaccard`` = content-word Jaccard >= ``dedupe_threshold``;
    #: ``embedding`` = record-vector cosine >= ``dedupe_threshold`` (a record with no
    #: vector falls back to jaccard). ``dedupe_keep``: the ``best``-scored copy stays, or
    #: the ``earliest`` (by event time, with the better score). The dropped pairs are in
    #: ``search_forensics()["dedupe_dropped"]``. The pinned persona is never dropped.
    dedupe: Literal["off", "exact", "jaccard", "embedding"] = "off"
    dedupe_threshold: float = Field(default=0.9, gt=0.0, le=1.0)
    dedupe_keep: Literal["best", "earliest"] = "best"
    #: I33: a profile / preference line is shown only when it bears on the question, not
    #: whatever the question is (MemOS injects up to 6 preferences at threshold 0.0).
    #: ``overlap`` = the line shares at least one content word with the question (the
    #: slots header, and the profile header's insights). ``off`` = today's behaviour.
    #: The decider's relevance check will replace the overlap test through
    #: ``Engine._profile_line_relevant``; until then it is the interface seam.
    profile_relevance_gate: Literal["off", "overlap"] = "off"
    #: RETRIEVAL_GAPS finding 3: a reranked candidate list is min-max normalised
    #: (best 1.0, worst 0.0), so ``assembly.relative_floor`` then drops on average half
    #: of the hits (and their replay windows) however relevant the reranker found them.
    #: ``minmax`` = unchanged. ``skip`` = reranked reads do not apply the relative
    #: floor (the reranker, ``rerank_keep`` and the budget bound the context instead);
    #: a read the reranker did not score (off, gated, failed) still applies it. A
    #: ``raw`` mode was left out: reranker outputs are only calibrated 0-1 for some
    #: models, and the floor would still multiply the composite score.
    rerank_floor: Literal["minmax", "skip"] = "minmax"
    #: H16: for ordering questions ("first", "latest", "most recent", ...) present
    #: the assembled volatile records in event-time order instead of score order.
    order_by_time_for_ordering: bool = False
    #: H5: ``dated`` prefixes each episodic/semantic record with its event date,
    #: ``[2023-05-08 Mon]``, after the stable prefix. ``plain`` = unchanged.
    render: Literal["plain", "dated"] = "plain"
    cue_min_trust: float = 0.5
    #: #61 (ADR-050): the read-time query encoder. ``none`` = no encoder (reads are
    #: byte-identical). ``cues`` matches the query against stored anticipatory cues
    #: (H8 / ``add_cues``) by content-word overlap, no model, and adds the cued
    #: records as one more fused retrieval leg; matches respect ``cue_min_trust``
    #: and the cued records pass every read gate. Independent of
    #: ``anticipatory_cues`` (which searches cue text directly).
    query_encoder: Literal["none", "cues"] = "none"
    #: H22 (Mnemon): open the volatile context with a dated timeline per topic
    #: entity of the retrieved keyed facts: every live fact on that entity plus its
    #: superseded history, oldest first. Built at read time from the stored facts
    #: (no model, nothing stored); entries pass the same gates as any context record.
    topic_timelines: bool = False
    #: H22: how many entities get a timeline (best-scored first).
    timeline_entities: int = Field(default=3, ge=1)
    #: H22: the newest entries kept per timeline.
    timeline_items: int = Field(default=8, ge=2)
    #: H22: lead the context, right after the pinned persona, with preferences and
    #: standing requests the user stated ("from now on ...", "please always ...").
    #: Only user-role records at or above ``standing_min_trust``; shown as data the
    #: user stated, never as system instructions. Deterministic cue rules.
    standing_instructions: bool = False
    standing_min_trust: float = Field(default=0.7, ge=0.0, le=1.0)
    #: O4-08 (LIB-32, opt-in L5): lead the context with the precomputed standing answers the
    #: ``offline.standing_v2`` sleep stage stored, for the topic entities of the retrieved keyed
    #: facts, plus the guarded standing instructions (only when ``standing_instructions`` is off,
    #: which keeps its own block). A stale answer (its fact group changed since the build) is never
    #: shown; the reader falls back to live evidence. Read-only: nothing is written at read time.
    standing_v2: bool = False
    #: O4-08: words the standing-answer block may use.
    standing_answers_budget: int = Field(default=120, ge=10)
    #: N18 (plan v3.2): the standing-preference cues. ``narrow`` (unchanged): requests
    #: aimed at the assistant ("from now on", "please always", "call me").
    #: ``wide``: also first-person evaluative forms ("I avoid / can't stand / am
    #: allergic to / I'm vegetarian / my favourite"). PrefEval explicit preferences:
    #: narrow 11.3%, wide 83.1%; LoCoMo turns matched: 0.4% vs 4.8%.
    standing_patterns: Literal["narrow", "wide"] = "narrow"
    #: H22: the token sub-budget of the lead section (standing preferences, then
    #: timelines), taken out of the assembly budget.
    lead_budget_tokens: int = Field(default=400, ge=0)
    #: G1b (JustMem cards): ``header`` opens the volatile context with the mined
    #: atomic facts relevant to the query, one dated line each, retrieved by the same
    #: hybrid search restricted to ``atomic_fact`` records and gated like any record.
    #: The block stays within ``cards_budget_share`` of the budget; the rest goes to
    #: the normal read, which then leaves mined facts out (no fact twice). ``off``:
    #: byte-identical.
    cards: Literal["off", "header"] = "off"
    cards_budget_share: float = Field(default=0.25, gt=0.0, le=1.0)
    cards_top_k: int = Field(default=10, ge=1)
    #: Smoke 2026-10-05: cards carry the miner's own, often wrong, absolute dates and
    #: cost temporal questions -16; skip the cards header for date questions
    #: (``query_shape.is_temporal``) so the H1-resolved raw turns answer them.
    cards_skip_temporal: bool = False
    #: #29: a card whose mined fact carries a happened date
    #: (``consolidation.mine_event_dates``) that differs from the day it was said
    #: renders ``[said d1 · happened d2]``. Off: byte-identical.
    cards_event_date: bool = False
    #: G3b: after the cards header, an "about" block of the H14 profile insights
    #: (``consolidation.reflect_profile`` records) on the people the query names, or
    #: the most relevant insights when none matches, within ``profile_budget_share``
    #: of the budget. Gated like any record. Off: byte-identical.
    profile_header: bool = False
    profile_budget_share: float = Field(default=0.15, gt=0.0, le=1.0)
    #: E3: for count questions ("how many times ...", ``query_shape.is_count``), lead
    #: the context with an "Occurrences (dated):" block: the distinct dated mentions of
    #: the counted event among the episodic records the read retrieved, one line each
    #: (same-day mentions of one event count once), within ``count_budget_share`` of the
    #: budget, which the read gives up for it. Off: byte-identical.
    count_timeline: bool = False
    count_budget_share: float = Field(default=0.1, gt=0.0, le=1.0)
    #: #60: with ``count_timeline``, mentions of one event are merged before they are
    #: listed: a mention's day is the event day its single-day relative phrase names
    #: ("yesterday", "last Friday", H1 rules), else the day it was said, and two
    #: mentions on the same event day with high word overlap are one occurrence, even
    #: when said on different days. Off: byte-identical.
    count_dedupe: bool = False
    #: GP-3 (#14): fuse a graph leg into the RRF ranking: from the entities the
    #: query names (else those of the best 3 hits of the other legs), walk the
    #: association graph ``graph_depth`` entity hops to fact records and their
    #: source turns. Needs associative memory with ``entity_nodes``. Off:
    #: byte-identical.
    graph_leg: bool = False
    graph_depth: int = Field(default=2, ge=1, le=3)
    graph_leg_k: int = Field(default=10, ge=1)
    #: GP-10 (#16): a graph walk never enters a record below this trust (nor a
    #: quarantined one). Default: the firewall's quarantine threshold.
    graph_min_trust: float = Field(default=constants.GRAPH_MIN_TRUST_DEFAULT, ge=0.0, le=1.0)
    #: GP-5 (#15): a graph facts block after the cards: the edge facts reached from
    #: the entities the query names, with validity ranges and source counts, within
    #: ``cards_budget_share`` (shared with the cards header). Off: byte-identical.
    cards_include_edges: bool = False
    #: GP-4/KB-4/GR-19 (#22): rerank the gated candidates by graph proximity to
    #: the graph leg's seeds (the entities the query names, else those of the best
    #: hits): ``distance`` = 1 / entity hops of the seed walk, ``ppr`` = local
    #: push-PPR over the seeds' subgraph (normalised to the best record). Each
    #: boost, and an episode-mentions boost (facts restated by more episodes,
    #: ``edge_source:`` provenance), lifts relevance as ``r + w * b * (1 - r)``
    #: with ``w = graph_rerank_weight``; unboosted candidates keep their score.
    #: Needs associative memory with ``entity_nodes`` for the graph part. Off:
    #: byte-identical.
    graph_rerank: Literal["off", "distance", "ppr"] = "off"
    graph_rerank_weight: float = Field(default=0.2, ge=0.0, le=1.0)
    #: W12 (plan v3.2, ADR-061): before the cut, walk the ``because`` (effect ->
    #: cause) and ``reply_to`` (reply -> answered message) edges from the best
    #: ``CAUSAL_WALK_SEEDS`` candidates up to ``causal_walk_hops`` hops. A reached
    #: record joins (or is lifted to) ``seed relevance x CAUSAL_WALK_DECAY ** hops``
    #: and then passes every search gate. ``why``: only for questions asking for a
    #: cause ("Why ...", "What made ..."); ``always``: every read. Edges come from
    #: ``memories.associative.policies.rule_edges`` and ``write(reply_to=)``;
    #: needs associative memory. Off: byte-identical.
    causal_walk: Literal["off", "why", "always"] = "off"
    causal_walk_hops: int = Field(default=2, ge=1, le=3)
    #: G27 (plan v3.2, ADR-061): in a replay read, a turn written with ``reply_to``
    #: also shows the message it answers (gated like any replayed neighbour, within
    #: the budget). Off: byte-identical.
    reply_links: bool = False
    #: A07: reply / reference edges inferred from the turn order. ``on``: at write time
    #: (``write_messages``) a short reply from another speaker, and a turn that opens with a
    #: deictic reference ("this", "that", "these", "those", "we"), is linked to the immediately
    #: preceding turn through ``reply_to:<id>`` plus a ``ref:short_reply`` / ``ref:deictic``
    #: tag; an explicit ``reply_to`` is never overridden. At read time (replay) the antecedent
    #: of a retrieved turn that carries such a tag is attached (same namespace, within the
    #: budget, at most ``reference_max_per_read`` per read) and every decision is logged in
    #: the forensics as ``reference_edges``. Off: byte-identical.
    reference_edges: Literal["off", "on"] = "off"
    #: A07: a turn of at most this many words (speaker prefix excluded), by another speaker
    #: than the previous turn, counts as a short reply.
    reference_short_words: int = Field(default=6, ge=1, le=20)
    #: A07: the most antecedents one read attaches.
    reference_max_per_read: int = Field(default=4, ge=1, le=16)
    #: GP-6 (#17): an "About <Name>: …" block of the entity summaries
    #: (``summarize_entities`` stage) of the entities the query names (the graph
    #: leg's seeds), within ``cards_budget_share`` after the cards and the graph
    #: facts. Off: byte-identical.
    entity_summaries: bool = False
    #: GP-9 (#23): community summaries (``reorganize`` parents) are read only when
    #: a seed entity of the graph leg is a member: one of the records it mentions
    #: belongs to the community. Others never reach the context; with
    #: ``graph_leg`` on, the admitted ones join the graph leg. Off: byte-identical.
    graph_communities: bool = False
    #: #36: with ``planner: llm`` and ``planner_version: v3``, the routed read fuses a
    #: structured leg by RRF: records whose ``valid_from`` lies in the span the plan's
    #: ``time_expr`` names (H1 rules) and/or that are about the plan's ``persons``
    #: (``person:`` tags, else the record's entity), at most this many.
    person_time_leg_k: int = Field(default=10, ge=1)
    #: #38: for aggregate / list / count reads routed to compose, ask the ``sufficiency``
    #: role (else ``plan``) whether the context is complete (+1 call) and, when it is
    #: not, for up to three missing-information queries (+1 call, ``sufficiency@missing``);
    #: the compose read then runs once more with them as extra probes. One round at most.
    #: Off: no call, byte-identical.
    completeness_check: bool = False
    #: G-17 (SimpleMem multi-round reflection): how many completeness rounds a checked
    #: compose read may run (each one sufficiency + one missing-queries call). 1 = #38.
    completeness_rounds: int = Field(default=1, ge=1, le=3)
    #: #40: the profile header packs, within ``profile_header_budget`` tokens (at most
    #: half the read budget), the session summaries, then the profile observations
    #: (H14 insights), then the best other hits for the query, one dated, escaped line
    #: each, in a fixed section order. Independent of ``profile_header``. Off:
    #: byte-identical.
    profile_header_packing: bool = False
    profile_header_budget: int = Field(default=300, ge=1)
    #: ADR-055 question-shape gates on the replay read path (all off by default).
    #: A1: a ``replay`` read of a list or count question (``query_shape.is_aggregation``
    #: / ``is_count``) retrieves ``aggregate_top_k`` candidates (when set) through the
    #: normal replay path; no compose rendering. The budget still caps the context.
    aggregate_in_replay: bool = False
    #: A2: the cards header shows #30 list cards only to list and count questions.
    list_cards_only_aggregate: bool = False
    #: With ``cards: header`` and ``cards_skip_temporal``, a date question gets no cards
    #: header, and then (with this key) no mined fact either: it reads raw turns, as
    #: with mining off. Off: mined facts compete in its raw read (measured to push out
    #: dated turns on LoCoMo temporal questions).
    cards_skip_hides_facts: bool = False
    #: ADR-055 addendum: with ``cards: header``, the cards header (mined facts and
    #: list cards) is built only for list and count questions; any other question
    #: that is not a date question also gets no mined fact (``atomic_fact``) in its
    #: routed read, so it reads raw turns only. Date questions keep the
    #: ``cards_skip_temporal`` / ``cards_temporal`` behaviour. The rule holds in every
    #: read mode, ``full`` included (as with mining off). Off: byte-identical.
    cards_only_aggregate: bool = False
    #: B2: with ``temporal_leg``, a record whose ``happened:`` date (a mined fact's
    #: event date) overlaps the query's date span also enters the temporal leg.
    temporal_leg_event_dates: bool = False
    #: B3: the cards header on date/time questions (``query_shape.is_temporal``):
    #: ``skip`` = today's behaviour (no cards when ``cards_skip_temporal``);
    #: ``event_dates`` = only cards with a ``happened:`` date, rendered
    #: ``[happened d · said d']``, whatever ``cards_skip_temporal`` says.
    cards_temporal: Literal["skip", "event_dates"] = "skip"
    #: D1: no profile header (plain or packed) on date/time questions.
    profile_skip_temporal: bool = False
    #: W9 (plan v3.2, OP-Bench): the profile header (plain or packed) only for questions
    #: about the asker, a choice they face, or a named person (``is_personal`` or a
    #: name in the query); a general-knowledge question ("What is the capital of
    #: France?") gets none, so the profile is not injected where it does not apply.
    #: Standing instructions (style requests) are unaffected. Off: unchanged.
    profile_scope_gate: bool = False
    #: G32 (plan v3.2): a request for something new (``is_novelty``: "a book I haven't
    #: read", "something different") gets a header of what memory says the asker
    #: already likes / does / has (keyed likes, activities, favourite_*, pets facts and
    #: list cards), so the answer avoids repeats. Off: unchanged.
    novelty_exclusions: bool = False
    #: W5 (plan v3.2): the Λ-profile header: the current keyed STATE facts (rule- or
    #: LLM-mined: home, origin, job, favourites, attitudes …) of each person the
    #: question names, else of ``user``; within ``profile_budget_share``. Off: none.
    profile_slots_header: bool = False
    #: W5 / W16: include slots tagged ``sensitive:*`` in that header (default: never).
    profile_sensitive: bool = False
    #: N10 (plan v3.2): a duration question ("how long after ...", "how many weeks
    #: between ...") gets one engine-computed line from the two best-matching dated
    #: turns: "A (date) -> B (date): N days (about W weeks, M months)". Off: none.
    span_line: bool = False
    #: N16 (plan v3.2, LaMP RSPG): per-leg RRF weights for this deployment's task
    #: (``{"vector": w, "lexical": w, "extra": w}``; missing keys weigh 1). Empty:
    #: unchanged. With ``recency_leg``, a recency-only leg (newest records first) joins
    #: the fusion as an extra leg, for tasks where the latest behaviour matters most.
    leg_weights: dict[str, float] = Field(default_factory=dict)
    #: N62 (Dakera per-route weights): leg weights by question shape (``temporal``,
    #: ``count``, ``ordering``, ``inference``, ``plain``; ``query_shape.question_shape``),
    #: applied over ``leg_weights``. Named legs: ``vector``, ``lexical``, ``temporal``,
    #: ``entity``; every other extra leg is ``extra``. Empty: unchanged.
    leg_weights_by_shape: dict[str, dict[str, float]] = Field(default_factory=dict)
    #: N52 (Mem0 / Dakera calibrated fusion): how the retrieval legs are fused. ``rrf``
    #: (unchanged, reciprocal rank) or ``minmax`` (each leg's scores min-max normalised
    #: and summed; tied rule legs normalised by rank).
    fusion: Literal["rrf", "minmax"] = "rrf"
    #: N43 (Dakera session cohesion): an RRF leg of records said within
    #: ``COHESION_WINDOW_MINUTES`` of the first-pass top hits (``ANCHOR_TOP``).
    cohesion_leg: bool = False
    #: N32 (Hindsight / EverMemOS entity links) + N53 (Mem0 damping): an RRF leg of
    #: records naming the proper nouns and years the first-pass top hits name; names
    #: in more than ``ENTITY_EXPAND_MAX_SHARE`` of the records are dropped.
    entity_expand_leg: bool = False
    #: N60 (EverMemOS MaxSim, Dakera sentence sub-memories): an RRF leg of first-pass
    #: candidates with two or more sentences, ranked by their best sentence's cosine
    #: with the query. Embeds candidate sentences at read (cached); no index change.
    maxsim_leg: bool = False
    #: Gap B5 (2026-10-10): drop speaker/person names (the "Speaker:" prefixes of the
    #: namespace's records) from the BM25 query only, so a name in the question does not
    #: make BM25 rank by the name. The vector leg is untouched; if nothing is left the
    #: original query is used.
    lexical_strip_names: bool = False
    #: Gap B8 (2026-10-10): an RRF leg ("session") that ranks sessions (``group_id``) by the
    #: cosine of the query with the mean of their record vectors, then contributes the best
    #: ``session_leg_per_session`` records of each of the top ``session_leg_top_sessions``.
    session_leg: bool = False
    session_leg_top_sessions: int = Field(default=3, ge=1)
    session_leg_per_session: int = Field(default=2, ge=1)
    #: Gap B1 (2026-10-10) list mode, opt-in. For a list / set question (``list_trigger``)
    #: that names exactly one speaker, the read (1) adds a ``speaker_vote`` RRF leg: the
    #: vector leg fetched ``list_vote_depth`` deep, kept to the named speaker's turns (top
    #: ``list_vote_top_k``); (2) draws ``list_pool`` x top_k candidates (never fewer than
    #: ``candidate_pool``) and skips the ``rerank_keep`` cut; (3) gives only the best
    #: ``window_full_hits`` replay hits their neighbour window, the rest are single turns
    #: (``None``: every hit keeps its window). Other questions read exactly as before.
    list_mode: bool = False
    #: ``set_question_wide`` (R2-3) = ``is_set_question_wide``: also "How did X <verb> ..." and
    #: plural-object questions. Where a question names two speakers with a comparison cue
    #: ("both", "in common", "each") the vote runs once per speaker (R2-2).
    #: ``set_question`` = ``query_shape.is_set_question``; ``aggregation`` = ``is_aggregation``
    #: or ``is_count``.
    #: ``intent`` (I4) = ``query_shape.is_intent_list``, a no-model generic trigger: counts,
    #: totals, "so far", "all the", "each", "both", plural answer heads, enumerating
    #: conjunctions; it stays quiet on one-item "most recent / last / favourite" questions.
    list_trigger: Literal["set_question", "set_question_wide", "aggregation", "intent"] = (
        "set_question"
    )
    #: I5: how the list-mode speaker vote picks its speaker. ``name`` (default) = the one
    #: speaker the question names (``speaker:`` tag or capitalised "Name:" prefix). ``subject``
    #: = that, and otherwise the question's subject: a first-person "I / my" votes on the
    #: ``user`` turns, a second-person "you" on the ``assistant`` turns (role from a
    #: "user:" / "assistant:" prefix or the record's source role; needs both roles in the
    #: store); a pronoun or mixed subject casts no vote.
    #: I39: ``perspective`` = the vote keyed on the resolved question perspective
    #: (``core/perspective``): a named participant, the asker (I / my), the assistant (you),
    #: or a relation-bound third party ("my cousin"); needs the tags of
    #: ``memories.episodic.policies.perspective``, else it falls back to the ``subject`` rules.
    speaker_vote_mode: Literal["name", "subject", "perspective"] = "name"
    #: I39 (perspective layer, tags written by ``memories.episodic.policies.perspective``).
    #: ``subject_weight`` = an RRF leg of the records about the question's subject plus a
    #: multiplier on every candidate's relevance, ``1 - perspective_weight * (1 - match)``
    #: (about the subject 1.0, the subject speaking of others 0.6, unresolved third party 0.5,
    #: about someone else 0.0). ``subject_filter`` = the same leg, and candidates about someone
    #: else are dropped (never below ``perspective_min_keep`` candidates). Off: unchanged.
    perspective_mode: Literal["off", "subject_weight", "subject_filter"] = "off"
    perspective_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    #: I75b: False = the perspective only multiplies the relevance of candidates the other legs
    #: found (``1 - perspective_weight * (1 - match)``); no perspective RRF leg is added.
    #: True (default) = today's behaviour, leg plus multiplier.
    perspective_leg: bool = True
    perspective_min_keep: int = Field(default=3, ge=0)
    #: The asker's id ("caroline", "user"); None = ``user`` when the store has user turns, a
    #: lone participant, else unresolved (first-person questions then stay neutral).
    perspective_asker: str | None = None
    #: Which axes the read uses. ``subject`` = whose / about whom; ``modality`` = a plan,
    #: wish, hypothetical, question or request is weaker evidence of a fact; ``polarity`` = a
    #: negated statement is weaker evidence for a positive question (kept for "never / not /
    #: ever" questions); ``scope`` = a one-off event is weaker evidence of a trait question
    #: ("what does X like"); ``sensitivity`` = a ``sensitive:*`` record needs the question to
    #: touch the same category. Each multiplies the relevance by ``1 - perspective_weight``
    #: (scope: half of it). I43 ``hearsay`` = a reported-speech record ("my mom said ...") is
    #: weaker evidence for a question about its source (half of it); I44 ``certainty`` = a
    #: ``cert:hedged`` record is weaker evidence for a fact question unless the question
    #: itself hedges ("do you think ...", "maybe"); I47 ``ack`` = an assistant statement about
    #: the user that no later user turn acknowledged is weaker evidence for a user-profile
    #: question (dropped under ``subject_filter``).
    perspective_axes: list[
        Literal[
            "subject", "modality", "polarity", "scope", "sensitivity", "hearsay", "certainty", "ack"
        ]
    ] = Field(default_factory=lambda: ["subject"])
    #: I39: show ``[about: Caroline's cousin]`` before a record whose subject differs from its
    #: speaker (the stored content is unchanged). Off: unchanged.
    perspective_marker: bool = False
    #: I59: the read-side owner check, after retrieval and before the reader. Each context line
    #: is judged against the person the question names (``core/owner_check``). ``mark`` = prefix
    #: ``[Melanie, about Melanie]`` (speaker, subject) on every tagged line; ``note`` = when NO
    #: line is about the named person and some line is about someone else, add one reader note
    #: ("none of the memories describe X doing this; the matching memories are about Y"); ``both``
    #: = both. Never drops a line; a question naming two people keeps both. Needs the tags of
    #: ``memories.episodic.policies.perspective``. With ``decider_tasks`` containing
    #: ``about_target`` an untagged line is judged by the decider. Off: unchanged.
    owner_check: Literal["off", "mark", "note", "both"] = "off"
    #: I60: a capitalised name in the question that occurs nowhere in the store (no record text,
    #: no participant; exact match, a 3+ letter prefix of a stored word counts as present) adds
    #: the reader note "<name> is not mentioned in the memories". Off: unchanged.
    entity_check: Literal["off", "note"] = "off"
    #: I63: with a known asker (``perspective_asker`` or the harness's persona / asker), the
    #: context opens with "You are the assistant. The user is <asker>. Memory lines are
    #: labelled with their speaker." No asker (LoCoMo): nothing is added, none is invented.
    user_header: Literal["off", "on"] = "off"
    #: A03: a typed query contract built from the question (``core/query_contract``): target
    #: subjects, predicate, answer type (person / place:city|country|... / date|time|year /
    #: duration / count / quantity / title / name / yes-no / reason / description), time scope,
    #: expected cardinality (one / many / count) and request kind (recall / inference /
    #: recommendation). ``heuristic`` = rules only (English surface cues, no model, no
    #: vocabulary of any dataset); ``llm`` = the rules first, and ONE ``plan@contract`` call
    #: only when the rules are unsure of the answer type (the call may refine, never blank, the
    #: contract). ``off`` (default): nothing is built. The contract is always recorded in the
    #: forensics (``query_contract``); ``query_contract_use`` says where else it goes.
    query_contract: Literal["off", "heuristic", "llm"] = "off"
    #: A03: where a built contract is used. ``header`` = one lead line in the reader's context
    #: ("Answer type: place:city; expected: one."), only when the answer type is known;
    #: ``rerank`` = the same hint appended to the reranker's query (never to the embedder's).
    query_contract_use: list[Literal["header", "rerank"]] = Field(
        default_factory=lambda: ["header"]
    )
    #: I64: with a session id, a record injected in the last ``reinjection_window`` replies of
    #: that (namespace, session) has its relevance multiplied by
    #: ``1 - penalty * uses / window`` (floor ``1 - penalty``). 0 = off. Without a session id
    #: nothing is tracked or penalised, so independent QA is untouched.
    reinjection_penalty: float = Field(default=0.0, ge=0.0, le=1.0)
    reinjection_window: int = Field(default=5, ge=1)
    #: I42: under an as-of read (``as_of``) with a question that resolves to a subject, a
    #: superseded record that was current at that time is admitted as history only when it is
    #: about a target (or carries no subject tags); other subjects' history stays out. Needs
    #: ``perspective_mode != off``. Off: unchanged.
    perspective_as_of_subject: bool = False
    #: I50: with ``profile_slots_header``, the slots block becomes a per-subject card: the live
    #: ``kind:state`` facts rolled up per (owner, subject) (``sub:`` tags, else the entity;
    #: cardinality one = the latest, many = a list), injected only for the subjects the
    #: question's resolved persons match. Off: the per-entity slots block, unchanged.
    profile_subject_card: bool = False
    list_vote_depth: int = Field(default=100, ge=1)
    list_vote_top_k: int = Field(default=30, ge=1)
    list_pool: int = Field(default=3, ge=1, le=10)
    window_full_hits: int | None = Field(default=10, ge=0)
    #: GR-6 (Graphiti node search): an RRF leg of the records that mention the entity
    #: nodes best matching the question (cosine on embedded entity names + text match,
    #: fused). Needs ``graph.entity_embeddings`` and associative entity nodes.
    graph_node_search: bool = False
    #: G-16 (SimpleMem symbolic leg): an RRF leg of records whose location / topic /
    #: person view tags (``consolidation.mine_multiview``) share words with the question.
    view_tag_leg: bool = False
    #: G-22 (SimpleMem pyramid retrieval): None = off; N = the best N hits keep their
    #: full text and the rest are shown as their one sentence most like the question,
    #: before the budget fit, so more distinct evidence fits.
    gist_after: int | None = Field(default=None, ge=0)
    #: N31 (Memori session summaries, extractive): a read header with, for the
    #: sessions of the first hits, the two sentences most like the question. It does
    #: not hide the session's turns from the read. Off: no header.
    session_digest: bool = False
    #: N54 (EverMemOS episode/fact pairing): a mined fact in the hits gives its slot,
    #: rank and score to its live source turns, so facts never displace raw turns.
    facts_to_sources: bool = False
    #: N55 (EverMemOS per-source quotas): the most records of each memory type the
    #: read keeps (``{"semantic": 3}``); types not listed are uncapped. Empty: unchanged.
    type_quotas: dict[str, int] = Field(default_factory=dict)
    #: C2 (replay the last exchanges): a read header with the namespace's last N live
    #: episodic turns, oldest first (``RECENT_SHARE`` of the budget); the in-flight
    #: question is left out and the turns are not hidden from the read. 0: off.
    recent_exchanges: int = Field(default=0, ge=0, le=20)
    #: C6 (per-leg score floors): ``{"vector": 0.3, "lexical": 1.0}`` drops a leg's
    #: hits below its floor before fusion (vector = cosine, lexical = BM25). Empty: off.
    leg_min_scores: dict[str, float] = Field(default_factory=dict)
    #: C7 (labelled sections): when read headers lead the context, the retrieved part
    #: gets a caption too (``RETRIEVED_CAPTION``). Off: unchanged.
    section_captions: bool = False
    #: N33 (Memori, Mem0): the BM25 leg's RRF weight for a question of at most
    #: ``SHORT_QUERY_WORDS`` content words. None: unchanged.
    short_query_lexical_weight: float | None = Field(default=None, ge=0.0)
    #: N59 (Dakera name boost): an RRF leg of raw turns naming the question's proper
    #: nouns and years beyond the speakers (``temporal_query.entity_leg``).
    entity_leg: bool = False
    #: R3-05a (LIB-34, R-08): the entity leg over RESOLVED entity ids (the associative
    #: entity index): Mem0 boost, max over the matched entities, capped, with hub damping
    #: ``1/(1+0.001 (degree-1)^2)``. Joins fusion as the ``entity_v2`` leg. Off: unchanged.
    entity_leg_v2: bool = False
    #: R3-14 (LIB-22, R-24, S6-3): as-of reads through the three-valued time view: ``true_at``
    #: judges event time (valid_from/valid_to), ``known_at`` recorded time, undecidable
    #: records are kept (flagged) rather than dropped. Applies to the read's date allow-list
    #: when an as-of or a ``recorded_before`` bound is active. Off: the date filter as before.
    timeview_v2: bool = False
    #: R3-10 (LIB-24, retrieve step 15): neighbour windows after rerank through
    #: ``read.neighbours.expand``: budget-aware, viewer-checked, overlapping windows merged.
    #: Replaces the G2c compose expansion (turn windows only). Off: unchanged.
    neighbours_v2: bool = False
    #: N63 (EverMemOS multi-query, by rules): the question rewritten as a statement
    #: ("When did Ana go camping?" -> "Ana go camping") joins the search as a probe.
    statement_probe: bool = False
    #: N40 (MemMachine): when the question names a speaker, "Name: <core terms>" (the
    #: stored-turn form) joins the search as a vector probe.
    speaker_probe: bool = False
    recency_leg: bool = False
    #: H12: cap the total of all lead blocks (cards, graph facts, entity summaries,
    #: profile, count timeline) at this share of the read budget, dropping the
    #: lowest-priority blocks first, so raw evidence keeps its budget. None = no cap.
    lead_budget_share: float | None = Field(default=None, gt=0.0, le=1.0)
    #: W19 (plan v3.2, F3): derived records (mined facts, cards, summaries) found by
    #: the routed search no longer take search slots from raw (episodic) turns: the
    #: search widens by their number, so every raw turn hit of the read without them
    #: stays, with its replay window. The 4K budget was not binding on LoCoMo (about
    #: 1.7K tokens used); slots were. Off: byte-identical.
    raw_turn_floor: bool = False
    #: F5 (plan v3.2): a verbatim question (``is_verbatim``: "what did X say about
    #: ...", "exact words") gets no read header (cards, profile, graph facts, entity
    #: summaries) and, with ``cards: header``, no mined fact either: it reads raw
    #: turns only (LoCoMo quote questions fell 92 -> 68-74 under every header).
    #: Off: byte-identical.
    verbatim_raw_only: bool = False
    #: W3 (plan v3.2, G02): attach an evidence-sufficiency signal to every assembled
    #: context (``AssembledContext.evidence``, ``core/evidence.py``): top score, spread,
    #: distinct days, the asked answer type and whether a top candidate holds one, and
    #: ``weak``. Reported only; the context is unchanged. Off: ``evidence`` is None.
    evidence_signal: bool = False
    #: W3: with ``evidence_signal``, a best evidence score below this marks the read
    #: ``weak`` (scores are on the search's own scale). None: only a missing answer
    #: type makes it weak.
    evidence_weak_below: float | None = Field(default=None, ge=0.0)
    #: W3 step 2 (plan v3.2): with ``evidence_signal``, a weak read opens its volatile
    #: part with a one-line note that memory holds no clear record answering the
    #: question (``constants.WEAK_EVIDENCE_LINE``). Off: unchanged.
    evidence_line: bool = False
    #: F4 (plan v3.2): with ``cards: header``, show the cards header (and let mined
    #: facts into the read) only when a raw-turns-only search gives *weak* evidence
    #: (the W3 signal, with ``evidence_weak_below``); strong raw evidence reads raw turns
    #: only, like a gated question. Replaces question-shape gates (cards helped weak
    #: conversations and hurt strong ones on LoCoMo). One extra local search per read.
    #: The threshold is calibrated on one dataset and checked on another (U5). Off:
    #: byte-identical.
    cards_when_weak: bool = False
    #: T10 (plan v3.2): in a replay read, the best search hit's window of turns comes
    #: first (in time order), then the other windows in time order, so the reader meets
    #: the best evidence before the context around it. Off: all turns in time order.
    evidence_first: bool = False
    #: N01 (plan v3.2, MAB TAM): presentation order of the retrieved (volatile) records.
    #: ``relevance`` (unchanged): score order. ``recorded``: always time order.
    #: ``recorded_if_shared_key``: time order only when two records state the same
    #: keyed fact (entity + attribute), so the newer value reads last. An ordering
    #: question under ``order_by_time_for_ordering`` is time-ordered either way.
    present_order: Literal["relevance", "recorded", "recorded_if_shared_key"] = "relevance"
    #: I17 (``core/latest_wins.py``): when the retrieved evidence states one topic at
    #: several times (a keyed fact, or raw turns by one speaker with overlapping content
    #: words), mark the newest statement ``[latest]`` and each older one ``[earlier
    #: statement; a later one on this topic is dated D]``, keeping the older value as
    #: history. ``annotate_recent_first`` also gathers those records, newest first.
    #: Records tagged ``disputed`` get a disputed mark and no ordering claim. Off: unchanged.
    latest_wins: Literal["off", "annotate", "annotate_recent_first"] = "off"
    #: I17: overlap coefficient (shared / smaller set of content words) at which two raw
    #: turns by one speaker count as one topic. Keyed records match on their key instead.
    latest_wins_min_overlap: float = Field(default=0.5, gt=0.0, le=1.0)
    #: N13 (plan v3.2, Mnemon ``focused``): a retrieved record of six or more lines is
    #: shown as its two lines that best match the question, each with its next line,
    #: cuts marked "…" (``core/excerpt.py``). Never for a verbatim question. The stored
    #: record is unchanged. Off: whole records.
    focused_excerpt: bool = False
    #: W10 (plan v3.2, Mnemon ``fillPerGroup``): at most this many raw-turn hits from any
    #: one session (episodic gap-split sessions, else the calendar day), drawn from a
    #: 4x wider search, so evidence spread over many sessions reaches the read. In
    #: replay each hit brings its window. None: unchanged (no cap).
    session_cap: int | None = Field(default=None, ge=1)
    #: N21 (plan v3.2, RAGDefender): collapse each dense near-duplicate cluster among
    #: the search candidates (3+ records sharing ``concentration_jaccard`` of their
    #: content words) to its best member, tagged ``concentrated:<n>``
    #: (``core/concentration.py``). PoisonedRAG planted sets: 257 / 300 clustered at
    #: 0.2; LoCoMo 10-turn windows: 8 / 581. Off: unchanged.
    concentration_filter: bool = False
    concentration_jaccard: float = Field(default=0.2, gt=0.0, le=1.0)
    #: N03 (plan v3.2, Mnemon ``feedback``): pseudo-relevance feedback. Up to five content
    #: words that two or more of the first-round top five hits share and the question
    #: lacks join the search as one more RRF probe (one extra local search). Off:
    #: unchanged.
    prf_expansion: bool = False
    #: G34 (plan v3.2): a multi-part question ("..., and also where did he move?") is
    #: split on discourse markers and each part joins the search as an RRF probe.
    #: Off: unchanged.
    multi_intent_split: bool = False
    #: N04 (plan v3.2, EverOS / Honcho): when the first search's evidence is weak (W3
    #: signal with ``evidence_weak_below``), the names and dates its top three hits
    #: mention and the question lacks seed a second search over a doubled pool. Off:
    #: unchanged.
    second_round: bool = False
    #: R2-1 (bridge hop): after the first search, up to three key noun phrases of the top
    #: three hits that the question lacks ("home country") join the question subject as
    #: one extra search (``bridge_hop_top_k`` hits), fused in as the ``bridge`` leg. One
    #: hop at most; the phrases are recorded in ``search_forensics`` ("bridge_phrases").
    #: Off: unchanged.
    bridge_hop: bool = False
    bridge_hop_top_k: int = Field(default=10, ge=1)
    #: R2-1b: when the hop fires. ``always`` (default): every read. ``cue``: only when the
    #: question describes its answer's entity ("home country", "where ... move from",
    #: "the studio that X opened", "her son's"). ``weak``: only when the first pass found
    #: one confident anchor without support (second-best raw reranker score under
    #: ``bridge_hop_weak_threshold``). ``cue_or_weak``: either. The decision is recorded
    #: in ``search_forensics`` as ``bridge_gate``.
    bridge_hop_gate: Literal["always", "cue", "weak", "cue_or_weak"] = "always"
    #: Second-best raw reranker score below which the first pass counts as weak. 0.4 was
    #: chosen offline on the dev forensics (second-best score of the 233 screen questions:
    #: 31 under 0.4, 28 under 0.35, 40 under 0.5).
    bridge_hop_weak_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    #: I67 (opt-in agentic read): after the normal first pass (step 0, unchanged), the
    #: ``sufficiency`` role (else ``plan``) sees the question and a compact view of the
    #: evidence and returns ONE structured action: ``answer_ready``, ``search`` (a query)
    #: or ``search_person_time`` (a person and/or a time). Each search goes through the
    #: same gated read search of the same namespace; its new hits are RRF-fused and
    #: appended under the same token budget. Not native tool calling. Off: no call,
    #: byte-identical. Every step is kept in ``search_forensics()["agentic_steps"]``.
    agentic: bool = False
    #: Most action steps per question (each is one LLM call and at most one extra search).
    #: The schema caps it at 5.
    agentic_max_steps: int = Field(default=2, ge=1, le=5)
    #: When the loop fires. ``always``; ``multi_hop`` (a generic surface heuristic:
    #: several names, a relation word such as both / same / before / after, a possessive
    #: chain, a bridge cue); ``decider`` (the ``needs_more_evidence`` task of the
    #: OpenDecider port, falling back to the ``multi_hop`` heuristic when the decider is
    #: off, not listed in ``decider_tasks``, unsure or failing).
    agentic_trigger: Literal["always", "multi_hop", "decider"] = "multi_hop"
    #: Hits one extra search keeps (before de-duplication against the evidence).
    agentic_top_k: int = Field(default=8, ge=1, le=20)
    #: Most NEW records the loop adds over all steps.
    agentic_max_new: int = Field(default=6, ge=1, le=20)
    #: Share of the read budget reserved for the step-0 hits (taken in their order). New
    #: records may displace only step-0 records after that share, last first, and only to
    #: fit themselves.
    agentic_first_share: float = Field(default=0.6, ge=0.0, le=1.0)
    #: A02 (read-time): ``on`` makes ONE bounded structured call per triggered read
    #: (``extract@events``, the ``extract`` role) over the evidence already in context for
    #: candidate event mentions (actor, action, object, status planned / done / cancelled, exact
    #: source span, the quoted time phrase). Code validates every span, resolves the EVENT time
    #: from the quoted phrase (never from the mention date), links repeated mentions into one
    #: event only on identity evidence (the later line says so, same actor, no conflicting
    #: interval) and shows the reader a compact ledger block: event, state, event time, mention
    #: times, source lines. Raw turns stay authoritative; nothing is stored. Any failure, no
    #: valid mention or an unbound ``extract`` role keeps the read unchanged. Off (default): no
    #: call, byte-identical. Forensics: ``search_forensics()["event_ledger"]``.
    event_ledger: Literal["off", "on"] = "off"
    #: When the ledger call fires: ``event_questions`` (default; a date / duration / count
    #: question, or one that uses the grammar of plans and outcomes) or ``always``.
    event_ledger_trigger: Literal["event_questions", "always"] = "event_questions"
    #: I28: the optional decider for read-path decisions. ``heuristic`` (default) = the
    #: existing regexes and rules, byte-identical. ``opendecider`` asks OpenDecider-nano
    #: (``[decider]`` extra) at the decision points named in ``decider_tasks``; a decision
    #: below ``decider_min_confidence``, or any failure, keeps the heuristic answer. Every
    #: decision is recorded in ``search_forensics`` under ``decisions``.
    decider: Literal["heuristic", "opendecider"] = "heuristic"
    decider_model: str = "manjunathshiva/opendecider-nano"
    #: CPU by default: the GPU is usually shared with the reader.
    decider_device: Literal["cpu", "cuda", "mps", "auto"] = "cpu"
    decider_tasks: list[
        Literal["list_mode", "bridge_hop", "about_target", "needs_more_evidence"]
    ] = Field(default_factory=lambda: ["list_mode", "bridge_hop"])
    #: I29: whether retrieved memories are injected at all. ``off`` (default): always, as
    #: today. (The ``decider`` form measured LoCoMo 81.6 -> 59.2 and is retired, G-22.)
    #: ``store_calibrated``: the
    #: namespace's own off-topic score level is learned lazily from a fixed set of generic
    #: off-topic probes (``core/relevance_probes.py``) and a message passes only when its RAW
    #: top score (vector cosine, or the reranker's raw score) clears that level by
    #: ``relevance_gate_margin_sd`` standard deviations; otherwise an empty, abstained context.
    relevance_gate: Literal["off", "store_calibrated"] = "off"
    #: I74: the personal-reference bypass of the gate. A message that names the store's
    #: own people / entities (a participant of the namespace, or a capitalised name present
    #: in the store, same rules as I60 ``entity_check``: exact, or a 3+ letter prefix) needs
    #: memory, so the gate is skipped and the memories are injected as normal. ``none``: no
    #: bypass. ``named`` (default, only acts when the gate is on): names only.
    #: ``named_or_first_person``: also "I / my / me" (separate option: OP-Bench baiting
    #: probes are phrased in the first person).
    relevance_gate_bypass: Literal["none", "named", "named_or_first_person"] = "named"
    #: I29/I37: margin, in standard deviations of the off-topic probes' top raw scores, above
    #: their 95th percentile. A FIXED portable default (one value for every store, embedder
    #: and reranker); NOT tuned on any benchmark. Raise it for a stricter gate.
    relevance_gate_margin_sd: float = Field(default=1.0, ge=0.0, le=10.0)
    #: Which raw score the calibrated gate reads. ``auto``: the reranker's raw score when a
    #: reranker is configured, else the vector cosine. ``vector`` / ``rerank`` force one.
    #: ``any``: pass when any available leg clears its own threshold.
    relevance_gate_leg: Literal["auto", "vector", "rerank", "any"] = "auto"
    #: Candidates (top of the vector leg) the gate scores per message; the reranker leg
    #: rescored only these.
    relevance_gate_candidates: int = Field(default=10, ge=1, le=100)
    #: Recalibrate when the namespace holds more than this multiple of the record count it
    #: was calibrated on.
    relevance_gate_regrow: float = Field(default=2.0, gt=1.0)
    #: R3-11 (LIB-12, ER-23; wired in ``read/pipeline/gate.py``): ``True`` replaces the
    #: all-or-nothing calibrated gate by the five-state gate (answerable / conflicting / weak /
    #: missing / bypass) scored per candidate record against the store's own off-topic level.
    #: Needs ``relevance_gate: store_calibrated`` NOT to be set (it is a replacement, not a
    #: layer). Off (default): the I29/I74 path, unchanged. A named-entity bypass applies only
    #: when the named entity is in the store AND a candidate scores above the weak threshold.
    gate_v2: bool = False
    #: R3-11: what a ``weak`` gate decision does under ``gate_v2``. ``keep``: the weak
    #: records pass, flagged low-confidence in the forensics. ``abstain``: an empty, abstained
    #: context (the same as ``missing``).
    gate_v2_weak: Literal["keep", "abstain"] = "keep"
    #: R3-11: what makes a named reference real under ``gate_v2`` (an unknown name never
    #: bypasses). ``hit`` (default): the name resolves in the store AND a candidate record
    #: mentions it or scores above the weak threshold. ``scored``: stricter, a candidate must
    #: score above the weak threshold (a name in a question about something the store never
    #: stated then no longer waves its records through).
    gate_v2_named: Literal["hit", "scored"] = "hit"
    #: R3-12 (LIB-43, R-22 B; wired in ``read/pipeline/deep.py``): ``True`` runs the deep
    #: tier after the first pass: rules first (zero calls when every obligation is met), then
    #: one sufficiency check, at most one rewrite and one extra retrieval, never a loop. Model
    #: calls go through the bound ``sufficiency``/``plan`` role and are counted. Needs
    #: ``obligations`` for its rule check (it derives the report itself when that is off).
    deep_v2: bool = False
    #: R3-16 (LIB-15, ER-19; wired in ``read/pipeline/deep.py``): ``True`` derives evidence
    #: obligations (joins, durations, lists, counts, latest) from the typed query contract
    #: and checks them against the read's records; the report goes to the forensics and the
    #: wiretap and drives the deep tier's rule check. No model call.
    obligations: bool = False
    #: I30: judge ``assembly.theta_abstain`` and ``assembly.relative_floor`` on the RAW
    #: reranker scores (before min-max), so they work under rerank (the min-max makes the top
    #: candidate 1.0, which kills ``theta_abstain``). Off (default): the composite scores,
    #: as before. Raw scores are model-specific; prefer ``relevance_gate: store_calibrated``
    #: for a portable cut.
    abstain_on_raw: bool = False
    decider_min_confidence: float = Field(default=0.5, ge=0.5, le=1.0)
    #: I52: graded-sensitivity read bar. ``off`` (default): every retrieved memory is
    #: eligible as today. ``on``: a record graded ``medium`` or ``high`` at write time
    #: (``write.sensitivity``; the W16 ``sensitive:*`` tags count as a grade too) is injected
    #: only when the question is about its category, names its subject (medium) or shares
    #: enough content words with it (1 for medium, 2 for high). ``decider``: as ``on``, and
    #: when the question names no sensitive topic the ``sensitivity_scope`` decider task may
    #: open the gate (it never closes it). See ``core/sensitivity.py``.
    sensitivity_gate: Literal["off", "on", "decider"] = "off"
    #: I48: records tagged ``src:inferred`` (``write.inferred``) are used only once they
    #: have ``inferred_min_support`` distinct supporting user turns. ``off`` (default): used
    #: as any record.
    inferred_gate: Literal["off", "on"] = "off"
    inferred_min_support: int = Field(default=2, ge=1)
    #: CPU speed (I38). Intra-op threads of the model; 0 = the physical cores. With
    #: ``decider_workers`` > 1 each worker gets ``cores // workers``.
    decider_threads: int = Field(default=0, ge=0)
    #: ``torch`` (default, fp32 as the model was evaluated) | ``onnx`` (the published 8-bit
    #: ONNX build through ONNX Runtime, CPU only; needs ``onnxruntime``).
    decider_backend: Literal["torch", "onnx"] = "torch"
    #: Concurrent decision calls (a bounded thread pool). 1 keeps the engine path as it is.
    decider_workers: int = Field(default=1, ge=1)
    #: torch weights dtype. ``float32`` (default) is how the model was evaluated;
    #: ``bfloat16`` was about 1.5x faster on this 16-core CPU and agreed on 300 of 300
    #: decisions (max probability difference 0.0075).
    decider_dtype: Literal["float32", "bfloat16"] = "float32"
    #: N06 (plan v3.2, EverMemOS clusters / HyperMem hyperedges, read-time variant): the
    #: embedding neighbourhoods of the top two hits, across sessions, join the search
    #: as RRF legs (two embeds and two vector queries per read). Off: unchanged.
    cluster_expand: bool = False
    #: E04: image evidence for a question that needs what a picture shows (requires
    #: ``ingest.assets: on`` at write time). ``cached``: use only evidence already computed
    #: (no network, no model). ``fetch``: also download the turn's own referenced URI once and
    #: describe it through the vision port (needs ``ingest.asset_dir``). Off: unchanged.
    asset_evidence: Literal["off", "cached", "fetch"] = "off"
    #: E04: at most this many assets are resolved for one read.
    asset_max_per_read: int = Field(default=2, ge=1)
    #: E03: public knowledge for an invited inference / recommendation question. ``cache``:
    #: only cached results (no network). ``web``: also call the provider, within
    #: ``external_max_calls``. Only generic topic words leave the process. Off: unchanged.
    external_evidence: Literal["off", "cache", "web"] = "off"
    #: E03: ``none`` (a provider can still be injected with ``Engine.set_external_provider``)
    #: or ``http`` (``MEMSPINE_EXTERNAL_SEARCH_URL`` / ``_KEY`` / ``_HEADER`` from the env)
    #: or ``wikipedia`` (keyless MediaWiki search + REST summary; generic query only).
    external_provider: Literal["none", "http", "wikipedia"] = "none"
    #: E03: network calls the engine may make in its lifetime (cache hits are free).
    external_max_calls: int = Field(default=20, ge=0)
    external_max_results: int = Field(default=3, ge=1, le=10)
    #: E03: directory of the persistent query cache (JSON). None: in memory.
    external_cache_dir: str | None = None

    #: R3-13 / LIB-45: the external public-knowledge block of ``read/external.py`` on top of
    #: the ``external_evidence`` opt-in (which must be ``cache`` or ``web``; this flag never
    #: enables lookups by itself). The planner (a generic or explicitly public question,
    #: within the call cap) decides whether to call; snippets are filtered (freshness, https,
    #: dedupe, per-domain cap) and packed into a separate labelled ``public`` block of at
    #: most ``external_v2_budget_tokens``; it is never deposited. Off: the E03 block is
    #: unchanged.
    external_v2: bool = False
    external_v2_budget_tokens: int = Field(default=300, ge=1)
    #: R3-15 / LIB-19 (ER-31 B): cache whole read results, keyed by scope, viewer, query,
    #: config, model ids and the store / erasure generations. Every hit is revalidated
    #: (POL-1) against the live store before it is returned; a partial revalidation
    #: recomputes. Off: no cache is consulted or filled. A read that names a ``session_id``
    #: (stateful read ledger) is never cached.
    cache: ReadCacheConfig = Field(default_factory=lambda: ReadCacheConfig())
    #: R3-15: run the independent retrieval legs (vector, lexical, metadata, encoder, probes)
    #: with ``asyncio.gather``. Result order is the sequential order. Off: sequential.
    parallel_legs: bool = False

    #: R3-01 (LIB-47, R-01/R-02, ER-29): the v2 read entry. On: the question is NFKC-normalised
    #: (whitespace collapsed), the viewer is bound to the principal (``viewer``), and a depth tier
    #: (fast / standard / deep, with its generative-call and rerank-pool caps) is chosen from
    #: ``tiers_v2_depth`` and the typed contract; the tier is recorded (wiretap, forensics) and
    #: served to later stages. Off (default): the read entry is unchanged.
    tiers_v2: bool = False
    #: R3-01: the default depth under ``tiers_v2``; ``auto`` picks deep only for join / list /
    #: history shapes when a worker model is bound, else the first allowed tier (fast).
    tiers_v2_depth: Literal["auto", "fast", "standard", "deep"] = "fast"
    #: R3-02 (LIB-16, R-03): build the typed query contract by rules (answer type, shape, time view,
    #: scope, targets, attributes; unknown fields stay unknown) and render its ``[contract] ...``
    #: header as the first lead line of the context. Independent of ``query_contract`` (the A03
    #: arm, screened +15/-7 HOLD), which stays available; with both on both headers appear.
    #: Off (default): unchanged.
    contract_rules: bool = False
    #: R3-03 (LIB-23, R-04/R-05): the rule router. On: the legs come from a ``RoutePlan`` over the
    #: layers the store actually has populated (L0 turns, L1 facts, L2 sessions, L3 graph, L5
    #: profile); the optional entity / graph / episode legs that the plan leaves out or skips
    #: (layer empty or disabled, service missing, not asked for by a rule) do not run, even when
    #: their own flag is on, and every skip is recorded. Core legs (vector, lexical, temporal)
    #: are unchanged. LLM rewrites stay deep-tier only. Off (default): unchanged.
    router_rules: bool = False

    @model_validator(mode="after")
    def _header_shares_leave_room(self) -> ReadConfig:
        """A-9: the active read headers' shares must leave budget for the read itself."""
        shares = (
            (
                self.cards_budget_share
                if self.cards == "header" or self.cards_include_edges or self.entity_summaries
                else 0.0
            )
            + (self.profile_budget_share if self.profile_header else 0.0)
            + (self.count_budget_share if self.count_timeline else 0.0)
        )
        if shares >= 1.0:
            raise ConfigError(
                "the active read header shares (read.cards_budget_share, "
                "read.profile_budget_share, read.count_budget_share) must sum to < 1, "
                f"got {shares:g}"
            )
        return self
