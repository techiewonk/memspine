"""Flow 13: shared memory (grants, shared search, subscriptions, messaging, authorize, isolation)."""

from __future__ import annotations

from flows._common import Ctx, Step

NAME = "shared"
TITLE = "Shared memory: grant, shared_search, revoke, subscribe, send/approve/authorize, isolation"
CONFIG = {"memories": {"shared": {"enabled": True}}}

OWNER = "bram"
GUEST = "noor"
OUTSIDER = "tomasz"


async def grant(ctx: Ctx) -> None:
    e = ctx.engine
    a = await e.write(
        "Bram keeps two beehives behind the allotment shed.",
        namespace=OWNER,
        memory_type="episodic",
    )
    b = await e.write(
        "Noor repairs vintage bicycles on weekends.", namespace=GUEST, memory_type="episodic"
    )
    ctx.state.update(a=a.record_id, b=b.record_id)
    g = await e.grant(GUEST, namespace=OWNER)
    ctx.show(
        "grant record",
        {"record_id": g.record_id, "memory_type": g.memory_type, "content": g.content},
    )
    grants = await e.grants_from(OWNER)
    ctx.show("grants_from(bram)", grants)
    ctx.expect("one live grant issued by bram", 1, len(grants))
    ctx.expect("grantee", GUEST, grants[0].grantee)
    ctx.expect(
        "a grantor lists only its own grants (noor has none)", 0, len(await e.grants_from(GUEST))
    )


async def search_granted(ctx: Ctx) -> None:
    e = ctx.engine
    hits = await e.shared_search("beehives allotment", namespace=GUEST, top_k=5)
    ctx.show("noor shared_search", [(r.namespace, r.content[:40], round(s, 3)) for r, s in hits])
    foreign = [r for r, _ in hits if r.namespace == OWNER]
    ctx.check(
        "noor sees bram's granted record", any(r.record_id == ctx.state["a"] for r in foreign)
    )
    ctx.check(
        "foreign record is a live view marked by its own namespace",
        all(r.namespace == OWNER for r in foreign),
    )
    ctx.check(
        "foreign trust is capped (home trust does not ride along)",
        all(r.trust <= 0.5 for r in foreign),
        [r.trust for r in foreign],
    )
    own = await e.shared_search("vintage bicycles", namespace=GUEST, top_k=5)
    ctx.check("noor still sees her own record", any(r.record_id == ctx.state["b"] for r, _ in own))
    back = await e.shared_search("beehives allotment", namespace=OWNER, top_k=5)
    ctx.check(
        "grant is one-way: bram does not see noor's records",
        all(
            r.namespace == OWNER
            for r, _ in await e.shared_search("vintage bicycles", namespace=OWNER, top_k=5)
        ),
    )
    ctx.check("bram sees his own record", any(r.record_id == ctx.state["a"] for r, _ in back))


async def search_ungranted(ctx: Ctx) -> None:
    e = ctx.engine
    hits = await e.shared_search("beehives allotment", namespace=OUTSIDER, top_k=5)
    ctx.show("tomasz (no grant) shared_search", [(r.namespace, r.content[:40]) for r, _ in hits])
    ctx.expect(
        "a namespace with no grant sees no foreign record",
        [],
        [r.record_id for r, _ in hits if r.namespace != OUTSIDER],
    )


async def revoke(ctx: Ctx) -> None:
    e = ctx.engine
    rec = await e.revoke(GUEST, namespace=OWNER)
    ctx.show("revoked grant record status", rec.status.value)
    hits = await e.shared_search("beehives allotment", namespace=GUEST, top_k=5)
    ctx.expect(
        "after revoke noor sees no bram records",
        [],
        [r.record_id for r, _ in hits if r.namespace == OWNER],
    )
    ctx.expect("grants_from empty after revoke", 0, len(await e.grants_from(OWNER)))
    try:
        await e.revoke(GUEST, namespace=OWNER)
        raised = False
    except Exception as exc:
        raised = True
        ctx.show("second revoke raises", f"{type(exc).__name__}: {exc}")
    ctx.check("revoking a non-live grant raises (typo'd grantee is not success)", raised)


async def scoped_grant(ctx: Ctx) -> None:
    e = ctx.engine
    await e.write(
        "Bram's neighbour is called Ilse.",
        namespace=OWNER,
        memory_type="semantic",
        entity="Bram",
        attribute="neighbour",
    )
    await e.grant(GUEST, namespace=OWNER, memory_types=["semantic"])
    hits = await e.shared_search("beehives neighbour Ilse", namespace=GUEST, top_k=10)
    types = sorted({r.memory_type for r, _ in hits if r.namespace == OWNER})
    ctx.show("foreign memory types visible under a semantic-only grant", types)
    ctx.check(
        "only semantic records cross a semantic-scoped grant", set(types) <= {"semantic"}, types
    )
    ctx.check("the semantic record does cross", "semantic" in types)
    await e.revoke(GUEST, namespace=OWNER)


async def subscribe(ctx: Ctx) -> None:
    e = ctx.engine
    s = await e.subscribe("beehives allotment", namespace=GUEST)
    ctx.state["sub"] = s.record_id
    ctx.show(
        "subscription", {"record_id": s.record_id, "content": s.content, "type": s.memory_type}
    )
    subs = await e.subscriptions(GUEST)
    ctx.expect("one live subscription", 1, len(subs))
    ctx.expect("subscription content is the standing query", "beehives allotment", subs[0].content)
    ctx.expect("subscriptions are per namespace", 0, len(await e.subscriptions(OWNER)))
    await e.grant(GUEST, namespace=OWNER)
    hits = await e.shared_search(subs[0].content, namespace=GUEST, top_k=5)
    ctx.check(
        "feeding the standing query to shared_search pulls the granted record",
        any(r.record_id == ctx.state["a"] for r, _ in hits),
    )


async def send(ctx: Ctx) -> None:
    e = ctx.engine
    msg = await e.send(
        "Hive count is two, one needs a new queen.",
        from_namespace=OWNER,
        to_namespace=GUEST,
        principal="bram-agent",
    )
    ctx.state["msg"] = msg.record_id
    ctx.show(
        "message record",
        {
            "ns": msg.namespace,
            "type": msg.memory_type,
            "channel": msg.source.channel,
            "trust": msg.trust,
            "parents": msg.source.parents,
        },
    )
    ctx.expect("message lands in the receiver's namespace", GUEST, msg.namespace)
    ctx.expect("message channel", "message", msg.source.channel)
    ctx.check(
        "message trust is attenuated (below a fresh user write at 1.0)", msg.trust < 1.0, msg.trust
    )
    own = await e.shared_search("queen hive count", namespace=GUEST, top_k=5)
    ctx.check("receiver can read the message", any(r.record_id == msg.record_id for r, _ in own))
    try:
        await e.send("Unsolicited note.", from_namespace=OWNER, to_namespace=OUTSIDER)
        refused = False
    except Exception as exc:
        refused = True
        ctx.show("send without a grant", f"{type(exc).__name__}: {exc}")
    ctx.check("send without a grant edge is refused (no new reachability)", refused)


async def reputation(ctx: Ctx) -> None:
    e = ctx.engine
    r = await e.principal_reputation("bram-agent")
    ctx.show("reputation of a clean principal", r)
    ctx.expect("clean principal multiplier", 1.0, r)
    r2 = await e.principal_reputation("never-seen")
    ctx.expect("unknown principal multiplier", 1.0, r2)
    ctx.check("reputation is in (0, 1]", 0 < r <= 1.0)


async def authorize(ctx: Ctx) -> None:
    e = ctx.engine
    ok = await e.authorize([ctx.state["b"]], namespace=GUEST, threshold=0.1)
    ctx.show("authorize own evidence @0.1", ok)
    ctx.check("own evidence above a low threshold is allowed", ok.allowed)
    hi = await e.authorize([ctx.state["b"]], namespace=GUEST, threshold=0.99)
    ctx.show("authorize own evidence @0.99", hi)
    ctx.check("same evidence below a high threshold is denied", not hi.allowed)
    ctx.expect("weakest link reported", ctx.state["b"], hi.weakest_id)
    foreign = await e.authorize([ctx.state["b"]], namespace=OUTSIDER, threshold=0.1)
    ctx.check(
        "evidence the namespace cannot read denies (fail closed)",
        not foreign.allowed,
        foreign.reason,
    )
    missing = await e.authorize(["no-such-record"], namespace=GUEST, threshold=0.1)
    ctx.check("unknown evidence denies", not missing.allowed, missing.reason)


async def approve(ctx: Ctx) -> None:
    e = ctx.engine
    ev = [ctx.state["b"]]
    before = await e.authorize(
        ev, namespace=GUEST, threshold=0.1, approval=("Noor", "shop_hours", "10-18")
    )
    ctx.check("without an approval the change is denied", not before.allowed, before.reason)
    ap = await e.approve("Noor", "shop_hours", "10-18", namespace=GUEST)
    ctx.show("approval record", {"id": ap.record_id, "tags": ap.tags, "content": ap.content})
    after = await e.authorize(
        ev, namespace=GUEST, threshold=0.1, approval=("Noor", "shop_hours", "10-18")
    )
    ctx.check("with a matching approval the change is allowed", after.allowed, after.reason)
    other = await e.authorize(
        ev, namespace=GUEST, threshold=0.1, approval=("Noor", "shop_hours", "9-17")
    )
    ctx.check(
        "a different value is not approved (exact match only)", not other.allowed, other.reason
    )


async def isolation(ctx: Ctx) -> None:
    e = ctx.engine
    await e.write("Tomasz brews cider in a garage.", namespace=OUTSIDER, memory_type="episodic")
    hit_own = await e.search("cider garage", namespace=OUTSIDER, top_k=5)
    ctx.check("tomasz finds his own record", len(hit_own) > 0)
    hit_other = await e.search("cider garage", namespace=OWNER, top_k=5)
    ctx.check(
        "bram's plain search never returns tomasz's record",
        all(r.namespace == OWNER for r, _ in hit_other),
    )
    shared = await e.shared_search("cider garage", namespace=GUEST, top_k=5)
    ctx.check(
        "shared_search from noor never returns tomasz's record (no grant)",
        all(r.namespace == GUEST or r.namespace == OWNER for r, _ in shared),
    )
    rec = await e.get_record(ctx.state["a"], namespace=OUTSIDER)
    ctx.expect("get_record across namespaces reads as missing", None, rec)
    ids_owner = {r.record_id for r in await e.list_records(OWNER)}
    ids_out = {r.record_id for r in await e.list_records(OUTSIDER)}
    ctx.expect("list_records sets are disjoint", set(), ids_owner & ids_out)


STEPS = [
    Step(
        "F13.1",
        "grant bram -> noor; grants_from lists it",
        grant,
        "memories/shared + api/engine.py:grant, grants_from",
    ),
    Step(
        "F13.2",
        "grantee sees only granted records (live view, capped trust, one-way)",
        search_granted,
        "api/engine.py:shared_search, ops/memory:_shared_search",
    ),
    Step(
        "F13.3",
        "a namespace without a grant sees nothing foreign",
        search_ungranted,
        "core/namespace.py:grant_allows",
    ),
    Step(
        "F13.4", "revoke: grantee sees none; second revoke raises", revoke, "api/engine.py:revoke"
    ),
    Step("F13.5", "memory_types-scoped grant", scoped_grant, "api/engine.py:grant(memory_types=)"),
    Step(
        "F13.6",
        "subscribe / subscriptions (standing query, pull-based)",
        subscribe,
        "api/engine.py:subscribe, subscriptions",
    ),
    Step(
        "F13.7",
        "send: agent-to-agent message through the grant edge",
        send,
        "trust/firewall.py:send",
    ),
    Step("F13.8", "principal_reputation", reputation, "trust/firewall.py:principal_reputation"),
    Step(
        "F13.9", "authorize: trust threshold, fail closed", authorize, "trust/firewall.py:authorize"
    ),
    Step(
        "F13.10",
        "approve + authorize(approval=...)",
        approve,
        "trust/firewall.py:approve, authorize",
    ),
    Step("F13.11", "cross-namespace isolation", isolation, "core/namespace.py, scope_guard"),
]
