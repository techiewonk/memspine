"""Generate ADR-084 references BEFORE removing fastembed.

Run with fastembed==0.9.0 installed: python scripts/make_onnx_parity_fixtures.py
Uses pinned catalogue exports in the HF cache (not fastembed's mutable main).
The output records SHA-256 of every file supplied to fastembed.
"""

from __future__ import annotations

import gc
import hashlib
import importlib.metadata
import json
from pathlib import Path

from huggingface_hub import snapshot_download

from memspine.clients.weights.onnx_catalogue import CATALOGUE

TEXTS = [
    "",
    "A memory engine preserves evidence.",
    "A short sentence.",
    "नमस्ते दुनिया — स्मृति और प्रमाण।",
    "Bonjour le monde, café et mémoire.",
    "你好，世界。记忆与证据。",
    "Long context " * 300,
]
QUERY = "What does a memory engine preserve?"
DOCUMENTS = [
    "A memory engine preserves evidence.",
    "Bananas are yellow.",
    "Memory stores facts and evidence.",
    "",
]


def main() -> None:
    from fastembed import TextEmbedding
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    if importlib.metadata.version("fastembed") != "0.9.0":
        raise RuntimeError("References require fastembed==0.9.0")
    output = Path(__file__).resolve().parents[1] / "tests/unit/services/embedding/data"
    output.mkdir(parents=True, exist_ok=True)
    for name, spec in CATALOGUE.items():
        print(f"Generating {name}", flush=True)
        files = [
            spec.model_file,
            spec.tokenizer_file,
            "tokenizer_config.json",
            "special_tokens_map.json",
            "config.json",
            *spec.additional_files,
        ]
        directory = Path(snapshot_download(spec.repo, revision=spec.revision, allow_patterns=files))
        kwargs = {
            "model_name": name,
            "specific_model_path": str(directory),
            "threads": 1,
            "providers": ["CPUExecutionProvider"],
            "local_files_only": True,
        }
        reference: dict[str, object] = {
            "model": name,
            "repo": spec.repo,
            "revision": spec.revision,
            "fastembed_version": "0.9.0",
            "files_sha256": {
                file: hashlib.sha256((directory / file).read_bytes()).hexdigest()
                for file in files
                if (directory / file).is_file()
            },
        }
        if spec.pooling == "score":
            instance = TextCrossEncoder(**kwargs)
            reference.update(
                query=QUERY,
                documents=DOCUMENTS,
                scores=[round(float(x), 7) for x in instance.rerank(QUERY, DOCUMENTS)],
            )
        else:
            instance = TextEmbedding(**kwargs)
            reference.update(
                texts=TEXTS,
                vectors=[[round(float(x), 7) for x in vector] for vector in instance.embed(TEXTS)],
            )
        destination = output / (name.replace("/", "__") + ".json")
        destination.write_text(
            json.dumps(reference, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        del instance
        gc.collect()


if __name__ == "__main__":
    main()
