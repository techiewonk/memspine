"""W16 (plan v3.2, G20): sensitive-topic tagging. Lexicon only, no model.

The identifier patterns (``core/redaction.py``) find what can be masked: keys, card
numbers, addresses. Sensitive *topics* cannot be masked without losing the memory,
but they need governance: GDPR art. 9 special categories (health, religion or belief,
sexual orientation, political opinion, ethnic origin, trade-union membership),
criminal and legal matters, and financial hardship. :func:`sensitive_topics` names
the categories a text touches; the write door tags them ``sensitive:<category>`` and
raises the record's ``pii_tier`` (``firewall.sensitive_topics``), so consent, purpose
and remote-LLM tier rules apply to them. Deliberately cue-based and English.
"""

from __future__ import annotations

import re

__all__ = ["SENSITIVE_CATEGORIES", "sensitive_topics"]

SENSITIVE_CATEGORIES: dict[str, re.Pattern[str]] = {
    "health": re.compile(
        r"\b(?:diagnos(?:ed|is)|my (?:doctor|therapist|psychiatrist|medication|meds|surgery|"
        r"treatment|condition|illness|disorder)|chemotherapy|cancer|diabetes|depression|"
        r"anxiety disorder|bipolar|adhd|autism|hiv|pregnan(?:t|cy)|miscarriage|"
        r"chronic (?:pain|illness|condition)|disabilit(?:y|ies)|rehab|addiction|"
        r"mental health|prescription|allerg(?:y|ic) to|medical (?:condition|history|record))\b",
        re.IGNORECASE,
    ),
    "religion": re.compile(
        r"\b(?:my (?:church|mosque|synagogue|temple|faith|religion|parish|pastor|priest|imam|"
        r"rabbi)|archdiocese|diocese|parishioner|congregation|i(?:'m| am) (?:a )?(?:christian|"
        r"catholic|muslim|jewish|hindu|buddhist|sikh|atheist|agnostic|mormon)|ramadan|"
        r"kosher|halal diet|bible study|baptis(?:m|ed)|confirmation class)\b",
        re.IGNORECASE,
    ),
    "sexual_orientation": re.compile(
        r"\b(?:i(?:'m| am) (?:gay|lesbian|bisexual|bi|queer|asexual|pansexual|trans|"
        r"transgender|non-binary|nonbinary)|coming out|came out|my (?:transition|pronouns)|"
        r"lgbtq\+?|same-sex (?:partner|marriage))\b",
        re.IGNORECASE,
    ),
    "political": re.compile(
        r"\b(?:i vote[ds]? (?:for|labour|conservative|democrat|republican)|my (?:party|"
        r"political views)|i(?:'m| am) a (?:democrat|republican|socialist|communist|"
        r"libertarian|conservative|liberal)|union member(?:ship)?|trade union)\b",
        re.IGNORECASE,
    ),
    "ethnicity": re.compile(
        r"\b(?:my (?:ethnicity|race|ethnic (?:background|origin))|immigration status|"
        r"undocumented|asylum (?:seeker|claim)|refugee status|visa status)\b",
        re.IGNORECASE,
    ),
    "legal": re.compile(
        r"\b(?:criminal record|arrested|convicted|conviction|probation|parole|my lawyer|"
        r"lawsuit|court (?:case|date|hearing)|custody (?:battle|hearing)|restraining order|"
        r"parking (?:violation|ticket|fine)|speeding ticket|citation number|dui)\b",
        re.IGNORECASE,
    ),
    "financial": re.compile(
        r"\b(?:bankrupt(?:cy)?|debt collector|in debt|my (?:salary|income|credit score|"
        r"mortgage|loan|debts?|tax return)|overdraft|late payment|foreclosure|"
        r"duplicate charge|chargeback|unemployment benefits)\b",
        re.IGNORECASE,
    ),
}


def sensitive_topics(text: str) -> list[str]:
    """The sensitive categories ``text`` touches, in :data:`SENSITIVE_CATEGORIES` order."""
    return [name for name, pattern in SENSITIVE_CATEGORIES.items() if pattern.search(text)]
