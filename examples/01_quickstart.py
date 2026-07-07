"""memspine quickstart: a working brain in ~10 lines.

Run:  uv run python examples/01_quickstart.py

Uses the zero-download hash embedder so the example runs offline; production
installs use the default fastembed (ONNX, CPU) embedder — just drop the
``embedding=`` override.
"""

from __future__ import annotations

import asyncio

from memspine import Engine


async def main() -> None:
    engine = Engine(
        template="base",
        storage={"path": "./quickstart.db"},
        embedding={"provider": "hash"},  # offline demo; omit for fastembed
    )
    await engine.start()

    await engine.write("Alice prefers her coffee black", namespace="agent/alice")
    await engine.write("Alice's timezone is Europe/Berlin", namespace="agent/alice")
    await engine.write("The deploy pipeline runs at 06:00 UTC", namespace="agent/alice")

    for record, score in await engine.search(
        "how does alice take her coffee?", namespace="agent/alice", top_k=2
    ):
        print(f"{score:0.3f}  {record.content}")

    print("\nEffective world:")
    for key, value in engine.describe().items():
        print(f"  {key}: {value}")

    await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
