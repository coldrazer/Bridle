"""Text embeddings (MiniLM, BGE, Qwen3-Embedding, EmbeddingGemma, …) via the optional mlx-embeddings package."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .runtime_errors import FeatureUnavailable

BATCH = 32
MAX_LENGTH = 512


def load_embedding_model(path: Path) -> tuple[Any, Any]:
    try:
        from mlx_embeddings.utils import load
    except ImportError as exc:
        raise FeatureUnavailable("embeddings support isn't installed; run `scripts/bridle extras embeddings` "
                                 "(installs mlx-embeddings)") from exc
    return load(str(path))


def _encode(tokenizer: Any, texts: list[str]) -> dict:
    fn = getattr(tokenizer, "batch_encode_plus", None) or tokenizer
    return fn(texts, return_tensors="mlx", padding=True, truncation=True, max_length=MAX_LENGTH)


def embed_texts(model: Any, tokenizer: Any, texts: list[str]) -> tuple[list[list[float]], int]:
    """Returns L2-normalized embeddings and the number of input tokens."""
    import mlx.core as mx

    vectors: list[list[float]] = []
    total_tokens = 0
    for i in range(0, len(texts), BATCH):
        batch = texts[i:i + BATCH]
        enc = _encode(tokenizer, batch)
        mask = enc["attention_mask"]
        total_tokens += int(mask.sum().item())
        kwargs = {"attention_mask": mask}
        if "token_type_ids" in enc:
            kwargs["token_type_ids"] = enc["token_type_ids"]
        try:
            out = model(enc["input_ids"], **kwargs)
        except TypeError:
            out = model(enc["input_ids"], attention_mask=mask)
        emb = getattr(out, "text_embeds", None)
        if emb is None:
            hidden = getattr(out, "last_hidden_state", None)
            if hidden is None:
                hidden = out[0] if isinstance(out, (tuple, list)) else out
            m = mask[..., None].astype(hidden.dtype)
            emb = (hidden * m).sum(axis=1) / mx.maximum(m.sum(axis=1), 1)
        emb = emb.astype(mx.float32)
        emb = emb / mx.maximum(mx.linalg.norm(emb, axis=-1, keepdims=True), 1e-12)
        vectors.extend(emb.tolist())
    return vectors, total_tokens
