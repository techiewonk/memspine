"""Word-vector encoders for the word-vector retrieval leg (``read.word_vector_leg``).

A text is represented by its words' static vectors, pooled and unit-normalised, so related
words meet even when they never co-occur ("hobbies" near "kayaking"), unlike BM25, which only
matches identical terms. Two providers:

* ``model2vec`` (default, ``[static]`` extra): a distilled static model such as
  ``minishlab/potion-retrieval-32M``; its own tokenizer and token weighting.
* ``word2vec`` (``gensim`` installed separately): a classic word2vec / GloVe / fastText table
  loaded with gensim ``KeyedVectors`` from ``read.word_vector_model`` (a local path; ``.bin`` is
  read as the binary word2vec format, ``.kv`` as gensim's native format, anything else as text);
  a text is the mean of its in-vocabulary lower-cased word vectors.
"""

from __future__ import annotations

import importlib
import math
import re
from collections.abc import Sequence
from typing import Any

from memspine.exceptions import MissingServiceError

__all__ = ["DEFAULT_WORD_VECTOR_MODEL", "WordVectorEncoder"]

DEFAULT_WORD_VECTOR_MODEL = "minishlab/potion-retrieval-32M"
_WORD = re.compile(r"[a-z0-9']+")


def _unit(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else [0.0 for _ in vec]


class WordVectorEncoder:
    """Synchronous encoder; the engine calls it in a worker thread."""

    def __init__(self, provider: str = "model2vec", model: str | None = None) -> None:
        if provider not in ("model2vec", "word2vec"):
            raise ValueError(
                f"unknown read.word_vector_provider {provider!r} (model2vec, word2vec)"
            )
        self.provider = provider
        self.model_name = model or DEFAULT_WORD_VECTOR_MODEL
        module = "model2vec" if provider == "model2vec" else "gensim"
        try:
            importlib.import_module(module)
        except ImportError as exc:
            extra = "static" if provider == "model2vec" else "gensim"
            raise MissingServiceError(
                f"services.embedding.word_vectors ({module})", extra=extra
            ) from exc
        self._model: Any = None

    @property
    def encoder_id(self) -> str:
        return f"{self.provider}:{self.model_name}"

    def _load(self) -> Any:
        if self._model is None:
            if self.provider == "model2vec":
                from model2vec import StaticModel

                self._model = StaticModel.from_pretrained(self.model_name)
            else:
                from gensim.models import KeyedVectors

                path = self.model_name
                if path.endswith(".kv"):
                    self._model = KeyedVectors.load(path)
                else:
                    self._model = KeyedVectors.load_word2vec_format(
                        path, binary=path.endswith(".bin")
                    )
        return self._model

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._load()
        if self.provider == "model2vec":
            return [_unit([float(x) for x in v]) for v in model.encode(list(texts))]
        out: list[list[float]] = []
        dim = int(model.vector_size)
        for text in texts:
            words = [w for w in _WORD.findall(text.lower()) if w in model.key_to_index]
            if not words:
                out.append([0.0] * dim)
                continue
            total = [0.0] * dim
            for w in words:
                for i, x in enumerate(model[w]):
                    total[i] += float(x)
            out.append(_unit(total))
        return out
