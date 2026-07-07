"""C6 §6: background-runner boots (D-16).

* ``inline`` (default) starts and flushes on exit;
* ``dbos`` / ``taskiq`` either construct when their extra is installed, or
  hard-fail with :class:`MissingServiceError` naming the extra when absent
  (D-10) — this test needs neither redis nor dbos actually running;
* an unknown runner name is a config error.
"""

from __future__ import annotations

import importlib.util

import pytest

from memspine.exceptions import ConfigError, MissingServiceError


async def test_inline_runner_boots_and_flushes_on_exit(make_engine) -> None:
    engine = make_engine(template="base", workers={"runner": "inline"})
    await engine.start()
    try:
        assert engine.describe()["runner"] == "inline"
        # A sleep cycle runs the pipelines inline and returns per-stage stats.
        stats = await engine.sleep()
        assert isinstance(stats, dict) and stats
    finally:
        await engine.stop()  # flush-on-exit: the runner closes cleanly


@pytest.mark.parametrize(
    ("runner", "modules", "extra"),
    [
        ("dbos", ("dbos",), "dbos"),
        ("taskiq", ("taskiq", "taskiq_redis"), "taskiq"),
    ],
)
async def test_optional_runner_constructs_or_names_its_extra(
    runner: str, modules: tuple[str, ...], extra: str, make_engine
) -> None:
    installed = all(importlib.util.find_spec(m) is not None for m in modules)
    engine = make_engine(template="base", workers={"runner": runner})
    if installed:  # pragma: no cover - env-dependent
        await engine.start()
        try:
            assert engine.describe()["runner"] == runner
        finally:
            await engine.stop()
    else:
        with pytest.raises(MissingServiceError) as excinfo:
            await engine.start()
        assert excinfo.value.extra == extra
        assert f"memspine[{extra}]" in str(excinfo.value)


async def test_unknown_runner_is_a_config_error(make_engine) -> None:
    engine = make_engine(template="base", workers={"runner": "bogus"})
    with pytest.raises(ConfigError, match=r"unknown workers\.runner"):
        await engine.start()
