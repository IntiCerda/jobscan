"""Semantic similarity between a posting and the profile summary.

This is the one part of the pipeline that can be absent: if Ollama is not
running, `NullEmbedder` returns None for everything and scoring falls back to
keyword matching alone. That is deliberate — the tool has to work on a laptop
with nothing installed, and a semantic bonus that cannot be computed should
subtract nothing rather than break the run.

The port/adapter split is the same shape used for the LLM boundary in
media-compliance-intel: the caller depends on `Embedder`, never on Ollama.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import Protocol

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "nomic-embed-text"

# Postings run long and the tail is usually benefits boilerplate. Truncating
# keeps every document inside the model's context without a second pass.
MAX_CHARS = 4000


class Embedder(Protocol):
    def embed(self, text: str) -> list[float] | None: ...


class NullEmbedder:
    """Used when no embedding backend is reachable."""

    def embed(self, text: str) -> list[float] | None:  # noqa: ARG002
        return None


class OllamaEmbedder:
    """Talks to a local Ollama instance over HTTP.

    Failures return None rather than raising: one posting that fails to embed
    should cost that posting its semantic bonus, not abort the ranking.
    """

    def __init__(
        self,
        url: str = DEFAULT_URL,
        model: str = DEFAULT_MODEL,
        *,
        timeout: int = 60,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def available(self) -> bool:
        try:
            req = urllib.request.Request(f"{self.url}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:
                names = {
                    m.get("name", "").split(":")[0]
                    for m in json.loads(resp.read()).get("models", [])
                }
            return self.model.split(":")[0] in names
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return False

    def embed(self, text: str) -> list[float] | None:
        payload = json.dumps(
            {"model": self.model, "input": text[:MAX_CHARS]}
        ).encode()
        try:
            req = urllib.request.Request(
                f"{self.url}/api/embed",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None

        # /api/embed returns {"embeddings": [[...]]}; the older /api/embeddings
        # returned {"embedding": [...]}. Accept either so the tool survives an
        # Ollama version bump.
        vectors = body.get("embeddings")
        if isinstance(vectors, list) and vectors and isinstance(vectors[0], list):
            return [float(x) for x in vectors[0]]
        single = body.get("embedding")
        if isinstance(single, list) and single:
            return [float(x) for x in single]
        return None


def resolve(url: str = DEFAULT_URL, model: str = DEFAULT_MODEL) -> Embedder:
    """Pick the best available backend. Probing once here means the caller does
    not have to care which one it got."""
    candidate = OllamaEmbedder(url, model)
    return candidate if candidate.available() else NullEmbedder()


def cosine(a: list[float] | None, b: list[float] | None) -> float | None:
    """Cosine similarity, or None when either side is missing.

    None is distinct from 0.0 on purpose: "not computed" must not be scored the
    same as "computed and unrelated".
    """
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return None
    return dot / (na * nb)
