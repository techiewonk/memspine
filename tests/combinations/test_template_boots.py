"""C6 §6: every shipped template boots clean on ``:memory:``.

One boot per template asserting: clean startup, a well-shaped ``describe()``
(schema keys present, the expected memories enabled after C1(b) closure), and a
clean ``stop()``. The write→read round-trips live in ``test_single_type_boots``
and ``test_kitchen_sink``; here we only prove the profiles start and describe
themselves.

Template-count note: the plan (§6) says "5 templates"; ``base`` + the five
profiles, ``assistant`` (2026-10-03), ``core`` (ADR-033), ``protected`` (W1),
``benchmark`` (measurement-only, the lean row of the cost-versus-capability
frontier) and ``graph`` (GR-14) ship today. We boot all of them and pin the count.
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

#: template -> (expected profile, expected enabled set after closure, expected runner).
#: Server profiles use Temporal; embedded profiles stay on inline. The Temporal
#: runner is replaced with a local fake in this template-shape test so no server
#: is required here.
TEMPLATES: dict[str, tuple[str, frozenset[str], str]] = {
    "assistant": ("assistant", frozenset({"working", "episodic", "semantic"}), "inline"),
    "base": ("simple", frozenset({"working", "episodic", "semantic"}), "inline"),
    "core": ("core", frozenset({"working", "episodic", "semantic"}), "inline"),
    # Measurement profile: `working` is off (no paging window in LoCoMo /
    # LongMemEval) and `associative` is on, satisfying its C1(b) hard dependency
    # on `semantic`.
    "benchmark": (
        "benchmark",
        frozenset({"episodic", "semantic", "associative"}),
        "inline",
    ),
    "coding": ("coding", frozenset({"working", "episodic", "semantic", "procedural"}), "inline"),
    "multi_agent": (
        "multi_agent",
        frozenset({"working", "episodic", "semantic", "shared"}),
        "temporal",
    ),
    "graph": (
        "graph",
        frozenset({"working", "episodic", "semantic", "associative"}),
        "inline",
    ),
    "personal": (
        "personal",
        frozenset({"working", "episodic", "semantic", "reflective", "prospective"}),
        "inline",
    ),
    "protected": ("protected", frozenset({"working", "episodic", "semantic"}), "inline"),
    "regulated_financial": (
        "regulated_financial",
        frozenset({"working", "episodic", "semantic"}),
        "temporal",
    ),
    "voice": ("voice", frozenset({"working", "episodic", "semantic"}), "inline"),
}

#: The describe() schema every started engine must expose (§4 step 7).
_REQUIRED_DESCRIBE_KEYS = frozenset(
    {
        "profile",
        "memories",
        "event_log",
        "storage",
        "embedding",
        "vector",
        "graph",
        "projectors",
        "runner",
        "strict_services",
        "firewall",
    }
)


def test_all_shipped_templates_are_in_the_matrix() -> None:
    """Guard the reconciliation: if a template is added/removed, this fails
    until the matrix table is updated (so the count never silently drifts)."""
    from memspine.config.loader import template_dir

    shipped = {p.stem for p in template_dir().glob("*.yaml")}
    assert shipped == set(TEMPLATES), f"matrix out of sync with shipped templates: {shipped}"
    assert len(TEMPLATES) == 11
    assert len(TEMPLATES) == 11


@pytest.mark.parametrize("template", sorted(TEMPLATES))
async def test_template_boots_clean(template: str, monkeypatch, make_engine) -> None:
    class FakeTemporalRunner:
        def __init__(self, **kwargs: object) -> None:
            self.pipelines = {}

        def register(self, name, pipeline) -> None:
            self.pipelines[name] = pipeline

        async def start(self) -> None:
            assert self.pipelines

        async def run(self, name, ctx) -> dict[str, object]:
            return {"status": "ok"}

        async def close(self) -> None:
            return None

    module = ModuleType("memspine.workers.temporal_runner")
    module.TemporalRunner = FakeTemporalRunner  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "memspine.workers.temporal_runner", module)

    expected_profile, expected_enabled, expected_runner = TEMPLATES[template]
    engine = make_engine(template=template)

    await engine.start()
    try:
        world = engine.describe()
        # Schema shape: every required key is present.
        assert set(world) >= _REQUIRED_DESCRIBE_KEYS
        # The profile and the fully-closed enabled set are exactly as declared.
        assert world["profile"] == expected_profile
        assert set(world["memories"]["enabled"]) == set(expected_enabled)
        # A zero-extra boot always lands on the pure-Python defaults.
        assert world["storage"]["provider"] == "sqlite"
        assert world["embedding"] == engine._embedder.embedder_id  # hash embedder
        assert world["runner"] == expected_runner
        assert world["event_log"]["mode"] in {"full", "rolling", "ephemeral"}
        assert "records" in world["projectors"]  # the M1 projector always runs
    finally:
        await engine.stop()
