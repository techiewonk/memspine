"""I14: errata files for ANY benchmark (generalises ``analysis/locomo_errata.json`` loading).

An errata file lists questions whose gold is unusable (wrong, or needs an image the harness
does not have). Accuracy is reported with and without them. No corpus text is stored.

Format (``errata/v1``; the older ``locomo_errata/v1`` header is accepted unchanged)::

    {
      "schema": "errata/v1",
      "dataset": "prefeval",                    # dataset_id the entries refer to
      "dataset_sha256_prefix": "79fa87e90f04",  # optional: refuse a different data revision
      "exclude_tags": ["gold_error", "needs_image"],   # optional; this is the default
      "entries": [
        {"item": "conv-26", "qid": "0-5", "tag": "gold_error", "borderline": false,
         "reason": "gold contradicts the transcript"}
      ]
    }

An entry may also carry an evidence adjudication (M02), for the tags ``bad_evidence_id`` and
``evidence_label_error``: ``evidence_adjudicated`` (the turn ids the evidence should name),
``source_span`` (``{"turn": id, "quote": text}``, the exact span that supports it) and
``reviewed_by`` (an independent reviewer). Official labels stay immutable: the adjudication is a
separate diagnostic column (``adjudications`` / ``check_adjudications``), and each id must be a
turn of the same conversation. Mechanical repairs (``D:11:26`` -> ``D11:26``) need no entry: see
``memspine_evals.evidence``.

``item_id`` / ``query_id`` are accepted as aliases of ``item`` / ``qid``. A tag outside
``exclude_tags`` (e.g. ``evidence_label_error``: right answer, wrong evidence label) is kept in
the file for the record but does not drop the question. When one question has several
entries, a non-borderline one wins. ``python -m memspine_evals.errata FILE`` validates a file.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_EXCLUDE_TAGS",
    "ErrataError",
    "adjudications",
    "check_adjudications",
    "evidence_candidates",
    "load_errata",
    "partition",
    "validate_errata_file",
]

DEFAULT_EXCLUDE_TAGS: tuple[str, ...] = ("gold_error", "needs_image")


class ErrataError(ValueError):
    """The errata file is malformed or refers to other data than the run used."""


def _read(path: Path | str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise ErrataError(f"{path}: expected an object with an 'entries' list")
    schema = str(payload.get("schema", ""))
    if schema and not (schema == "errata/v1" or schema.endswith("_errata/v1")):
        raise ErrataError(f"{path}: unknown schema {schema!r} (want errata/v1)")
    return payload


def _entry_key(e: Mapping[str, Any]) -> tuple[str, str]:
    item = e.get("item", e.get("item_id"))
    qid = e.get("qid", e.get("query_id"))
    if item is None or qid is None or not e.get("tag"):
        raise ErrataError(f"entry needs item/qid/tag: {dict(e)!r}")
    return str(item), str(qid)


def validate_errata_file(path: Path | str, *, dataset: str | None = None) -> dict[str, Any]:
    """Raise ``ErrataError`` on a malformed file; return counts per tag otherwise."""
    payload = _read(path)
    if dataset is not None and payload.get("dataset") not in (None, dataset):
        raise ErrataError(f"{path}: file is for dataset {payload.get('dataset')!r}, not {dataset!r}")
    counts: dict[str, int] = {}
    for e in payload["entries"]:
        _entry_key(e)
        counts[e["tag"]] = counts.get(e["tag"], 0) + 1
    return {"dataset": payload.get("dataset"), "entries": len(payload["entries"]), "by_tag": counts}


def load_errata(
    path: Path | str | None,
    *,
    exclude_tags: Iterable[str] | None = None,
    dataset: str | None = None,
    content_sha256: str | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    """(item, qid) -> {"tag", "borderline"} for the entries that invalidate grading.

    ``{}`` when ``path`` is None or missing (no errata known for that benchmark). With
    ``dataset`` / ``content_sha256`` given, a file made for other data raises ``ErrataError``
    (an errata list does not transfer across data revisions). ``exclude_tags`` defaults to the
    file's own list, else ``DEFAULT_EXCLUDE_TAGS``.
    """
    if path is None or not Path(path).exists():
        return {}
    payload = _read(path)
    if dataset is not None and payload.get("dataset") not in (None, dataset, f"{dataset}.json"):
        raise ErrataError(f"{path}: file is for dataset {payload.get('dataset')!r}, not {dataset!r}")
    prefix = payload.get("dataset_sha256_prefix")
    if content_sha256 and prefix and not content_sha256.startswith(str(prefix).removeprefix("sha256:")):
        raise ErrataError(f"{path}: made for data {prefix}, this run used {content_sha256[:12]}")
    tags = tuple(exclude_tags) if exclude_tags is not None else tuple(
        payload.get("exclude_tags") or DEFAULT_EXCLUDE_TAGS
    )
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for e in payload["entries"]:
        if e.get("tag") not in tags:
            continue
        key = _entry_key(e)
        cur = out.get(key)
        if cur is None or (cur["borderline"] and not e.get("borderline", False)):
            out[key] = {"tag": e["tag"], "borderline": bool(e.get("borderline", False))}
    return out


def partition(
    rows: Sequence[Mapping[str, Any]],
    errata: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    include_borderline: bool = False,
    item_key: str = "item_id",
    query_key: str = "query_id",
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """(kept, dropped) rows. Works on result rows (``item_id``/``query_id``) or forensic rows
    (pass ``item_key="item"``, ``query_key="qid"``)."""
    kept: list[Mapping[str, Any]] = []
    dropped: list[Mapping[str, Any]] = []
    for r in rows:
        e = errata.get((str(r[item_key]), str(r[query_key])))
        (dropped if e is not None and (include_borderline or not e["borderline"]) else kept).append(r)
    return kept, dropped


def adjudications(path: Path | str | None) -> dict[tuple[str, str], dict[str, Any]]:
    """(item, qid) -> {"tag", "adjudicated", "source_span", "reviewed_by"} for the entries that
    carry ``evidence_adjudicated``. A diagnostic only: it never changes an official label."""
    if path is None or not Path(path).exists():
        return {}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for e in _read(path)["entries"]:
        if "evidence_adjudicated" not in e:
            continue
        out[_entry_key(e)] = {
            "tag": e["tag"],
            "adjudicated": [str(x) for x in e["evidence_adjudicated"]],
            "source_span": e.get("source_span"),
            "reviewed_by": e.get("reviewed_by"),
        }
    return out


def check_adjudications(
    adj: Mapping[tuple[str, str], Mapping[str, Any]],
    known_ids: Mapping[str, Collection[str]],
) -> None:
    """Raise ``ErrataError`` unless every adjudication names turns of its own conversation, has an
    exact source span (a turn of that conversation plus a non-empty quote) and a reviewer.
    ``known_ids`` maps item id -> the turn ids of that conversation."""
    for (item, qid), a in adj.items():
        known = known_ids.get(item)
        where = f"{item}:{qid}"
        if known is None:
            raise ErrataError(f"{where}: no turn ids for item {item!r}")
        if not a["adjudicated"]:
            raise ErrataError(f"{where}: empty evidence_adjudicated")
        for t in a["adjudicated"]:
            if t not in known:
                raise ErrataError(f"{where}: {t!r} is not a turn of {item}")
        span = a.get("source_span") or {}
        if span.get("turn") not in known or not str(span.get("quote") or "").strip():
            raise ErrataError(f"{where}: adjudication needs source_span {{turn, quote}} in {item}")
        if not a.get("reviewed_by"):
            raise ErrataError(f"{where}: adjudication needs an independent reviewed_by")


def evidence_candidates(
    questions: Iterable[tuple[str, str, Sequence[str]]],
    known_ids: Mapping[str, Collection[str]],
) -> list[dict[str, Any]]:
    """Errata-shaped ``bad_evidence_id`` candidates from raw evidence: every question whose view is
    ``repaired`` or ``unresolved``. ``questions`` yields (item, qid, raw evidence list). Candidates
    are for review; nothing is added to a file and no label changes."""
    from .evidence import normalise_evidence

    out: list[dict[str, Any]] = []
    for item, qid, raw in questions:
        view = normalise_evidence(raw, known_ids.get(item, ()))
        if view.status == "ok":
            continue
        bad = [r.to_dict() for r in view.refs if r.status != "ok"]
        out.append(
            {
                "item": item,
                "qid": qid,
                "tag": "bad_evidence_id",
                "status": view.status,
                "refs": bad,
                "resolved": list(view.resolved_ids),
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    for p in args:
        print(p, json.dumps(validate_errata_file(p)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
