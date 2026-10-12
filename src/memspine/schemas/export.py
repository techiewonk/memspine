# ruff: noqa: E501
"""JSON Schema export (S1-4): ``python -m memspine.schemas export <outdir>``.

Layout: ``<outdir>/{durable,public,internal}/<Name>.json`` plus ``<outdir>/index.json``. Output is
deterministic (sorted keys, two-space indent, trailing newline, LF line endings). Durable and public are
strict (CI fails on a diff without a version bump); internal is advisory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from memspine.schemas.base import DurableModel, InternalModel, PublicModel, TierModel

TIERS: tuple[str, ...] = ("durable", "public", "internal")
_ROOTS: dict[str, type[TierModel]] = {
    "durable": DurableModel,
    "public": PublicModel,
    "internal": InternalModel,
}
_README = {
    "durable": "strict tier; changes need a version bump; populated by later schema sessions\n",
    "public": "strict tier; changes need a version bump; populated by later schema sessions\n",
}
_INTERNAL_NOTE = "no promise: exported for reading; diffs are not enforced"


def _load_all() -> None:
    """Import every module that defines models so subclasses are registered."""
    import memspine.schemas.steps  # noqa: F401


def _walk(root: type[TierModel]) -> list[type[TierModel]]:
    seen: dict[str, type[TierModel]] = {}
    stack = list(root.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if not cls.__module__.startswith("memspine.schemas."):
            continue  # models defined in tests or plug-ins are not part of the schema package
        if cls.__name__ in seen and seen[cls.__name__] is not cls:
            raise ValueError(f"duplicate model name {cls.__name__!r}")
        seen[cls.__name__] = cls
    return [seen[name] for name in sorted(seen)]


def collect_models() -> dict[str, list[type[TierModel]]]:
    """Models per tier, sorted by name. Names must be unique across tiers."""
    _load_all()
    out = {tier: _walk(root) for tier, root in _ROOTS.items()}
    names = [c.__name__ for models in out.values() for c in models]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"model names not unique across tiers: {dupes}")
    return out


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render() -> dict[str, str]:
    """Relative path -> file text for the whole export (no I/O)."""
    files: dict[str, str] = {}
    index: dict[str, Any] = {"models": [], "note": {"internal": _INTERNAL_NOTE}}
    for tier, models in collect_models().items():
        for cls in models:
            rel = f"{tier}/{cls.__name__}.json"
            files[rel] = _dump(cls.model_json_schema())
            entry: dict[str, Any] = {
                "name": cls.__name__,
                "tier": tier,
                "file": rel,
                "module": cls.__module__,
            }
            if tier == "durable":
                entry["kind"] = getattr(cls, "kind", None)
                entry["schema_version"] = cls.model_fields["schema_version"].default
            if tier == "public":
                entry["semver"] = getattr(cls, "__semver__", None)
            index["models"].append(entry)
    index["models"].sort(key=lambda e: (e["tier"], e["name"]))
    files["index.json"] = _dump(index)
    for tier, text in _README.items():
        files[f"{tier}/README.md"] = text
    return files


def export(outdir: str | Path) -> list[Path]:
    """Write the export to ``outdir``; returns written paths. Stale ``*.json`` files of a tier are removed."""
    out = Path(outdir)
    files = render()
    for tier in TIERS:
        (out / tier).mkdir(parents=True, exist_ok=True)
        for old in (out / tier).glob("*.json"):
            if f"{tier}/{old.name}" not in files:
                old.unlink()
    written: list[Path] = []
    for rel, text in sorted(files.items()):
        path = out / rel
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        written.append(path)
    return written
