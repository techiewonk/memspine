# ruff: noqa: RUF001
"""W2-02 (W-08): provenance class at write: ``SourceClassifier`` and ``AssentChecker``.

Pure: no I/O, no clock, no model call. Two stages, as decided in W-08:

* Stage 1 (:class:`SourceClassifier`): the role maps to a closed class (``user_stated``,
  ``assistant_proposed``, ``tool``, ``document``, ``external``) with the role's base trust. The class is
  the asserting principal's (S4-2). ``user_confirmed`` is never read off a role: it needs stage 2.
* Stage 2 (:class:`AssentChecker`): a code check on a user line, against the assistant proposal it
  replies to. A real yes ("yes please", "sí, adelante", "ja, genau") is ``assent``; an acknowledgement
  ("ok, thanks", "got it") is not; a refusal, a question and a conditional yes ("yes, but make it 5pm")
  are not either. Assent promotes a proposal to ``user_confirmed`` only when it also *binds* the
  proposal: a short direct reply, or a longer line that shares words with the proposal.

If the check cannot run (no replied-to proposal text, or the caller passes no verdict), the stage-1 label
stands, except that a requested ``user_confirmed`` is downgraded to ``assistant_proposed`` (W-08).

The lexicons are small on purpose and cover English, Spanish, French, German, Portuguese, Italian,
romanised Hindi, Russian, Chinese, Japanese, Korean and Arabic; a language outside them reads as
``none`` (never as assent), which fails safe: no promotion.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from memspine.ingest.roles import RolePolicy

__all__ = [
    "AssentChecker",
    "AssentConfig",
    "AssentVerdict",
    "Classification",
    "ProvClass",
    "SourceClassifier",
]

ProvClass = Literal[
    "user_stated", "assistant_proposed", "user_confirmed", "tool", "document", "external"
]
AssentKind = Literal["assent", "acknowledgement", "rejection", "conditional", "question", "none"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ----------------------------------------------------------------------------------------- lexicons

# Phrases are matched on a normalised string (NFKC, casefold, punctuation -> space, word boundaries).
_ASSENT = (
    # en
    "yes",
    "yeah",
    "yep",
    "yup",
    "sure",
    "absolutely",
    "definitely",
    "certainly",
    "agreed",
    "i agree",
    "correct",
    "exactly",
    "that s right",
    "that is right",
    "that s correct",
    "go ahead",
    "do it",
    "please do",
    "sounds good",
    "sounds great",
    "works for me",
    "that works",
    "let s do it",
    "let s go",
    "i confirm",
    "confirmed",
    "book it",
    "yes please",
    "of course",
    "by all means",
    "i d like that",
    "i would like that",
    "go for it",
    # es / pt / it
    "sí",
    "claro",
    "de acuerdo",
    "perfecto",
    "exacto",
    "adelante",
    "vale",
    "por supuesto",
    "sim",
    "exato",
    "combinado",
    "pode ser",
    "com certeza",
    "certo",
    "va bene",
    "certo che si",
    "esatto",
    "perfetto",
    "d accordo",
    "andiamo",
    # fr
    "oui",
    "d accord",
    "exactement",
    "bien sûr",
    "bien sur",
    "parfait",
    "allez y",
    "volontiers",
    "tout à fait",
    "c est ça",
    "c est bon",
    # de
    "ja",
    "genau",
    "richtig",
    "einverstanden",
    "klar",
    "in ordnung",
    "mach das",
    "gerne",
    "stimmt",
    "natürlich",
    "sehr gern",
    # hi (romanised)
    "haan",
    "haanji",
    "han ji",
    "theek hai",
    "thik hai",
    "bilkul",
    "sahi hai",
    "zaroor",
    # ru
    "да",
    "конечно",
    "согласен",
    "согласна",
    "хорошо давайте",
    "давайте",
    "точно",
    # cjk / ko / ar (substring match, no spaces in these scripts)
    "是的",
    "好的",
    "没错",
    "對",
    "对的",
    "可以",
    "同意",
    "はい",
    "そうです",
    "お願いします",
    "ええ",
    "네",
    "예",
    "맞아요",
    "좋아요",
    "نعم",
    "أجل",
    "بالتأكيد",
    "موافق",
)
_ACK = (
    "ok",
    "okay",
    "k",
    "kk",
    "thanks",
    "thank you",
    "thx",
    "ty",
    "cheers",
    "got it",
    "i see",
    "noted",
    "understood",
    "alright",
    "all right",
    "mm",
    "mmm",
    "hmm",
    "cool",
    "nice",
    "great",
    "fine",
    "interesting",
    "right",
    "i understand",
    "no problem",
    "no worries",
    "gracias",
    "entendido",
    "obrigado",
    "obrigada",
    "grazie",
    "merci",
    "compris",
    "danke",
    "verstanden",
    "dhanyavad",
    "shukriya",
    "samajh gaya",
    "спасибо",
    "понятно",
    "谢谢",
    "謝謝",
    "明白",
    "了解",
    "ありがとう",
    "わかりました",
    "감사합니다",
    "알겠습니다",
    "شكرا",
    "حسنا",
)
_NEGATIVE = (
    "no",
    "nope",
    "nah",
    "not",
    "never",
    "don t",
    "do not",
    "dont",
    "never mind",
    "nevermind",
    "wrong",
    "incorrect",
    "stop",
    "cancel",
    "disagree",
    "i disagree",
    "not really",
    "no thanks",
    "no thank you",
    "non",
    "nein",
    "nao",
    "não",
    "nahi",
    "нет",
    "不要",
    "不",
    "いいえ",
    "아니요",
    "아니",
    "لا",
)
_CONDITIONAL = (
    "but",
    "however",
    "except",
    "instead",
    "rather",
    "unless",
    "only if",
    "provided",
    "although",
    "pero",
    "sino",
    "mais",
    "aber",
    "porém",
    "mas",
    "però",
    "но",
    "但是",
    "でも",
    "しかし",
    "하지만",
)
_NOT_REFUSAL = ("no problem", "no worries", "no doubt", "not bad", "no issue")
_UNSPACED = re.compile(r"[぀-ヿ㐀-鿿가-힯؀-ۿ]")
_PUNCT = re.compile(r"[^\w]+", re.UNICODE)


def _norm(text: str) -> str:
    s = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    s = _PUNCT.sub(" ", s.replace("'", " ")).strip()
    return f" {s} " if s else ""


def _has(norm: str, raw_cf: str, phrases: Iterable[str]) -> list[str]:
    hits: list[str] = []
    for p in phrases:
        if _UNSPACED.search(p):
            if p in raw_cf:
                hits.append(p)
            continue
        q = _norm(p)
        if q and q in norm:
            hits.append(p)
    return hits


class AssentConfig(_Frozen):
    #: a line with at most this many tokens answers the preceding proposal directly (binds without word overlap)
    direct_reply_tokens: int = Field(default=12, ge=1)
    #: a longer line binds only when this share of its words appears in the proposal
    bind_overlap: float = Field(default=0.15, ge=0.0, le=1.0)
    #: a line may start with at most this many tokens before the first assent marker ("well, yes")
    lead_tokens: int = Field(default=4, ge=0)


class AssentVerdict(_Frozen):
    kind: AssentKind
    #: assent that also binds the proposal (the only thing that promotes to ``user_confirmed``)
    binds: bool = False
    matched: tuple[str, ...] = ()
    overlap: float = 0.0
    reasons: tuple[str, ...] = ()

    @property
    def confirms(self) -> bool:
        return self.kind == "assent" and self.binds


class AssentChecker:
    """Decide whether a user line is a real assent to the assistant proposal it replies to."""

    def __init__(self, config: AssentConfig | None = None) -> None:
        self.config = config or AssentConfig()

    def check(self, user_line: str, proposal: str | None = None) -> AssentVerdict:
        raw = unicodedata.normalize("NFKC", user_line or "").casefold()
        norm = _norm(user_line or "")
        if not norm:
            return AssentVerdict(kind="none", reasons=("empty",))
        tokens = norm.split()
        asked = "?" in raw or "？" in raw
        no_refusal = bool(_has(norm, raw, _NOT_REFUSAL))
        neg = [h for h in _has(norm, raw, _NEGATIVE) if h not in {"not", "no"} or not no_refusal]
        # "not" / "no" inside an assent phrase's neighbourhood: a leading negation is a refusal
        first = tokens[0] if tokens else ""
        leading_neg = (
            first
            in {
                "no",
                "nope",
                "nah",
                "non",
                "nein",
                "nao",
                "não",
                "nahi",
                "нет",
                "never",
                "not",
                "stop",
                "cancel",
            }
            and not no_refusal
        )
        assent = _has(norm, raw, _ASSENT)
        # drop an assent hit that is merely the second half of a refusal ("not right", "don't agree")
        if assent and neg and not leading_neg:
            before_neg = any(
                re.search(
                    rf"(?:^| )(?:not|don t|dont|do not|never|nicht|pas) (?:\w+ ){{0,1}}{re.escape(_norm(a).strip())}(?: |$)",
                    norm,
                )
                for a in assent
                if not _UNSPACED.search(a)
            )
            if before_neg:
                assent = []
        if leading_neg and not assent:
            return AssentVerdict(
                kind="rejection", matched=tuple(neg), reasons=("leading_negation",)
            )
        if leading_neg:
            return AssentVerdict(
                kind="rejection", matched=tuple(neg), reasons=("leading_negation_with_assent_word",)
            )
        if neg and not assent and not _has(norm, raw, _ACK):
            return AssentVerdict(kind="rejection", matched=tuple(neg), reasons=("negation",))
        if assent:
            cond = _has(norm, raw, _CONDITIONAL)
            if cond:
                return AssentVerdict(
                    kind="conditional", matched=tuple(assent), reasons=("assent_with_" + cond[0],)
                )
            # a question that opens with an assent word ("yes?") is still a question mark on a one-word line
            if asked and len(tokens) <= 1:
                return AssentVerdict(
                    kind="question", matched=tuple(assent), reasons=("bare_question",)
                )
            # an assent marker buried deep in a long line is not an answer to the proposal
            cjk = bool(_UNSPACED.search(raw))
            pos = (
                min((tokens.index(w) for w in assent[0].split() if w in tokens), default=0)
                if not cjk
                else 0
            )
            if (
                not cjk
                and pos > self.config.lead_tokens
                and len(tokens) > self.config.direct_reply_tokens
            ):
                return AssentVerdict(
                    kind="none", matched=tuple(assent), reasons=("assent_word_not_leading",)
                )
            overlap = self._overlap(norm, proposal) if proposal else 0.0
            direct = len(tokens) <= self.config.direct_reply_tokens or (cjk and len(raw) <= 24)
            binds = bool(proposal and proposal.strip()) and (
                direct or overlap >= self.config.bind_overlap
            )
            reasons = ("assent",) + (
                () if binds else ("no_proposal" if not proposal else "not_bound",)
            )
            return AssentVerdict(
                kind="assent", binds=binds, matched=tuple(assent), overlap=overlap, reasons=reasons
            )
        ack = _has(norm, raw, _ACK)
        if ack:
            return AssentVerdict(kind="acknowledgement", matched=tuple(ack), reasons=("ack_only",))
        if asked:
            return AssentVerdict(kind="question", reasons=("question",))
        return AssentVerdict(kind="none", reasons=("no_marker",))

    @staticmethod
    def _overlap(norm_line: str, proposal: str) -> float:
        words = {w for w in norm_line.split() if len(w) > 2}
        if not words:
            return 0.0
        prop = {w for w in _norm(proposal).split() if len(w) > 2}
        return len(words & prop) / len(words)


# ------------------------------------------------------------------------------------- classification


class Classification(_Frozen):
    src_class: ProvClass
    base_trust: float
    #: the highest trust the record may carry (the role's base trust; a proposal never exceeds it)
    trust_cap: float
    downgraded: bool = False
    reasons: tuple[str, ...] = ()


class SourceClassifier:
    """Stage 1 of W-08: the role gives the class; assent (stage 2) may promote a proposal."""

    def __init__(self, roles: RolePolicy | None = None) -> None:
        self.roles = roles or RolePolicy()

    def classify(self, role: str) -> Classification:
        """A raw turn: the class and trust of its role."""
        rule = self.roles.rule(role)
        return Classification(
            src_class=rule.src_class,
            base_trust=rule.base_trust,
            trust_cap=rule.base_trust,
            reasons=(f"role:{role}",),
        )

    def classify_item(
        self,
        asserter_role: str,
        *,
        assent: AssentVerdict | None = None,
        requested: ProvClass | None = None,
    ) -> Classification:
        """An extracted item, classed by its asserting principal's role (S4-2).

        An item the assistant proposed becomes ``user_confirmed`` only when ``assent`` is a verdict that
        confirms (assent that binds the proposal). ``requested="user_confirmed"`` without such a verdict
        (including when the check could not run, ``assent=None``) is downgraded to the stage-1 label."""
        base = self.classify(asserter_role)
        user = self.roles.rule("user")
        if base.src_class == "assistant_proposed" and assent is not None and assent.confirms:
            return Classification(
                src_class="user_confirmed",
                base_trust=user.base_trust,
                trust_cap=user.base_trust,
                reasons=(*base.reasons, "assent_binds_proposal"),
            )
        if requested == "user_confirmed":
            return Classification(
                src_class=base.src_class,
                base_trust=base.base_trust,
                trust_cap=base.trust_cap,
                downgraded=True,
                reasons=(
                    *base.reasons,
                    "user_confirmed_without_assent"
                    if assent is not None
                    else "assent_check_unavailable",
                ),
            )
        return base
