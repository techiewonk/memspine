"""C6 §6: ``config validate`` golden tests.

Four deterministic properties of combination validation:

(a) dependency auto-enable (C1(b)/D-13) — asking for a dependent type enables
    its dependencies and logs each one;
(b) missing-service hard-fail (D-10) — a config demanding an optional service
    whose extra is absent raises :class:`MissingServiceError` naming the extra;
(c) reserved-key rejection (D-14) — ``namespaces.<ns>.memories`` is refused;
(d) ``extends:`` cycle detection — a looping template chain is refused.

All error types are the real ones from ``memspine.exceptions``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from memspine.config.loader import load_config
from memspine.core.registry import dependency_closure
from memspine.exceptions import ConfigError, MissingServiceError

# ── (a) dependency auto-enable ────────────────────────────────────────────────


def test_dependency_closure_auto_enables_hard_deps() -> None:
    closure, auto = dependency_closure({"reflective"})
    assert closure == {"reflective", "episodic"}
    assert auto == ["episodic"]


@pytest.mark.parametrize(
    ("asked", "expected_dep"),
    [
        ("reflective", "episodic"),  # hard require
        ("prospective", "semantic"),  # hard require
        ("associative", "semantic"),  # hard require
        ("shared", "semantic"),  # any-of {semantic, episodic} -> first
    ],
)
def test_closure_logs_every_auto_enable(asked: str, expected_dep: str) -> None:
    with structlog.testing.capture_logs() as logs:
        closure, auto = dependency_closure({asked})
    assert expected_dep in closure and expected_dep in auto
    notices = [e for e in logs if e["event"] == "memory.dependency_auto_enabled"]
    assert any(e["memory_type"] == expected_dep and e["required_by"] == asked for e in notices)


async def test_engine_reports_auto_enabled_in_describe(make_engine) -> None:
    # A minimal config asking only for reflective: episodic is not otherwise
    # enabled, so the closure surfaces it in describe()'s auto_enabled list.
    engine = make_engine(memories={"reflective": {"enabled": True}})
    await engine.start()
    try:
        world = engine.describe()
        assert "episodic" in world["memories"]["enabled"]
        assert "episodic" in world["memories"]["auto_enabled"]
    finally:
        await engine.stop()


# ── (b) missing-service hard-fail ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("provider", "extra"),
    [
        ("ladybug", "graph"),  # embedded default is not on PyPI yet — stub always raises
        ("neo4j", "neo4j"),  # server adapter is out of scope for v0.1 — stub always raises
    ],
)
async def test_missing_graph_service_hard_fails_naming_the_extra(
    provider: str, extra: str, make_engine
) -> None:
    # A stub graph provider fires MissingServiceError deterministically,
    # independent of what happens to be installed in this env.
    engine = make_engine(
        template="base",
        memories={"associative": {"enabled": True}},
        graph={"provider": provider},
    )
    with pytest.raises(MissingServiceError) as excinfo:
        await engine.start()
    assert excinfo.value.extra == extra
    assert f"memspine[{extra}]" in str(excinfo.value)


async def test_strict_services_is_the_gate_default(make_engine) -> None:
    # The hard-fail above depends on strict_services (D-10); base pins it true.
    engine = make_engine(template="base")
    await engine.start()
    try:
        assert engine.describe()["strict_services"] is True
    finally:
        await engine.stop()


# ── (c) reserved-key rejection ────────────────────────────────────────────────


def test_per_namespace_type_enablement_is_reserved() -> None:
    with pytest.raises(ConfigError, match=r"reserved for v0\.2"):
        load_config(
            template="base",
            overrides={"namespaces": {"default": {"memories": {"working": {"enabled": True}}}}},
        )


async def test_reserved_key_surfaces_through_engine_start(make_engine) -> None:
    engine = make_engine(
        template="base",
        namespaces={"tenant-a": {"memories": {"semantic": {"enabled": True}}}},
    )
    with pytest.raises(ConfigError, match=r"reserved for v0\.2"):
        await engine.start()


def test_unknown_memory_type_is_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown memory type"):
        load_config(template="base", overrides={"memories": {"telepathic": {"enabled": True}}})


# ── (d) extends: cycle detection ──────────────────────────────────────────────


def _write_template(directory: Path, name: str, body: str) -> None:
    (directory / f"{name}.yaml").write_text(body, encoding="utf-8")


def test_extends_cycle_is_detected(tmp_path: Path) -> None:
    _write_template(tmp_path, "cyc_a", "extends: cyc_b\nprofile: a\n")
    _write_template(tmp_path, "cyc_b", "extends: cyc_a\nprofile: b\n")
    with pytest.raises(ConfigError, match="cycle detected"):
        load_config(template="cyc_a", extra_template_dirs=[tmp_path])


def test_self_extends_cycle_is_detected(tmp_path: Path) -> None:
    _write_template(tmp_path, "loop", "extends: loop\nprofile: loop\n")
    with pytest.raises(ConfigError, match="cycle detected"):
        load_config(template="loop", extra_template_dirs=[tmp_path])
