"""JSON Schema export (S1-4): ``python -m memspine.schemas export <outdir>``.

Layout: ``<outdir>/{durable,public,internal}/<Name>.json`` plus ``<outdir>/index.json``. Output is
deterministic (sorted keys, two-space indent, trailing newline, LF line endings). Durable and public are
strict (CI fails on a diff without a version bump); internal is advisory.
"""

from __future__ import annotations

import json
import sys
from functools import reduce
from pathlib import Path
from typing import Any

from memspine.schemas.base import (
    DurableModel,
    DurablePart,
    InternalModel,
    PublicModel,
    TierModel,
    kind_id,
)

TIERS: tuple[str, ...] = ("durable", "public", "internal")
_ROOTS: dict[str, tuple[type[TierModel], ...]] = {
    "durable": (DurableModel, DurablePart),
    "public": (PublicModel,),
    "internal": (InternalModel,),
}
_README = {
    "durable": (
        "strict tier. Each persisted kind carries x-kind and x-schema-version; a change to a schema needs\n"
        "a schema_version bump. Value types embedded in kinds (no x-schema-version) are\n"
        "checked through the kinds that embed them.\n"
    ),
    "public": "strict tier. Each model carries x-semver; a change to a schema needs a semver bump.\n",
}
_INTERNAL_NOTE = "no promise: exported for reading; diffs are not enforced"


def _load_all() -> None:
    """Import every module that defines models so subclasses are registered."""
    import memspine.schemas.durable
    import memspine.schemas.public
    import memspine.schemas.steps  # noqa: F401


def _walk(roots: tuple[type[TierModel], ...]) -> list[type[TierModel]]:
    seen: dict[str, type[TierModel]] = {}
    stack = [sub for root in roots for sub in root.__subclasses__()]
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if not cls.__module__.startswith("memspine.schemas."):
            continue  # models defined in tests or plug-ins are not part of the schema package
        path = cls.__qualname__.split(".")
        start: object = sys.modules.get(cls.__module__)
        owner = reduce(lambda obj, name: getattr(obj, name, None), path, start)
        if owner is not cls:
            continue  # runtime-built subclasses (e.g. frozen config snapshots) are not importable
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
            schema = cls.model_json_schema()
            if tier == "durable" and kind_id(cls) is not None:
                schema["x-kind"] = kind_id(cls)
                schema["x-schema-version"] = cls.model_fields["schema_version"].default
            if tier == "public":
                schema["x-semver"] = getattr(cls, "__semver__", "0.0.0")
            files[rel] = _dump(schema)
            entry: dict[str, Any] = {
                "name": cls.__name__,
                "tier": tier,
                "file": rel,
                "module": cls.__module__,
            }
            if tier == "durable":
                entry["kind"] = kind_id(cls)
                entry["schema_version"] = (
                    cls.model_fields["schema_version"].default if kind_id(cls) else None
                )
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


def schema_version_of(tier: str, schema_text: str) -> Any:
    """The version recorded in an exported schema: ``x-schema-version`` (durable kind) or ``x-semver``
    (public). ``None`` for durable value types, which carry no version of their own."""
    data = json.loads(schema_text)
    return data.get("x-schema-version") if tier == "durable" else data.get("x-semver")


def strict_violations(fresh: dict[str, str], baseline: dict[str, str]) -> list[str]:
    """Strict-tier CI check (S1-4): compare a fresh export with the committed baseline.

    ``fresh`` and ``baseline`` map relative paths (``durable/X.json``) to file text. Only the strict tiers
    (durable and public) are compared. Returns human-readable problems:

    * a schema changed but its ``schema_version`` (durable) or ``x-semver`` (public) did not;
    * a schema changed with a bump, or was added or removed, and the committed export was not refreshed.

    An empty list means the committed baseline is current and no change slipped through unversioned.
    """
    problems: list[str] = []
    strict = ("durable/", "public/")
    names = sorted({n for n in (*fresh, *baseline) if n.startswith(strict) and n.endswith(".json")})
    for name in names:
        tier = name.split("/", 1)[0]
        new, old = fresh.get(name), baseline.get(name)
        if new is None:
            problems.append(
                f"{name}: removed from the models but still in the baseline (re-export)"
            )
        elif old is None:
            problems.append(f"{name}: new model missing from the committed baseline (re-export)")
        elif new != old:
            if schema_version_of(tier, new) is None:
                problems.append(
                    f"{name}: value type changed; bump the schema_version of every kind embedding it "
                    f"(those kinds' schemas changed too) and re-export"
                )
            elif schema_version_of(tier, new) == schema_version_of(tier, old):
                problems.append(
                    f"{name}: schema changed without a version bump "
                    f"({schema_version_of(tier, old)!r}); bump schema_version (durable, with current-format validation) or __semver__ (public)"
                )
            else:
                problems.append(
                    f"{name}: version bumped but the committed baseline is stale (re-export)"
                )
    return problems
