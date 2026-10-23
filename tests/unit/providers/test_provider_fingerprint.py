from pydantic import SecretStr

from memspine.providers.engine_changes import resolved_config_fingerprint
from memspine.providers.settings import OpenAICompatRoleSettings


def test_masked_credentials_still_change_the_target_fingerprint():
    first = OpenAICompatRoleSettings(
        model="openai/test", timeout_seconds=30.0, api_key=SecretStr("secret-a")
    )
    second = first.model_copy(update={"api_key": SecretStr("secret-b")})
    digest = resolved_config_fingerprint(first)
    assert digest != resolved_config_fingerprint(second)
    assert len(digest) == 64
    assert "secret-a" not in digest
    assert digest == resolved_config_fingerprint(first)
