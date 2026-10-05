"""#50 remote-LLM gate: keep high-tier record text out of prompts that leave the host.

``consent.remote_llm_max_tier`` names the highest PII tier a remote provider may
see. Every role bound to a remote provider is wrapped in :class:`TierGatedLLM`,
which, before each call, replaces any text of a record above that tier (its
content, archived versions, the 400-character prefix the relevance filter sends,
and the JSON-escaped form of each) with :data:`WITHHELD_MARKER`.

This is a textual gate at the provider boundary: it catches every prompt builder
without each one having to know about tiers, but it cannot catch a paraphrase of
the content (a summary written earlier by a local model, for example). Records
derived from high-tier content carry the tier themselves (the write door gives a
derived record its parents' highest tier), and the engine's prompt builders that
hold context records replace a withheld record by the marker before rendering;
this text match is the backstop. It is whitespace- and case-insensitive: a
builder that collapses whitespace, re-cases or JSON-escapes the text still
matches (whitespace runs, ``\\n``/``\\t``/``\\r`` escapes and case all compare equal).

A provider counts as **local** when its model id is ``llamacpp/...`` (in process),
or its ``api_base`` host is a loopback/local name (``localhost``, ``127.0.0.1``,
``::1``, ``0.0.0.0``, ``*.local``, or one of ``consent.local_hosts``), or it is an
``ollama``/``ollama_chat`` model with no ``api_base`` (LiteLLM's localhost default).
Everything else (``openai/``, ``anthropic/``, ``bedrock/``, ``vertex_ai/``,
``azure/``, a hosted ``vllm``) is remote.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

import orjson

from memspine.config import constants
from memspine.services.llm.base import LLMService

__all__ = [
    "WITHHELD_MARKER",
    "TierGatedLLM",
    "is_local_provider",
    "withheld_pattern",
    "withheld_variants",
]

WITHHELD_MARKER = "[WITHHELD: above the remote-LLM PII tier]"
_NOTE_PREFIX_CHARS = constants.REMOTE_GATE_NOTE_PREFIX_CHARS
_MIN_WITHHELD_CHARS = constants.REMOTE_GATE_MIN_CHARS
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"})
_LOCAL_PREFIXES = ("ollama/", "ollama_chat/")
#: Whitespace, or its JSON escape, between two words of a withheld text.
_GAP = r"(?:\s|\\[nrt])+"
_GAP_RE = re.compile(_GAP)


def is_local_provider(model: str, api_base: str | None, local_hosts: Iterable[str] = ()) -> bool:
    """Whether a role's provider runs on this host or the deployer's own network."""
    if model.startswith("llamacpp/"):
        return True
    if api_base:
        host = (urlparse(api_base).hostname or "").lower()
        extra = {h.lower() for h in local_hosts}
        return host in _LOCAL_HOSTS or host in extra or host.endswith(".local")
    return model.startswith(_LOCAL_PREFIXES)


def withheld_variants(texts: Iterable[str]) -> list[str]:
    """Every form in which a withheld text may appear in a prompt, longest first."""
    variants: set[str] = set()
    for text in texts:
        if len(text) < _MIN_WITHHELD_CHARS:
            continue
        for form in (text, text[:_NOTE_PREFIX_CHARS]):
            variants.add(form)
            variants.add(orjson.dumps(form).decode()[1:-1])
    return sorted(variants, key=len, reverse=True)


def _word_pattern(form: str) -> str | None:
    words = [w for w in _GAP_RE.split(form) if w]
    if not words or len(" ".join(words)) < _MIN_WITHHELD_CHARS:
        return None
    return _GAP.join(re.escape(word) for word in words)


@lru_cache(maxsize=8)
def _compiled(texts: tuple[str, ...]) -> re.Pattern[str] | None:
    patterns: set[str] = set()
    for text in texts:
        normal = " ".join(text.split())
        for form in (text, text[:_NOTE_PREFIX_CHARS], normal[:_NOTE_PREFIX_CHARS]):
            for variant in (form, orjson.dumps(form).decode()[1:-1]):
                pattern = _word_pattern(variant)
                if pattern is not None:
                    patterns.add(pattern)
    if not patterns:
        return None
    ordered = sorted(patterns, key=len, reverse=True)
    return re.compile("|".join(ordered), re.IGNORECASE)


def withheld_pattern(texts: Iterable[str]) -> re.Pattern[str] | None:
    """One pattern matching every withheld text in any whitespace, case or JSON
    escaping, plus its note prefix; None when nothing is long enough to match."""
    return _compiled(tuple(sorted(set(texts))))


class TierGatedLLM:
    """A remote provider whose prompts never carry withheld record text."""

    def __init__(self, inner: LLMService, withheld: Callable[[], Awaitable[list[str]]]) -> None:
        self._inner = inner
        self._withheld = withheld
        self.withheld_count = 0

    @property
    def provider_id(self) -> str:
        return self._inner.provider_id

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        pattern = withheld_pattern(await self._withheld())
        if pattern is not None:
            gated: list[dict[str, str]] = []
            for message in messages:
                content, hits = pattern.subn(WITHHELD_MARKER, str(message.get("content", "")))
                self.withheld_count += hits
                gated.append({**message, "content": content})
            messages = gated
        return await self._inner.chat(messages, **options)

    def __getattr__(self, name: str) -> Any:
        # Delegate ``model``/``usage_totals`` etc.; never dunders, never during copy.
        if name == "_inner" or (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __repr__(self) -> str:
        return f"TierGatedLLM(inner={self._inner!r})"
