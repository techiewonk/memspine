"""memspine prospective + shared memory (P7/M13.8/R2) tour.

Run:  uv run python examples/04_prospective_shared_rest.py

Shows time-triggered and invalidation-triggered watches with pull-based
delivery, cross-namespace grants with trust-capped live views, and the REST
surface — all offline (hash embedder, in-memory DB).

REST (pip install memspine[rest]) — two lines, engine lifecycle stays yours:

    from memspine.protocols.rest import create_app
    app = create_app(engine)  # serve with: uvicorn my_module:app

⚠️  The REST app has NO authn in v0.1: the caller's namespace comes from the
X-Memspine-Namespace header verbatim. Binding caller→namespace is the
deployer's job — put it behind your auth proxy and override the
`resolve_namespace` dependency (ADR-017).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from memspine import Engine


async def main() -> None:
    engine = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "prospective": {"enabled": True},
            "shared": {"enabled": True},
        },
    )
    await engine.start()
    now = datetime.now(UTC)

    # 1. A due-time watch: the due instant rides valid_from (bi-temporal reuse,
    #    ADR-016) — no scheduler, no new columns. Delivery is pull-based.
    reminder = await engine.watch(
        "rotate the deploy API key", due_at=now + timedelta(minutes=30), namespace="ops"
    )
    fired = await engine.due(namespace="ops", now=now + timedelta(hours=1))
    print(f"due watch fired: {[record.content for record in fired]}")

    # 2. Acknowledge = delta archive (reason='watch_fired'); idempotent.
    await engine.acknowledge_watch(reminder.record_id, namespace="ops")
    print(f"after ack, pending: {await engine.due(namespace='ops', now=now + timedelta(hours=1))}")

    # 3. An invalidation watch fires when the M4 conflict ladder changes the
    #    watched fact's truth — not on any write, only a real supersession.
    await engine.write(
        "primary region is eu-west-1", namespace="ops", entity="deploy", attribute="region"
    )
    await engine.watch(
        "recheck runbooks: region changed", namespace="ops", entity="deploy", attribute="region"
    )
    await engine.write(
        "primary region is us-east-2", namespace="ops", entity="deploy", attribute="region"
    )
    fired = await engine.due(namespace="ops", now=datetime.now(UTC))
    print(f"invalidation watch fired: {[record.content for record in fired]}")

    # 4. Shared memory (R2): ops grants the analyst namespace read access.
    #    Foreign results are LIVE VIEWS — never copied — and trust-capped (E1).
    await engine.grant("analyst", namespace="ops", memory_types=["semantic"])
    results = await engine.shared_search("primary region", namespace="analyst")
    for record, _score in results:
        if record.namespace != "analyst":
            print(f"foreign hit from {record.namespace!r}: trust={record.trust} (capped)")

    # 5. Revocation is immediate — the grant archives through the log.
    await engine.revoke("analyst", namespace="ops")
    remaining = await engine.shared_search("primary region", namespace="analyst")
    print(f"after revoke, foreign hits: {sum(1 for r, _ in remaining if r.namespace != 'analyst')}")

    await engine.stop()


if __name__ == "__main__":
    asyncio.run(main())
