"""Tool calls in a streamed reply.

A stream is relayed as it arrives until a tool call starts. From there the gateway keeps
the rest back, rebuilds the reply's tool calls from it, and checks them against the rules
as it would a reply that was not streamed. Held calls are taken out of the kept events and
a note is put in their place; blocks are renumbered so the agent's SDK still reads the
stream. Text before the first tool call reaches the agent without waiting.

Anthropic Messages, OpenAI Chat Completions and OpenAI Responses streams are understood.
Each event is one server-sent message: its raw bytes and its parsed data (or None).
"""

import json

from .rules import TOOL_ITEMS, without_tool_calls

Event = tuple[bytes, dict | None]


def data_of(message: bytes) -> dict | None:
    """The JSON of an event's data lines, or None ("[DONE]", comments, keep-alives)."""
    text = b"\n".join(
        line[5:].lstrip(b" ") for line in message.splitlines() if line.startswith(b"data:")
    )
    try:
        event = json.loads(text) if text else None
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def starts_tool(event: dict | None) -> bool:
    if not event:
        return False
    kind = event.get("type")
    if kind == "content_block_start":
        return (event.get("content_block") or {}).get("type") == "tool_use"
    if kind == "response.output_item.added":
        return (event.get("item") or {}).get("type") in TOOL_ITEMS
    return any(
        isinstance(c, dict) and (c.get("delta") or {}).get("tool_calls")
        for c in _list(event.get("choices"))
    )


def reply_of(events: list[Event]) -> tuple[dict, dict]:
    """The kept events as a reply rules.held reads, and where each part came from."""
    parsed = [e for _, e in events if e]
    if any(str(e.get("type", "")).startswith("response.") for e in parsed):
        return _responses_reply(parsed)
    if any(e.get("type") == "content_block_start" for e in parsed):
        return _anthropic_reply(parsed)
    return _chat_reply(parsed)


def without(events: list[Event], held: list[tuple], origin: dict, note: str) -> list[bytes]:
    """The kept events with the held tool calls taken out and the note in their place."""
    gone = {origin[where] for where, *_ in held}
    if origin.get("format") == "anthropic":
        return _anthropic_without(events, gone, note)
    if origin.get("format") == "responses":
        return _responses_without(events, gone, held, note)
    return _chat_without(events, gone, note)


# Anthropic: content_block_start, content_block_delta, content_block_stop by index


def _anthropic_reply(parsed: list[dict]) -> tuple[dict, dict]:
    blocks, partial = {}, {}
    for e in parsed:
        index = e.get("index", 0)
        if e.get("type") == "content_block_start":
            blocks[index] = dict(e.get("content_block") or {})
        elif e.get("type") == "content_block_delta" and index in blocks:
            delta = e.get("delta") or {}
            if delta.get("type") == "input_json_delta":
                partial[index] = partial.get(index, "") + str(delta.get("partial_json", ""))
    for index, text in partial.items():
        try:
            blocks[index]["input"] = json.loads(text) if text else {}
        except ValueError:
            blocks[index]["input"] = text  # a cut-off stream: still searched as text
    order = sorted(blocks, key=lambda i: (i is None, i))
    origin = {("content", n): index for n, index in enumerate(order)} | {"format": "anthropic"}
    return {"content": [blocks[i] for i in order]}, origin


def _anthropic_without(events: list[Event], gone: set, note: str) -> list[bytes]:
    starts = [e.get("index", 0) for _, e in events if e and e.get("type") == "content_block_start"]
    renumber, nxt = {}, min(starts, default=0)
    for index in starts:
        if index not in gone:
            renumber[index], nxt = nxt, nxt + 1
    tools_kept = any(
        e and e.get("type") == "content_block_start" and e.get("index", 0) not in gone
        and (e.get("content_block") or {}).get("type") == "tool_use"
        for _, e in events
    )
    note_events = [
        {"type": "content_block_start", "index": nxt, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": nxt, "delta": {"type": "text_delta", "text": note}},
        {"type": "content_block_stop", "index": nxt},
    ]
    out, noted = [], False
    for raw, e in events:
        kind = e.get("type") if e else None
        if kind in ("content_block_start", "content_block_delta", "content_block_stop"):
            if e.get("index", 0) in gone:
                continue
            out.append(_sse({**e, "index": renumber.get(e.get("index", 0), e.get("index", 0))}, kind))
            continue
        if kind in ("message_delta", "message_stop") and not noted:
            out += [_sse(n, n["type"]) for n in note_events]
            noted = True
        if kind == "message_delta" and not tools_kept:
            e = {**e, "delta": {**(e.get("delta") or {}), "stop_reason": "end_turn"}}
            out.append(_sse(e, kind))
            continue
        out.append(raw)
    if not noted:
        out += [_sse(n, n["type"]) for n in note_events]
    return out


# OpenAI Chat Completions: choices[].delta.tool_calls[] by index


def _chat_reply(parsed: list[dict]) -> tuple[dict, dict]:
    calls: dict[tuple, dict] = {}
    for e in parsed:
        for choice in _list(e.get("choices")):
            if not isinstance(choice, dict):
                continue
            for call in _list((choice.get("delta") or {}).get("tool_calls")):
                if not isinstance(call, dict):
                    continue
                key = (choice.get("index", 0), call.get("index", 0))
                fn = call.get("function") or {}
                got = calls.setdefault(key, {"name": "", "arguments": ""})
                got["name"] += str(fn.get("name") or "")
                got["arguments"] += str(fn.get("arguments") or "")
    by_choice: dict[int, list[int]] = {}
    for c, j in sorted(calls):
        by_choice.setdefault(c, []).append(j)
    ordered = sorted(by_choice.items())
    reply = {"choices": [{"message": {"tool_calls": [{"function": calls[c, j]} for j in js]}}
                         for c, js in ordered]}
    origin = {("choices", n, m): (c, j) for n, (c, js) in enumerate(ordered) for m, j in enumerate(js)}
    return reply, origin | {"format": "chat"}


def _chat_without(events: list[Event], gone: set, note: str) -> list[bytes]:
    kept: dict[int, list[int]] = {}
    noted = {c for c, _ in gone}
    for _, e in events:
        for choice in _list((e or {}).get("choices")):
            for call in _list((choice.get("delta") or {}).get("tool_calls")):
                c, j = choice.get("index", 0), call.get("index", 0)
                if (c, j) not in gone and j not in kept.setdefault(c, []):
                    kept[c].append(j)
    out = []
    for raw, e in events:
        if not e or not _list(e.get("choices")):
            out.append(raw)
            continue
        choices = []
        for choice in e["choices"]:
            c, delta = choice.get("index", 0), dict(choice.get("delta") or {})
            if "tool_calls" in delta:
                calls = [
                    {**call, "index": kept[c].index(call.get("index", 0))}
                    for call in _list(delta["tool_calls"])
                    if (c, call.get("index", 0)) not in gone
                ]
                delta["tool_calls"] = calls
                if not calls:
                    del delta["tool_calls"]
            choice = {**choice, "delta": delta}
            if choice.get("finish_reason") and c in noted:
                if not kept.get(c):
                    choice["finish_reason"] = "stop"
                delta["content"] = (delta.get("content") or "") + "\n\n" + note
            choices.append(choice)
        out.append(_sse({**e, "choices": choices}))
    return out


# OpenAI Responses: output items by output_index, and the whole reply in response.completed


def _responses_reply(parsed: list[dict]) -> tuple[dict, dict]:
    done = next((e["response"] for e in reversed(parsed)
                 if e.get("type") == "response.completed" and isinstance(e.get("response"), dict)), None)
    if done is not None:
        output = _list(done.get("output"))
        return {"output": output}, {("output", i): i for i in range(len(output))} | {"format": "responses"}
    items = {}
    for e in parsed:
        if e.get("type") in ("response.output_item.added", "response.output_item.done"):
            items[e.get("output_index")] = e.get("item")
        elif e.get("type") == "response.function_call_arguments.done":
            item = items.get(e.get("output_index"))
            if isinstance(item, dict):
                item["arguments"] = e.get("arguments")
    order = sorted(i for i in items if isinstance(i, int))
    origin = {("output", n): i for n, i in enumerate(order)} | {"format": "responses"}
    return {"output": [items[i] for i in order]}, origin


def _responses_without(events: list[Event], gone: set, held: list[tuple], note: str) -> list[bytes]:
    indexes = sorted({e["output_index"] for _, e in events if e and isinstance(e.get("output_index"), int)})
    renumber, nxt = {}, min(indexes, default=0)
    for i in indexes:
        if i not in gone:
            renumber[i], nxt = nxt, nxt + 1
    message = {"type": "message", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": note, "annotations": []}]}
    out, noted = [], False
    for raw, e in events:
        if e and isinstance(e.get("output_index"), int):
            if e["output_index"] in gone:
                continue
            out.append(_sse({**e, "output_index": renumber[e["output_index"]]}, e.get("type")))
            continue
        if e and e.get("type") in ("response.completed", "response.incomplete") and not noted:
            for kind in ("response.output_item.added", "response.output_item.done"):
                out.append(_sse({"type": kind, "output_index": nxt, "item": message}, kind))
            noted = True
            if isinstance(e.get("response"), dict):
                e = {**e, "response": without_tool_calls(e["response"], held, note)}
                out.append(_sse(e, e["type"]))
                continue
        out.append(raw)
    return out


def _sse(event: dict, name: str | None = None) -> bytes:
    head = f"event: {name}\n" if name else ""
    return f"{head}data: {json.dumps(event)}\n\n".encode()


def _list(value) -> list:
    return value if isinstance(value, list) else []
