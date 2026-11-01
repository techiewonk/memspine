"""Tests for the ADR-087 quarantine list plugin (tests/quarantine_plugin.py).

Each case runs pytest in a subprocess on a temporary project whose conftest registers the
plugin against its own quarantine file, so the real suite's list is never touched.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from quarantine_plugin import parse_quarantine

_PLUGIN_DIR = Path(__file__).resolve().parents[1]

_CONFTEST = f"""
import sys
from pathlib import Path

sys.path.insert(0, {str(_PLUGIN_DIR)!r})
from quarantine_plugin import QuarantinePlugin


def pytest_configure(config):
    config.pluginmanager.register(
        QuarantinePlugin(Path(__file__).with_name("quarantine.txt")), "memspine-quarantine"
    )
"""

_TESTS = """
import pytest


def test_flaky():
    assert False, "intermittent"


def test_steady():
    pass


def test_broken():
    assert False


@pytest.mark.parametrize("n", [1, 2])
def test_param(n):
    assert n == 1


@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown boom")


def test_teardown(broken_teardown):
    pass
"""


def _run(tmp_path: Path, quarantine: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(_CONFTEST, encoding="utf-8")
    (tmp_path / "test_sample.py").write_text(_TESTS, encoding="utf-8")
    (tmp_path / "quarantine.txt").write_text(textwrap.dedent(quarantine), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:deepeval",
            "-rxX",
            "-c",
            str(tmp_path / "pytest.ini"),
            str(tmp_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_quarantined_failure_reports_xfail_with_row_and_summary(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        """\
        # header comment
        test_sample.py::test_flaky  # RM-901 timer race on slow runners
        test_sample.py::test_param  # RM-902 second case flakes

        test_sample.py::test_steady  # RM-903 passes now
        """,
    )
    out = result.stdout
    assert result.returncode == 1, out  # test_broken is not quarantined and still fails
    assert "1 failed" in out and "2 xfailed" in out and "2 xpassed" in out, out
    assert "matched no collected test" not in out
    assert "quarantined (RM-901): timer race on slow runners" in out
    assert "quarantine: 4 quarantined test(s) ran" in out
    assert "QUARANTINED xfailed test_sample.py::test_flaky (RM-901)" in out
    assert "QUARANTINED xfailed test_sample.py::test_param[2] (RM-902)" in out
    assert "QUARANTINED xpassed test_sample.py::test_param[1] (RM-902)" in out
    assert "QUARANTINED xpassed test_sample.py::test_steady (RM-903)" in out
    assert "test_broken" not in out.split("quarantine:", 1)[1]


def test_empty_list_changes_nothing(tmp_path: Path) -> None:
    result = _run(tmp_path, "# nothing quarantined\n")
    assert result.returncode == 1
    assert "3 failed, 3 passed, 1 error" in result.stdout, result.stdout
    assert "quarantine:" not in result.stdout


def test_entry_without_row_id_is_a_collection_error(tmp_path: Path) -> None:
    result = _run(tmp_path, "test_sample.py::test_flaky  # flaky sometimes\n")
    combined = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, combined
    assert "quarantine.txt:1" in combined and "tracker row id" in combined


def test_parse_rejects_missing_comment_and_duplicates() -> None:
    with pytest.raises(pytest.UsageError, match="row id"):
        parse_quarantine("tests/x.py::test_a\n")
    with pytest.raises(pytest.UsageError, match="duplicate"):
        parse_quarantine("a.py::t  # RM-1 x\na.py::t  # RM-2 y\n")
    entries = parse_quarantine("# c\n\na.py::t  #RM-12\n")
    assert entries["a.py::t"].row == "RM-12" and entries["a.py::t"].reason == ""


def test_entry_matching_no_collected_test_is_warned(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        """\
        test_sample.py::test_renamed_away  # RM-904 the test was renamed
        test_other.py::test_elsewhere  # RM-905 its file was not collected
        """,
    )
    out = result.stdout
    assert "quarantine: 1 entry matched no collected test" in out, out
    assert "QUARANTINE UNMATCHED test_sample.py::test_renamed_away (RM-904)" in out
    assert "test_elsewhere" not in out  # a file the run did not collect is not judged


def test_teardown_failure_is_recorded(tmp_path: Path) -> None:
    result = _run(tmp_path, "test_sample.py::test_teardown  # RM-906 teardown flakes\n")
    out = result.stdout
    assert "QUARANTINED xpassed+teardown-failed test_sample.py::test_teardown (RM-906)" in out, out
