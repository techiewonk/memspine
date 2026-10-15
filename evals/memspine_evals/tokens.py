"""Token accounting and budget truncation.

The budget is part of the protocol, not an implementation detail: two systems
answering the same question from contexts of 4k and 200k tokens have not been
compared. Every context the harness passes to a reader goes through
``truncate_to_budget`` and reports whether the budget bit.

The counter itself is recorded in the manifest. A heuristic counter is honest
and reproducible; a heuristic counter whose identity is lost is not.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)


@runtime_checkable
class TokenCounter(Protocol):
    counter_id: str

    def count(self, text: str) -> int: ...

    def describe(self) -> Mapping[str, Any]: ...


class HeuristicTokenCounter:
    """Deterministic chars-per-token estimate — no model, no network, no drift.

    Good enough to enforce a budget consistently across systems, which is what
    the budget is for. It is *not* good enough to publish as a token cost
    against a provider's billing; runs that report cost must carry a real
    tokenizer, and the manifest says which one ran.
    """

    counter_id = "heuristic-chars4"

    def __init__(self, chars_per_token: float = 4.0) -> None:
        if chars_per_token <= 0:
            raise ValueError("chars_per_token must be positive")
        self.chars_per_token = chars_per_token

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, round(len(text) / self.chars_per_token))

    def describe(self) -> Mapping[str, Any]:
        return {"counter_id": self.counter_id, "chars_per_token": self.chars_per_token}


class TiktokenCounter:
    """Real BPE counts via ``tiktoken``. Optional: hard-fails naming the install."""

    def __init__(self, encoding: str = "cl100k_base") -> None:
        try:
            import tiktoken
        except ImportError as exc:  # pragma: no cover - exercised only with the extra
            raise RuntimeError(
                "TiktokenCounter needs tiktoken — `uv pip install tiktoken`, "
                "or use HeuristicTokenCounter and say so in the manifest"
            ) from exc
        self._enc = tiktoken.get_encoding(encoding)
        self.encoding = encoding
        self.counter_id = f"tiktoken-{encoding}"

    def count(self, text: str) -> int:
        return len(self._enc.encode(text))

    def describe(self) -> Mapping[str, Any]:
        return {"counter_id": self.counter_id, "encoding": self.encoding}


def truncate_to_budget(
    text: str, budget_tokens: int, counter: TokenCounter
) -> tuple[str, int, bool]:
    """Cut ``text`` to fit ``budget_tokens``. Returns ``(text, tokens, truncated)``.

    Truncation is from the tail: the head of an assembled context is where the
    stable, cache-aligned material sits (memspine E2), and dropping the head to
    keep the tail would silently change what is being evaluated.
    """
    if budget_tokens <= 0:
        raise ValueError("budget_tokens must be positive")
    tokens = counter.count(text)
    if tokens <= budget_tokens:
        return text, tokens, False
    # Binary-search the character cut so the result holds for any counter.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if counter.count(text[:mid]) <= budget_tokens:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    return cut, counter.count(cut), True


#: D1 [HAR-1]: tokenizers tried, in order, for ``--token-count reader``. The Qwen3 family
#: share one tokenizer; the first one in the local Hugging Face cache is used (offline).
DEFAULT_TOKENIZER_IDS = ("Qwen/Qwen3-0.6B", "Qwen/Qwen3-1.7B", "Qwen/Qwen3-4B", "Qwen/Qwen3-8B")


class HFTokenCounter:
    """Token counts from a Hugging Face ``tokenizer.json`` (the reader's own tokenizer).

    Loaded once from the local cache with the ``tokenizers`` library; no network, no
    ``transformers`` import. ``count`` excludes special tokens, like a prompt body.
    """

    def __init__(self, tokenizer: Any, tokenizer_id: str, source: str = "") -> None:
        self._tok = tokenizer
        self.tokenizer_id = tokenizer_id
        self.source = source
        self.counter_id = f"hf-{tokenizer_id}"

    def count(self, text: str) -> int:
        if not text:
            return 0
        return len(self._tok.encode(text, add_special_tokens=False).ids)

    def describe(self) -> Mapping[str, Any]:
        return {"counter_id": self.counter_id, "tokenizer": self.tokenizer_id, "file": self.source}


_HF_COUNTERS: dict[str, HFTokenCounter | None] = {}


def load_reader_tokenizer_counter(tokenizer_id: str | None = None) -> HFTokenCounter | None:
    """The reader-tokenizer counter, or None (with a logged warning) when no tokenizer is in
    the local cache, so callers fall back to the heuristic counter. Cached per id."""
    candidates = (tokenizer_id,) if tokenizer_id else DEFAULT_TOKENIZER_IDS
    key = "|".join(candidates)
    if key in _HF_COUNTERS:
        return _HF_COUNTERS[key]
    counter: HFTokenCounter | None = None
    try:
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer
    except ImportError as exc:
        log.warning("reader tokenizer unavailable (%s); using the chars/4 heuristic", exc)
        _HF_COUNTERS[key] = None
        return None
    for candidate in candidates:
        try:
            path = hf_hub_download(candidate, "tokenizer.json", local_files_only=True)
            counter = HFTokenCounter(Tokenizer.from_file(path), candidate, path)
            break
        except Exception as exc:  # not cached / unreadable: try the next candidate
            log.debug("tokenizer %s not usable offline: %s", candidate, exc)
    if counter is None:
        log.warning(
            "no reader tokenizer in the local Hugging Face cache (tried %s); "
            "using the chars/4 heuristic",
            ", ".join(candidates),
        )
    _HF_COUNTERS[key] = counter
    return counter


def _fit_units(
    text: str,
    evidence: Sequence[Any],
    budget_tokens: int,
    counter: TokenCounter,
    drop_order: Callable[[list[Any]], list[int]],
) -> tuple[str, int, bool, tuple[Any, ...]] | None:
    """Shared body of the ranked truncations: drop whole evidence lines in ``drop_order``
    (a function of the position-sorted rows returning indices, first = drop first) until
    the text fits. None when the evidence has no usable spans."""
    if budget_tokens <= 0:
        raise ValueError("budget_tokens must be positive")
    tokens = counter.count(text)
    if tokens <= budget_tokens:
        return text, tokens, False, tuple(evidence)
    units = []
    for row in evidence:
        span = (getattr(row, "meta", None) or {}).get("span")
        if span is None:
            return None
        units.append((int(span[0]), int(span[1]), row))
    if not units or any(not (0 <= a <= b <= len(text)) for a, b, _ in units):
        return None
    units.sort(key=lambda u: u[0])
    # a leading/trailing part of the text outside every span is not rank-droppable; keep it
    pieces = [(text[a:b], row) for a, b, row in units]
    order = drop_order([row for _, row in pieces])
    dropped: set[int] = set()

    def render() -> str:
        return "\n".join(p for i, (p, _) in enumerate(pieces) if i not in dropped)

    current = render()
    for i in order:
        if counter.count(current) <= budget_tokens:
            break
        dropped.add(i)
        current = render()
    if counter.count(current) > budget_tokens:  # even one line is too long: cut that tail
        current, _, _ = truncate_to_budget(current, budget_tokens, counter)
    kept = []
    offset = 0
    from dataclasses import replace

    for i, (piece, row) in enumerate(pieces):
        if i in dropped:
            continue
        end = min(offset + len(piece), len(current))
        meta = dict(getattr(row, "meta", None) or {})
        meta["span"] = (offset, offset + len(piece))
        if offset + len(piece) <= len(current):
            kept.append(replace(row, meta=meta))
        offset = end + 1
    return current, counter.count(current), True, tuple(kept)


def truncate_by_rank(
    text: str,
    evidence: Sequence[Any],
    budget_tokens: int,
    counter: TokenCounter,
) -> tuple[str, int, bool, tuple[Any, ...]] | None:
    """D1 [HAR-1]: fit ``text`` into ``budget_tokens`` by dropping whole evidence lines,
    lowest score first, instead of cutting the tail of the (chronological) text.

    ``evidence`` rows carry ``meta["span"] = (start, end)`` into ``text`` and a ``score``
    (higher = ranked better). Returns ``(text, tokens, truncated, kept_evidence)`` with the
    kept lines in their original order, joined by newlines and their spans recomputed; None
    when the evidence has no usable spans (the caller falls back to the tail cut).
    """

    def order(rows: list[Any]) -> list[int]:
        # lowest score first; ties drop the later line first
        return sorted(range(len(rows)), key=lambda i: (getattr(rows[i], "score", 0.0), -i))

    return _fit_units(text, evidence, budget_tokens, counter, order)


def hit_drop_order(rows: Sequence[Any]) -> list[int] | None:
    """D1: the order to drop context lines of a replay-mode (chronological) context.

    ``rows`` are position-sorted evidence whose ``meta["hit_rank"]`` is the 1-based rank of
    a final search hit, or None for a neighbour line. Neighbours go first: each belongs to
    its nearest hit (ties: the worse-ranked one), and those of the lowest-ranked hit are
    dropped first, farther lines before nearer ones. Then the hits, lowest rank first.
    None when no row is a hit (nothing to rank by).
    """
    ranks = [(getattr(r, "meta", None) or {}).get("hit_rank") for r in rows]
    hit_pos = [i for i, rk in enumerate(ranks) if rk is not None]
    if not hit_pos:
        return None
    neighbours = []
    for i, rk in enumerate(ranks):
        if rk is not None:
            continue
        dist, neg_rank = min((abs(i - j), -ranks[j]) for j in hit_pos)
        neighbours.append((-neg_rank, dist, i))  # (parent rank, distance, position)
    neighbours.sort(reverse=True)  # worst parent, farthest, later line first
    hits = sorted(hit_pos, key=lambda i: (-ranks[i], -i))
    return [n[2] for n in neighbours] + hits


def truncate_by_hit_rank(
    text: str,
    evidence: Sequence[Any],
    budget_tokens: int,
    counter: TokenCounter,
) -> tuple[str, int, bool, tuple[Any, ...]] | None:
    """D1 [HAR-1], replay mode: like ``truncate_by_rank`` for a chronological context
    whose lines are search hits (``meta["hit_rank"]``) and neighbour lines (None). Drops
    neighbours of the lowest-ranked hits first, then the lowest-ranked hits; what remains
    keeps its chronological order. None (tail-cut fallback) without hit ranks or spans."""
    if not any((getattr(r, "meta", None) or {}).get("hit_rank") is not None for r in evidence):
        return None
    return _fit_units(
        text,
        evidence,
        budget_tokens,
        counter,
        lambda rows: hit_drop_order(rows) or [],
    )
