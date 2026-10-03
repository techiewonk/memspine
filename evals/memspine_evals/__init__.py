"""memspine evaluation harness — any system x any dataset x one fixed protocol.

Lives at the repo root, **outside the wheel** (memspine D-35): nothing here is
importable from ``memspine`` and nothing here ships.

Design (research `PLAN_A_EVIDENCE_REFRESH.md` §A4.1) is three interfaces and
nothing else:

* ``DatasetAdapter``  yields ``(history stream, query, gold, type labels, revision id)``
* ``SystemAdapter``   ``insert(turn)`` then ``query(q) -> context`` — the verified
  LongMemEval-V2 precedent: sequential insert, query returns a *bounded* context
* ``RunProtocol``     fixed reader, fixed budget, fixed judge (model + prompt +
  **scale**), fixed seed — the only layer that makes two runs comparable

Three rules are enforced in code rather than in documentation, because each one
is a mistake this project already found in the published literature:

1. **A score without its judge scale is not a score** (the MAGMA lesson: 0.700
   is a graded partial-credit mean, not an accuracy). ``JudgeSpec`` has no
   default scale and cannot be built without one.
2. **A dataset without a revision id is not a dataset** (the LongMemEval lesson:
   an unannounced September 2025 re-release means pre- and post-revision scores
   cannot be pooled). ``DatasetInfo`` requires ``revision_id``.
3. **A result cannot exist without its manifest.** ``ResultWriter`` takes the
   manifest in its constructor; there is no path that writes a bare number.
"""

from __future__ import annotations

__version__ = "0.2.0"

HARNESS_ID = "memspine-evals"
