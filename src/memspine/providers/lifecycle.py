"""Engine-local lifecycle and dependency-aware provider cleanup."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum

from memspine.providers.contracts import ResourceRef


class LifecycleState(StrEnum):
    REGISTERED = "registered"
    SELECTED = "selected"
    BUILDING = "building"
    STARTED = "started"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class ResourceOwnershipError(ValueError):
    """Raised when resource ownership is ambiguous or dependencies are invalid."""


@dataclass(frozen=True, slots=True)
class ResourceCloseFailure:
    resource_id: str
    error_type: str
    timed_out: bool
    blocked_by: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LifecycleCloseReport:
    closed: tuple[str, ...]
    failures: tuple[ResourceCloseFailure, ...]

    @property
    def complete(self) -> bool:
        return not self.failures


class ProviderResourceCleanupError(RuntimeError):
    """Sanitized error raised when one or more owned resources fail to close."""

    def __init__(self, report: LifecycleCloseReport) -> None:
        self.report = report
        details = ", ".join(
            f"{failure.resource_id}:{failure.error_type}"
            + (f" (blocked by {', '.join(failure.blocked_by)})" if failure.blocked_by else "")
            + (" (timed out)" if failure.timed_out else "")
            for failure in report.failures
        )
        super().__init__(f"provider resource cleanup failed: {details}")


@dataclass(slots=True)
class _OwnedResource:
    ref: ResourceRef
    close: Callable[[], Awaitable[None]]
    depends_on: tuple[str, ...]
    registration_order: int
    state: LifecycleState = LifecycleState.REGISTERED


class ProviderLifecycleManager:
    """Own resources for one engine and close them in dependency order.

    The timeout is explicit by design. A resource's closer is invoked once, even
    when it times out or raises, so cleanup never accidentally double-closes a
    partially acquired native handle.
    """

    def __init__(self, *, close_timeout_s: float) -> None:
        if (
            isinstance(close_timeout_s, bool)
            or not math.isfinite(close_timeout_s)
            or close_timeout_s <= 0
        ):
            raise ValueError("close_timeout_s must be finite and positive")
        self._close_timeout_s = close_timeout_s
        self._resources: dict[str, _OwnedResource] = {}
        self._borrowed: set[str] = set()
        self._close_task: asyncio.Task[LifecycleCloseReport] | None = None
        self._report: LifecycleCloseReport | None = None

    @property
    def states(self) -> dict[str, LifecycleState]:
        """Return a snapshot of known owned-resource states."""
        return {key: value.state for key, value in self._resources.items()}

    @property
    def borrowed_resources(self) -> frozenset[str]:
        return frozenset(self._borrowed)

    def own(
        self,
        resource_id: str,
        close: Callable[[], Awaitable[None]],
        *,
        depends_on: tuple[str, ...] = (),
    ) -> ResourceRef:
        """Register an owned closer before acquiring the resource."""
        self._ensure_open()
        if not resource_id or resource_id.strip() != resource_id:
            raise ResourceOwnershipError("resource_id must be a non-empty trimmed identifier")
        if not callable(close):
            raise ResourceOwnershipError("close must be callable")
        if resource_id in self._resources or resource_id in self._borrowed:
            raise ResourceOwnershipError(f"resource {resource_id!r} already has an owner")
        if resource_id in depends_on:
            raise ResourceOwnershipError("a resource cannot depend on itself")
        missing = tuple(dep for dep in depends_on if dep not in self._resources)
        if missing:
            raise ResourceOwnershipError(f"unknown resource dependencies: {', '.join(missing)}")
        ref = ResourceRef(resource_id=resource_id, ownership="owned")
        self._resources[resource_id] = _OwnedResource(
            ref=ref,
            close=close,
            depends_on=tuple(dict.fromkeys(depends_on)),
            registration_order=len(self._resources),
        )
        return ref

    def borrow(self, resource_id: str) -> ResourceRef:
        """Return a non-owning reference to an already registered resource."""
        self._ensure_open()
        if resource_id not in self._resources:
            raise ResourceOwnershipError(f"cannot borrow unknown resource {resource_id!r}")
        self._borrowed.add(resource_id)
        return ResourceRef(resource_id=resource_id, ownership="borrowed")

    def validate_references(self, refs: tuple[ResourceRef, ...]) -> None:
        """Reject provider results that claim resources this manager did not register."""
        seen: set[str] = set()
        for ref in refs:
            if ref.resource_id in seen:
                raise ResourceOwnershipError("provider result repeats a resource reference")
            seen.add(ref.resource_id)
            if ref.ownership == "owned":
                resource = self._resources.get(ref.resource_id)
                if resource is None or resource.ref != ref:
                    raise ResourceOwnershipError("provider result contains an unregistered owned resource")
            elif ref.ownership == "borrowed":
                if ref.resource_id not in self._borrowed or ref.resource_id not in self._resources:
                    raise ResourceOwnershipError("provider result contains an unregistered borrowed resource")
            else:
                raise ResourceOwnershipError("provider result contains an invalid ownership reference")

    async def close_all(self) -> LifecycleCloseReport:
        """Close every owned resource and return failures without masking callers."""
        if self._report is not None:
            return self._report
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_registered())
        task = self._close_task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Finish releasing resources, then preserve the caller's cancellation.
            await asyncio.shield(task)
            raise

    async def aclose(self) -> LifecycleCloseReport:
        """Alias useful for async context manager integrations."""
        return await self.close_all()

    @asynccontextmanager
    async def closing_on_error(self) -> AsyncIterator[ProviderLifecycleManager]:
        """Preserve an original build exception while still reporting cleanup faults."""
        try:
            yield self
        except BaseException as original:
            report = await self.close_all()
            if report.failures:
                ids = ", ".join(item.resource_id for item in report.failures)
                original.add_note(f"Provider cleanup incomplete for resources: {ids}")
            raise

    def _ensure_open(self) -> None:
        if self._close_task is not None or self._report is not None:
            raise ResourceOwnershipError("lifecycle manager is already closing or closed")

    async def _close_registered(self) -> LifecycleCloseReport:
        ordered = self._close_order()
        closed: list[str] = []
        failures: list[ResourceCloseFailure] = []
        failed_closers: set[str] = set()
        for resource_id in ordered:
            resource = self._resources[resource_id]
            blockers = tuple(
                failed_id
                for failed_id in sorted(failed_closers)
                if self._depends_transitively(failed_id, resource_id)
            )
            if blockers:
                # A failed dependent may still be using this resource. Preserve
                # it for operator-directed recovery rather than closing out of
                # dependency order underneath potentially live work.
                failures.append(
                    ResourceCloseFailure(
                        resource_id,
                        "DependencyCloseBlocked",
                        False,
                        blockers,
                    )
                )
                continue
            resource.state = LifecycleState.STOPPING
            try:
                async with asyncio.timeout(self._close_timeout_s):
                    await resource.close()
            except TimeoutError:
                resource.state = LifecycleState.FAILED
                failed_closers.add(resource_id)
                failures.append(ResourceCloseFailure(resource_id, "TimeoutError", True))
            except asyncio.CancelledError:
                # Cancellation of a closer is an incomplete cleanup result; keep
                # going so independent resources are still released.
                resource.state = LifecycleState.FAILED
                failed_closers.add(resource_id)
                failures.append(ResourceCloseFailure(resource_id, "CancelledError", False))
            except Exception as exc:
                resource.state = LifecycleState.FAILED
                failed_closers.add(resource_id)
                failures.append(ResourceCloseFailure(resource_id, type(exc).__name__, False))
            else:
                resource.state = LifecycleState.STOPPED
                closed.append(resource_id)
        self._report = LifecycleCloseReport(tuple(closed), tuple(failures))
        return self._report

    def _depends_transitively(self, resource_id: str, dependency_id: str) -> bool:
        """Return whether ``resource_id`` may use ``dependency_id`` directly or indirectly."""
        pending = list(self._resources[resource_id].depends_on)
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == dependency_id:
                return True
            if current not in visited:
                visited.add(current)
                pending.extend(self._resources[current].depends_on)
        return False

    def _close_order(self) -> tuple[str, ...]:
        # A resource must close before each resource it depends on. Among
        # currently eligible nodes, close most recently registered first.
        remaining = set(self._resources)
        order: list[str] = []
        while remaining:
            eligible = [
                resource_id
                for resource_id in remaining
                if not any(resource_id in self._resources[item].depends_on for item in remaining)
            ]
            if not eligible:
                raise ResourceOwnershipError("resource dependency cycle detected")
            chosen = max(eligible, key=lambda item: self._resources[item].registration_order)
            remaining.remove(chosen)
            order.append(chosen)
        return tuple(order)
