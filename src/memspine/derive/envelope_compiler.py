"""``derive.EnvelopeCompiler``: ``ExtractionItem`` (wire) to ``AssertionEnvelope`` (stored) for the write path (W2-06a).

A thin owner of the policy around the pure compiler ``derive/compile.py`` (LIB-05): the kind registry (new
kinds are accepted, capped at :data:`KIND_CAP`, anything past the cap becomes ``other``), the role policy
(trust and provenance class per cited line role) and the clock for ``recorded_at``. It builds the
:class:`~memspine.derive.compile.CompileContext` for a window from lines the caller prepared, so the model
never authors an id, a scope, a trust value or a time axis.

A new kind carries safe defaults: it is stored with ``event`` cardinality (many may hold at once), so an
unknown kind can never supersede another statement. Only the kind ``state`` keeps the single-valued key.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from memspine.derive.compile import (
    DEFAULT_ROLE_POLICY,
    FALLBACK_KIND,
    CompileContext,
    CompileResult,
    RolePolicy,
    TimeAnchor,
    WindowLine,
    compile_items,
    keep_text_only,
    normalise_kind,
)
from memspine.schemas.durable import OwnerScope

__all__ = [
    "BASE_KINDS",
    "KIND_CAP",
    "EnvelopeCompiler",
    "KindRegistry",
    "storage_kind",
]

KIND_CAP = 64
"""Most distinct kinds the registry holds (owner ruling: new kinds accepted with safe defaults, capped at 64)."""

BASE_KINDS: tuple[str, ...] = (
    "state",
    "event",
    "fact",
    "plan",
    "preference",
    "relationship",
    "attribute",
    "other",
)


class KindRegistry:
    """The open kind set: a known kind passes, a new one is registered until the cap, then ``other``."""

    def __init__(self, cap: int = KIND_CAP, known: Sequence[str] = BASE_KINDS) -> None:
        self.cap = max(cap, 1)
        self._kinds: dict[str, None] = dict.fromkeys(normalise_kind(k) for k in known if k)
        self._kinds.setdefault(FALLBACK_KIND, None)
        self.capped = 0
        self.added = 0
        self._fresh: list[str] = []  # FU-GAPS: kinds registered since the last drain

    def __call__(self, kind: str) -> str | None:
        if kind in self._kinds:
            return kind
        if len(self._kinds) < self.cap:
            self._kinds[kind] = None
            self.added += 1
            self._fresh.append(kind)
            return kind
        self.capped += 1
        return FALLBACK_KIND

    def drain_new(self) -> list[str]:
        """Kinds registered since the last drain (what the learned-state log has not seen)."""
        out, self._fresh = self._fresh, []
        return out

    def restore(self, kinds: Sequence[str]) -> int:
        """Re-register logged kinds at start (not queued again); returns how many were added."""
        n = 0
        for kind in kinds:
            k = normalise_kind(kind)
            if k and k not in self._kinds and len(self._kinds) < self.cap:
                self._kinds[k] = None
                n += 1
        return n

    def __len__(self) -> int:
        return len(self._kinds)

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(self._kinds)


def storage_kind(kind: str) -> str:
    """The cardinality class the write door gets: ``state`` stays single-valued, every other (new) kind is
    ``event`` (many), the safe default."""
    return "state" if kind == "state" else "event"


class EnvelopeCompiler:
    def __init__(
        self,
        *,
        kinds: KindRegistry | None = None,
        role_policy: Mapping[str, RolePolicy] | None = None,
        prompt_hash: str | None = None,
        clock: Callable[[], datetime] | None = None,
        time_anchor: TimeAnchor = keep_text_only,
    ) -> None:
        self.kinds = kinds if kinds is not None else KindRegistry()
        self._role_policy = dict(role_policy if role_policy is not None else DEFAULT_ROLE_POLICY)
        self._prompt_hash = prompt_hash
        self._clock = clock or (lambda: datetime.now(UTC))
        self._anchor = time_anchor

    def context(
        self,
        scope: OwnerScope,
        source_id: str,
        window: Mapping[int, WindowLine],
        **fields: Any,
    ) -> CompileContext:
        """The system-authored context of one window (``fields`` set session, source time, epoch ...)."""
        base: dict[str, Any] = {
            "role_policy": self._role_policy,
            "prompt_hash": self._prompt_hash,
            "recorded_at": self._clock(),
            "kind_registry": self.kinds,
            "time_anchor": self._anchor,
        }
        base.update(fields)
        return CompileContext(scope=scope, source_id=source_id, window=dict(window), **base)

    def compile(self, raw: str | Sequence[Any], ctx: CompileContext) -> CompileResult:
        return compile_items(raw, ctx)
