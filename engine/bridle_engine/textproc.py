"""Streaming text post-processing: stop sequences, reasoning (<think>) and tool-call extraction."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable

from .domain import ToolCall

log = logging.getLogger("bridle.text")


class StopMatcher:
    """Finds stop sequences across streamed chunks, holding back just enough text to never
    emit the beginning of a stop sequence."""

    def __init__(self, stops: tuple[str, ...]):
        self.stops = stops
        self.hold = max((len(s) for s in stops), default=1) - 1
        self.buf = ""

    def feed(self, text: str) -> tuple[str, bool]:
        self.buf += text
        hits = [i for i in (self.buf.find(s) for s in self.stops) if i >= 0]
        if hits:
            out, self.buf = self.buf[: min(hits)], ""
            return out, True
        cut = max(0, len(self.buf) - self.hold)
        out, self.buf = self.buf[:cut], self.buf[cut:]
        return out, False

    def flush(self) -> str:
        out, self.buf = self.buf, ""
        return out


class TextRouter:
    """Splits generated text into normal content, reasoning and tool-call bodies using the
    model's marker strings (e.g. <think>…</think>, <tool_call>…</tool_call>).

    feed() returns a list of (kind, text) with kind in {"content", "reasoning", "tool"};
    "tool" items are complete tool-call bodies (markers removed)."""

    def __init__(self, think: tuple[str, str] | None, tool: tuple[str, str] | None, start_in_reasoning: bool = False):
        self.markers: dict[str, tuple[str, str]] = {}
        if think and think[0] and think[1]:
            self.markers["reasoning"] = think
        if tool and tool[0] and tool[1]:
            self.markers["tool"] = tool
        self.state = "reasoning" if (start_in_reasoning and "reasoning" in self.markers) else "content"
        self.buf = ""
        self.tool_buf = ""
        self.hold = max((len(m) for pair in self.markers.values() for m in pair), default=1) - 1

    def feed(self, text: str) -> list[tuple[str, str]]:
        self.buf += text
        out: list[tuple[str, str]] = []
        while True:
            if self.state == "content":
                # Earliest opening marker of any kind.
                best = None
                for kind, (start, _) in self.markers.items():
                    i = self.buf.find(start)
                    if i >= 0 and (best is None or i < best[0]):
                        best = (i, kind, start)
                if best is None:
                    break
                i, kind, start = best
                self._emit(out, "content", self.buf[:i])
                self.buf = self.buf[i + len(start):]
                self.state = kind
            else:
                end = self.markers[self.state][1]
                i = self.buf.find(end)
                if i < 0:
                    break
                self._emit(out, self.state, self.buf[:i])
                if self.state == "tool":
                    out.append(("tool", self.tool_buf))
                    self.tool_buf = ""
                self.buf = self.buf[i + len(end):]
                self.state = "content"
        # Emit what can't be the start of a marker.
        cut = max(0, len(self.buf) - self.hold)
        if cut:
            self._emit(out, self.state, self.buf[:cut])
            self.buf = self.buf[cut:]
        return out

    def flush(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self._emit(out, self.state, self.buf)
        self.buf = ""
        if self.state == "tool" and self.tool_buf:
            out.append(("tool", self.tool_buf))  # unterminated (likely truncated by max_tokens)
            self.tool_buf = ""
        return out

    def _emit(self, out: list, kind: str, text: str) -> None:
        if not text:
            return
        if kind == "tool":
            self.tool_buf += text
        else:
            out.append((kind, text))


def router_for(tokenizer: Any, prompt: str, tools_enabled: bool) -> TextRouter:
    think = None
    if getattr(tokenizer, "has_thinking", False):
        think = (tokenizer.think_start, tokenizer.think_end)
    tool = None
    if tools_enabled and getattr(tokenizer, "has_tool_calling", False):
        tool = (tokenizer.tool_call_start, tokenizer.tool_call_end)
    starts_in_reasoning = bool(think and prompt.rstrip().endswith(think[0]))
    return TextRouter(think, tool, starts_in_reasoning)


def parse_tool_calls(parser: Callable[[str, Any], Any] | None, bodies: list[str],
                     tools: tuple[dict, ...]) -> list[ToolCall]:
    """Turns raw tool-call bodies into ToolCall objects using the model's parser
    (mlx-lm ships parsers for Qwen, Llama, Mistral, Gemma, GLM, Kimi, …)."""
    calls: list[ToolCall] = []
    for body in bodies:
        try:
            parsed = parser(body, list(tools)) if parser else json.loads(body.strip())
        except (ValueError, KeyError, TypeError) as exc:
            parsed = _lenient_objects(body)   # small models often wrap or repeat braces
            if not parsed:
                log.warning("could not parse tool call (%s): %.200s", exc, body)
                continue
        for item in parsed if isinstance(parsed, list) else [parsed]:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            args = item.get("arguments", item.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            if not isinstance(args, dict):
                args = {"value": args}
            calls.append(ToolCall(str(item.get("id") or f"call_{uuid.uuid4().hex[:24]}"), item["name"], args))
    return calls


def _lenient_objects(body: str) -> list[dict]:
    """Every well-formed JSON object with a "name" key inside otherwise malformed text."""
    decoder = json.JSONDecoder()
    found: list[dict] = []
    i = 0
    while (i := body.find("{", i)) >= 0:
        try:
            obj, end = decoder.raw_decode(body, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict) and isinstance(obj.get("name"), str):
            found.append(obj)
            i = end
        else:
            i += 1
    return found
