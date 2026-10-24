"""F-16f (M-25): a thin ``ErasureService`` over the lineage projection. Not wired into the engine.

The service is a façade, not a second implementation: it plans an erasure over the lineage
projection (the lineage table plus the edges the records and the log name, as built by
:func:`memspine.trust.pipeline.erasure_state.build_graph`) and executes it by delegating to
:func:`memspine.trust.pipeline.erasure_state.forget`. The current erasure implementation is always available. Use it from tests and from callers that want to
see the plan before they act.

* :meth:`ErasureService.plan` is pure: no state, event or store is touched.
* :meth:`ErasureService.execute` is the hard forget under ``privacy.erasure``.
* :meth:`ErasureService.is_gone` and :meth:`ErasureService.gone` answer from the erasure state
  (POL-1), which is also what every read gate asks.
"""

from __future__ import annotations

from typing import Any

from memspine.store.scope import validate_namespace
from memspine.trust.erasure_plan import ErasurePlan, plan_erasure
from memspine.trust.pipeline import erasure_state as erasure

__all__ = ["ErasureService"]


class ErasureService:
    """Plan and execute erasure over the lineage projection of one engine."""

    def __init__(self, engine: Any) -> None:
        self._eng = engine

    async def plan(
        self, record_id: str, namespace: str, *, cascade: bool | None = None
    ) -> ErasurePlan:
        """The plan a hard forget of ``record_id`` would execute now. Touches nothing."""
        eng = self._eng
        storage = eng._require_started()
        ns = validate_namespace(namespace)
        prior = erasure.state(eng)
        graph = await erasure.build_graph(
            eng, storage, ns, record_id, dependants=cascade is not False
        )
        generation = max(prior.generation, eng.__dict__.get("_erasure_generation", 0)) + 1
        return plan_erasure([record_id], graph, generation=generation, already_lost=prior.lost)

    async def execute(
        self,
        record_id: str,
        namespace: str,
        *,
        cascade: bool | None = None,
        actor: str = "user",
        reason: str | None = None,
    ) -> ErasurePlan:
        """Plan, then hard-forget through ``erasure.forget``. Returns the plan that was shown."""
        plan = await self.plan(record_id, namespace, cascade=cascade)
        await erasure.forget(self._eng, record_id, namespace, cascade, actor=actor, reason=reason)
        return plan

    def gone(self) -> frozenset[str]:
        """Erased and hidden ids: invisible to every read."""
        st = erasure.state(self._eng)
        return frozenset(st.erased) | frozenset(st.hidden)

    def is_gone(self, record_id: str) -> bool:
        return not erasure.visible_id(self._eng, record_id)

    def status(self) -> dict[str, object]:
        return erasure.status(self._eng)
