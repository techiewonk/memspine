"""DBOS runner (D-16, ``[dbos]``): durable background-pipeline execution.

Real durability at **whole-pipeline-invocation granularity**. Every call to
:meth:`DBOSRunner.run` executes the named pipeline through an
``@DBOS.workflow``-decorated function, so DBOS's system database durably
records a PENDING row *before* the pipeline body runs and a SUCCESS/ERROR row
(with the returned stats or the raised error) when it finishes. If the
process dies mid-invocation, the next ``DBOS.launch()`` — in a fresh process,
or a fresh engine in the *same* process after ``close()`` — finds the
still-PENDING workflow and re-executes it automatically. That is DBOS's own
recovery, triggered synchronously inside ``launch()``; this module runs no
polling loop of its own.

The durable *unit* is the pipeline invocation, not its internal steps:
:class:`~memspine.workers.pipelines.PipelineContext` holds live storage
connections and callables (``append_event``, ``summarize``, the namespace
lock) that cannot be serialized into DBOS's checkpoint store, so the workflow
body cannot suspend mid-pipeline and resume from an intermediate step — only
"run the whole thing again" is a meaningful recovery action. That is also
always *safe*: every pipeline is an idempotent step function (D-17), so
re-executing one from the top after a crash reaches the same state as if it
had completed uninterrupted. Accordingly the checkpointed workflow function
below does exactly one thing: resolve the pipeline by name and hand it a
FRESH ``PipelineContext`` built by ``context_factory`` on demand — a context
captured before the crash would hold dead connections, so nothing but the
factory callable itself ever crosses the checkpoint boundary. Same posture as
``taskiq_runner``'s claim-recovery: durable dispatch + idempotent
re-execution, never a distributed step-checkpoint engine.

Zero-infra by default (D-10): DBOS's system database defaults to a SQLite
file colocated with ``storage.path`` (``<path>.dbos.sqlite``) via
:func:`default_system_database_url` — no Postgres server required to install,
configure, or test ``[dbos]``. ``workers.dbos_system_database_url`` overrides
this for deployments that want a *shared* system database (e.g. Postgres
across a fleet of instances that recover each other's abandoned work) —
DBOS's own multi-instance recovery story requires exactly that, so the
override exists but is never required by memspine itself.

The import is guarded: without ``pip install memspine[dbos]`` construction
raises :class:`MissingServiceError` naming the extra (D-10). DBOS itself is a
process-wide singleton (``DBOS(config=...)`` silently returns the SAME
instance — ignoring the new config — on a second call, until
``DBOS.destroy()`` runs), so ``close()`` always destroys it, and construction
defensively destroys a lingering instance left by a runner that was never
closed (a real crash, not just ``engine.stop()``) so a stop()/start() cycle
in the same process never binds to a stale system database.

**A second, sharper SDK edge ``close()`` also has to repair**: calling *any*
DBOS async workflow function repoints the CALLING event loop's *default*
executor at DBOS's own ``ThreadPoolExecutor``
(``dbos._configure_asyncio_thread_pool()`` — undocumented, discovered by
tracing the installed 2.26 SDK) and never points it back. ``DBOS.destroy()``
then shuts that executor down. On a loop that outlives one engine's lifetime
— exactly the stop()/start() cycle this codebase's own test suite exercises
constantly — the NEXT plain ``asyncio.to_thread(...)`` on that loop (nothing
to do with DBOS; e.g. a fresh engine's own SQLite schema setup) fails with
"cannot schedule new futures after shutdown". ``close()`` gives the loop a
fresh, live default executor back so unrelated code on the same loop is never
left holding a dead one.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from memspine.exceptions import MissingServiceError
from memspine.observability.logging import get_logger
from memspine.workers.pipelines import Pipeline, PipelineContext

__all__ = ["DBOSRunner", "default_system_database_url"]

_log = get_logger(__name__)

#: DBOS app name (must match ``^[a-z0-9-_]+$``, 3-30 chars). Fixed and stable
#: across releases: it only scopes recovery within one system database, and
#: every memspine deployment uses exactly one DBOS app.
_APP_NAME = "memspine"

#: The runner the module-level workflow function below resolves pipelines and
#: the context factory against. DBOS is a process-wide singleton (see module
#: docstring), so at most one runner is ever "active"; construction always
#: sets this BEFORE anyone calls ``launch()`` (the caller registers every
#: pipeline in between — see ``__init__``), so recovery — dispatched the
#: moment ``launch()`` runs — never resolves against a stale, pre-crash
#: runner, nor an empty, not-yet-registered one.
_active_runner: DBOSRunner | None = None

dbos: Any = None
_DBOS_AVAILABLE = False
try:
    import dbos  # reassigns the Any placeholder above; ignore_missing_imports covers typing
except ImportError:
    pass
else:
    _DBOS_AVAILABLE = True

    @dbos.DBOS.workflow(name="memspine.run_pipeline")  # type: ignore[untyped-decorator]
    async def _run_pipeline_workflow(name: str) -> dict[str, object]:
        """The one checkpointed unit (see module docstring): resolve + run.

        Decorated exactly once, at first import of this module (not per
        ``DBOSRunner`` instance) — re-decorating on every construction would
        just spam DBOS's "duplicate registration" warning across the
        stop()/start() cycles this codebase exercises heavily, since the
        underlying function registry outlives ``DBOS.destroy()``.
        """
        runner = _active_runner
        if runner is None or runner._context_factory is None:
            return {"status": "error", "error": "dbos runner not active"}
        pipeline = runner._pipelines.get(name)
        if pipeline is None:
            return {"status": "error", "error": f"unknown pipeline {name!r}"}
        ctx = runner._context_factory()
        return await pipeline(ctx)


def default_system_database_url(storage_path: str) -> str:
    """The zero-infra default (D-10): one SQLite file next to ``storage.path``.

    ``:memory:`` storage (ephemeral engines) still gets a real (if scratch)
    *file*-backed system database, not a bare ``sqlite://`` in-memory URL: the
    installed DBOS SDK's SQLite engine unconditionally passes pool-sizing
    kwargs (``pool_timeout``/``max_overflow``/...) that SQLAlchemy's
    ``SingletonThreadPool`` — the pool class it selects for a bare in-memory
    URL — rejects outright (``TypeError`` at ``create_engine()``). A one-off
    temp file sidesteps that pool-class special-case; durability is moot
    either way without a persisted event log, DBOS just needs somewhere to
    track the run so ``launch()`` can succeed.
    """
    if storage_path == ":memory:":
        scratch = Path(tempfile.gettempdir()) / f"memspine-dbos-{uuid.uuid4().hex}.sqlite"
        return scratch.as_uri().replace("file://", "sqlite://", 1)
    sys_path = Path(storage_path).resolve()
    sys_path = sys_path.with_name(sys_path.name + ".dbos.sqlite")
    # SQLAlchemy sqlite URLs want forward slashes even on Windows; as_uri()
    # already produces them (``file:///C:/...``) — just swap the scheme.
    return sys_path.as_uri().replace("file://", "sqlite://", 1)


class DBOSRunner:
    """TaskRunner over DBOS durable workflows (D-16)."""

    def __init__(
        self,
        *,
        system_database_url: str,
        context_factory: Callable[[], PipelineContext] | None = None,
    ) -> None:
        if not _DBOS_AVAILABLE:
            raise MissingServiceError("workers.dbos", extra="dbos")
        global _active_runner
        if _active_runner is not None:
            # A previous runner in this process never called close() (a real
            # crash, or a test that skipped teardown) — DBOS(...) would
            # otherwise silently hand back that STALE instance (wrong config,
            # wrong system db) instead of raising. Self-heal (D-18 posture):
            # destroy it and start clean rather than run against the wrong db.
            _log.warning(
                "dbos.stale_instance_destroyed",
                detail="a previous DBOSRunner was never closed; destroying "
                "the leftover DBOS singleton before launching a fresh one",
            )
            dbos.DBOS.destroy()
            _active_runner = None
        self._pipelines: dict[str, Pipeline] = {}
        self._context_factory = context_factory
        self._dbos = dbos.DBOS(
            config=dbos.DBOSConfig(
                name=_APP_NAME,
                system_database_url=system_database_url,
                # No dashboard/HTTP surface — memspine owns its own REST
                # protocol (D-06); the admin server would just fight over a
                # port across the many engines this test suite boots.
                run_admin_server=False,
            )
        )
        _active_runner = self
        # NOT launched yet — see `launch()`. Recovery of any PENDING workflow
        # from a prior crash is dispatched (on a background thread) the
        # MOMENT `DBOS.launch()` runs, and that recovery calls straight into
        # the module-level workflow function, which looks up the pipeline by
        # name in `self._pipelines` — empty until `register()` runs. Engine
        # registers every pipeline (`workers/pipelines.PIPELINES`) before
        # calling `launch()` (`_build_runner`), so recovery never races an
        # empty registry; construct-then-immediately-launch would not have
        # that guarantee.

    def register(self, name: str, pipeline: Pipeline) -> None:
        self._pipelines[name] = pipeline

    def launch(self) -> None:
        """Start DBOS: register every pipeline FIRST (see ``__init__``)."""
        dbos.DBOS.launch()

    async def run(self, name: str, ctx: PipelineContext) -> dict[str, object]:
        # ``ctx`` is accepted for TaskRunner-protocol compatibility but never
        # used to execute: the checkpointed workflow function rebuilds its
        # own context via ``context_factory`` on every call — including on
        # recovery, where no caller-supplied ``ctx`` exists at all (see
        # module docstring).
        if name not in self._pipelines:
            return {"status": "error", "error": f"unknown pipeline {name!r}"}
        try:
            result: dict[str, object] = await _run_pipeline_workflow(name)
        except Exception as exc:  # dead-letter, not crash (D-18)
            _log.warning(
                "pipeline.dead_letter",
                pipeline=name,
                runner="dbos",
                error=str(exc),
                exc_info=True,
            )
            return {"status": "error", "error": str(exc)}
        return result

    async def close(self) -> None:
        global _active_runner
        try:
            dbos.DBOS.destroy()
        finally:
            if _active_runner is self:
                _active_runner = None
            # Repair the SDK edge documented in the module docstring: give the
            # current loop back a live default executor now that DBOS's own
            # is shut down, so unrelated `asyncio.to_thread` calls later on
            # this SAME loop (a fresh engine's SQLite setup, most commonly)
            # never inherit a dead one.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                loop.set_default_executor(concurrent.futures.ThreadPoolExecutor())
