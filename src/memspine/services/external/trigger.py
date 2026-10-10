"""When may public knowledge be consulted at all? Only for an invited inference.

A question about what someone would, might or is likely to do, or for a recommendation, can
use general background. A question about what a person did, owns, said or visited is a
private fact and never opens the port. Rules only, English, no model, no gold.
"""

from __future__ import annotations

import re

from memspine.core.query_shape import is_inference

__all__ = ["invites_inference", "is_private_fact_question"]

_RECOMMEND = re.compile(
    r"\b(?:recommend|suggest)\w*\b|\bwhat (?:should|could|might) (?:i|we|\w+) (?:try|read|watch|"
    r"listen|visit|get|buy|do|see)\b|\bany (?:ideas|suggestions)\b",
    re.IGNORECASE,
)
#: Past-tense or possession questions about a person: a private event or attribute.
_PRIVATE_FACT = re.compile(
    r"^\s*(?:did|does|do|has|have|had|was|were|is|are)\b[^?]*\b(?:own|owns|owned|have|has|had|"
    r"visit|visited|go|went|bought|buy|attend|attended|play|played|read|watch|watched|"
    r"listen|listened|meet|met|say|said|tell|told|adopt|adopted|get|got)\b"
    r"|^\s*(?:when|where|how many|how much|how long|how often)\b[^?]*\b(?:did|has|have|had|was|"
    r"were)\b",
    re.IGNORECASE,
)


_MODAL_OPENER = re.compile(r"^\s*(?:would|could|might|is it likely|how likely)\b", re.IGNORECASE)


def is_private_fact_question(question: str) -> bool:
    return bool(_PRIVATE_FACT.search(question))


def invites_inference(question: str) -> bool:
    """True for a would / likely / might / could question or a recommendation request that is
    not a private-fact question."""
    if _MODAL_OPENER.match(question):  # "Would X likely have visited ...": an invitation
        return True
    if is_private_fact_question(question):
        return False
    return is_inference(question) or bool(_RECOMMEND.search(question))
