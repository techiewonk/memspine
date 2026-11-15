"""Canonical scoped cardinality identities for accepted built-in predicates (RM-45).

The keys are typed and contain no evidence text. They are used only to find
comparable claims; the canonical claims and their citations remain the source
of truth. Unknown or incomplete interpretations do not opt out of the legacy
write ladder.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from functools import lru_cache

from memspine.config.constants import CUE_TAG
from memspine.schemas.durable.semantic import SemanticEnvelope
from memspine.semantics.registry import SemanticRegistry, load_builtin_registry
from memspine.store.claim_index import claim_subject_token
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.store.visibility_policy import audience_key


class UnknownRegistryDigest(ValueError):
    """A live canonical claim pins a registry this runtime cannot interpret."""


@dataclass(frozen=True)
class ClaimGroup:
    namespace: str
    tenant: str
    owner: str
    owner_kind: str
    audience: tuple[str, ...]
    subject: str
    predicate: str
    registry_digest: str
    predicate_version: int
    scope: tuple[tuple[str, str], ...]


def group_key(group: ClaimGroup) -> str:
    """Opaque, stable physical index key; no claim text or rival value is stored."""
    payload = json.dumps(asdict(group), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _builtins() -> SemanticRegistry:
    return load_builtin_registry()


def subject_token(env: SemanticEnvelope) -> str | None:
    """Prefer opaque entity identity; never equate an id with a surface string."""
    return claim_subject_token({"subject": env.subject.model_dump()} if env.subject else None)


def group_of(
    env: SemanticEnvelope,
    namespace: str,
    *,
    audience: tuple[str, ...],
    transition: bool = False,
) -> ClaimGroup | None:
    """Return a comparable group for a pinned, fully scoped built-in claim.

    ``transition`` identifies the group an explicit correction/retraction may
    target; it never makes that input an affirmative conflict participant.
    """
    typing = env.registry
    if typing is None or env.attribute != typing.predicate:
        return None
    registry = _builtins()
    spec = registry.predicates.get(typing.predicate)
    mode_ok = (
        env.assessment.modality in {"asserted", "negated"}
        if transition
        else env.assessment.modality == "asserted" and not env.assessment.negated
    )
    if (
        typing.cardinality in {"one", "contextual"}
        and mode_ok
        and env.assessment.binding == "bound"
        and env.status in {"active", "contested"}
        and any(item.role == "support" for item in env.evidence)
        and typing.cardinality_scope
        and all(typing.cardinality_scope.values())
        and typing.registry_digest != registry.digest
    ):
        raise UnknownRegistryDigest(
            f"unsupported pinned registry digest for predicate {typing.predicate!r}"
        )
    if (
        spec is None
        or typing.registry_digest != registry.digest
        or typing.predicate_version != spec.version
        or typing.cardinality != spec.cardinality
        or typing.cardinality not in {"one", "contextual"}
        or typing.subject_type not in spec.subject_types
        or typing.object_kind not in spec.value_kinds
        or (
            typing.object_kind == "entity"
            and typing.object_entity_type not in spec.object_entity_types
        )
        or not mode_ok
        or env.assessment.binding != "bound"
        or env.status not in {"active", "contested"}
        or not any(item.role == "support" for item in env.evidence)
    ):
        return None
    subject = subject_token(env)
    if (
        subject is None
        or (not transition and value_token(env) is None)
        or any(not value for value in typing.cardinality_scope.values())
        or any(not typing.cardinality_scope.get(key) for key in spec.cardinality_scope)
    ):
        return None
    # Preserve every explicitly pinned token. An extra context or time token may narrow
    # comparability; omitting it would falsely pair two different contexts.
    scope = tuple(sorted(typing.cardinality_scope.items()))
    return ClaimGroup(
        namespace=namespace,
        tenant=env.scope.tenant,
        owner=env.scope.owner,
        owner_kind=env.scope.owner_kind,
        audience=audience,
        subject=subject,
        predicate=typing.predicate,
        registry_digest=typing.registry_digest,
        predicate_version=typing.predicate_version,
        scope=scope,
    )


def record_group_of(record: MemoryRecord) -> ClaimGroup | None:
    """The complete comparable group from a validated projected record."""
    if (
        record.memory_type != "semantic"
        or record.status is not RecordStatus.ACTIVATED
        or record.quarantined
        or record.instruction_flag
        or record.valid_to is not None
        or record.superseded_at is not None
        or CUE_TAG in record.tags
        or "retract" in record.tags
    ):
        return None
    raw = record.fingerprint.envelope
    if raw is None:
        return None
    try:
        env = SemanticEnvelope.model_validate(raw)
    except (TypeError, ValueError):
        return None
    return group_of(env, record.namespace, audience=audience_key(record))


def transition_group_of(record: MemoryRecord) -> ClaimGroup | None:
    """Scoped target group of an explicit correction or retraction input."""
    if (
        record.memory_type != "semantic"
        or record.status is not RecordStatus.ACTIVATED
        or record.quarantined
        or record.instruction_flag
        or record.valid_to is not None
        or record.superseded_at is not None
        or CUE_TAG in record.tags
    ):
        return None
    raw = record.fingerprint.envelope
    if raw is None:
        return None
    try:
        env = SemanticEnvelope.model_validate(raw)
    except (TypeError, ValueError):
        return None
    return group_of(env, record.namespace, audience=audience_key(record), transition=True)


def scoped_claim_identity(
    record: MemoryRecord,
) -> tuple[ClaimGroup, tuple[object, ...]] | None:
    """Typed value within its complete comparable group, when accepted and live."""
    group = record_group_of(record)
    if group is None or record.fingerprint.envelope is None:
        return None
    token = value_token(SemanticEnvelope.model_validate(record.fingerprint.envelope))
    return (group, token) if token is not None else None


def same_scoped_claim_or_untyped(a: MemoryRecord, b: MemoryRecord) -> bool:
    """Text identity may merge claims only when accepted typed identities agree."""
    left, right = scoped_claim_identity(a), scoped_claim_identity(b)
    return (left is None and right is None) or (left is not None and left == right)


def typed_negation_targets(incoming: MemoryRecord, existing: MemoryRecord) -> bool:
    """A bound explicit negative assertion retires only its exact typed value."""
    raw = incoming.fingerprint.envelope
    prior = existing.fingerprint.envelope
    if raw is None or prior is None:
        return False
    env = SemanticEnvelope.model_validate(raw)
    if not (env.change_hint or "retract" in incoming.tags) or not (
        env.assessment.modality == "negated" or env.assessment.negated
    ):
        return False
    group = transition_group_of(incoming)
    if group is None or group != record_group_of(existing):
        return False
    token = value_token(env)
    return token is not None and token == value_token(SemanticEnvelope.model_validate(prior))


def value_token(env: SemanticEnvelope) -> tuple[object, ...] | None:
    """Keep entity ids, scalar types, property, amount, unit and currency distinct."""
    typing = env.registry
    if typing is None:
        return None
    if typing.object_kind == "entity":
        if env.object is not None and env.object.entity_id:
            value: tuple[object, ...] = ("entity_id", env.object.entity_id)
        elif env.value is not None:
            value = ("entity_surface", str(env.value).strip().casefold())
        elif env.object is not None:
            value = ("entity_surface", env.object.surface.strip().casefold())
        else:
            return None
    elif typing.object_kind == "number":
        if typing.amount is None:
            return None
        value = ("number", str(typing.amount.normalize()))
    elif typing.object_kind == "boolean":
        if not isinstance(env.value, bool):
            return None
        value = ("boolean", env.value)
    elif typing.object_kind in {"text", "temporal"}:
        if env.value is None:
            return None
        value = (typing.object_kind, str(env.value).strip().casefold())
    else:
        return None
    return (
        typing.object_kind,
        typing.attribute_key,
        typing.unit,
        typing.currency,
        *value,
    )
