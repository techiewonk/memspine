"""Current ONNX provider settings."""

import pytest


def test_execution_providers_accept_json_list_and_freeze_it() -> None:
    from pydantic import ValidationError

    from memspine.config.schema.providers import OnnxEmbeddingSettings, OnnxRerankerSettings

    for settings in (OnnxEmbeddingSettings, OnnxRerankerSettings):
        assert settings.model_validate({"providers": ["CPUExecutionProvider"]}).providers == (
            "CPUExecutionProvider",
        )
        with pytest.raises(ValidationError):
            settings.model_validate({"providers": []})
