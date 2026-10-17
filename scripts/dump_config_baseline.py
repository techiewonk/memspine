"""Dump the resolved config of every shipped template and preset (F-07 same-YAML-keys proof).

``python scripts/dump_config_baseline.py > out.json``; the committed baseline in
``tests/unit/config/data/config_dump_baseline.json`` was taken before the F-07 split.
"""

from __future__ import annotations

import hashlib
import json

from memspine.config import presets
from memspine.config.loader import load_config, template_dir
from memspine.config.schema import MemspineConfig


def dump() -> dict:
    # Lifecycle deadlines are intentionally required inputs. Supply the documented
    # example values here so the default schema snapshot remains reproducible.
    explicit_runtime = {
        "provider_services": {
            "health_timeout_s": 5.0,
            "close_timeout_s": 10.0,
            "startup_timeout_s": None,
        }
    }
    out: dict = {
        "default": MemspineConfig(runtime=explicit_runtime).model_dump(mode="json")
    }
    for path in sorted(template_dir().glob("*.yaml")):
        out[f"template:{path.stem}"] = load_config(template=path.stem).config.model_dump(
            mode="json"
        )
    for name in presets.preset_names():
        out[f"preset:{name}"] = load_config(
            template="base", overrides=presets.load_preset(name)
        ).config.model_dump(mode="json")
    schema = json.dumps(MemspineConfig.model_json_schema(), sort_keys=True)
    out["json_schema_sha256"] = hashlib.sha256(schema.encode()).hexdigest()
    return out


if __name__ == "__main__":
    print(json.dumps(dump(), indent=1, sort_keys=True, default=str))
