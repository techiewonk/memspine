"""Global test hygiene: the suite must be hermetic against the developer env.

Engine.start() feeds ``os.environ`` into the config layering, so any exported
``MEMSPINE_*`` variable (e.g. someone experimenting with
``MEMSPINE_EVENT_LOG__MODE=ephemeral``) would silently reconfigure every test
engine. Scrub them for the duration of each test.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _scrub_memspine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("MEMSPINE_"):
            monkeypatch.delenv(key, raising=False)
