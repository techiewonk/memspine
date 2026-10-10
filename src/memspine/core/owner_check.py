"""Read-side owner / subject check, entity existence, user header, re-injection (I59/I60/I63/I64).

Pure rules; the engine applies them after retrieval and before the reader. Nothing here drops
evidence: a mismatch is a marker or a note, and the reader decides. Names come from the
question and the store's own speaker / subject tags, never from a list.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from memspine.core.perspective import (
    QuestionPerspective,
    RecordView,
    match_subject,
)

#: Subject ids that are not a person's name.
_NOT_NAMES = frozenset({"3p", "user", "assistant", "tool", "addressee"})


def shown_name(ident: str) -> str:
    """A subject id as the reader should see it: ``caroline`` -> ``Caroline``; the roles
    ``user`` / ``assistant`` keep their lower-case role word."""
    if ident in ("user", "assistant", "tool"):
        return f"the {ident}"
    return ident.title()


def line_subject(rv: RecordView) -> str | None:
    """Whom a line is about: its non-relational subject, else its speaker; None = unknown."""
    named = sorted(s for s in rv.subs if "@" not in s and s not in _NOT_NAMES)
    if named:
        return named[0]
    if rv.spk and (not rv.subs or rv.spk in rv.subs):
        return rv.spk
    rest = sorted(s for s in rv.subs if s != "3p")
    if rest:
        return rest[0]
    return rv.spk


def owner_marker(rv: RecordView) -> str | None:
    """``[Melanie, about Melanie]``; None when the line carries no speaker tag."""
    if not rv.spk:
        return None
    subject = line_subject(rv)
    if subject is None:
        return f"[{shown_name(rv.spk)}]"
    return f"[{shown_name(rv.spk)}, about {shown_name(subject)}]"


@dataclass
class OwnerVerdict:
    """The outcome of the owner check over one context."""

    target: str | None = None
    #: per line index: ``target`` | ``other`` | ``unknown``
    labels: list[str] = field(default_factory=list)
    others: list[str] = field(default_factory=list)
    note: str | None = None


def target_label(qp: QuestionPerspective) -> str | None:
    """The person(s) the question asks about, for a note; None when the question names no
    person (relation-bound third parties and unresolved questions get no note)."""
    names = [t.id for t in qp.targets if t.kind in ("self", "named", "addressee") and not t.rel]
    if not names:
        return None
    return " and ".join(shown_name(n) for n in names)


def judge(
    qp: QuestionPerspective,
    views: Sequence[RecordView | None],
    *,
    overrides: Mapping[int, bool] | None = None,
) -> OwnerVerdict:
    """Label each line about the target / about someone else / unknown, and build the
    reader note when the question asks for a target-specific fact and NO line is about the
    target while at least one is about someone else (and none is unknown).

    ``overrides``: line index -> the decider's ``about_target`` answer for an unknown line.
    Any target (not every one) counts, so a two-person question keeps both people's lines."""
    verdict = OwnerVerdict(target=target_label(qp))
    if not qp.bound:
        verdict.labels = ["unknown"] * len(views)
        return verdict
    others: Counter[str] = Counter()
    for i, rv in enumerate(views):
        m = match_subject(qp, rv) if rv is not None else None
        if m is None:
            if overrides and i in overrides:
                verdict.labels.append("target" if overrides[i] else "other")
            else:
                verdict.labels.append("unknown")
            continue
        if m >= 1.0:
            verdict.labels.append("target")
            continue
        verdict.labels.append("other")
        who = line_subject(rv) if rv is not None else None
        if who:
            others[who] += 1
    verdict.others = [w for w, _ in others.most_common(2)]
    labels = verdict.labels
    if (
        verdict.target
        and "target" not in labels
        and "unknown" not in labels
        and "other" in labels
        and verdict.others
    ):
        about = " and ".join(shown_name(w) for w in verdict.others)
        verdict.note = (
            f"Note: none of the memories describe {verdict.target} doing or saying this; "
            f"the matching memories are about {about}."
        )
    return verdict


def unknown_lines(verdict: OwnerVerdict) -> list[int]:
    return [i for i, label in enumerate(verdict.labels) if label == "unknown"]


# ------------------------------------------------------------------------ entity existence

_APOS = chr(0x2019)  # the typographic apostrophe
_WORD = re.compile(rf"[^\W\d_][\w'{_APOS}-]*", re.U)
_CAP = re.compile(r"\b[A-Z][a-zA-Z]{1,29}\b")
_MONTHS = frozenset(
    {
        *("january", "february", "march", "april", "may", "june", "july"),
        *("august", "september", "october", "november", "december"),
        *("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
    }
)


def store_vocab(texts: Iterable[str], known: Iterable[str] = ()) -> frozenset[str]:
    """The lower-cased word set of the store (possessives stripped) plus the participants."""
    words: set[str] = {k.lower() for k in known}
    for text in texts:
        for w in _WORD.findall(text):
            w = w.lower()
            if w.endswith(("'s", _APOS + "s")):
                w = w[:-2]
            words.add(w)
    return frozenset(words)


def question_names(query: str) -> list[str]:
    """Capitalised tokens of the question that can be names: not the first word, not a
    month or weekday, not 'I'."""
    first = _WORD.search(query)
    start = first.end() if first else 0
    out: list[str] = []
    for m in _CAP.finditer(query, start):
        tok = m.group(0)
        if tok.lower() in _MONTHS or tok == "I":
            continue
        if tok not in out:
            out.append(tok)
    return out


def missing_names(query: str, vocab: Collection[str]) -> list[str]:
    """Names in the question that occur nowhere in the store. Exact (case-insensitive)
    membership; a name of 3+ letters that is a prefix of a stored word ("Mel" / "Melanie")
    counts as present, a distinct name ("Oscar" / "Oliver") does not."""
    out = []
    for name in question_names(query):
        low = name.lower()
        if low in vocab:
            continue
        if len(low) >= 3 and any(w.startswith(low) for w in vocab):
            continue
        out.append(name)
    return out


def entity_note(names: Sequence[str]) -> str | None:
    if not names:
        return None
    if len(names) == 1:
        return f"Note: {names[0]} is not mentioned in the memories."
    return f"Note: {', '.join(names[:-1])} and {names[-1]} are not mentioned in the memories."


# ----------------------------------------------------------------------------- user header


def user_header(asker: str | None) -> str | None:
    """I63: who the user is, only when the harness knows (a persona / owner); None otherwise."""
    if not asker or asker in ("user", "assistant", "tool"):
        return None
    return (
        f"You are the assistant. The user is {shown_name(asker)}. "
        "Memory lines are labelled with their speaker."
    )


# ------------------------------------------------------------------------ re-injection (I64)


class InjectionLog:
    """Which record ids were injected in the last ``window`` replies of each (namespace,
    session). Only a session id activates it."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], deque[frozenset[str]]] = {}

    def record(self, ns: str, session: str | None, ids: Iterable[str], window: int) -> None:
        if not session or window < 1:
            return
        q = self._by_key.setdefault((ns, session), deque(maxlen=window))
        if q.maxlen != window:
            q = self._by_key[(ns, session)] = deque(q, maxlen=window)
        q.append(frozenset(ids))

    def uses(self, ns: str, session: str | None) -> Counter[str]:
        out: Counter[str] = Counter()
        if session:
            for ids in self._by_key.get((ns, session), ()):
                out.update(ids)
        return out

    def factor(self, ns: str, session: str | None, record_id: str, penalty: float) -> float:
        """1.0 unused; ``1 - penalty * used / window`` for a record used in ``used`` of the
        last replies (floored at ``1 - penalty``)."""
        if not session or penalty <= 0.0:
            return 1.0
        q = self._by_key.get((ns, session))
        if not q:
            return 1.0
        used = self.uses(ns, session)[record_id]
        if not used:
            return 1.0
        return max(1.0 - penalty, 1.0 - penalty * used / (q.maxlen or 1))

    def clear(self, ns: str | None = None) -> None:
        if ns is None:
            self._by_key.clear()
        else:
            for key in [k for k in self._by_key if k[0] == ns]:
                del self._by_key[key]
