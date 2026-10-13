"""Config area: write (F-07 split of the former ``config/schema.py``)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WriteConfig(BaseModel):
    """Write-time governance tags (I48, I52, I53). Every key is off by default: a record is
    stored exactly as it was before. The tags are labels only (never the text)."""

    model_config = ConfigDict(extra="forbid")

    #: I52: grade each record none/low/medium/high with a category (``core/sensitivity.py``)
    #: and tag it ``sens:<grade>`` / ``sensc:<category>``. ``heuristic``: the fixed lexicon.
    #: ``decider``: the lexicon, raised by the decider ``sensitivity`` task when it is sure
    #: (``read.decider: opendecider``); the decider can raise a grade, never lower it.
    sensitivity: Literal["off", "heuristic", "decider"] = "off"
    #: I53: ``session``: ``write_messages`` tags each turn ``participant:<name>`` for every
    #: speaker of the call (the turn's own speaker first; a turn's ``name``/``speaker`` key,
    #: else its role), and a derived record (mined fact, summary) inherits the union of its
    #: parents' participants and the strictest visibility. ``viewer`` reads then filter.
    participants: Literal["off", "session"] = "off"
    #: I48: tag engine-derived facts ``src:inferred`` (mined / reflected / consolidated, or
    #: assistant-proposed with no user turn behind them), cap their trust at
    #: ``inferred_trust_cap`` and record the distinct supporting user turns as ``support:<id>``.
    #: A mined fact whose words are at least ``inferred_explicit_overlap`` contained in one user
    #: turn is that user's statement, not an inference, and is left alone.
    inferred: Literal["off", "on"] = "off"
    inferred_trust_cap: float = Field(default=0.4, ge=0.0, le=1.0)
    inferred_explicit_overlap: float = Field(default=0.6, gt=0.0, le=1.0)
    inferred_support_overlap: float = Field(default=0.3, gt=0.0, le=1.0)

    #: W1 (adaptive-window write, call 1; ``docs/ENGINE_COMPARISON_AND_WRITE_PATH_DESIGN.html``
    #: "Decided design"). ``off`` (default): no window call, a write is unchanged. ``facts``:
    #: one structured ``window_extract`` call per window yields atomic, dated, grounded facts
    #: (semantic layer, parents = the source turns). ``facts_graph`` adds the entity-edge
    #: section (handed to the sleep stage ``extract_graph``, the call-3 wiring); ``all`` adds
    #: the episode-fragment section. Needs an LLM role ``window_extract`` and semantic memory.
    window_extract: Literal["off", "facts", "facts_graph", "all"] = "off"
    #: A window closes at the first of: a topic shift, this many turns, ``window_max_tokens``
    #: (prompt tokens, 4 chars each), ``window_idle_s`` of silence, or the session's end.
    window_max_turns: int = Field(default=6, ge=1, le=64)
    window_max_tokens: int = Field(default=1500, ge=64)
    #: Topic shift: the similarity of two consecutive turns (their embeddings) falls below the
    #: store's running ``mean - k * sd`` of that similarity (per namespace, no LLM). k = 2 is
    #: the usual two-sigma outlier line, not tuned. 0 turns the topic boundary off.
    window_topic_k: float = Field(default=2.0, ge=0.0)
    #: Seconds without a write in a namespace before the open window is cut (async mode). 0: never.
    window_idle_s: float = Field(default=120.0, ge=0.0)
    #: ``auto``: split a window's call into facts+edges / episode after repeated parse failures;
    #: ``always`` / ``never``. Only matters when more than one section is enabled.
    window_split: Literal["auto", "always", "never"] = "auto"
    #: True (live): window calls run in a background task after the write returns (idle
    #: debounced). False (batch / eval): they run only at ``Engine.flush_windows()``,
    #: ``close_session`` or ``sleep``. The result is identical; only the timing differs.
    window_async: bool = True
    #: Cache each call by (model, prompt version, sections, window text hash), so a re-run on
    #: the same history is free (needs the engine cache; in memory unless a durable one is set).
    window_cache: bool = True

    #: F-16a (M-09, G-08, POL-2): the lineage table and the single derived-write door. ``True``:
    #: every derived record written through ``derive/write.py`` gets one lineage edge per parent
    #: (``support`` unless the site names a role), its trust capped at the least-trusted parent, a
    #: ``layer:`` and ``deriv:`` tag, and is refused when it has no parent at all. ``False``
    #: (default): the old write path exactly, and the table is never written.
    lineage_table: bool = False

    #: O4-02 (O-07..O-09, M-14; LIB-40): the real call 2 at session close. ``True``: when a session
    #: closes, ONE structured call (role ``session_close``, else ``summarize``, else ``extract``)
    #: reads the session's turns and returns a summary, episode narratives and proposed profile
    #: ops; code validates every item (citations, reserved fields, derived time), stitches episodes
    #: onto adjacent stubs (deterministic) and writes the survivors through the derived-write door
    #: (parents = the cited turns). ``False`` (default): the stub (a marker only).
    session_close_v2: bool = False
    #: Prompt budget (tokens, 4 chars each) of the call-2 plan; turns beyond it are deferred.
    session_close_budget: int = Field(default=6000, ge=256)
    #: O4-07 (O-18..O-20, M-20; LIB-39): the profile card (L4/L5), opt-in. ``True``: the profile
    #: ops call 2 proposes go through the guarded ``apply_ops`` (shape, evidence, stance, trust,
    #: staleness, kappa_P bound, EMA drift) and the card is kept in the event log (marker
    #: ``profile_card_v2``); the sleep stage ``reflect_profile`` then invalidates entries whose
    #: support was forgotten and reports the reflection plan. Needs ``session_close_v2`` to
    #: receive ops. ``False`` (default): no card, call-2 profile ops are only counted.
    profile_card_v2: bool = False
    #: kappa_P: the L5 profile bound (maximum card entries) and the card's character budget.
    profile_kappa_p: int = Field(default=24, ge=1)
    profile_max_chars: int = Field(default=2400, ge=64)
    #: O-19: ``inferred`` ops are rejected unless this is on (stated-only card by default).
    profile_allow_inferred: bool = False
    #: EMA gain of the preference drift ``P_{t+1} = alpha p_t + (1 - alpha) P_t``.
    profile_alpha: float = Field(default=0.3, gt=0.0, le=1.0)


class IngestConfig(BaseModel):
    """E04 attachment handling at write time. Off by default: a message's ``attachments`` are
    ignored exactly as before. On: each attachment's identity (source turn, original URI,
    source caption, availability) is registered and the record is tagged ``asset:<id>``;
    nothing is downloaded at ingest."""

    model_config = ConfigDict(extra="forbid")

    assets: Literal["off", "on"] = "off"
    #: Cache directory (gitignored in the eval harness): ``registry.sqlite`` and the
    #: downloaded bytes under ``blobs/`` by content hash. None: registry in memory, no blobs
    #: (so ``read.asset_evidence: fetch`` is refused at start).
    asset_dir: str | None = None
    asset_max_bytes: int = Field(default=5_000_000, ge=1)
    asset_timeout_s: float = Field(default=15.0, gt=0.0)
    asset_allowed_mime: list[str] = Field(
        default_factory=lambda: ["image/jpeg", "image/png", "image/gif", "image/webp"]
    )
    #: ``none``: no description is produced (text-only). ``ollama``: a local vision model.
    asset_vision: Literal["none", "ollama"] = "none"
    asset_vision_model: str = "qwen2.5vl:3b"
    asset_vision_url: str = "http://localhost:11434"
    asset_vision_timeout_s: float = Field(default=120.0, gt=0.0)


class DataShapeConfig(BaseModel):
    """I25: facts about the data the engine will serve, declared by the data adapter (or
    the deployer). Only ``data_profile: auto`` reads them; an undeclared fact (None /
    ``unknown``) selects no preset."""

    model_config = ConfigDict(extra="forbid")

    has_timestamps: bool | None = None
    has_question_date: bool | None = None
    speaker_kind: Literal["named", "user_assistant", "single_author", "unknown"] = "unknown"
    turn_length: Literal["short", "medium", "long", "unknown"] = "unknown"
    history_size: Literal["small", "medium", "large", "unknown"] = "unknown"
    language: str | None = None
    abstention_possible: bool | None = None
