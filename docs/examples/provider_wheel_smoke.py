"""Run from outside the checkout with PYTHONPATH cleared after installing the wheel."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import memspine
from memspine import Engine
from memspine.derive.gliner_structured import (
    GLiNER25StructuredExtractor,
    SourceContext,
    StructuredExtractionSchema,
)
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import TurnRef


async def main() -> None:
    package = Path(memspine.__file__).resolve()
    assert "site-packages" in package.parts, package
    assert "torch" not in sys.modules, "Candidate adapter imported the optional inference runtime"

    class Model:
        def extract(self, text: str, schema: object, **kwargs: object) -> dict:
            return {
                "entities": {
                    "person": [{"text": text, "start": 0, "end": len(text), "confidence": 0.9}]
                }
            }

    source = SourceContext(
        scope=OwnerScope(tenant="wheel", owner="mira"),
        turn=TurnRef(source_id="wheel-message", source_version=1, seg_index=0),
        sender_id="mira",
        text="Mira",
    )
    candidates = GLiNER25StructuredExtractor(
        Model(), StructuredExtractionSchema(entity_types=("person",))
    ).extract(source)
    assert candidates.source == source and len(candidates.mentions) == 1
    assert candidates.mentions[0].span.quote == "Mira"

    engine = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            },
            "cache": {"provider": "memory", "settings": {}},
        },
        embedding={"provider": "hash", "settings": {"dim": 64}},
    )
    await engine.start()
    try:
        record = await engine.write("Wheel checksum inventory", namespace="wheel")
        hits = await engine.search("Wheel checksum inventory", namespace="wheel")
        assert any(item.record_id == record.record_id for item, _ in hits)
    finally:
        await engine.stop()
    print(
        json.dumps(
            {
                "status": "passed",
                "package_path": str(package),
                "candidate_adapter": "passed",
                "engine_lifecycle": "passed",
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
