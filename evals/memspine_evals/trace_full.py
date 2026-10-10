"""``--trace-full``: log what a run normally leaves out (opt-in, off by default: size).

Per question the harness then keeps, in the result row's ``meta["trace_full"]``, the exact
reader prompt (system + user message), the raw reader reply, every further reader call of the
question (refusal retry, count-verify, date repair, each with its own prompt and reply), and
every judge call (prompt and raw reply). With ``MEMSPINE_FORENSICS_DIR`` set the memspine
system adapter also writes ``write_trace.jsonl`` (one line per stored record: the stored
fields, the firewall verdict as stamped on the record, the tags, deterministic write-side
signals, then the sleep cycle's derived records with their parents) and adds a ``trace_full``
block (query analysis, perspective, overlay, dedupe drops, assembled summary) to every
``forensics.jsonl`` row. Scoring is untouched: the flag only records.

The pure helpers below (:func:`query_analysis`, :func:`write_signals`) are deterministic and
model-free, so the forensics explorer can run the same code offline to reconstruct a step a
run did not log (and label it "reconstructed").
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_SINK: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "memspine_evals_trace_full", default=None
)

#: environment switch read by the memspine system adapter (the CLI sets it with the flag)
ENV = "MEMSPINE_TRACE_FULL"


@contextmanager
def capture(enabled: bool) -> Iterator[list[dict[str, Any]] | None]:
    """Collect the :func:`record` calls made inside the block (None when disabled)."""
    if not enabled:
        yield None
        return
    sink: list[dict[str, Any]] = []
    token = _SINK.set(sink)
    try:
        yield sink
    finally:
        _SINK.reset(token)


def record(kind: str, **fields: Any) -> None:
    """Append one model call to the active capture; a single ``is None`` test otherwise."""
    sink = _SINK.get()
    if sink is not None:
        sink.append({"kind": kind, "step": len(sink), **fields})


def enabled_in(environ: Any) -> bool:
    return str(environ.get(ENV, "")).lower() in ("1", "true", "yes", "on")


_FLAGS = (
    "is_set_question",
    "is_set_question_wide",
    "is_intent_list",
    "is_count",
    "is_aggregation",
    "is_temporal",
    "is_ordering",
    "is_inference",
    "is_verbatim",
    "is_duration",
    "is_novelty",
    "is_personal",
)


def query_analysis(question: str) -> dict[str, Any]:
    """Shape flags of a question, from the engine's own rule code (no model, no store)."""
    from memspine.core import query_shape as qs

    flags: dict[str, Any] = {}
    for name in _FLAGS:
        try:
            flags[name] = bool(getattr(qs, name)(question))
        except Exception as exc:  # a rule that needs more than the text
            flags[name] = f"error: {type(exc).__name__}"
    out: dict[str, Any] = {"flags": flags}
    for name in ("rule_read_mode", "question_shape", "statement_form"):
        try:
            out[name] = getattr(qs, name)(question)
        except Exception as exc:
            out[name] = f"error: {type(exc).__name__}"
    with contextlib.suppress(Exception):
        out["split_intents"] = list(qs.split_intents(question))
    try:
        from .readers import generic_qa_shape, qa_shape

        out["qa_shape"] = qa_shape(question)
        out["generic_qa_shape"] = generic_qa_shape(question)
    except Exception:
        pass
    return out


def write_signals(text: str) -> dict[str, Any]:
    """The deterministic write-side signals of one turn's text: the instruction-framing
    screens of the firewall, PII hits and the sensitivity grade. The embedding-outlier and
    MINJA prefix signals need the store's history and are not reproducible from one turn."""
    from memspine.core import firewall, redaction, sensitivity

    out: dict[str, Any] = {
        "instruction_shaped": bool(firewall.instruction_shaped(text)),
        "instruction_extended": bool(firewall.extended_instruction_shaped(text)),
        "semantic_risk": list(firewall.semantic_risk(text)),
        "pii": list(redaction.find_pii(text)),
    }
    label = sensitivity.grade_text(text)
    out["sensitivity"] = {"grade": label.grade, "categories": list(label.categories)}
    out["validation"] = {"empty": not text.strip(), "chars": len(text)}
    return out


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


def write_trace_row(record_obj: Any, turn_id: str, source_text: str) -> dict[str, Any]:
    """One ``write_trace.jsonl`` line: the record as stored plus the signals of its source."""
    r = record_obj
    src = getattr(r, "source", None)
    stored = None
    if r is not None:
        stored = {
            "memory_type": str(getattr(r, "memory_type", None)),
            "tags": list(getattr(r, "tags", []) or []),
            "entity": getattr(r, "entity", None),
            "attribute": getattr(r, "attribute", None),
            "group_id": getattr(r, "group_id", None),
            "status": str(getattr(r, "status", None)),
            "version": getattr(r, "version", None),
            "evolve_to": getattr(r, "evolve_to", None),
            "trust": getattr(r, "trust", None),
            "quarantined": getattr(r, "quarantined", None),
            "instruction_flag": getattr(r, "instruction_flag", None),
            "corroborations": getattr(r, "corroborations", None),
            "pii_tier": str(getattr(r, "pii_tier", None)),
            "consent_tags": list(getattr(r, "consent_tags", []) or []),
            "fingerprint": getattr(r, "content_fingerprint", None),
            "simhash": getattr(r, "simhash", None),
            "valid_from": _iso(getattr(r, "valid_from", None)),
            "recorded_at": _iso(getattr(r, "recorded_at", None)),
            "source": None
            if src is None
            else {
                "role": getattr(src, "role", None),
                "channel": getattr(src, "channel", None),
                "principal": getattr(src, "principal", None),
                "parents": list(getattr(src, "parents", []) or []),
            },
        }
    return {
        "turn": turn_id,
        "record_id": str(r.record_id) if r is not None else None,
        "written": r is not None,
        "stored": stored,
        "signals": write_signals(source_text),
    }


def derived_row(record_obj: Any, origin: dict[str, str]) -> dict[str, Any]:
    """A record the engine derived itself (sleep cycle: facts, cards, summaries, edges)."""
    row = write_trace_row(record_obj, "", getattr(record_obj, "content", "") or "")
    stored = row["stored"] or {}
    parents = (stored.get("source") or {}).get("parents", [])
    row.update(
        derived=True,
        content=getattr(record_obj, "content", None),
        parent_turns=[origin.get(str(p), str(p)) for p in parents],
    )
    return row


READS_FILE = "reads.jsonl"
#: files above this size are gzipped when the run ends (``finalize``)
GZIP_ABOVE_BYTES = 20_000_000


def write_read(directory: Any, row: dict[str, Any]) -> None:
    """Append one per-question line (prompts, raw replies, verdict) to ``reads.jsonl``."""
    import json
    from pathlib import Path

    path = Path(directory) / READS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def finalize(directory: Any, threshold: int = GZIP_ABOVE_BYTES) -> list[str]:
    """Gzip every ``*.jsonl`` of the trace folder larger than ``threshold`` bytes (the plain
    file is removed); returns the names compressed. Readers open ``.jsonl`` or ``.jsonl.gz``."""
    import gzip
    import shutil
    from pathlib import Path

    done = []
    for path in sorted(Path(directory).glob("*.jsonl")):
        if path.stat().st_size > threshold:
            with path.open("rb") as src, gzip.open(f"{path}.gz", "wb") as dst:
                shutil.copyfileobj(src, dst)
            path.unlink()
            done.append(path.name)
    return done
