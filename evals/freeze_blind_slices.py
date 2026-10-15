"""V03: cut the two BLIND validation slices once, with a content hash, and freeze them.

    python evals/freeze_blind_slices.py --mab data/mab/Conflict_Resolution.parquet \
        --convomem data/convomem          # writes analysis/blind_split_{mab,convomem}.json
    python evals/freeze_blind_slices.py --check   # verify the saved files (ids vs hash)

BLIND - validation only, never inspect per-question failures for design. These slices are the
anti-overfitting check on every adopted engine change: nothing is tuned on them, and nobody
reads their per-question rows to design a fix. Only aggregate deltas leave ``eval_blind.py``.

* MAB-CR: the 6k and 32k FactConsolidation items (sh and mh), 4 items x 100 q = 400 q.
* ConvoMem: 200 questions stratified by evidence type (6 types: 34/34/33/33/33/33), picked by
  the sha256 rank of the item id inside each type. No content is read to choose.

The files hold ids and hashes only (no questions, answers or conversation text). Both go
through ``memspine_evals.split.split_ids`` (the engine of ``make_split.py``): ``dev_items`` is
the blind slice, ``heldout_items`` the rest (never used), ``blind_items`` repeats the slice
under its real name, and ``content_sha256`` pins the data bytes. ConvoMem ids need
``--per-stratum 1000`` (run.sh adds it).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE]
sys.path.append(str(HERE))

from memspine_evals.split import Split, split_ids  # noqa: E402

ANALYSIS = HERE / "analysis"
MAB_FILE = ANALYSIS / "blind_split_mab.json"
CONVOMEM_FILE = ANALYSIS / "blind_split_convomem.json"
BLIND_NOTE = (
    "BLIND - validation only, never inspect per-question failures for design. "
    "Never used for tuning. dev_items = blind_items = the slice; heldout_items = the rest, unused."
)
MAB_BLIND_ITEMS = (
    "factconsolidation_sh_6k",
    "factconsolidation_mh_6k",
    "factconsolidation_sh_32k",
    "factconsolidation_mh_32k",
)
CONVOMEM_N = 200
CONVOMEM_SALT = "blind-v1"


def convomem_pick(ids_by_type: dict[str, list[str]], n: int = CONVOMEM_N) -> list[str]:
    """Stratified pick: ``n`` split over evidence types (earlier types take the remainder),
    inside a type the lowest sha256(salt:id) first. Deterministic; reads no content."""
    types = sorted(ids_by_type)
    base, extra = divmod(n, len(types))
    picked: list[str] = []
    for i, t in enumerate(types):
        quota = base + (1 if i < extra else 0)
        ranked = sorted(
            ids_by_type[t], key=lambda x: hashlib.sha256(f"{CONVOMEM_SALT}:{x}".encode()).hexdigest()
        )
        picked.extend(ranked[:quota])
    return picked


def _stamp(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["blind"] = True
    payload["blind_items"] = list(payload["dev_items"])
    payload["status"] = BLIND_NOTE
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _save(ids: list[str], blind: list[str], *, dataset_id: str, unit: str, sha: str,
          revision: str, out: Path, note: str) -> Path:
    split = split_ids(ids, dataset_id=dataset_id, content_sha256=sha, unit=unit, dev=blind,
                      seed=None, revision_id=revision, note=f"{BLIND_NOTE} {note}")
    split.save(out)
    _stamp(out)
    return out


def cut_mab(path: Path, out: Path = MAB_FILE) -> Path:
    from memspine_evals.datasets import MemoryAgentBenchDataset

    ds = MemoryAgentBenchDataset(path, revision_id="auto")
    info = ds.info()
    ids = [i.item_id for i in ds.items()]
    return _save(ids, list(MAB_BLIND_ITEMS), dataset_id=info.dataset_id, unit="item (context size)",
                 sha=info.content_sha256, revision=info.revision_id, out=out,
                 note="6k + 32k items, sh and mh: 400 q.")


def cut_convomem(root: Path, out: Path = CONVOMEM_FILE) -> Path:
    from memspine_evals.datasets import ConvoMemDataset

    ds = ConvoMemDataset(root, revision_id="auto", per_stratum=1000)
    info = ds.info()
    by_type: dict[str, list[str]] = {}
    for item in ds.items():
        by_type.setdefault(item.item_id.split("/")[0], []).append(item.item_id)
    ids = [i for v in by_type.values() for i in v]
    return _save(ids, convomem_pick(by_type), dataset_id=info.dataset_id, unit="question",
                 sha=info.content_sha256, revision=info.revision_id, out=out,
                 note=f"{CONVOMEM_N} q stratified over evidence types by sha256 rank "
                 f"(salt {CONVOMEM_SALT}); ids need --per-stratum 1000.")


def check(path: Path) -> int:
    split = Split.load(path)
    split.verify_ids()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not payload.get("blind") or payload.get("blind_items") != list(split.dev_items):
        print(f"{path.name}: not a consistent blind file")
        return 1
    print(f"ok: {path.name} blind={len(split.dev_items)} rest={len(split.heldout_items)} "
          f"sha={split.content_sha256[:12]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mab")
    ap.add_argument("--convomem")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    if a.check:
        return max(check(MAB_FILE), check(CONVOMEM_FILE))
    for flag, fn, out in ((a.mab, cut_mab, MAB_FILE), (a.convomem, cut_convomem, CONVOMEM_FILE)):
        if flag:
            if out.exists():
                raise SystemExit(f"{out.name} already exists: the slices are cut once and frozen")
            print(fn(Path(flag)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
