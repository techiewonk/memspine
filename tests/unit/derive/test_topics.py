"""RM-106: the GLiNER topic tagger (pack-domain labels + operator label map, threshold,
offline only). Model-free: a stub pool stands in for GLiNER2."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from memspine.config import constants
from memspine.config.schema.model import GlinerSettings
from memspine.config.schema.root import MemspineConfig
from memspine.derive.topics import GlinerTopicTagger, parse_topic_hits, readable_label
from memspine.exceptions import ConfigError
from memspine.semantics.domains import atopic_domains, declared_domains
from memspine.semantics.registry import load_builtin_registry


class _Extractor:
    """``classify_text`` only (the fallback API); answers with a fixed label + confidence."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[tuple[str, list[str]]] = []

    def classify_text(self, text: str, fields: dict[str, list[str]], **_: Any) -> Any:
        self.calls.append((text, fields["topic"]))
        return {"topic": self.answer}


class _Binding:
    def __init__(self, extractor: Any) -> None:
        self.extractor = extractor

    async def call(self, fn: Callable[[Any], Any]) -> Any:
        return fn(self.extractor)


class _Pool:
    def __init__(self, extractor: Any) -> None:
        self.extractor = extractor
        self.bound: list[Any] = []

    def binding(self, settings: Any) -> _Binding:
        self.bound.append(settings)
        return _Binding(self.extractor)


def _settings(**kw: Any) -> GlinerSettings:
    return GlinerSettings.model_validate({"tasks": ["classification"], **kw})


def test_candidate_labels_are_the_active_pack_domains() -> None:
    tagger = GlinerTopicTagger(_Pool(_Extractor(None)), _settings())
    domains = declared_domains(load_builtin_registry())
    assert domains and "location/residence" in domains
    assert dict(tagger.labels) == {readable_label(d): d for d in domains}
    assert tagger.labels["location residence"] == "location/residence"


async def test_pack_domain_hit_above_threshold_sets_the_domain() -> None:
    extractor = _Extractor({"label": "location residence", "confidence": 0.81})
    tagger = GlinerTopicTagger(_Pool(extractor), _settings())
    assert list(await tagger.atag("I moved to Porto")) == ["location/residence"]
    assert extractor.calls[0][0] == "I moved to Porto"
    assert "location residence" in extractor.calls[0][1]


async def test_threshold_and_unscored_hits() -> None:
    low = _Extractor({"label": "work", "confidence": 0.49})
    assert list(await GlinerTopicTagger(_Pool(low), _settings()).atag("x")) == []
    at = _Extractor({"label": "work", "confidence": constants.TOPIC_MIN_CONFIDENCE})
    assert list(await GlinerTopicTagger(_Pool(at), _settings()).atag("x")) == ["work"]
    strict = GlinerTopicTagger(_Pool(at), _settings(), min_confidence=0.9)
    assert list(await strict.atag("x")) == []
    bare = _Extractor("work")  # no confidence: never sets a domain
    assert list(await GlinerTopicTagger(_Pool(bare), _settings()).atag("x")) == []
    unknown = _Extractor({"label": "astrology", "confidence": 0.99})
    assert list(await GlinerTopicTagger(_Pool(unknown), _settings()).atag("x")) == []


async def test_best_scored_hit_wins() -> None:
    many = _Extractor(
        [{"label": "work", "confidence": 0.6}, {"label": "finance", "confidence": 0.8}]
    )
    assert list(await GlinerTopicTagger(_Pool(many), _settings()).atag("x")) == ["finance"]


async def test_operator_label_map_adds_and_overrides_labels() -> None:
    extractor = _Extractor({"label": "cinema", "confidence": 0.7})
    tagger = GlinerTopicTagger(
        _Pool(extractor),
        _settings(),
        labels={"cinema": "media/film", "work": "work/team"},
    )
    assert tagger.labels["cinema"] == "media/film"
    assert tagger.labels["work"] == "work/team"  # overrides the pack's readable label
    assert list(await tagger.atag("We saw a film")) == ["media/film"]
    with pytest.raises(ValueError, match="invalid domain"):
        GlinerTopicTagger(_Pool(extractor), _settings(), labels={"cinema": "Media Film"})


async def test_results_are_cached_per_text() -> None:
    extractor = _Extractor({"label": "work", "confidence": 0.9})
    tagger = GlinerTopicTagger(_Pool(extractor), _settings())
    await tagger.atag("same text")
    await tagger.atag("same text")
    assert len(extractor.calls) == 1


def test_offline_only_and_classification_task_required() -> None:
    pool = _Pool(_Extractor(None))
    with pytest.raises(ValueError, match="offline only"):
        GlinerTopicTagger(pool, _settings(local_files_only=False))
    with pytest.raises(ValueError, match="classification"):
        GlinerTopicTagger(pool, GlinerSettings.model_validate({"tasks": ["entities"]}))
    assert pool.bound == []  # nothing was bound, so nothing could load


async def test_a_failing_tagger_leaves_the_domain_unknown() -> None:
    class _Boom:
        def classify_text(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("no local weights")

    tagger = GlinerTopicTagger(_Pool(_Boom()), _settings())
    assert await atopic_domains("text", tagger) == []


def test_parse_topic_hits_shapes() -> None:
    labels = {"work": "work"}
    assert parse_topic_hits({"topic": "work"}, labels) == [("work", None)]
    assert parse_topic_hits({"topic": {"label": "work", "confidence": "0.7"}}, labels) == [
        ("work", 0.7)
    ]
    assert parse_topic_hits({"other": "work"}, labels) == []
    assert parse_topic_hits(None, labels) == []


# ------------------------------------------------------------------ config


def _config(topic: dict[str, Any] | None = None, **settings: Any) -> dict[str, Any]:
    return {
        "runtime": {
            "provider_services": {
                "health_timeout_s": 5.0,
                "close_timeout_s": 10.0,
                "startup_timeout_s": None,
            }
        },
        "model": {
            "extraction": {
                "topic": {
                    "provider": "gliner2",
                    **(topic or {}),
                    "settings": {"tasks": ["classification"], **settings},
                }
            }
        },
    }


def test_config_label_map_and_threshold() -> None:
    cfg = MemspineConfig.model_validate(
        _config({"labels": {"cinema": "media/film"}, "min_confidence": 0.7})
    )
    topic = cfg.model.extraction.topic
    assert topic is not None
    assert topic.labels == {"cinema": "media/film"}
    assert topic.min_confidence == 0.7
    default = MemspineConfig.model_validate(_config()).model.extraction.topic
    assert default is not None
    assert default.min_confidence == constants.TOPIC_MIN_CONFIDENCE


@pytest.mark.parametrize(
    ("topic", "match"),
    [
        ({"labels": {"cinema": "Media/Film"}}, "not a domain id"),
        ({"labels": {"cinema": "media//film"}}, "not a domain id"),
        ({"labels": {" cinema": "media/film"}}, "surrounding whitespace"),
        ({"min_confidence": 1.5}, "less than or equal"),
        ({"unknown": 1}, "Extra inputs"),
    ],
)
def test_config_rejects_bad_topic_settings(topic: dict[str, Any], match: str) -> None:
    with pytest.raises((ConfigError, ValueError), match=match):
        MemspineConfig.model_validate(_config(topic))


def test_config_gliner_topic_binding_is_offline_only_with_classification() -> None:
    with pytest.raises((ConfigError, ValueError), match="offline only"):
        MemspineConfig.model_validate(_config(local_files_only=False))
    bad = _config()
    bad["model"]["extraction"]["topic"]["settings"]["tasks"] = ["entities"]
    with pytest.raises((ConfigError, ValueError), match="classification"):
        MemspineConfig.model_validate(bad)
