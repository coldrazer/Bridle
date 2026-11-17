"""Guaranteed JSON output via grammar-constrained decoding (llguidance).

At every step the grammar computes which tokens may come next and all others are masked,
so the output always parses and matches the JSON schema (unless max_tokens truncates it)."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import numpy as np

from .domain import ResponseFormat

log = logging.getLogger("bridle.structured")


class StructuredOutputUnavailable(Exception):
    pass


def instruction(fmt: ResponseFormat) -> str:
    if fmt.type == "json_schema":
        return ("Respond only with a JSON object that conforms to this JSON schema:\n"
                + json.dumps(fmt.schema, ensure_ascii=False))
    return "Respond only with a valid JSON object."


class GrammarFactory:
    """One per loaded model: building the llguidance tokenizer takes ~1s, so it is cached."""

    def __init__(self, tokenizer: Any):
        self._tokenizer = tokenizer
        self._lltok = None
        self._lock = threading.Lock()

    def _lltokenizer(self):
        with self._lock:
            if self._lltok is None:
                try:
                    import llguidance.hf
                except ImportError as exc:
                    raise StructuredOutputUnavailable(
                        "structured output needs the 'llguidance' package (pip install llguidance)") from exc
                hf = getattr(self._tokenizer, "_tokenizer", self._tokenizer)
                eos = sorted(getattr(self._tokenizer, "eos_token_ids", []) or [])
                self._lltok = llguidance.hf.from_tokenizer(hf, eos_token=eos or None)
            return self._lltok

    def processor(self, fmt: ResponseFormat) -> "GrammarProcessor":
        from llguidance import LLMatcher

        lltok = self._lltokenizer()
        grammar = LLMatcher.grammar_from_json_schema(fmt.schema or {"type": "object"},
                                                     defaults={"whitespace_flexible": False})
        is_error, messages = LLMatcher.validate_grammar_with_warnings(grammar, lltok)
        if is_error:
            raise ValueError(f"unsupported JSON schema: {'; '.join(messages)}")
        return GrammarProcessor(LLMatcher(lltok, grammar), lltok.vocab_size)


class GrammarProcessor:
    """mlx-lm logits processor: (tokens, logits) -> logits. It sees the tokens fed so far on
    each call; tokens beyond the first call's length are the ones it sampled, which it feeds
    to the grammar before computing the next mask."""

    def __init__(self, matcher: Any, vocab_size: int):
        from llguidance.numpy import allocate_token_bitmask

        self.matcher = matcher
        self.vocab_size = vocab_size
        self._bitmask = allocate_token_bitmask(1, vocab_size)
        self._base: int | None = None
        self._consumed = 0

    def __call__(self, tokens: Any, logits: Any) -> Any:
        import mlx.core as mx
        from llguidance.numpy import fill_next_token_bitmask

        n = int(tokens.shape[-1]) if tokens is not None else 0
        if self._base is None:
            self._base = n
        new = [int(t) for t in np.array(tokens).reshape(-1)[self._base + self._consumed:]] if n else []
        for tok in new:
            if not self.matcher.is_stopped():
                self.matcher.consume_token(tok)
            self._consumed += 1
        if self.matcher.is_error():
            log.warning("grammar error: %s", self.matcher.get_error())
            return logits
        fill_next_token_bitmask(self.matcher, self._bitmask, 0)
        allowed = np.unpackbits(self._bitmask.view(np.uint8), bitorder="little")[: self.vocab_size].astype(bool)
        width = logits.shape[-1]
        if width > allowed.size:   # model head may be padded beyond the tokenizer vocab
            allowed = np.concatenate([allowed, np.zeros(width - allowed.size, dtype=bool)])
        mask = mx.array(allowed[:width].reshape(1, width))
        return mx.where(mask, logits, mx.array(-float("inf"), dtype=logits.dtype))


def validate(text: str, fmt: ResponseFormat) -> str | None:
    """Returns an error message if text isn't valid for fmt, else None."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"output is not valid JSON ({exc.msg}); it may have been cut off by max_tokens"
    if fmt.type == "json_schema" and fmt.schema:
        try:
            import jsonschema
        except ImportError:
            return None
        try:
            jsonschema.validate(value, fmt.schema)
        except jsonschema.ValidationError as exc:
            return f"output does not match the schema: {exc.message}"
    return None
