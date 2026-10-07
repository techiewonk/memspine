"""TOFU and MUSE-News adapters for memory erasure, retrieval-only.

Both benchmarks were built for *model* unlearning (forgetting in the weights). Here they are
read as *memory* erasure probes: the corpus is deposited as memories, the forget set is
hard-deleted, and the question is whether retrieval can still bring the deleted content back
(residual recall) and whether the retain set is still found (collateral loss).

Sources, checked 2026-10-07:

- TOFU (Maini et al., COLM 2024): Hugging Face ``locuslab/TOFU`` @ ``324592d``. **MIT.**
  JSON-lines files despite the ``.json`` suffix, one ``{"question", "answer"}`` per line.
  ``full.json`` holds 4,000 QA about 200 fictitious authors (20 each, in author order);
  ``forget01/05/10`` are the last 40 / 200 / 400 rows of it and ``retain99/95/90`` the
  rest; ``holdout01/05/10`` are *other* authors, never in ``full``. ``forgetNN_perturbed``
  and ``retain_perturbed`` add ``paraphrased_question`` and ``paraphrased_answer``.
- MUSE-News (Shi et al., ICLR 2025): Hugging Face ``muse-bench/MUSE-News`` @ ``506bd5b``.
  **CC BY 4.0.** JSON lists. Local: ``privleak/{forget,retain2,holdout}.json`` (100 news
  passages each), ``verbmem/forget.json`` (the same 100 forget passages) and
  ``knowmem/{forget,retain2}-qa.json``. The ``raw`` corpora (889 forget / 1,778 retain
  documents) are not held.

Mapping to the harness contract (one ``EvalItem`` holding the whole deposited corpus):

- **TOFU:** one ``Turn`` per ``full`` row (``turn_id`` = ``"tofu:<row>"``, ``session_id`` =
  ``"author<row // 20>"``, ``speaker`` = ``"fact"``), whose text is the *answer* (the fact);
  the question is kept in ``Turn.meta``. Queries, each with ``meta["probe_set"]``:
  ``forget`` (the original question; gold = its row), ``forget_paraphrase`` (the
  paraphrased question; same gold), ``retain`` and ``retain_paraphrase`` (from
  ``retain_perturbed``; gold = their row), ``holdout`` (other authors; no gold, for a
  membership AUROC over top scores).
- **MUSE-News:** one ``Turn`` per ``privleak`` forget and retain passage (``"muse:forget:<n>"``
  / ``"muse:retain:<n>"``). Queries: ``forget`` and ``retain`` = the first ``probe_words``
  words of each passage (the verbmem prompt shape; gold = that passage), ``holdout`` = the
  same prefix of a never-deposited holdout passage (no gold).

``item.meta["forget_turn_ids"]`` lists what the erasure step must delete and
``item.meta["forget_probes"]`` maps each to the texts :func:`erase_and_verify` hands
``Engine.verify_forget(probe=...)``.

**Free measures:** before erasure, R@k of the ``forget`` probes is the sanity ceiling; after
it, the same R@k is the **residual recall** (should be 0) and ``retain`` R@k is the
collateral check (should not move). :func:`erase_and_verify` gives the engine-side proof
(``clean`` and ``residual_recall`` per record), and :func:`membership_auroc` the privleak-style
leakage over forget-vs-holdout top scores (0.5 = no leakage).

**Gaps:** the official metrics are model-unlearning ones (forget quality via a KS test on
truth ratios, ROUGE, model utility; MUSE verbmem / knowmem ROUGE, privleak via Min-K% on
logits) and need a model; none is reproduced. MUSE knowmem has no gold passage (the answers
sit in the raw corpus, which is not held), so it is not adapted.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from ..contracts import DatasetInfo, EvalItem, Query, Turn

__all__ = [
    "MUSE_SOURCE",
    "TOFU_SOURCE",
    "MUSENewsDataset",
    "TOFUDataset",
    "erase_and_verify",
    "membership_auroc",
]

TOFU_SOURCE = {
    "data": "https://huggingface.co/datasets/locuslab/TOFU",
    "data_revision": "324592d84ae4f482ac7249b9285c2ecdb53e3a68",
    "paper": "TOFU: A Task of Fictitious Unlearning for LLMs (COLM 2024)",
    "licence": "MIT",
}
MUSE_SOURCE = {
    "data": "https://huggingface.co/datasets/muse-bench/MUSE-News",
    "data_revision": "506bd5b150b92814d45e4404a82f120ab2d748bf",
    "paper": "MUSE: Machine Unlearning Six-Way Evaluation (ICLR 2025)",
    "licence": "CC BY 4.0",
}

_TOFU_SPLITS = {"forget01": "holdout01", "forget05": "holdout05", "forget10": "holdout10"}
_PER_AUTHOR = 20


class _Digest:
    def __init__(self, root: Path) -> None:
        self.root, self._h = root, hashlib.sha256()

    def read(self, name: str) -> bytes:
        raw = (self.root / name).read_bytes()
        self._h.update(name.encode() + b"\0" + raw)
        return raw

    def hexdigest(self) -> str:
        return self._h.hexdigest()


def _jsonl(raw: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]


class TOFUDataset:
    """TOFU as an erasure probe. ``split`` picks the forget set (and its holdout)."""

    def __init__(
        self,
        root: str | Path,
        revision_id: str,
        split: str = "forget01",
        max_retain: int | None = 100,
        licence: str = "MIT (locuslab/TOFU)",
    ) -> None:
        self.root = Path(root)
        if not (self.root / "full.json").exists():
            raise FileNotFoundError(f"{self.root / 'full.json'} not found; fetch TOFU first")
        if split not in _TOFU_SPLITS:
            raise ValueError(f"split must be one of {sorted(_TOFU_SPLITS)}")
        self.split, self.max_retain, self.licence = split, max_retain, licence
        digest = _Digest(self.root)
        self._full = _jsonl(digest.read("full.json"))
        self._forget = _jsonl(digest.read(f"{split}_perturbed.json"))
        self._retain = _jsonl(digest.read("retain_perturbed.json"))
        self._holdout = _jsonl(digest.read(f"{_TOFU_SPLITS[split]}.json"))
        self._sha = digest.hexdigest()
        self.revision_id = f"sha256:{self._sha[:16]}" if revision_id == "auto" else revision_id
        self._items = [self._build()]

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id="tofu",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.root),
            content_sha256=self._sha,
            n_items=1,
            n_queries=len(self._items[0].queries),
            subset=f"{self.split},max_retain={self.max_retain}",
            notes="memory-erasure probe: residual R@k after hard delete; no model unlearning",
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _build(self) -> EvalItem:
        row_of = {(r["question"], r["answer"]): n for n, r in enumerate(self._full)}
        history = tuple(
            Turn(
                turn_id=f"tofu:{n}",
                session_id=f"author{n // _PER_AUTHOR}",
                speaker="fact",
                text=str(r["answer"]),
                meta={"question": r["question"]},
            )
            for n, r in enumerate(self._full)
        )
        forget_rows = {row_of[(r["question"], r["answer"])] for r in self._forget}
        queries: list[Query] = []
        forget_ids: list[str] = []
        probes: dict[str, list[str]] = {}

        def add(set_name: str, n: int | None, text: str, index: int) -> None:
            gold = (f"tofu:{n}",) if n is not None else ()
            queries.append(
                Query(
                    query_id=f"tofu:{set_name}:{index}",
                    text=text,
                    gold_turn_ids=gold,
                    type_label=set_name,
                    meta={"benchmark": "tofu", "probe_set": set_name},
                )
            )

        for i, r in enumerate(self._forget):
            n = row_of[(r["question"], r["answer"])]
            tid = f"tofu:{n}"
            forget_ids.append(tid)
            probes[tid] = [str(r["answer"]), str(r.get("paraphrased_answer") or "")]
            add("forget", n, str(r["question"]), i)
            if r.get("paraphrased_question"):
                add("forget_paraphrase", n, str(r["paraphrased_question"]), i)
        retain = [r for r in self._retain if (r["question"], r["answer"]) in row_of]
        retain = [r for r in retain if row_of[(r["question"], r["answer"])] not in forget_rows]
        if self.max_retain is not None:
            retain = retain[: self.max_retain]
        for i, r in enumerate(retain):
            n = row_of[(r["question"], r["answer"])]
            add("retain", n, str(r["question"]), i)
            if r.get("paraphrased_question"):
                add("retain_paraphrase", n, str(r["paraphrased_question"]), i)
        for i, r in enumerate(self._holdout):
            add("holdout", None, str(r["question"]), i)
        return EvalItem(
            item_id=f"tofu_{self.split}",
            history=history,
            queries=tuple(queries),
            meta={
                "forget_turn_ids": forget_ids,
                "forget_probes": {k: [p for p in v if p] for k, v in probes.items()},
            },
        )


class MUSENewsDataset:
    """MUSE-News privleak/verbmem passages as an erasure probe."""

    def __init__(
        self,
        root: str | Path,
        revision_id: str,
        probe_words: int = 48,
        max_passages: int | None = None,
        licence: str = "CC BY 4.0 (muse-bench/MUSE-News)",
    ) -> None:
        self.root = Path(root)
        if not (self.root / "privleak" / "forget.json").exists():
            raise FileNotFoundError(f"{self.root}/privleak not found; fetch MUSE-News first")
        self.probe_words, self.max_passages, self.licence = probe_words, max_passages, licence
        digest = _Digest(self.root)
        self._sets = {
            name: json.loads(digest.read(f"privleak/{name}.json").decode("utf-8"))
            for name in ("forget", "retain2", "holdout")
        }
        self._sha = digest.hexdigest()
        self.revision_id = f"sha256:{self._sha[:16]}" if revision_id == "auto" else revision_id
        self._items = [self._build()]

    def info(self) -> DatasetInfo:
        return DatasetInfo(
            dataset_id="muse_news",
            revision_id=self.revision_id,
            licence=self.licence,
            source_path=str(self.root),
            content_sha256=self._sha,
            n_items=1,
            n_queries=len(self._items[0].queries),
            subset=f"privleak,probe_words={self.probe_words},max_passages={self.max_passages}",
            notes="memory-erasure probe over privleak passages; knowmem not adapted (no gold)",
        )

    def items(self) -> Iterator[EvalItem]:
        yield from self._items

    def _prefix(self, text: str) -> str:
        return " ".join(text.split()[: self.probe_words])

    def _build(self) -> EvalItem:
        cap = self.max_passages
        history: list[Turn] = []
        queries: list[Query] = []
        forget_ids: list[str] = []
        probes: dict[str, list[str]] = {}
        for set_name, key in (("forget", "forget"), ("retain", "retain2")):
            for n, text in enumerate(self._sets[key][:cap]):
                tid = f"muse:{set_name}:{n}"
                history.append(
                    Turn(turn_id=tid, session_id=f"muse_{set_name}", speaker="doc", text=str(text))
                )
                prefix = self._prefix(str(text))
                queries.append(
                    Query(
                        query_id=f"muse:{set_name}:{n}",
                        text=prefix,
                        gold_turn_ids=(tid,),
                        type_label=set_name,
                        meta={"benchmark": "muse_news", "probe_set": set_name},
                    )
                )
                if set_name == "forget":
                    forget_ids.append(tid)
                    probes[tid] = [prefix]
        for n, text in enumerate(self._sets["holdout"][:cap]):
            queries.append(
                Query(
                    query_id=f"muse:holdout:{n}",
                    text=self._prefix(str(text)),
                    type_label="holdout",
                    meta={"benchmark": "muse_news", "probe_set": "holdout"},
                )
            )
        return EvalItem(
            item_id="muse_news_privleak",
            history=tuple(history),
            queries=tuple(queries),
            meta={"forget_turn_ids": forget_ids, "forget_probes": probes},
        )


class _Erasable(Protocol):
    async def forget(self, record_id: str, namespace: str = ..., hard: bool = ...) -> None: ...

    async def verify_forget(
        self, record_id: str, namespace: str = ..., *, probe: str | None = ...
    ) -> Mapping[str, Any]: ...


async def erase_and_verify(
    engine: _Erasable,
    item: EvalItem,
    deposits: Mapping[str, Sequence[str]],
    namespace: str = "default",
) -> dict[str, Any]:
    """Hard-forget every record deposited from ``item.meta["forget_turn_ids"]``, then prove
    the erasure with ``verify_forget(probe=...)`` once per (record, probe text).

    ``deposits`` maps a turn id to the record ids its insert produced (the system adapter's
    ``DepositResult.record_ids``). Returns counts and rates: ``clean_rate`` (share of
    records whose proof is clean) and ``residual_rate`` (share of (record, probe) checks that
    still recall something)."""
    forget_ids = list(item.meta.get("forget_turn_ids") or ())
    probes: Mapping[str, Sequence[str]] = item.meta.get("forget_probes") or {}
    missing = [t for t in forget_ids if not deposits.get(t)]
    records = [(t, r) for t in forget_ids for r in deposits.get(t, ())]
    for _, rid in records:
        await engine.forget(rid, namespace=namespace, hard=True)
    clean = checks = residual = 0
    residual_records: list[str] = []
    for tid, rid in records:
        texts = [p for p in probes.get(tid, ()) if p] or [None]
        record_clean = True
        for probe in texts:
            report = await engine.verify_forget(rid, namespace, probe=probe)
            record_clean = record_clean and bool(report.get("clean"))
            if probe is not None:
                checks += 1
                if report.get("residual_recall"):
                    residual += 1
                    residual_records.append(rid)
        clean += record_clean
    return {
        "n_forget_turns": len(forget_ids),
        "n_records": len(records),
        "turns_without_records": missing,
        "n_clean": clean,
        "clean_rate": clean / len(records) if records else None,
        "n_probe_checks": checks,
        "n_residual": residual,
        "residual_rate": residual / checks if checks else None,
        "residual_records": sorted(set(residual_records)),
    }


def membership_auroc(member: Sequence[float], nonmember: Sequence[float]) -> float | None:
    """AUROC of "top retrieval score is higher for a deleted (member) passage than for a
    never-deposited holdout one" (ties count half). After a clean erasure it should be
    ~0.5; well above 0.5 means the store still leaks that the item existed."""
    if not member or not nonmember:
        return None
    wins = sum(1.0 if m > n else 0.5 if m == n else 0.0 for m in member for n in nonmember)
    return wins / (len(member) * len(nonmember))
