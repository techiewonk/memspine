"""Flow 10: procedural memory (skills, plans, outcomes, lessons, advisory procedures)."""

from __future__ import annotations

from flows._common import Ctx, Step
from memspine.exceptions import ConflictError

NAME = "procedural"
TITLE = "Procedural memory: skill ladder, plans, outcomes, lessons, pruning, advisory procedures"

CONFIG = {"memories": {"procedural": {"enabled": True, "policies": {"v2": True}}}}


def _ascii(text: object) -> str:
    return str(text).encode("ascii", "replace").decode()


SKILL_STEPS = "1. open the expense form\n2. fill in the amount\n3. submit"


async def add_skill_draft(ctx: Ctx) -> None:
    s = await ctx.engine.add_skill(SKILL_STEPS, "file an expense claim", namespace=ctx.ns)
    ctx.state["skill"] = s.record_id
    ctx.show(
        "added",
        {
            "id": s.record_id[:8],
            "stage": s.skill_stage,
            "attribute": s.attribute,
            "entity": s.entity,
            "status": s.status,
        },
    )
    ctx.expect(
        "a new skill starts at draft", "draft", getattr(s.skill_stage, "value", s.skill_stage)
    )
    usable = await ctx.engine.skills(namespace=ctx.ns)
    everything = await ctx.engine.skills(namespace=ctx.ns, usable_only=False)
    ctx.show("skills() usable / all", [len(usable), len(everything)])
    ctx.expect("a draft is not offered by skills()", 0, len(usable))
    ctx.expect("usable_only=False lists it", 1, len(everything))


async def promote_ladder(ctx: Ctx) -> None:
    rid = ctx.state["skill"]
    stages = []
    for _ in range(2):
        r = await ctx.engine.promote_skill(rid, namespace=ctx.ns)
        stages.append(getattr(r.skill_stage, "value", r.skill_stage))
    ctx.expect(
        "draft -> staged -> verified, one legal step per call", ["staged", "verified"], stages
    )
    try:
        await ctx.engine.promote_skill(rid, namespace=ctx.ns)
        blocked = False
    except ConflictError as exc:
        blocked = True
        ctx.show("verified -> active without a dry run", f"ConflictError: {_ascii(exc)}")
    ctx.check("verified -> active is refused without dry_run_passed", blocked)
    r = await ctx.engine.promote_skill(rid, namespace=ctx.ns, dry_run_passed=True)
    ctx.expect(
        "dry_run_passed=True activates it", "active", getattr(r.skill_stage, "value", r.skill_stage)
    )
    usable = await ctx.engine.skills(namespace=ctx.ns, kind="skill")
    ctx.show("skills(kind='skill') usable", [x.entity for x in usable])
    ctx.expect("the active skill is now offered", [rid], [x.record_id for x in usable])


async def deprecate(ctx: Ctx) -> None:
    rid = ctx.state["skill"]
    r = await ctx.engine.deprecate_skill(rid, namespace=ctx.ns)
    ctx.show("deprecated", {"stage": r.skill_stage, "status": r.status})
    ctx.expect(
        "deprecation is a stage", "deprecated", getattr(r.skill_stage, "value", r.skill_stage)
    )
    ctx.expect(
        "a deprecated skill leaves the usable set",
        0,
        len(await ctx.engine.skills(namespace=ctx.ns)),
    )
    try:
        await ctx.engine.promote_skill(rid, namespace=ctx.ns, dry_run_passed=True)
        refused = False
    except ConflictError as exc:
        refused = True
        ctx.show("promote after deprecate", f"ConflictError: {_ascii(exc)}")
    ctx.check("deprecation is terminal (no promote back)", refused)


async def plans_and_recall(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    train = await e.record_plan(
        "book the train to Leiden for Bram",
        "1. open the rail site\n2. pick the date\n3. pay",
        namespace=ns,
    )
    card = await e.record_plan(
        "renew Ilse's library card", "1. log in\n2. press renew", namespace=ns
    )
    ctx.state.update(train=train.record_id, card=card.record_id)
    ctx.expect(
        "a recorded plan enters at staged (success was the first validation)",
        "staged",
        getattr(train.skill_stage, "value", train.skill_stage),
    )
    ctx.expect(
        "a staged plan is held out of plan recall",
        None,
        await e.recall_plan("book the train to Leiden for Bram", namespace=ns),
    )
    for rid in (train.record_id, card.record_id):
        await e.promote_skill(rid, namespace=ns)
        await e.promote_skill(rid, namespace=ns, dry_run_passed=True)
    hit = await e.recall_plan("book the train to Leiden for Bram", namespace=ns)
    ctx.show("recall_plan same task", hit and {"task": hit.entity, "steps": hit.content})
    ctx.check(
        "an active plan is recalled by its task",
        hit is not None and hit.record_id == train.record_id,
    )
    miss = await e.recall_plan("grow tomatoes on a balcony", namespace=ns)
    ctx.expect("an unrelated task clears no similarity floor", None, miss)
    ranked = await e.recall_plans("renew Ilse's library card", namespace=ns)
    ctx.show(
        "recall_plans 'renew Ilse's library card'", [(r.entity, round(s, 3)) for r, s in ranked]
    )
    ctx.check(
        "recall_plans puts the matching plan first",
        bool(ranked) and ranked[0][0].record_id == card.record_id,
    )
    ctx.check(
        "recall_plans keeps only plans over the floor (the train plan is not offered)",
        all(r.record_id != train.record_id for r, _ in ranked),
    )


async def outcome_lesson(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    lesson = await e.record_outcome(
        "book the train to Leiden for Bram",
        "failure",
        namespace=ns,
        action="pay",
        error="card declined",
        next_action="try the other card",
        used_ids=[ctx.state["train"]],
    )
    ctx.state["lesson"] = lesson.record_id
    ctx.show(
        "lesson",
        {
            "kind": lesson.attribute,
            "stage": lesson.skill_stage,
            "text": lesson.content,
            "tags": lesson.tags,
        },
    )
    ctx.expect("a receipt becomes a lesson record", "lesson", lesson.attribute)
    ctx.expect(
        "a lesson is advisory only (never promotable)",
        "advisory",
        getattr(lesson.skill_stage, "value", lesson.skill_stage),
    )
    plan = await e.get_record(ctx.state["train"], namespace=ns)
    ctx.show("used plan counts (likes, dislikes)", [plan.scoring.likes, plan.scoring.dislikes])
    ctx.expect("a failure on a used plan counts one harmful (dislike)", 1, plan.scoring.dislikes)
    plain = await e.record_outcome("sort the shed", "success", namespace=ns, lesson=False)
    ctx.expect("lesson=False records feedback only and returns None", None, plain)
    try:
        await e.record_outcome("x", "failure", namespace=ns, used_ids=["no-such-id"])
        refused = False
    except ConflictError:
        refused = True
    ctx.check("an unknown used_id is refused", refused)


async def lessons_recall_repeat(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    got = await e.recall_lessons("book the train to Leiden for Bram", namespace=ns)
    ctx.show("recall_lessons", [(r.entity, round(s, 3)) for r, s in got])
    ctx.check(
        "the lesson is recalled for the same task",
        any(r.record_id == ctx.state["lesson"] for r, _ in got),
    )
    ctx.expect(
        "an unrelated task recalls no lesson",
        [],
        await e.recall_lessons("grow tomatoes on a balcony", namespace=ns),
    )
    again = await e.record_outcome(
        "book the train to Leiden for Bram",
        "failure",
        namespace=ns,
        action="pay",
        error="card declined",
        next_action="try the other card",
    )
    ctx.show("same receipt again", {"id": again.record_id[:8], "notes": again.scoring.notes})
    ctx.expect(
        "a repeat receipt adds a note to the same lesson (no new row)",
        ctx.state["lesson"],
        again.record_id,
    )
    ctx.expect("the repeat is counted as a note", 1, again.scoring.notes)
    lessons = [r for r in await e.skills(namespace=ns, kind="lesson", usable_only=False)]
    ctx.expect(
        "still exactly one lesson for that key",
        1,
        len([r for r in lessons if r.entity == "book the train to Leiden for Bram"]),
    )


async def prune(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    train = ctx.state["train"]
    await e.record_outcome(
        "book the train to Leiden for Bram", "failure", namespace=ns, used_ids=[train], lesson=False
    )
    plan = await e.get_record(train, namespace=ns)
    ctx.show("train plan (likes, dislikes)", [plan.scoring.likes, plan.scoring.dislikes])
    ctx.check(
        "failures now dominate (>=2 harmful, more harmful than helpful)",
        plan.scoring.dislikes >= 2 and plan.scoring.dislikes > plan.scoring.likes,
    )
    ranked = await e.recall_plans("book the train to Leiden for Bram", namespace=ns)
    ctx.expect(
        "recall_plans no longer offers a failing plan",
        [],
        [r.record_id for r, _ in ranked if r.record_id == train],
    )
    retired = await e.prune_experience(namespace=ns)
    ctx.show("prune_experience retired", [i[:8] for i in retired])
    ctx.check("the failing plan is retired", train in retired)
    after = await e.get_record(train, namespace=ns)
    ctx.expect(
        "retired = deprecated (terminal)",
        "deprecated",
        getattr(after.skill_stage, "value", after.skill_stage),
    )
    ctx.check("the healthy plan is not retired", ctx.state["card"] not in retired)


async def suggest(ctx: Ctx) -> None:
    e, ns = ctx.engine, ctx.ns
    ids = []
    for _ in range(
        2
    ):  # a procedure needs >=2 agreeing sources; tags ride the write door (add_skill takes none)
        r = await e.write(
            "1. open the pay slip\n2. copy the total\n3. paste into the tax form",
            namespace=ns,
            memory_type="procedural",
            entity="file the tax form",
            attribute="skill",
            tags=["taxes", "pre:slip in hand"],
        )
        ids.append(r.record_id)
    ctx.show("write door (procedural + tags)", ids)
    hit = await e.suggest_procedures(["taxes", "slip in hand"], namespace=ns)
    ctx.show(
        "suggest_procedures(['taxes','slip in hand'])",
        [(s.name, s.score, s.unmet_preconditions, s.reliability) for s in hit],
    )
    ctx.check(
        "a procedure is suggested by tag overlap", any(s.name == "file the tax form" for s in hit)
    )
    unmet = await e.suggest_procedures(["taxes"], namespace=ns)
    ctx.show(
        "suggest_procedures(['taxes']) unmet", [(s.score, s.unmet_preconditions) for s in unmet]
    )
    ctx.expect(
        "a missing precondition is listed as unmet",
        ("slip in hand",),
        unmet[0].unmet_preconditions if unmet else None,
    )
    src = await e.get_record(ids[0], namespace=ns)
    want = round(
        1.0 * unmet[0].reliability * src.trust * 0.5, 9
    )  # jaccard 1 x reliability x trust x 0.5 per unmet precondition
    ctx.expect(
        "score = jaccard x reliability x trust x 0.5 per unmet precondition",
        want,
        round(unmet[0].score, 9),
    )
    ctx.expect(
        "no shared tag means no suggestion",
        [],
        await e.suggest_procedures(["gardening"], namespace=ns),
    )
    base = hit[0].reliability
    await e.feedback(ids[0], "like", namespace=ns, actor="user")
    boosted = await e.suggest_procedures(["taxes", "slip in hand"], namespace=ns)
    ctx.show("reliability before / after one explicit like", [base, boosted[0].reliability])
    ctx.check("explicit feedback raises reliability", boosted[0].reliability > base)


STEPS = [
    Step(
        "F10.1",
        "add_skill: starts at draft, hidden from skills()",
        add_skill_draft,
        "memory/pipeline/procedural.py:add_skill, memories/procedural/skills.py:make_skill_record",
    ),
    Step(
        "F10.2",
        "promote_skill: draft>staged>verified, dry-run gate to active",
        promote_ladder,
        "memories/procedural/lifecycle.py:next_stage, skills.py:promote",
    ),
    Step(
        "F10.3",
        "deprecate_skill: terminal",
        deprecate,
        "memory/pipeline/procedural.py:deprecate_skill",
    ),
    Step(
        "F10.4",
        "record_plan + recall_plan (staged is held out, active is recalled by task)",
        plans_and_recall,
        "pipeline/procedural.py:record_plan, api/ops_memory.py:recall_plan",
    ),
    Step(
        "F10.5",
        "record_outcome: lesson record + harmful count on the used plan",
        outcome_lesson,
        "pipeline/procedural.py:record_outcome, memories/procedural/lessons.py",
    ),
    Step(
        "F10.6",
        "recall_lessons and repeat-receipt dedup",
        lessons_recall_repeat,
        "pipeline/procedural.py:recall_lessons, _repeat_lesson",
    ),
    Step(
        "F10.7",
        "failures dominate: recall_plans drops, prune_experience retires",
        prune,
        "api/ops_memory.py:recall_plans, pipeline/procedural.py:prune_experience",
    ),
    Step(
        "F10.8",
        "suggest_procedures: tag overlap, preconditions, explicit feedback",
        suggest,
        "api/ops_advisory.py:suggest_procedures, memory/procedural.py:suggest",
    ),
]
