"""W2-02 (W-05): ``AsserterSubjectCheck``: an extracted item's asserter and subject against the cited line.

Pure: no I/O, no clock, no model call. Raw turns carry ``spk`` / ``addr`` (typed columns); an extracted
item carries the *asserter* (who said it) and the *subject* (whom it is about). This check separates
"Bram said Ilse moved" from "Ilse said she moved" by comparing both to the cited lines:

* the asserter must be the speaker of a cited line, or a cited line must attribute the statement to
  them ("Bram said ...", "according to Bram"); otherwise ``asserter_mismatch`` (hard);
* a cited line spoken in the first person ("I moved to Oslo") is about its speaker: an item whose subject
  is somebody else and who is not named in that line is ``subject_mismatch_first_person`` (hard);
* a subject that is not the speaker and appears in no cited line is ``subject_not_in_cited`` (soft: the
  link may be implied, so it is flagged, not refused).

A hard flag makes ``ok`` False. The check never raises and never edits the item: the caller decides
whether to drop, downgrade or keep it flagged.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict

__all__ = ["AsserterSubjectCheck", "AsserterVerdict", "CitedLine"]

HARD_FLAGS = frozenset(
    {"asserter_missing", "no_cited_line", "asserter_mismatch", "subject_mismatch_first_person"}
)
_SELF = frozenset({"self", "speaker", "me", "myself", "i", "user", "i'm"})
_FIRST_PERSON = re.compile(r"\b(?:i|i'm|i've|i'd|i'll|my|me|mine|myself)\b", re.IGNORECASE)
_ATTRIBUTION = (
    "said",
    "says",
    "told",
    "tells",
    "mentioned",
    "mentions",
    "thinks",
    "thought",
    "wants",
    "wanted",
    "claims",
    "claimed",
    "believes",
    "asked",
    "wrote",
    "reports",
    "reported",
    "noted",
    "added",
    "replied",
    "answered",
    "insists",
    "recalls",
    "remembers",
    "texted",
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CitedLine(_Frozen):
    speaker: str | None = None
    text: str
    role: str = "user"


class AsserterVerdict(_Frozen):
    ok: bool
    asserter: str | None
    subjects: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    #: indexes (into ``cited``) of the lines that support the asserter
    supporting: tuple[int, ...] = ()


def _norm(name: str | None, aliases: Mapping[str, str] | None = None) -> str | None:
    if name is None:
        return None
    s = " ".join(unicodedata.normalize("NFKC", str(name)).split()).casefold()
    if not s:
        return None
    return aliases.get(s, s) if aliases else s


def _mentions(text: str, name: str) -> bool:
    return (
        re.search(
            rf"(?<!\w){re.escape(name)}(?!\w)", unicodedata.normalize("NFKC", text).casefold()
        )
        is not None
    )


def _attributes(text: str, name: str) -> bool:
    t = unicodedata.normalize("NFKC", text).casefold()
    n = re.escape(name)
    verbs = "|".join(_ATTRIBUTION)
    return bool(
        re.search(rf"(?<!\w){n}(?:\s+\w+){{0,2}}\s+(?:{verbs})(?!\w)", t)
        or re.search(rf"according to {n}(?!\w)", t)
        or re.search(rf"(?<!\w){n}\s*[:\-]\s+\S", t)
    )


class AsserterSubjectCheck:
    def __init__(self, aliases: Mapping[str, str] | None = None) -> None:
        self.aliases = dict(aliases or {})

    def check(
        self,
        asserter: str | None,
        subject: str | Sequence[str] | None,
        cited: Sequence[CitedLine],
    ) -> AsserterVerdict:
        flags: list[str] = []
        who = _norm(asserter, self.aliases)
        if isinstance(subject, str):
            raw_subjects: Sequence[str] = [subject]
        else:
            raw_subjects = list(subject or [])
        subjects = tuple(s for s in (_norm(x, self.aliases) for x in raw_subjects) if s)
        if who is None:
            flags.append("asserter_missing")
        if not cited:
            flags.append("no_cited_line")
            return self._verdict(who, subjects, flags, ())
        speakers = [_norm(c.speaker, self.aliases) for c in cited]
        supporting: list[int] = []
        if who is not None:
            supporting = [i for i, sp in enumerate(speakers) if sp == who]
            if not supporting:
                supporting = [i for i, c in enumerate(cited) if _attributes(c.text, who)]
                if supporting:
                    flags.append("attributed")
                else:
                    flags.append("asserter_mismatch")
        # subjects, against the lines that support the asserter (all lines when none do)
        pool = supporting or list(range(len(cited)))
        for subj in subjects:
            if subj in _SELF or subj == who:
                continue
            named = any(_mentions(cited[i].text, subj) for i in pool)
            first_person_by_other = any(
                speakers[i] is not None
                and speakers[i] != subj
                and _FIRST_PERSON.search(cited[i].text)
                and not _mentions(cited[i].text, subj)
                for i in pool
            )
            if first_person_by_other and not named:
                flags.append("subject_mismatch_first_person")
            elif not any(_mentions(c.text, subj) for c in cited) and subj not in speakers:
                flags.append("subject_not_in_cited")
        return self._verdict(who, subjects, flags, tuple(supporting))

    @staticmethod
    def _verdict(
        who: str | None, subjects: tuple[str, ...], flags: list[str], supporting: tuple[int, ...]
    ) -> AsserterVerdict:
        uniq = tuple(dict.fromkeys(flags))
        return AsserterVerdict(
            ok=not any(f in HARD_FLAGS for f in uniq),
            asserter=who,
            subjects=subjects,
            flags=uniq,
            supporting=supporting,
        )
