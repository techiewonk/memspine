"""Quarantine list for known-intermittent tests (ADR-087 decision 3).

``tests/quarantine.txt`` names one pytest node id per line followed by the tracker row
that owns the flake: ``<nodeid>  # RM-123 reason``. A quarantined test still runs; when it
fails it is reported as a non-strict xfail whose reason carries the row id, and the
terminal summary lists every quarantined test that ran. An entry without a row id is a
collection error, so quarantine is always visible, owned debt.

An entry matches the exact node id, or every parametrisation of it when the entry omits
the ``[...]`` suffix. An entry that matches no collected test although the run collected its
file (a renamed or deleted test) is warned about in the terminal summary, and a quarantined
test whose teardown fails is recorded as ``teardown-failed``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

QUARANTINE_FILE = Path(__file__).with_name("quarantine.txt")
_ENTRY = re.compile(r"^(?P<nodeid>\S+)\s+#\s*(?P<row>[A-Z]{2,}-\d+)\b\s*(?P<reason>.*)$")


@dataclass(frozen=True)
class QuarantineEntry:
    nodeid: str
    row: str
    reason: str

    @property
    def label(self) -> str:
        suffix = f": {self.reason}" if self.reason else ""
        return f"quarantined ({self.row}){suffix}"


def parse_quarantine(text: str, source: str = "quarantine.txt") -> dict[str, QuarantineEntry]:
    """Parse the list; raise :class:`pytest.UsageError` on an entry without a row id."""
    entries: dict[str, QuarantineEntry] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENTRY.match(line)
        if match is None:
            raise pytest.UsageError(
                f"{source}:{number}: quarantine entry needs a tracker row id, "
                f"'<nodeid>  # RM-xxx reason': {line!r}"
            )
        nodeid = match["nodeid"]
        if nodeid in entries:
            raise pytest.UsageError(f"{source}:{number}: duplicate quarantine entry {nodeid}")
        entries[nodeid] = QuarantineEntry(nodeid, match["row"], match["reason"].strip())
    return entries


class QuarantinePlugin:
    """Mark quarantined items xfail(strict=False) and summarise the ones that ran."""

    def __init__(self, path: Path = QUARANTINE_FILE) -> None:
        self.path = path
        self.entries: dict[str, QuarantineEntry] = {}
        self.items: dict[str, QuarantineEntry] = {}
        self.outcomes: dict[str, str] = {}
        self.collected_files: set[str] = set()
        self.matched: set[str] = set()

    def _lookup(self, nodeid: str) -> QuarantineEntry | None:
        return self.entries.get(nodeid) or self.entries.get(nodeid.split("[", 1)[0])

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self, session: pytest.Session) -> None:
        del session
        if self.path.is_file():
            self.entries = parse_quarantine(self.path.read_text(encoding="utf-8"), self.path.name)

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        # tryfirst: see every collected item before ``-k``/``-m`` deselect any, so a
        # deselected entry is not mistaken for a stale one.
        for item in items:
            self.collected_files.add(item.nodeid.split("::", 1)[0])
            entry = self._lookup(item.nodeid)
            if entry is None:
                continue
            self.matched.add(entry.nodeid)
            self.items[item.nodeid] = entry
            item.add_marker(pytest.mark.xfail(reason=entry.label, strict=False))

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.nodeid not in self.items:
            return
        if report.when == "teardown":
            # xfail turns a teardown error into a skipped report carrying ``wasxfail``.
            if report.failed or (report.skipped and hasattr(report, "wasxfail")):
                previous = self.outcomes.get(report.nodeid)
                failed = "teardown-failed"
                self.outcomes[report.nodeid] = f"{previous}+{failed}" if previous else failed
            return
        if report.when == "call" or (report.when == "setup" and not report.passed):
            if hasattr(report, "wasxfail"):
                outcome = "xpassed" if report.passed else "xfailed"
            else:
                outcome = report.outcome
            self.outcomes[report.nodeid] = outcome

    def unmatched(self) -> list[QuarantineEntry]:
        """Entries whose file this run collected but which matched no collected test."""
        return [
            entry
            for nodeid, entry in self.entries.items()
            if nodeid not in self.matched and nodeid.split("::", 1)[0] in self.collected_files
        ]

    def pytest_terminal_summary(self, terminalreporter: pytest.TerminalReporter) -> None:
        stale = self.unmatched()
        if stale:
            noun = "entry" if len(stale) == 1 else "entries"
            terminalreporter.write_sep(
                "=", f"quarantine: {len(stale)} {noun} matched no collected test", yellow=True
            )
            for entry in stale:
                terminalreporter.write_line(
                    f"QUARANTINE UNMATCHED {entry.nodeid} ({entry.row}): its file was "
                    "collected but no test has this node id; fix or remove the entry",
                    yellow=True,
                )
        if not self.outcomes:
            return
        terminalreporter.write_sep("=", f"quarantine: {len(self.outcomes)} quarantined test(s) ran")
        for nodeid, outcome in self.outcomes.items():
            entry = self.items[nodeid]
            terminalreporter.write_line(f"QUARANTINED {outcome} {nodeid} ({entry.row})")
