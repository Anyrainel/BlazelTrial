"""Text embeddings for semantic search. Local only: no API key, no per-call cost."""

import hashlib
import math
import re
from functools import cache
from typing import Protocol

from chatbot.config import get_settings


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    """fastembed (ONNX) model, downloaded once into `.state/models/`."""

    def __init__(self, model_name: str, cache_dir: str):
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
        self.dim = len(next(iter(self._model.embed(["probe"]))))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.embed(texts)]


class HashEmbedder:
    """Deterministic bag-of-words hashing. For offline tests; no semantics beyond word overlap."""

    dim = 256

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for word in re.findall(r"\w+", text.lower()):
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                vec[h % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


@cache
def get_embedder() -> Embedder:
    settings = get_settings()
    if settings.embedding_backend == "hash":
        return HashEmbedder()
    settings.models_path.mkdir(parents=True, exist_ok=True)
    return FastEmbedder(settings.embedding_model, str(settings.models_path))
