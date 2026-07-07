"""REST protocol factory (D-06, ``[rest]``): the lazy entry point.

Only this function is safe to import without fastapi installed — the actual
app module (``memspine.protocols.rest.app``) imports fastapi at the top, so it
must never be imported from engine-side code (D-10 guard lives here).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from memspine.exceptions import MissingServiceError

if TYPE_CHECKING:
    from fastapi import FastAPI

    from memspine.engine import Engine

__all__ = ["create_app"]


def create_app(engine: Engine) -> FastAPI:
    """Wrap ONE engine (D-06) in a FastAPI app.

    The engine's lifecycle is owned by the CALLER: start it before serving,
    stop it after (see ``examples/04``). Raises :class:`MissingServiceError`
    naming the ``rest`` extra when fastapi is not installed (D-10).
    """
    try:
        from memspine.protocols.rest.app import build_app
    except ImportError as exc:
        raise MissingServiceError("protocols.rest", extra="rest") from exc
    return build_app(engine)
