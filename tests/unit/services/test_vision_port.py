"""RM-118 (e): the vision port lives in ``services/vision`` with no alias left in ``assets``."""

from __future__ import annotations

import importlib

import pytest

import memspine.services.assets as assets
from memspine.services.vision import NoopVision, OllamaVision, VisionBackend, VisionResult


def test_the_old_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("memspine.services.assets.vision")


def test_assets_no_longer_re_exports_vision() -> None:
    for name in ("NoopVision", "OllamaVision", "VisionBackend", "VisionResult"):
        assert not hasattr(assets, name)
        assert name not in assets.__all__


async def test_noop_vision_describes_nothing() -> None:
    backend: VisionBackend = NoopVision()
    assert await backend.describe(b"\xff\xd8", "image/jpeg") == VisionResult(text="")
    assert OllamaVision.__module__ == "memspine.services.vision.ollama"
