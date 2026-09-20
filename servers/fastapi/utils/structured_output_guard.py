"""
Safeguards for structured (JSON) LLM generations.

Structured calls run without an explicit max_tokens, and nothing on the
server side ever ended a generation that simply keeps producing tokens. A model
that loops, streams endless whitespace after its answer, or keeps "thinking"
therefore left the UI waiting forever on a request the model itself never
finished. This module provides:

- `JsonCompletionWatcher`: incrementally tracks a streamed reply so the caller
  knows when a complete top-level JSON object has arrived (and how much the
  model kept writing after it), and can spot degenerate output.
- `extract_json_object`: tolerant extraction of the first JSON object from text
  wrapped in code fences, `<think>` blocks or a sentence of prose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)

# A well-behaved model stops right after the closing brace (at most a fence
# and a newline follow). Anything much longer means it is not going to stop.
TRAILING_CHARS_AFTER_JSON = 256
# JSON has no legitimate run of thousands of whitespace characters; grammar-
# constrained decoders can get stuck emitting them forever.
MAX_WHITESPACE_RUN = 2_000
# If this much visible text arrives without a single "{", no JSON is coming.
MAX_CHARS_BEFORE_JSON = 20_000


def strip_reasoning(text: str) -> str:
    """Drop `<think>...</think>` blocks (and an unterminated trailing one)."""
    if _THINK_CLOSE in text:
        text = text.rsplit(_THINK_CLOSE, 1)[1]
    elif _THINK_OPEN in text:
        text = text.split(_THINK_OPEN, 1)[0]
    return text


def _first_balanced_object(text: str) -> Optional[str]:
    """First complete top-level {...} in `text`, string- and escape-aware."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        # Unbalanced from this "{": try the next one.
        start = text.find("{", start + 1)
    return None


def extract_json_object(text: str) -> Optional[dict]:
    """
    Parse the first JSON object in a model reply, tolerating `<think>` blocks,
    markdown fences and surrounding prose. Returns None when there is none.
    """
    if not text:
        return None
    cleaned = strip_reasoning(text)
    candidates = []
    for fenced in _FENCE_RE.findall(cleaned):
        candidates.append(fenced)
    candidates.append(cleaned)

    for candidate in candidates:
        snippet = _first_balanced_object(candidate)
        if not snippet:
            continue
        try:
            parsed = json.loads(snippet)
        except ValueError:
            try:
                import dirtyjson

                parsed = dict(dirtyjson.loads(snippet))
            except Exception:
                continue
        if isinstance(parsed, dict):
            return parsed
    return None


@dataclass
class JsonCompletionWatcher:
    """Feed streamed content chunks; inspect the flags after each `feed`."""

    total_chars: int = 0
    visible_chars: int = 0
    json_complete: bool = False
    chars_after_json: int = 0
    degenerate_reason: Optional[str] = None

    _in_think: bool = field(default=False, repr=False)
    _tail: str = field(default="", repr=False)
    _started: bool = field(default=False, repr=False)
    _depth: int = field(default=0, repr=False)
    _in_string: bool = field(default=False, repr=False)
    _escaped: bool = field(default=False, repr=False)
    _whitespace_run: int = field(default=0, repr=False)
    _visible_before_json: int = field(default=0, repr=False)

    def feed(self, chunk: str) -> None:
        if not chunk:
            return
        self.total_chars += len(chunk)

        # Track <think> boundaries, including tags split across chunks. Only
        # text outside think blocks counts as the model's actual answer.
        window = self._tail + chunk
        self._tail = window[-(len(_THINK_CLOSE) - 1) :]
        if _THINK_CLOSE in window:
            self._in_think = False
            self._reset_scan()
            chunk = window.rsplit(_THINK_CLOSE, 1)[1]
        elif self._in_think or _THINK_OPEN in window:
            chunk = window.split(_THINK_OPEN, 1)[0] if not self._in_think else ""
            self._in_think = True
        if _THINK_OPEN in chunk:
            chunk = chunk.split(_THINK_OPEN, 1)[0]
            self._in_think = True
        if not chunk:
            return
        self.visible_chars += len(chunk)

        for char in chunk:
            if self.json_complete:
                self.chars_after_json += 1
                continue

            if char.isspace():
                self._whitespace_run += 1
                if self._whitespace_run > MAX_WHITESPACE_RUN:
                    self.degenerate_reason = (
                        f"the model emitted more than {MAX_WHITESPACE_RUN} "
                        "whitespace characters in a row"
                    )
                    return
            else:
                self._whitespace_run = 0

            if not self._started:
                if char == "{":
                    self._started = True
                    self._depth = 1
                else:
                    self._visible_before_json += 1
                    if self._visible_before_json > MAX_CHARS_BEFORE_JSON:
                        self.degenerate_reason = (
                            f"no JSON began within {MAX_CHARS_BEFORE_JSON} characters"
                        )
                        return
                continue

            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif char == "\\":
                    self._escaped = True
                elif char == '"':
                    self._in_string = False
            elif char == '"':
                self._in_string = True
            elif char == "{":
                self._depth += 1
            elif char == "}":
                self._depth -= 1
                if self._depth == 0:
                    self.json_complete = True

    def _reset_scan(self) -> None:
        self._started = False
        self._depth = 0
        self._in_string = False
        self._escaped = False
        self._whitespace_run = 0
        self._visible_before_json = 0
        self.json_complete = False
        self.chars_after_json = 0

    @property
    def keeps_writing_after_json(self) -> bool:
        return self.json_complete and self.chars_after_json > TRAILING_CHARS_AFTER_JSON
