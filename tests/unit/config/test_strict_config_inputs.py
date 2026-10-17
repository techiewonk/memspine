"""Configuration inputs reject accidental scalar coercion."""

from __future__ import annotations

import math
from typing import Annotated

import pytest
from pydantic import ValidationError

from memspine.config.loader import load_config
from memspine.config.schema.base import ConfigModel
from memspine.config.schema.offline import OfflineConfig
from memspine.config.schema.read import ReadConfig
from memspine.config.schema.write import DataShapeConfig
from memspine.core.policies.scoring import ScoringPolicy
from memspine.exceptions import ConfigError


@pytest.mark.parametrize("value", [True, "4"])
def test_standalone_model_rejects_coerced_integer(value: object) -> None:
    with pytest.raises(ValidationError, match="candidate_pool"):
        ReadConfig(candidate_pool=value)  # type: ignore[arg-type]


def test_standalone_model_rejects_non_finite_float() -> None:
    with pytest.raises(ValidationError, match="finite"):
        ReadConfig(date_prior_boost=math.inf)


def test_nested_typed_policy_mapping_rejects_string_number() -> None:
    with pytest.raises(ValidationError, match="leg_weights.score"):
        ReadConfig(leg_weights={"score": "0.75"})  # type: ignore[dict-item]


def test_opaque_nested_policy_payload_rejects_non_finite_values() -> None:
    with pytest.raises(ValidationError, match="scoring.rank.*finite"):
        ReadConfig(scoring={"rank": {"weights": [0.5, math.nan]}})


def test_tuple_configuration_accepts_yaml_style_list() -> None:
    assert OfflineConfig(decay_boundaries=[0.5, 0.3, 0.15]).decay_boundaries == (
        0.5,
        0.3,
        0.15,
    )


def test_loader_reports_nested_path_for_unparsed_override() -> None:
    with pytest.raises(ConfigError, match=r"read\.candidate_pool.*integer"):
        load_config(template="base", overrides={"read": {"candidate_pool": "4"}})


def test_loader_rejects_non_finite_opaque_values_without_echoing_them() -> None:
    with pytest.raises(ConfigError, match=r"read\.scoring\.rank.*finite") as err:
        load_config(template="base", overrides={"read": {"scoring": {"rank": math.inf}}})
    assert "inf" not in str(err.value).lower()


def test_environment_scalar_parser_keeps_numeric_environment_values_valid() -> None:
    assert (
        load_config(
            template="base", env={"MEMSPINE_READ__CANDIDATE_POOL": "4"}
        ).config.read.candidate_pool
        == 4
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("rerank_max_top_k", "8"), ("rerank_blend", "0.5")],
)
def test_optional_numeric_fields_do_not_accept_strings(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        ReadConfig.model_validate({field: value})


def test_optional_boolean_field_does_not_accept_string() -> None:
    with pytest.raises(ValidationError):
        DataShapeConfig.model_validate({"has_timestamps": "false"})


def test_optional_scalar_fields_accept_none() -> None:
    config = ReadConfig.model_validate({"rerank_max_top_k": None, "rerank_blend": None})
    assert config.rerank_max_top_k is None
    assert config.rerank_blend is None
    assert DataShapeConfig.model_validate({"has_timestamps": None}).has_timestamps is None


class _AnnotatedConfig(ConfigModel):
    count: Annotated[int, "positive count"]
    values: list[int]


def test_annotated_scalar_is_still_strict_and_list_rejects_unordered_set() -> None:
    with pytest.raises(ValidationError, match="count.*integer"):
        _AnnotatedConfig.model_validate({"count": "2", "values": [1, 2]})
    with pytest.raises(ValidationError, match="values.*list"):
        _AnnotatedConfig.model_validate({"count": 2, "values": {1, 2}})


def test_policy_options_reject_scalar_coercion_inside_untyped_policy_map() -> None:
    with pytest.raises(ConfigError, match="importance_weight"):
        ScoringPolicy.bind({"importance_weight": "0.25"})
