"""Vision capability port (E04, slot ``model.vision``): an image in, a plain-text description out.

``NoopVision`` (``model.vision: null``) produces nothing, so a run with it is text-only by
construction; ``OllamaVision`` describes images through a local Ollama server. Moved from
``services/assets/vision.py`` by RM-118 (no alias, ADR-068).
"""

from memspine.services.vision.base import DESCRIBE_PROMPT, VisionBackend, VisionResult
from memspine.services.vision.noop import NoopVision
from memspine.services.vision.ollama import OllamaVision

__all__ = ["DESCRIBE_PROMPT", "NoopVision", "OllamaVision", "VisionBackend", "VisionResult"]
