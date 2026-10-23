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
   opt-in topic tagger, or (RM-106) the domain that tagger gives the record on the
   compile path. The tagger is OFF unless ``model.extraction.topic`` is
   configured AND a tagger is supplied; nothing here loads a model
   (``derive.topics`` builds the GLiNER tagger).

Nothing is inferred: no source, no domain (unknown, which fails any
``topical_domains`` allow-set).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from memspine.observability.logging import get_logger

if TYPE_CHECKING:
    from memspine.semantics.registry import SemanticRegistry

_log = get_logger(__name__)

__all__ = [
    "DOMAIN_PATTERN",
    "DOMAIN_TAG_PREFIX",
    "TOPIC_TAG_PREFIX",
    "AnyTopicTagger",
    "AsyncTopicTagger",
    "DomainSource",
    "ResolvedDomain",
    "TopicTagger",
    "atopic_domains",
    "atopic_tags",
    "declared_domains",
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

DomainSource = Literal["ingest_tag", "property", "predicate", "pack", "gliner_topic"]
#: Text -> candidate domain ids (a GLiNER classification adapter, or a test stub).
TopicTagger = Callable[[str], Sequence[str]]


@runtime_checkable
class AsyncTopicTagger(Protocol):
    """RM-106: an async topic tagger (``derive.topics.GlinerTopicTagger``)."""

    async def atag(self, text: str) -> Sequence[str]: ...


AnyTopicTagger = TopicTagger | AsyncTopicTagger


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
    topic: str | None = None,
) -> ResolvedDomain | None:
    """The record's topical domain and which source set it (None = unknown).

    ``topic`` (RM-106) is a domain the bound topic tagger computed for the record on the
    compile path; like a ``gliner_topic:`` tag it is the lowest-precedence source."""
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
    tagged_topic = _first_tag(tags, TOPIC_TAG_PREFIX)
    if tagged_topic is None and is_domain_id(topic):
        tagged_topic = topic
    return ResolvedDomain(tagged_topic, "gliner_topic") if tagged_topic is not None else None


def declared_domains(registry: SemanticRegistry) -> tuple[str, ...]:
    """RM-106: every domain id the registry's packs declare (predicate, property and pack
    top domains), sorted: the topic tagger's pack-derived candidate labels."""
    found = {s.domain for s in registry.predicates.values() if s.domain is not None}
    found |= {s.domain for s in registry.properties.values() if s.domain is not None}
    found |= set(registry.pack_domains.values())
    return tuple(sorted(d for d in found if is_domain_id(d)))


def topic_hook(config: Any, tagger: AnyTopicTagger | None) -> AnyTopicTagger | None:
    """The topic tagger to run, or None (the default).

    On only when ``model.extraction.topic`` is configured (ADR-082) and a tagger is
    supplied; this never constructs, downloads or activates a model."""
    if tagger is None:
        return None
    extraction = getattr(getattr(config, "model", None), "extraction", None)
    return tagger if getattr(extraction, "topic", None) is not None else None


def _topic_ids(found: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(d for d in found if is_domain_id(d)))


def topic_tags(text: str, tagger: TopicTagger | None) -> list[str]:
    """``gliner_topic:<id>`` tags for ``text`` (valid ids only, de-duplicated, ordered)."""
    if tagger is None or not text:
        return []
    return [f"{TOPIC_TAG_PREFIX}{d}" for d in _topic_ids(tagger(text))]


async def atopic_domains(text: str, tagger: AnyTopicTagger | None) -> list[str]:
    """RM-106: the tagger's valid domain ids for ``text``; an async tagger (``atag``) is
    awaited, a plain callable is called. A failing tagger never blocks the caller: the
    failure is logged and the domain stays unknown (empty)."""
    if tagger is None or not text:
        return []
    try:
        atag = getattr(tagger, "atag", None)
        if atag is not None:
            found = await atag(text)
        else:
            assert callable(tagger)
            found = tagger(text)
        return _topic_ids(found)
    except Exception as exc:
        _log.warning("topic.tagger_failed", error=type(exc).__name__)
        return []


async def atopic_tags(text: str, tagger: AnyTopicTagger | None) -> list[str]:
    """Async :func:`topic_tags` (see :func:`atopic_domains`)."""
    return [f"{TOPIC_TAG_PREFIX}{d}" for d in await atopic_domains(text, tagger)]
