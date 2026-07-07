"""Default secrets backend: process env + optional ``.env`` file (D-11 peers).

``.env`` parsing is deliberately minimal (KEY=VALUE, ``#`` comments, optional
quotes) — no python-dotenv dependency in core (D-03). Process env wins over the
file, matching the D-11 layering intuition (closer to runtime wins).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

__all__ = ["EnvSecrets"]


class EnvSecrets:
    def __init__(
        self,
        dotenv_path: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._environ: Mapping[str, str] = environ if environ is not None else os.environ
        self._file_values: dict[str, str] = {}
        if dotenv_path is not None:
            path = Path(dotenv_path)
            if path.exists():
                self._file_values = _parse_dotenv(path.read_text(encoding="utf-8"))

    def get(self, name: str) -> str | None:
        return self._environ.get(name) or self._file_values.get(name)


def _parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values
