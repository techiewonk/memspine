"""Source-origin taxonomy (RM-48), distinct from sender, assertion owner and trust.

An origin says *what kind of source record* evidence came from. It never says
who sent it, whose assertion it is, or how far it is trusted: those are separate
fields on :class:`SourceProvenance`, and no origin implies any of them. Tool,
reflection, observation and summary content does not, on its own, establish
that an external operation succeeded or that a claim is independently
corroborated. Canonical adapters (BI-02/BI-05) own the runtime mapping; this
module is pure vocabulary and shape validation.
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

SourceOrigin = Literal[
    # Origins accepted by the first foundation.
    "raw_message",
    "document",
    "attachment_text",
    # RM-48 additions selected 2026-10-22.
    "tool_output",
    "structured_record",
    "agent_observation",
    "reflection",
    "transcript",
    "ocr",
    "caption",
    "external_catalogue",
    "email",
    "calendar_event",
    "web_page",
    "form_input",
    "profile_import",
    "code_repository",
    "system_event",
]

SOURCE_ORIGINS: frozenset[str] = frozenset(get_args(SourceOrigin))

#: Text derived from an asset (OCR, caption, transcript, extracted attachment
#: text) must keep a link to that asset.
DERIVED_TEXT_ORIGINS: frozenset[str] = frozenset(
    {"attachment_text", "ocr", "caption", "transcript"}
)

#: Content produced by the system or an agent about itself. It is attributable
#: provenance, never independent corroboration or proof of external execution.
SYSTEM_PRODUCED_ORIGINS: frozenset[str] = frozenset(
    {"tool_output", "agent_observation", "reflection", "system_event"}
)

#: Web pages enter only through an explicit fetch, never ambient crawling.
EXPLICIT_FETCH_ORIGINS: frozenset[str] = frozenset({"web_page"})


class SourceProvenance(BaseModel):
    """Separate provenance axes; filling one never fills another.

    ``sender_id`` is the authenticated envelope sender, ``assertion_owner_id``
    the principal whose assertion the content is (e.g. a quoted third party),
    ``trust_tier`` the firewall assessment. None of them is derived from
    ``origin``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    origin: SourceOrigin
    source_id: str = Field(min_length=1)
    sender_id: str | None = None
    assertion_owner_id: str | None = None
    trust_tier: str | None = None
    asset_id: str | None = None
    explicit_fetch: bool = False

    @model_validator(mode="after")
    def _origin_rules(self) -> SourceProvenance:
        if self.origin in DERIVED_TEXT_ORIGINS and not self.asset_id:
            raise ValueError("derived text origin requires a link to its asset")
        if self.origin in EXPLICIT_FETCH_ORIGINS and not self.explicit_fetch:
            raise ValueError("web_page origin requires an explicit fetch")
        return self

    @property
    def is_independent_corroboration(self) -> bool:
        """System/agent-produced content never corroborates on its own."""
        return self.origin not in SYSTEM_PRODUCED_ORIGINS
