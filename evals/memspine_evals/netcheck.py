"""E1: warn when a harness endpoint is written as ``localhost`` (``--warn-localhost``).

On Windows ``localhost`` can resolve to ``::1`` first; a server listening on IPv4 only then costs a
failed IPv6 connect (measured at about 2 s per fresh connection) before every request. The harness
defaults are ``127.0.0.1``; this check is for an endpoint passed on the command line. It only
reads the URL, makes no request, and never changes it. ``evals/bench_http.py`` measures the cost.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import TextIO
from urllib.parse import urlparse

__all__ = ["localhost_warning", "warn_localhost"]


def localhost_warning(base_url: str, label: str = "--base-url") -> str | None:
    """A one-line warning when ``base_url`` uses the host name ``localhost``, else None."""
    host = (urlparse(base_url).hostname or "").lower()
    if host != "localhost":
        return None
    return (
        f"WARNING: {label} {base_url} uses 'localhost'; on Windows it may try ::1 first and add "
        "about 2 s per new connection. Use 127.0.0.1 (see evals/bench_http.py to measure)."
    )


def warn_localhost(
    urls: Iterable[tuple[str, str]], stream: TextIO | None = None
) -> list[str]:
    """Print (to stderr by default) and return the warnings for ``(label, url)`` pairs."""
    out = [w for label, url in urls if (w := localhost_warning(url, label))]
    for line in out:
        print(line, file=stream or sys.stderr, flush=True)
    return out
