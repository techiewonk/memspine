"""``prompts.PromptIdentity``: the hash of a prompt's stable prefix (W2-07; P-08, W-18).

The stable prefix is the rendered system message of a base-layout prompt: it holds no
per-call variable, so its hash is the same for every input. That hash is the key for
prompt-cache reuse (a hosted cache hint, or a local prefix cache) and the provenance stamp
written next to a derived item (``prompt_hash``, W-18). The identity also records the
prompt id and version, the language the variable part was rendered for, and the partials
that were active.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

__all__ = ["PromptIdentity", "hash_prefix"]


def hash_prefix(text: str) -> str:
    """SHA-256 hex of the prefix text (the same function ``layout.build`` uses)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PromptIdentity:
    prompt_id: str
    prompt_version: str
    #: SHA-256 of the rendered system message (the stable prefix).
    identity_hash: str
    language: str = "en"
    partials: tuple[str, ...] = ()

    @classmethod
    def of(
        cls,
        prompt_id: str,
        prompt_version: str,
        prefix: str,
        *,
        language: str = "en",
        partials: tuple[str, ...] = (),
    ) -> PromptIdentity:
        return cls(prompt_id, prompt_version, hash_prefix(prefix), language, partials)

    @property
    def short(self) -> str:
        """``<prompt_version>#<first 12 hex>``: a compact provenance string."""
        return f"{self.prompt_version}#{self.identity_hash[:12]}"
