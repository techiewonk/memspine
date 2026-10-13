"""The retention profile an engine runs under (T5-05): config -> LIB-41 ``RetentionProfile``.

Pure and config-only (no I/O). The profile starts from the named template when ``retention.profile``
is set, else from an empty one. The event-log mode always follows ``event_log.mode`` (it is what the
store actually does); ``audit.reads`` turns read auditing on. ``retention.classes`` are not folded
in here: they carry a namespace glob the LIB-41 match grammar cannot express, so
:func:`class_rule` makes one rule per class and the caller groups records by class.
"""

from __future__ import annotations

from typing import Any

from memspine.trust.retention import RetentionProfile, RetentionRule, default_profile

__all__ = ["build_profile", "class_rule"]

_CLASS_PRIORITY = 1000  # a configured class outranks the template's classes


def build_profile(config: Any) -> RetentionProfile:
    """The effective profile for ``config`` (a ``MemspineConfig``)."""
    name = config.retention.profile
    base = default_profile(name) if name else RetentionProfile(name="config")
    mode = str(getattr(config.event_log.mode, "value", config.event_log.mode))
    window = f"P{int(config.event_log.retention_days)}D" if mode == "rolling" else None
    return base.model_copy(
        update={
            "event_log_mode": mode,
            "rolling_window": window,
            "audit_reads": bool(base.audit_reads or config.audit.reads),
        }
    )


def class_rule(index: int, ttl_days: float) -> RetentionRule:
    """One rule for the ``index``-th ``retention.classes`` entry (matches every record given)."""
    seconds = max(1, round(ttl_days * 86400))
    return RetentionRule(
        name=f"class{index}", match="*", ttl=f"PT{seconds}S", priority=_CLASS_PRIORITY
    )
