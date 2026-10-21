"""A tiny zero-model embedder used to exercise installed provider plugins."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from pydantic import Field

from memspine.models.encoders.embedding.base import EmbedderManifest
from memspine.providers.contexts import SharedBuildContext
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.registry import ProviderDescriptor
from memspine.providers.settings import StrictSettings

__version__ = "1.2.3"
CLOSE_EVENTS: list[str] = []
CONTRACT_V2_BUILD_EVENTS: list[str] = []


class FixtureHashSettings(StrictSettings):
    """Small strict settings surface; unknown keys are refused by Pydantic."""

    dim: int = Field(default=64, ge=8)


class FixtureHashEmbedder:
    def __init__(self, dim: int) -> None:
        self._dim = dim
        self.closed = False

    @property
    def embedder_id(self) -> str:
        return f"fixture_hash:{self._dim}"

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def manifest(self) -> EmbedderManifest:
        return EmbedderManifest(embedder_id=self.embedder_id, dim=self._dim)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self._dim
            for token in text.casefold().split():
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                number = int.from_bytes(digest, "big")
                vector[number % self._dim] += 1.0 if number & (1 << 63) else -1.0
            norm = math.sqrt(sum(value * value for value in vector))
            if norm == 0.0:
                vector[0] = 1.0
            else:
                vector = [value / norm for value in vector]
            vectors.append(vector)
        return vectors

    async def aclose(self) -> None:
        if not self.closed:
            self.closed = True
            CLOSE_EVENTS.append("closed")


@dataclass(frozen=True, slots=True)
class FixtureHashBuilder:
    ref: ProviderRef

    async def build(
        self, settings: FixtureHashSettings, ctx: SharedBuildContext
    ) -> BuildResult[FixtureHashEmbedder]:
        service = FixtureHashEmbedder(settings.dim)
        resource = ctx.resources.own("fixture-plugin-embedder", service.aclose)
        return BuildResult(service, self.ref, (resource,), frozenset({"embedding"}))


@dataclass(frozen=True, slots=True)
class UnsupportedContractBuilder:
    ref: ProviderRef

    async def build(
        self, settings: FixtureHashSettings, ctx: SharedBuildContext
    ) -> BuildResult[FixtureHashEmbedder]:
        CONTRACT_V2_BUILD_EVENTS.append("called")
        return await FixtureHashBuilder(self.ref).build(settings, ctx)


_REF = ProviderRef("embedding", "fixture_hash")
_BAD_CONTRACT_REF = ProviderRef("embedding", "fixture_bad_contract", contract_version=2)
_IDENTITY = lambda settings: {
    "embedder_id": f"fixture_hash:{settings.dim}",
    "dimension": settings.dim,
}
DESCRIPTOR = ProviderDescriptor(
    ref=_REF,
    settings_model=FixtureHashSettings,
    declared_capabilities=frozenset({"embedding"}),
    requirements=(),
    builder=FixtureHashBuilder(_REF),
    target_identity=_IDENTITY,
)
BAD_CONTRACT_DESCRIPTOR = ProviderDescriptor(
    ref=_BAD_CONTRACT_REF,
    settings_model=FixtureHashSettings,
    declared_capabilities=frozenset({"embedding"}),
    requirements=(),
    builder=UnsupportedContractBuilder(_BAD_CONTRACT_REF),
    target_identity=_IDENTITY,
)
