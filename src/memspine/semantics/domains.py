"""Hierarchical topical domains and the four sources that may set one (RM-91).

A domain id is a ``/``-separated path of lower-case segments (``health``,
``health/condition``). A filter on ``health`` matches ``health`` and every
descendant (``health/condition``, ``health/condition/chronic``) on a segment
boundary, never a string prefix (``healthcare`` is not under ``health``).

Sources, in precedence order (the first that yields a valid id wins, and the
winning source is recorded next to the domain):

1. ``ingest_tag``: a caller-supplied record tag ``domain:<id>``;
2. ``property``: the default ``domain`` of the claim's property key in the
   vocabulary (``PropertySpec.domain``);
3. ``predicate``: the default ``domain`` of the claim's predicate
   (``PredicateSpec.domain``);
4. ``pack``: the top domain of the pack that owns the predicate
   (``PackManifest.domain``);
5. ``gliner_topic``: an engine-written ``gliner_topic:<id>`` tag from the
   opt-in topic tagger. The tagger is OFF unless ``models.bindings`` binds the
   ``gliner_topic`` role AND a tagger is supplied; nothing here loads a model.

Nothing is inferred: no source, no domain (unknown, which fails any
``topical_domains`` allow-set).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from memspine.semantics.registry import SemanticRegistry

__all__ = [
    "DOMAIN_PATTERN",
    "DOMAIN_TAG_PREFIX",
    "TOPIC_BINDING_ROLE",
    "TOPIC_TAG_PREFIX",
    "DomainSource",
    "ResolvedDomain",
    "TopicTagger",
    "domain_covers",
    "domain_in",
    "is_domain_id",
    "resolve_domain",
    "topic_hook",
    "topic_tags",
]

#: A hierarchical domain id: lower-case segments joined by ``/``.
DOMAIN_PATTERN = r"^[a-z][a-z0-9_]*(/[a-z][a-z0-9_]*)*$"
_DOMAIN_RE = re.compile(DOMAIN_PATTERN)
#: Caller-supplied ingest tag naming a record's domain.
DOMAIN_TAG_PREFIX = "domain:"
#: Engine-written tag from the opt-in GLiNER topic tagger (never accepted from callers).
TOPIC_TAG_PREFIX = "gliner_topic:"
#: The ``models.bindings`` role that switches the topic tagger on.
TOPIC_BINDING_ROLE = "gliner_topic"

DomainSource = Literal["ingest_tag", "property", "predicate", "pack", "gliner_topic"]
#: Text -> candidate domain ids (a GLiNER classification adapter, or a test stub).
TopicTagger = Callable[[str], Sequence[str]]


def is_domain_id(value: object) -> bool:
    return isinstance(value, str) and _DOMAIN_RE.match(value) is not None


def domain_covers(ancestor: str, domain: str) -> bool:
    """Whether ``domain`` is ``ancestor`` or one of its descendants (segment boundary)."""
    return domain == ancestor or domain.startswith(ancestor + "/")


def domain_in(domain: str | None, allowed: Iterable[str]) -> bool:
    """Whether ``domain`` falls under any allowed domain; an unknown domain never does."""
    if domain is None:
        return False
    return any(domain_covers(a, domain) for a in allowed)


@dataclass(frozen=True, slots=True)
class ResolvedDomain:
    domain: str
    source: DomainSource


def _first_tag(tags: Iterable[str], prefix: str) -> str | None:
    for tag in tags:
        if tag.startswith(prefix) and is_domain_id(tag[len(prefix) :]):
            return tag[len(prefix) :]
    return None


def resolve_domain(
    *,
    tags: Iterable[str] = (),
    predicate: str | None = None,
    attribute_key: str | None = None,
    registry: SemanticRegistry | None = None,
) -> ResolvedDomain | None:
    """The record's topical domain and which source set it (None = unknown)."""
    tags = list(tags)
    tagged = _first_tag(tags, DOMAIN_TAG_PREFIX)
    if tagged is not None:
        return ResolvedDomain(tagged, "ingest_tag")
    if registry is not None:
        prop = registry.properties.get(attribute_key) if attribute_key else None
        if prop is not None and prop.domain is not None:
            return ResolvedDomain(prop.domain, "property")
        spec = registry.predicates.get(predicate) if predicate else None
        if spec is not None:
            if spec.domain is not None:
                return ResolvedDomain(spec.domain, "predicate")
            pack_domain = registry.pack_domain("predicate", spec.name)
            if pack_domain is not None:
                return ResolvedDomain(pack_domain, "pack")
    topic = _first_tag(tags, TOPIC_TAG_PREFIX)
    return ResolvedDomain(topic, "gliner_topic") if topic is not None else None


def topic_hook(config: Any, tagger: TopicTagger | None) -> TopicTagger | None:
    """The topic tagger to run at ingest, or None (the default).

    On only when ``models.bindings`` binds :data:`TOPIC_BINDING_ROLE` and a tagger
    is supplied; this never constructs, downloads or activates a model."""
    if tagger is None:
        return None
    bindings = getattr(getattr(config, "models", None), "bindings", None) or {}
    return tagger if TOPIC_BINDING_ROLE in bindings else None


def topic_tags(text: str, tagger: TopicTagger | None) -> list[str]:
    """``gliner_topic:<id>`` tags for ``text`` (valid ids only, de-duplicated, ordered)."""
    if tagger is None or not text:
        return []
    out = [f"{TOPIC_TAG_PREFIX}{d}" for d in tagger(text) if is_domain_id(d)]
    return list(dict.fromkeys(out))
