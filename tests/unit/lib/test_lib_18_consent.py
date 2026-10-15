from memspine.trust.consent import (
    ConsentRegistry,
    Reason,
    RecordMeta,
    Tier,
    Viewer,
    ViewerKind,
    group_grantee,
    may_derive,
    may_read,
    may_store,
    purpose_covers,
    tier_from_pii,
)

T, OWN = "tenant-a", "ilse"
OWNER = Viewer(tenant=T, principal=OWN)


def meta(tier=Tier.PERSONAL, purpose="personalisation", groups=()):
    return RecordMeta(
        tenant=T, owner=OWN, tier=tier, purpose=purpose, shared_groups=frozenset(groups)
    )


def test_tier_order_and_mapping():
    assert Tier.PUBLIC < Tier.PERSONAL < Tier.SENSITIVE < Tier.SPECIAL_CATEGORY
    assert tier_from_pii("high") is Tier.SENSITIVE
    assert tier_from_pii("special_category") is Tier.SPECIAL_CATEGORY
    assert tier_from_pii("???") is Tier.SPECIAL_CATEGORY


def test_default_deny_sensitive_default_allow_personal():
    reg = ConsentRegistry()
    assert may_store(meta(Tier.PERSONAL), reg, "personalisation").allowed
    for tier in (Tier.SENSITIVE, Tier.SPECIAL_CATEGORY):
        d = may_store(meta(tier), reg, "personalisation")
        assert not d.allowed and d.reason is Reason.NO_CONSENT
        assert may_read(meta(tier), reg, "personalisation", OWNER).reason is Reason.NO_CONSENT
    reg = reg.grant(T, OWN, "personalisation", 1.0)
    assert may_store(meta(Tier.SENSITIVE), reg, "personalisation").allowed
    assert may_read(meta(Tier.SENSITIVE), reg, "personalisation", OWNER).allowed


def test_grant_then_revoke_hides():
    reg = ConsentRegistry().grant(T, OWN, "personalisation", 1.0)
    m = meta(Tier.SENSITIVE)
    assert may_read(m, reg, "personalisation", OWNER).allowed
    reg = reg.revoke(T, OWN, "personalisation", 2.0)
    d = may_read(m, reg, "personalisation", OWNER)
    assert not d.allowed and d.reason is Reason.CONSENT_REVOKED
    p = may_read(meta(Tier.PERSONAL), reg, "personalisation", OWNER)
    assert p.reason is Reason.CONSENT_REVOKED
    reg = reg.grant(T, OWN, "personalisation", 3.0)
    assert may_read(m, reg, "personalisation", OWNER).allowed


def test_specific_entry_wins_over_broad():
    reg = (
        ConsentRegistry()
        .revoke(T, OWN, "personalisation", 1.0)
        .grant(T, OWN, "personalisation:diet", 2.0)
    )
    diet = meta(purpose="personalisation:diet")
    mood = meta(purpose="personalisation:mood")
    assert may_read(diet, reg, "personalisation:diet", OWNER).allowed
    assert not may_read(mood, reg, "personalisation:mood", OWNER).allowed


def test_purpose_narrowing():
    assert purpose_covers("a", "a:b")
    assert not purpose_covers("a:b", "a")
    assert purpose_covers("*", "x")
    assert not purpose_covers("a", "ab")
    reg = ConsentRegistry()
    narrow = meta(purpose="personalisation:diet")
    assert may_read(narrow, reg, "personalisation:diet", OWNER).allowed
    assert may_read(narrow, reg, "personalisation:diet:vegan", OWNER).allowed
    d = may_read(narrow, reg, "personalisation", OWNER)
    assert d.reason is Reason.PURPOSE_TOO_BROAD
    assert may_read(meta(purpose="*"), reg, "analytics", OWNER).allowed
    assert not may_read(meta(purpose="analytics"), reg, "*", OWNER).allowed


def test_shared_group_viewer():
    reg = ConsentRegistry()
    member = Viewer(
        tenant=T, principal="bram", kind=ViewerKind.GROUP_MEMBER, groups=frozenset({"home"})
    )
    assert may_read(meta(groups=["home"]), reg, "personalisation", member).allowed
    assert (
        may_read(meta(groups=["work"]), reg, "personalisation", member).reason is Reason.NOT_SHARED
    )
    sens = meta(Tier.SENSITIVE, groups=["home"])
    own = reg.grant(T, OWN, "personalisation", 1.0)
    assert may_read(sens, own, "personalisation", member).reason is Reason.NO_GROUP_GRANT
    own = own.grant(T, OWN, "personalisation", 2.0, grantee=group_grantee("home"))
    assert may_read(sens, own, "personalisation", member).allowed
    own = own.revoke(T, OWN, "personalisation", 3.0)
    assert may_read(sens, own, "personalisation", member).reason is Reason.CONSENT_REVOKED


def test_agent_grant():
    agent = Viewer(tenant=T, principal="agent-x", kind=ViewerKind.AGENT)
    reg = ConsentRegistry()
    assert may_read(meta(), reg, "personalisation", agent).reason is Reason.NO_AGENT_GRANT
    reg = reg.grant(T, OWN, "personalisation", 1.0, grantee="agent-x")
    assert may_read(meta(), reg, "personalisation", agent).allowed
    other = Viewer(tenant=T, principal="agent-y", kind=ViewerKind.AGENT)
    assert not may_read(meta(), reg, "personalisation", other).allowed
    reg = reg.revoke(T, OWN, "personalisation", 2.0, grantee="agent-x")
    assert not may_read(meta(), reg, "personalisation", agent).allowed


def test_tenant_and_non_owner():
    reg = ConsentRegistry()
    foreign = Viewer(tenant="tenant-b", principal=OWN)
    assert may_read(meta(), reg, "personalisation", foreign).reason is Reason.TENANT_MISMATCH
    stranger = Viewer(tenant=T, principal="bram")
    assert may_read(meta(), reg, "personalisation", stranger).reason is Reason.NOT_OWNER


def test_generation_bumps_on_every_change():
    reg = ConsentRegistry()
    gens = [reg.generation]
    for step in (
        lambda r: r.grant(T, OWN, "a", 1.0),
        lambda r: r.grant(T, OWN, "a", 2.0),
        lambda r: r.revoke(T, OWN, "a", 3.0),
        lambda r: r.grant(T, OWN, "a", 4.0, grantee="agent-x"),
        lambda r: r.set_do_not_derive(T, OWN, "bram"),
        lambda r: r.set_do_not_derive(T, OWN, "bram", on=False),
    ):
        reg = step(reg)
        gens.append(reg.generation)
    assert gens == list(range(7))
    assert may_store(meta(), reg, "a").consent_generation == 6


def test_do_not_derive():
    reg = ConsentRegistry().set_do_not_derive(T, OWN, "bram")
    assert may_derive(reg, T, OWN, "bram").reason is Reason.DO_NOT_DERIVE
    assert may_derive(reg, T, OWN, "ilse").allowed
    assert may_derive(reg.set_do_not_derive(T, OWN, "bram", on=False), T, OWN, "bram").allowed


def test_determinism_and_immutability():
    def build():
        return ConsentRegistry().grant(T, OWN, "a", 1.0).revoke(T, OWN, "a:b", 2.0)

    a, b = build(), build()
    assert a == b
    m = meta(purpose="a:b")
    assert may_read(m, a, "a:b", OWNER) == may_read(m, b, "a:b", OWNER)
    base = ConsentRegistry()
    base.grant(T, OWN, "a", 1.0)
    assert base.generation == 0
    assert base.entries == ()
