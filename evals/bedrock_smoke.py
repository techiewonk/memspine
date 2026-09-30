"""End-to-end Bedrock smoke test: Engine(Cohere v4 embeddings) -> search -> Qwen3 answer.

    cd memspine && uv run --no-sync python evals/bedrock_smoke.py

Makes at most a handful of paid calls (Cohere embeds per write/search + 1 Qwen3
call), capped by CallBudget for the LLM side.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from memspine_evals.bedrock import (  # noqa: E402
    CallBudget,
    LiteLLMReader,
    bedrock_engine_config,
    load_aws_credentials,
)


async def main() -> None:
    region = load_aws_credentials(HERE.parent / ".env")
    from memspine import Engine

    engine = Engine(
        template="base",
        dotenv_path=None,  # never feed the whole .env into config layering
        storage={"path": ":memory:"},
        memories={
            "episodic": {"enabled": True},
            "semantic": {"enabled": True},
            "shared": {"enabled": True},
        },
        integrity={"enabled": True, "kappa": 0.5, "admission_threshold": 0.2},
        **bedrock_engine_config(region),
    )
    await engine.start()
    try:
        await engine.write(
            "VPN error 809 on contractor accounts is fixed by rotating the gateway certificate.",
            namespace="team/a",
            memory_type="episodic",
        )
        await engine.write(
            "The cafeteria closes at 3pm on Fridays.", namespace="team/a", memory_type="episodic"
        )
        await engine.grant("team/b", namespace="team/a")
        hits = await engine.shared_search(
            "how do I fix vpn error 809?", namespace="team/b", top_k=2
        )
        print("top hit:", hits[0][0].content[:60], "| view trust", round(hits[0][0].trust, 3))
        budget = CallBudget(max_calls=3)
        reader = LiteLLMReader(budget, max_tokens=60)
        context = "\n".join(record.content for record, _ in hits)
        ans = await reader.answer("How do I fix VPN error 809?", context)
        print("qwen3 answer:", ans.text[:160])
        print("latency_ms", round(ans.latency_ms), "| budget", budget.summary())
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
