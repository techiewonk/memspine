"""Synthetic conversations for the behavioural suite (P0-08, G-04).

Every name, place and fact here is invented for this suite. No benchmark text is used, so the
suite is dataset-free and cannot overfit to one benchmark's wording. A *spec* is plain data: a
list of sessions (date + turns) plus the queries; ``behavioural.py`` turns specs into engine calls.

Renaming works on the text of a spec through :func:`rename`, which swaps whole words only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = [
    "Session",
    "Story",
    "FILLER_TURNS",
    "STORY_A",
    "RENAME_MAP",
    "rename",
    "rename_story",
    "PARAPHRASES",
]

RENAME_MAP: dict[str, str] = {"Ilse": "Aiko", "Bram": "Cato"}


@dataclass(frozen=True)
class Session:
    session_id: str
    date: str  # ISO-8601 with offset
    turns: tuple[tuple[str, str], ...]  # (role, text)


@dataclass(frozen=True)
class Story:
    """One invented user's conversation plus the facts the queries are about."""

    sessions: tuple[Session, ...]
    #: (content, entity, attribute, valid_from): structured facts, written as semantic records.
    facts: tuple[tuple[str, str, str, str], ...] = ()
    queries: Mapping[str, str] = field(default_factory=dict)


STORY_A = Story(
    sessions=(
        Session(
            "s1",
            "2024-03-04T18:00:00+00:00",
            (
                ("user", "Ilse here. I just moved to Utrecht and I rent a small flat near the canal."),
                ("assistant", "Congratulations on the move. How are you settling in?"),
                ("user", "Ilse likes it. I planted tomatoes, basil and chives on the balcony."),
            ),
        ),
        Session(
            "s2",
            "2024-06-10T09:30:00+00:00",
            (
                ("user", "Update from Ilse: I moved again, and now I live in Rotterdam near the harbour."),
                ("assistant", "Rotterdam is lively. Is the balcony garden coming along?"),
                ("user", "Yes, the tomatoes are ripe. Next week Ilse visits a sister in Leiden."),
            ),
        ),
        Session(
            "s3",
            "2024-08-20T19:00:00+00:00",
            (
                ("user", "Ilse is learning the cello and practises every evening."),
                ("assistant", "That sounds lovely. Do you have a teacher?"),
                ("user", "Ilse studies with a teacher called Marguerite on Thursdays."),
            ),
        ),
    ),
    facts=(
        ("Ilse lives in Utrecht", "Ilse", "city", "2024-03-04T18:00:00+00:00"),
        ("Ilse lives in Rotterdam", "Ilse", "city", "2024-06-10T09:30:00+00:00"),
    ),
    queries={
        "city": "Where does Ilse live now?",
        "hobby": "Which instrument is Ilse learning?",
        "teacher": "Who teaches Ilse the cello?",
    },
)

#: Query paraphrases that keep the content words of the original (a hash embedder has no semantics, so a
#: paraphrase that drops every shared word would test the embedder, not the engine).
PARAPHRASES: dict[str, tuple[str, str]] = {
    "city": ("Where does Ilse live now?", "Now, where is the place Ilse lives?"),
    "hobby": ("Which instrument is Ilse learning?", "What instrument is Ilse learning to play?"),
    "teacher": ("Who teaches Ilse the cello?", "Which teacher teaches Ilse the cello?"),
}

#: Off-topic turns (invented) that carry none of the story's content words.
FILLER_TURNS: tuple[tuple[str, str], ...] = (
    ("user", "The weather report says the afternoon will be grey with light drizzle."),
    ("assistant", "Light drizzle is common this season; an umbrella should be enough."),
    ("user", "Thanks. Also remind me that the bin collection moved to a different weekday."),
    ("assistant", "Noted: bin collection moved."),
    ("user", "The train timetable changed again, so the early service now leaves later."),
    ("assistant", "Check the operator page for the revised timetable."),
)


def rename(text: str, mapping: Mapping[str, str] = RENAME_MAP) -> str:
    """Replace whole-word occurrences of each key by its value."""
    if not mapping:
        return text
    pattern = re.compile(r"\b(" + "|".join(re.escape(k) for k in mapping) + r")\b")
    return pattern.sub(lambda m: mapping[m.group(1)], text)


def rename_story(story: Story, mapping: Mapping[str, str] = RENAME_MAP) -> Story:
    return Story(
        sessions=tuple(
            Session(s.session_id, s.date, tuple((r, rename(t, mapping)) for r, t in s.turns))
            for s in story.sessions
        ),
        facts=tuple((rename(c, mapping), rename(e, mapping), a, v) for c, e, a, v in story.facts),
        queries={k: rename(q, mapping) for k, q in story.queries.items()},
    )
