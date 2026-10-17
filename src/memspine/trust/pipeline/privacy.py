"""Trust: privacy moved out of the engine (F-12)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from memspine.models.providers.llm.tier_gate import WITHHELD_MARKER, is_local_binding
from memspine.store.records import MemoryRecord
from memspine.trust.privacy import pii_rank


def _remote_view(eng: Any, role: str, records: Sequence[MemoryRecord]) -> list[MemoryRecord]:
    """#50: ``records`` as a prompt for ``role`` may carry them. When ``role`` is
    bound to a remote provider under ``consent.remote_llm_max_tier``, a record
    above the tier (a lead block or derived record carries its parts' highest
    tier) shows :data:`WITHHELD_MARKER` instead of its text, before the prompt
    is rendered; the provider-boundary text match stays as the backstop."""
    config = eng._config()
    limit = config.consent.remote_llm_max_tier
    role_config = config.llm.roles.get(role)
    if limit is None or role_config is None:
        return list(records)
    if is_local_binding(role_config.provider, role_config.settings, config.consent.local_hosts):
        return list(records)
    cap = pii_rank(limit)
    return [
        r.model_copy(update={"content": WITHHELD_MARKER}) if pii_rank(r.pii_tier) > cap else r
        for r in records
    ]
