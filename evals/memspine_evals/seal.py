"""ER-36: the exposure ledger of the sealed blind sets, and the readers the gate callers share.

A blind set is only blind while nobody tunes against it. Every time an arm is scored on one, a line is appended to an
append-only JSONL ledger (default ``<runs>/_logs/blind_exposure.jsonl``):

    set_id, slice_hash, arm {run_id, commit, flags_hash, signature, identity_status}, ref_run, comparability_key,
    timestamp (UTC), verdict, reexpose (reason or null), prior_exposures

Rule: an arm (``signature`` = commit + flags hash) is scored on a sealed set ONCE. A second scoring of the same
signature on the same set is refused (``SealRefused``) unless a reason is given, which is then recorded. The ledger
holds aggregates' identities only: no question id, text, answer or per-question result ever goes into it.

An arm whose manifest carries no identity (a run that predates P0-03) falls back to ``run:<run id>`` as its signature,
so two different old arms are never mistaken for one.

``read_manifest`` also lives here: the gate callers (``eval_focus``, ``eval_blind``, ``eval_paired``) use it to hand
each run's manifest to ``paired_gate.paired`` so ``INCOMPARABLE`` is enforced.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .manifest import comparability_key

LEDGER_NAME = "blind_exposure.jsonl"


class SealRefused(RuntimeError):
    """Raised when a sealed blind set would be scored a second time by the same arm without a reason."""


def default_ledger(runs_dir: Path) -> Path:
    return Path(runs_dir) / "_logs" / LEDGER_NAME


def read_manifest(results_path: Path) -> dict[str, Any] | None:
    """The manifest line of a results file, or ``None`` when the file has none (a run from before manifests).
    Stops at the first manifest line, so a large file is not read through."""
    path = Path(results_path)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            d = json.loads(line)
            if d.get("kind") == "manifest":
                return d
    return None


@dataclass(frozen=True)
class ArmId:
    run_id: str
    commit: str | None
    flags_hash: str | None
    signature: str
    identity_status: str  # "identity" | "git-only" | "none"

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "commit": self.commit,
            "flags_hash": self.flags_hash,
            "signature": self.signature,
            "identity_status": self.identity_status,
        }


def arm_id(run_id: str, manifest: Mapping[str, Any] | None) -> ArmId:
    """Identify an arm by commit + flags hash (the thing a behaviour change varies)."""
    ident = (manifest or {}).get("identity") or {}
    commit = ident.get("commit") or ((manifest or {}).get("code") or {}).get("memspine_git")
    flags_hash = ident.get("flags_hash") if ident.get("flags") is not None else None
    if ident.get("commit") and flags_hash:
        return ArmId(run_id, commit, flags_hash, f"{commit}+{flags_hash}", "identity")
    # no pinned commit or no recorded flags: the arm cannot be told apart from another run at the same commit
    return ArmId(run_id, commit, flags_hash, f"run:{run_id}", "none" if not commit else "git-only")


def read_ledger(ledger: Path) -> list[dict[str, Any]]:
    path = Path(ledger)
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def prior_exposures(ledger: Path, set_id: str, arm: ArmId) -> list[dict[str, Any]]:
    return [
        e
        for e in read_ledger(ledger)
        if e.get("set_id") == set_id and (e.get("arm") or {}).get("signature") == arm.signature
    ]


def check(ledger: Path, set_id: str, arm: ArmId, reexpose: str | None = None) -> int:
    """Number of earlier scorings of this arm on this set. Raises ``SealRefused`` when there is one and no
    ``reexpose`` reason was given."""
    n = len(prior_exposures(ledger, set_id, arm))
    if n and not (reexpose or "").strip():
        raise SealRefused(
            f"sealed blind set '{set_id}' was already scored for arm {arm.signature} ({n}x, see {ledger}); "
            'a second look needs --reexpose "<reason>", which is recorded'
        )
    return n


def record(
    ledger: Path,
    *,
    set_id: str,
    slice_hash: str,
    arm: ArmId,
    ref_run: str,
    manifest: Mapping[str, Any] | None,
    verdict: str,
    reexpose: str | None = None,
    prior: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one exposure to the ledger (creating ``_logs`` if needed). Returns the entry."""
    entry = {
        "set_id": set_id,
        "slice_hash": slice_hash,
        "arm": arm.as_dict(),
        "ref_run": ref_run,
        "comparability_key": comparability_key(manifest) if manifest else None,
        "timestamp": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "verdict": verdict,
        "reexpose": (reexpose or "").strip() or None,
        "prior_exposures": prior,
    }
    path = Path(ledger)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return entry
