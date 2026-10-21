"""Kernel-only GLiNER ownership benchmark; does not import SDKs or download models."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import time
from dataclasses import dataclass
from typing import Any

from memspine.models.runtime.gliner_runtime import GLiNERModelPool


@dataclass(frozen=True)
class Settings:
    model: str = "fixture/gliner"
    revision: str | None = "fixed-test-revision"
    device: str = "cpu"
    local_files_only: bool = True
    tasks: tuple[str, ...] = ("entities", "classification", "records", "relations")
    inference_concurrency: int = 1


class FakeModel:
    def close(self) -> None:
        pass

    def classify_and_extract(self, value: str) -> str:
        return value


async def run(repeats: int) -> dict[str, Any]:
    pooled_loads = 0

    def load(_: Settings) -> FakeModel:
        nonlocal pooled_loads
        pooled_loads += 1
        time.sleep(0.001)
        return FakeModel()

    pool = GLiNERModelPool(max_models=2, loader=load)
    start = time.perf_counter()
    leases = await asyncio.gather(*(pool.acquire(Settings()) for _ in range(3)))
    await asyncio.gather(
        *(
            lease.call(lambda model: model.classify_and_extract("synthetic text"))
            for lease in leases
            for _ in range(repeats)
        )
    )
    pooled_elapsed = time.perf_counter() - start
    for lease in leases:
        await lease.aclose()
    await pool.aclose()

    independent_loads = 0
    start = time.perf_counter()
    for _ in range(3):
        independent_loads += 1
        await asyncio.to_thread(time.sleep, 0.001)
    independent_elapsed = time.perf_counter() - start

    return {
        "benchmark": "gliner-model-ownership-kernel",
        "scope": "fake loader + callback; no GLiNER SDK, model weights, or inference",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "consumers": 3,
        "repeats_per_consumer": repeats,
        "settings_key": {
            "model": Settings.model,
            "revision": Settings.revision,
            "device": Settings.device,
            "local_files_only": Settings.local_files_only,
            "tasks": list(Settings.tasks),
            "inference_concurrency": Settings.inference_concurrency,
        },
        "independent_client_model_loads": independent_loads,
        "shared_pool_model_loads": pooled_loads,
        "load_reduction_fraction": 1 - pooled_loads / independent_loads,
        "independent_loader_elapsed_seconds": independent_elapsed,
        "shared_pool_elapsed_seconds_including_calls_and_close": pooled_elapsed,
        "performance_claim": "load-count behavior only; elapsed values are not a model latency comparison",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=100)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    print(json.dumps(asyncio.run(run(args.repeats)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
