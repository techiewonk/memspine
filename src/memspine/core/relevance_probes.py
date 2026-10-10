"""I29/I37: the fixed off-topic probe set and the calibration arithmetic of the
``read.relevance_gate: store_calibrated`` gate.

The probes are generic questions about nothing a personal memory store would hold (weather,
cooking, sport rules, general science, geography, maths, ...). They are written once, are
not derived from any benchmark, and are the same for every store, embedder and reranker. The
gate scores them through the store's own retrieval path and learns where *irrelevant* sits
for that store; a real message must clear it by a margin in standard deviations.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["OFF_TOPIC_PROBES", "Calibration", "calibrate", "percentile"]

OFF_TOPIC_PROBES: tuple[str, ...] = (
    "What is the weather forecast for tomorrow?",
    "How long should I boil an egg?",
    "What are the rules of offside in football?",
    "Why is the sky blue?",
    "What is the capital of Australia?",
    "How do I convert Celsius to Fahrenheit?",
    "What is the square root of 144?",
    "How many players are on a basketball team?",
    "What is photosynthesis?",
    "Give me a recipe for tomato soup.",
    "How far is the Moon from the Earth?",
    "Who wrote the play Hamlet?",
    "What is the boiling point of water at sea level?",
    "Explain how a rainbow forms.",
    "What is the tallest mountain in the world?",
    "How does a refrigerator work?",
    "What is the difference between a virus and a bacterium?",
    "How many sides does a hexagon have?",
    "Explain the rules of chess castling.",
    "Write me a short poem about autumn.",
)


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (``q`` in [0, 100]) without numpy."""
    if not values:
        raise ValueError("percentile of no values")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


@dataclass(frozen=True)
class Calibration:
    """The off-topic level of one namespace for one score leg."""

    leg: str
    n_probes: int
    mu: float
    sigma: float
    p95: float
    n_records: int

    def threshold(self, margin_sd: float) -> float:
        """The raw top score a message must exceed: p95 + ``margin_sd`` x sigma."""
        return self.p95 + margin_sd * self.sigma

    def as_meta(self) -> dict[str, float | int | str]:
        return {
            "leg": self.leg,
            "n_probes": self.n_probes,
            "mu": self.mu,
            "sigma": self.sigma,
            "p95": self.p95,
            "n_records": self.n_records,
        }


def calibrate(leg: str, top_scores: Sequence[float], n_records: int) -> Calibration:
    """Distribution of the probes' top RAW scores (population sd; 0 for one probe)."""
    n = len(top_scores)
    mu = sum(top_scores) / n
    sigma = math.sqrt(sum((x - mu) ** 2 for x in top_scores) / n)
    return Calibration(leg, n, mu, sigma, percentile(top_scores, 95.0), n_records)
