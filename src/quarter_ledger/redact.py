"""Redaction: values a rule says may not reach a model vendor are swapped for placeholders on
the way out and put back in the reply, so the agent works with the real data and the vendor
never sees it.

A placeholder is the kind and a keyed hash of the value, such as [EMAIL_3f2a9c1b]. The same
value gets the same placeholder in every call, so the prompt cache still hits, and the vendor
cannot work the value back out of it. A card number must pass the Luhn check and an IBAN its
check digits. A false match costs little, since the reply gets the value back.

In a streamed reply a placeholder usually arrives split over several events, so an event that
ends partway into one is held until the next completes it, and the text goes out restored.
"""

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field


def _luhn(text: str) -> bool:
    digits = [int(c) for c in text if c.isdigit()]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d > 4 else d * 2) for i, d in enumerate(reversed(digits)))
    return 13 <= len(digits) <= 19 and digits[0] in (2, 3, 4, 5, 6) and total % 10 == 0


def _iban(text: str) -> bool:
    compact = text.replace(" ", "")
    moved = compact[4:] + compact[:4]
    return int("".join(str(int(c, 36)) for c in moved)) % 97 == 1


def _phone(text: str) -> bool:
    return 8 <= sum(c.isdigit() for c in text) <= 15


KINDS: dict[str, tuple[str, Callable[[str], bool] | None]] = {
    "email": (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}", None),
    "card": (r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])", _luhn),
    "iban": (r"\b[A-Z]{2}\d{2}(?:[A-Z0-9]{11,30}|(?: [A-Z0-9]{4}){2,7}(?: [A-Z0-9]{1,4})?)\b", _iban),
    "ssn": (r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])", None),
    "phone": (r"(?<![\w+])\+\d[\d ().-]{6,18}\d(?!\d)", _phone),
}
PLACEHOLDER = re.compile(r"\[[A-Z][A-Z0-9_]*_[0-9a-f]{8}\]")
BETWEEN = re.compile(f"({PLACEHOLDER.pattern})")  # splits text so a placeholder is never masked again
JSON_TEXT = ("arguments", "partial_json")  # strings that hold JSON: a value put back is escaped
# Sent back to the vendor exactly as it came (signed thinking, encrypted reasoning), so never
# changed either way. The model's thinking keeps its placeholders.
UNTOUCHED = ("thinking", "signature", "encrypted_content")


def opaque(text: str) -> bool:
    """Data, not words: a data URL, or a long run with no spaces (base64 images and files),
    where a pattern would only ever match by chance and a change would corrupt it."""
    return text.startswith("data:") or (len(text) >= 200 and not any(c.isspace() for c in text))


@dataclass
class Redactor:
    """Masks one call's request. `found` maps each placeholder to the value it stands for."""

    kinds: list[tuple[str, re.Pattern, Callable[[str], bool] | None]]
    key: bytes
    found: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def text(self, text: str) -> str:
        if opaque(text):
            return text
        for label, pattern, check in self.kinds:
            def swap(m, label=label, check=check):
                return self._swap(label, m.group(), check)
            pieces = BETWEEN.split(text)
            text = "".join(p if i % 2 else pattern.sub(swap, p) for i, p in enumerate(pieces))
        return text

    def value(self, value):
        """Every string in a parsed JSON body masked; keys are left as they are."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, dict):
            return {k: v if k in UNTOUCHED else self.value(v) for k, v in value.items()}
        return value

    def body(self, raw: bytes) -> bytes:
        """The request body masked: as JSON when it parses, else as text."""
        try:
            masked = json.dumps(self.value(json.loads(raw))).encode() if raw else raw
        except ValueError:
            masked = self.text(raw.decode(errors="replace")).encode()
        return masked if self.found else raw

    def summary(self) -> str | None:
        """What was masked, never the values: "card:1,email:2"."""
        return ",".join(f"{k}:{n}" for k, n in sorted(self.counts.items())) or None

    def _swap(self, label: str, value: str, check) -> str:
        if check and not check(value):
            return value
        digest = hmac.new(self.key, f"{label}\x1f{value}".encode(), hashlib.sha256).hexdigest()[:8]
        placeholder = f"[{label.upper()}_{digest}]"
        if placeholder not in self.found:
            self.found[placeholder] = value
            self.counts[label] = self.counts.get(label, 0) + 1
        return placeholder


def redactor(kinds: list[str], patterns: dict[str, str], key: bytes) -> Redactor:
    compiled = [(k, re.compile(KINDS[k][0]), KINDS[k][1]) for k in dict.fromkeys(kinds)]
    compiled += [(name, re.compile(p), None) for name, p in patterns.items()]
    return Redactor(compiled, key)


def restore(text: str, found: dict[str, str], as_json: bool = False) -> str:
    def back(m: re.Match) -> str:
        value = found.get(m.group())
        if value is None:
            return m.group()
        return json.dumps(value)[1:-1] if as_json else value
    return PLACEHOLDER.sub(back, text) if "[" in text else text


def restore_value(value, found: dict[str, str], as_json: bool = False):
    """A parsed reply with the real values back. Tool arguments sent as JSON text get them
    escaped, so the arguments still parse."""
    if isinstance(value, str):
        return restore(value, found, as_json)
    if isinstance(value, list):
        return [restore_value(v, found) for v in value]
    if isinstance(value, dict):
        return {k: v if k in UNTOUCHED else restore_value(v, found, k in JSON_TEXT) for k, v in value.items()}
    return value


class StreamRestorer:
    """Puts the real values back in a streamed reply, event by event (see the module text)."""

    def __init__(self, found: dict[str, str]):
        self.found = found
        self.held: list[tuple[bytes, dict, tuple]] = []
        self.key = None

    def feed(self, message: bytes, event: dict | None) -> list[bytes]:
        """The events to send in place of this one (its parsed data, or None)."""
        growing = _growing(event) if event else None
        out = self.flush() if self.held and (growing is None or growing[0] != self.key) else []
        if growing is None:
            if event and b"[" in message:
                restored = restore_value(event, self.found)
                if restored != event:
                    message = _sse(restored, message)
            return out + [message]
        self.key, path = growing[0], growing[1:]
        self.held.append((message, event, path))
        if not self._unfinished("".join(_get(e, p[0]) for _, e, p in self.held)):
            out += self.flush()
        return out

    def flush(self) -> list[bytes]:
        held, self.held, self.key = self.held, [], None
        if not held:
            return []
        path, as_json = held[0][2]
        text = "".join(_get(e, path) for _, e, _ in held)
        restored = restore(text, self.found, as_json)
        if restored == text and len(held) == 1:
            return [held[0][0]]
        return [_sse(_set(e, path, restored if i == 0 else ""), raw) for i, (raw, e, _) in enumerate(held)]

    def _unfinished(self, text: str) -> bool:
        """Whether the text ends partway into a placeholder this call issued."""
        start = text.rfind("[")
        tail = text[start:]
        return start >= 0 and len(tail) < 64 and "]" not in tail and any(p.startswith(tail) for p in self.found)


def _growing(event: dict) -> tuple | None:
    """(which text, path to its string in the event, holds JSON) for an event that adds to a
    text or a tool call's arguments, in Anthropic, Chat Completions or Responses form."""
    kind = event.get("type")
    if kind == "content_block_delta":
        delta = event.get("delta") or {}
        for name in ("text", "partial_json"):
            if isinstance(delta.get(name), str):
                return ("block", event.get("index")), ("delta", name), name == "partial_json"
    texts = ("response.output_text.delta", "response.function_call_arguments.delta")
    if kind in texts and isinstance(event.get("delta"), str):
        where = ("output", event.get("output_index"), event.get("content_index"))
        return where, ("delta",), kind.endswith("arguments.delta")
    choices = event.get("choices")
    if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
        c, delta = choices[0].get("index", 0), choices[0].get("delta") or {}
        calls = delta.get("tool_calls")
        if isinstance(delta.get("content"), str) and not calls:
            return ("choice", c), ("choices", 0, "delta", "content"), False
        if isinstance(calls, list) and len(calls) == 1 and isinstance(calls[0], dict) and not delta.get("content"):
            args = (calls[0].get("function") or {}).get("arguments")
            if isinstance(args, str):
                path = ("choices", 0, "delta", "tool_calls", 0, "function", "arguments")
                return ("call", c, calls[0].get("index", 0)), path, True
    return None


def _get(event, path: tuple) -> str:
    for step in path:
        event = event[step]
    return event


def _set(event, path: tuple, text: str):
    copy = json.loads(json.dumps(event))
    target = copy
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = text
    return copy


def _sse(event: dict, raw: bytes) -> bytes:
    """The event re-encoded under the name the vendor gave it."""
    name = next((line[6:].strip() for line in raw.splitlines() if line.startswith(b"event:")), b"")
    head = b"event: " + name + b"\n" if name else b""
    return head + b"data: " + json.dumps(event).encode() + b"\n\n"
