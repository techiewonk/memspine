"""C6 §6: every shipped template boots clean on ``:memory:``.

One boot per template asserting: clean startup, a well-shaped ``describe()``
(schema keys present, the expected memories enabled after C1(b) closure), and a
clean ``stop()``. The write→read round-trips live in ``test_single_type_boots``
and ``test_kitchen_sink``; here we only prove the profiles start and describe
themselves.

Template-count note: the plan (§6) says "5 templates"; six ship today
(``base`` + the five profiles). We boot all six and pin the count.
"""

from __future__ import annotations

import pytest

#: template -> (expected profile, expected enabled set after closure, expected runner).
#: Server profiles (multi_agent, regulated_financial) default to the durable DBOS
#: runner (DEC-4/A4); the embedded profiles stay on inline. A dbos template only
#: boots when [dbos] is installed — otherwise start() hard-fails naming the extra
#: (D-10), which the boot below asserts instead, so the matrix stays honest in
#: both zero-extra and all-extras environments.
TEMPLATES: dict[str, tuple[str, frozenset[str], str]] = {
    "base": ("simple", frozenset({"working", "episodic", "semantic"}), "inline"),
    "coding": ("coding", frozenset({"working", "episodic", "semantic", "procedural"}), "inline"),
    "multi_agent": (
        "multi_agent",
        frozenset({"working", "episodic", "semantic", "shared"}),
        "dbos",
    ),
    "personal": (
        "personal",
        frozenset({"working", "episodic", "semantic", "reflective", "prospective"}),
        "inline",
    ),
    "regulated_financial": (
        "regulated_financial",
        frozenset({"working", "episodic", "semantic"}),
        "dbos",
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
    assert len(TEMPLATES) == 6


@pytest.mark.parametrize("template", sorted(TEMPLATES))
async def test_template_boots_clean(template: str, make_engine) -> None:
    import importlib.util

    from memspine.exceptions import MissingServiceError

    expected_profile, expected_enabled, expected_runner = TEMPLATES[template]
    engine = make_engine(template=template)

    # A server profile pins runner=dbos (DEC-4): it can only boot when [dbos] is
    # installed; without it, start() hard-fails naming the extra (D-10).
    if expected_runner == "dbos" and importlib.util.find_spec("dbos") is None:
        with pytest.raises(MissingServiceError) as excinfo:
            await engine.start()
        assert excinfo.value.extra == "dbos"
        return

    await engine.start()
    try:
        world = engine.describe()
        # Schema shape: every required key is present.
        assert set(world) >= _REQUIRED_DESCRIBE_KEYS
        # The profile and the fully-closed enabled set are exactly as declared.
        assert world["profile"] == expected_profile
        assert set(world["memories"]["enabled"]) == set(expected_enabled)
        # A zero-extra boot always lands on the pure-Python defaults.
        assert world["storage"]["backend"] == "sqlite"
        assert world["embedding"] == engine._embedder.embedder_id  # hash embedder
        assert world["runner"] == expected_runner
        assert world["event_log"]["mode"] in {"full", "rolling", "ephemeral"}
        assert "records" in world["projectors"]  # the M1 projector always runs
    finally:
        await engine.stop()
