"""Family L: habits and routines (RM-62), domain ``personal``."""

from __future__ import annotations

from families._harness import Claim, Door, Fact, FamilyContract, role, text


class TestFamilyL(FamilyContract):
    FAMILY = "routines"
    DOMAIN = "personal"
    ACTIVE = True
    POSITIVE = (
        Claim(
            "has_routine",
            value_kind="text",
            roles=(role("location", "entity", "place"),),
            qualifiers={"day_of_week": text("monday"), "time_of_day": text("morning")},
        ),
        Claim(
            "has_routine",
            subject_type="group",
            object_entity_type="event",
            roles=(role("context", "entity", "work"),),
        ),
    )
    NEGATIVE = (
        (
            Claim("has_routine", value_kind="text", qualifiers={"day_of_week": text("someday")}),
            "allowed values",
        ),
        (
            Claim("has_routine", subject_type="organisation", value_kind="text"),
            "incompatible subject",
        ),
        (
            Claim(
                "has_routine",
                value_kind="text",
                roles=(role("time", "temporal", evidence="temporal_ref"),),
            ),
            "missing or unsupported predicate role",
        ),
        (
            Claim("likes", value_kind="text", qualifiers={"time_of_day": text("evening")}),
            "not registered",
        ),
    )
    DOOR = Door("I go running every Monday.", "has_routine", "running", "text")
    DOOR_NEGATIVE = Door(
        "I go running every Monday.", "has_routine", "running", "text", subject_type="organisation"
    )
    FACT = Fact("Ilse", "has_routine", "Running")
