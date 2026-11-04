"""Run forensics: why a task cost what it did.

Each priced call leaves a fingerprint of its prompt: a short hash and the byte size of the
system prompt, of the tool definitions per MCP server, and of the message history, plus
where the cache markers sit. Never the text. When a call reads far less from the prompt
cache than the call before it in the same lane had cached, the first difference between
the two fingerprints is the reason. A lane is one task, one Claude Code subagent and one
request class: each has its own prompt, so each has its own cache.
"""

import hashlib
import json
from collections.abc import Callable, Iterator
from datetime import datetime

from .pricing import Price
from .store import Ledger, dicts

BREAK_SHARE = 0.5  # a call reading under half of what the call before it cached broke the cache
MIN_CACHEABLE = 1024  # tokens: shorter prompts are never cached
LIFETIME, EXTENDED = 300, 3600  # seconds an unused cached prefix lives
SYSTEM_ROLES = ("system", "developer")


def canonical(value: object) -> bytes:
    return json.dumps(
        _unmarked(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()[:16]


def _unmarked(value: object) -> object:
    """Without cache markers: they move every turn and do not change the prompt."""
    if isinstance(value, dict):
        return {k: _unmarked(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_unmarked(v) for v in value]
    return value


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _sized(value: object) -> dict:
    raw = canonical(value)
    return {"hash": hashlib.sha256(raw).hexdigest()[:16], "bytes": len(raw)}


def prompt_parts(request: dict) -> tuple[list, list, list]:
    """(system, tools, history) of an Anthropic Messages, OpenAI Chat or Responses body."""
    if "messages" in request:
        items = _list(request["messages"])
        system = request.get("system")
    else:
        items = request.get("input")
        items = [{"role": "user", "content": items}] if isinstance(items, str) else _list(items)
        system = request.get("instructions")
    system = system if isinstance(system, list) else [system] if system else []
    is_system = [isinstance(m, dict) and m.get("role") in SYSTEM_ROLES for m in items]
    system += [m for m, s in zip(items, is_system, strict=True) if s]
    history = [m for m, s in zip(items, is_system, strict=True) if not s]
    return system, _list(request.get("tools")), history


def server_of(tool: object) -> str:
    """The MCP server a tool definition came from, or "builtin"."""
    if not isinstance(tool, dict):
        return "builtin"
    if tool.get("type") == "mcp":  # OpenAI Responses names the server itself
        return str(tool.get("server_label") or "mcp")
    name = str(tool.get("name") or (tool.get("function") or {}).get("name") or "")
    if name.startswith("mcp__") and name.count("__") >= 2:
        return name.split("__")[1]
    return "builtin"


def marks(value: object) -> Iterator[dict]:
    """Every cache_control in a part of the prompt."""
    if isinstance(value, dict):
        if isinstance(value.get("cache_control"), dict):
            yield value["cache_control"]
        for v in value.values():
            yield from marks(v)
    elif isinstance(value, list):
        for v in value:
            yield from marks(v)


def fingerprint(request: dict) -> dict:
    """Hashes and byte sizes of the prompt's parts, and the blocks carrying cache markers."""
    system, tools, history = prompt_parts(request)
    groups: dict[str, list] = {}
    for tool in tools:
        groups.setdefault(server_of(tool), []).append(tool)
    markers = ["top"] if isinstance(request.get("cache_control"), dict) else []
    for name, part in (("tools", tools), ("system", system), ("history", history)):
        markers += [f"{name}[{i}]" for i, block in enumerate(part) if any(marks(block))]
    ttls = [m.get("ttl") for m in marks(request)]
    return {
        "system": _sized(system),
        "tools": {
            server: {**_sized(defs), "count": len(defs), "set": digest(sorted(map(digest, defs)))}
            for server, defs in groups.items()
        },
        "history": {**_sized(history), "count": len(history)},
        "markers": markers,
        "ttl": "1h" if "1h" in ttls else "5m",
    }


def tool_calls(request: dict) -> list[tuple[str, str, str]]:
    """(call id, hash of name and arguments, hash of its result) for each tool call of the
    model's last turn. The result is "" when the request does not carry it yet.

    Only the last turn: every request carries the whole history, so older calls would be
    counted again on every turn. The result tells a stuck loop (same call, same answer)
    from work that repeats a call while things change, such as running tests after edits."""
    found, results = [], {}
    if "messages" in request:
        messages = [m for m in _list(request["messages"]) if isinstance(m, dict)]
        at = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "assistant"), None)
        last = messages[at] if at is not None else {}
        for block in _list(last.get("content")):  # Anthropic
            if isinstance(block, dict) and block.get("type") == "tool_use":
                found.append((block.get("id"), block.get("name"), block.get("input")))
        for call in _list(last.get("tool_calls")):  # OpenAI Chat
            if isinstance(call, dict) and isinstance(call.get("function"), dict):
                f = call["function"]
                found.append((call.get("id"), f.get("name"), _arguments(f.get("arguments"))))
        for m in messages[at + 1:] if at is not None else []:
            if m.get("role") == "tool":  # OpenAI Chat
                results[m.get("tool_call_id")] = m.get("content")
            for block in _list(m.get("content")):  # Anthropic
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    results[block.get("tool_use_id")] = block.get("content")
    else:  # OpenAI Responses: the function calls after the last message
        for item in reversed(_list(request.get("input"))):
            kind = item.get("type") if isinstance(item, dict) else None
            if kind == "function_call_output":
                results[item.get("call_id")] = item.get("output")
                continue
            if kind != "function_call":
                break
            found.append((item.get("call_id"), item.get("name"), _arguments(item.get("arguments"))))
    return [
        (str(i or ""), digest([name, args]), digest(results[i]) if i in results else "")
        for i, name, args in found
    ]


def _arguments(text: object) -> object:
    try:
        return json.loads(text) if isinstance(text, str) else text
    except json.JSONDecodeError:
        return text


def _tools_change(old: dict, new: dict) -> str | None:
    for server in [*new, *(s for s in old if s not in new)]:
        a, b = old.get(server), new.get(server)
        if a is None:
            return f"tools added: {server} ({b['count']})"
        if b is None:
            return f"tools removed: {server} ({a['count']})"
        if a["count"] != b["count"]:
            change = "added" if b["count"] > a["count"] else "removed"
            return f"tools {change}: {server} ({abs(b['count'] - a['count'])})"
        if a["hash"] != b["hash"]:
            return f"tools {'reordered' if a['set'] == b['set'] else 'changed'}: {server}"
    if list(old) != list(new):
        return "tools reordered: servers moved"
    return None


def diagnose(
    prev: dict | None, fp: dict, request: dict, model: str, usage: dict, at: datetime,
    needs_marker: bool,
) -> tuple[str, int] | None:
    """(reason, tokens missed) when this call read far less from the cache than the
    previous call in its lane had cached. needs_marker: the vendor caches only marked
    prompts (Anthropic), rather than every long prefix (OpenAI)."""
    if prev is None or "input" not in usage:
        return None
    read, old = usage.get("cache_read", 0), json.loads(prev["fingerprint"])
    expected = prev["cache_read"] + prev["cache_write"]
    unmarked = needs_marker and not fp["markers"]
    if unmarked:
        expected = max(expected, prev["input"])
    missed = min(expected, usage["input"]) - read
    if expected < MIN_CACHEABLE or read >= expected * BREAK_SHARE or missed <= 0:
        return None
    _, _, history = prompt_parts(request)
    idle = (at - datetime.fromisoformat(prev["at"])).total_seconds()
    lifetime = EXTENDED if old.get("ttl") == "1h" else LIFETIME
    if unmarked:
        reason = "no cache marker"
    elif prev["model"] != model:
        reason = f"model switched: {prev['model']} to {model}"
    elif tools := _tools_change(old["tools"], fp["tools"]):
        reason = tools
    elif old["system"]["hash"] != fp["system"]["hash"]:
        reason = "system prompt changed"
    elif _sized(history[: old["history"]["count"]])["hash"] != old["history"]["hash"]:
        reason = "earlier history changed"
    elif idle > lifetime:
        reason = f"idle {idle / 60:.0f} minutes, past the {lifetime // 60}-minute cache lifetime"
    else:
        reason = "prefix unchanged: the vendor missed the cache"
    return reason, missed


def as_if_read(usage: dict, missed: int) -> dict:
    """The usage had `missed` more prompt tokens been read from the cache: they come out of
    the cache writes first (unknown lifetime, then short, then extended), then uncached input."""
    out = dict(usage)
    total = usage.get("cache_write_total", 0)
    short, extended = usage.get("cache_write", 0), usage.get("cache_write_extended", 0)
    take = min(missed, total)
    unsplit = max(0, total - short - extended) - take
    short -= max(0, -unsplit)
    extended -= max(0, -short)
    out.update(
        cache_read=usage.get("cache_read", 0) + missed,
        cache_write_total=total - take,
        cache_write=max(0, short),
        cache_write_extended=max(0, extended),
    )
    return out


def split(price: Price, usage: dict, fp: dict, total: int) -> dict[str, int]:
    """total (micro-dollars) spread over the tool definitions, system prompt, history and
    output. Prompt tokens are shared out by bytes, in prompt order, and priced as the vendor
    read them: the start from the cache, then cache writes, then uncached input."""
    tier = price.long if price.long and usage.get("input", 0) > price.long_above else price
    output = min(total, usage.get("output", 0) * tier.output_per_million // 1_000_000)
    sizes = [sum(g["bytes"] for g in fp["tools"].values()), fp["system"]["bytes"],
             fp["history"]["bytes"]]
    tokens, read = usage.get("input", 0), usage.get("cache_read", 0)
    written = usage.get("cache_write_total", 0)
    budget = [[read, tier.cache_read], [written, tier.cache_write],
              [max(0, tokens - read - written), tier.input_per_million]]
    weights = []
    for size in sizes:
        left = tokens * size // max(1, sum(sizes))
        weight = 0
        for tier_left in budget:
            take = min(left, tier_left[0])
            tier_left[0] -= take
            left -= take
            weight += take * tier_left[1]
        weights.append(weight + left * tier.input_per_million)
    if not any(weights):  # no usage reported: by bytes alone
        weights = sizes
    rest, whole = total - output, max(1, sum(weights))
    tools, system = rest * weights[0] // whole, rest * weights[1] // whole
    return {"tools": tools, "system": system, "history": rest - tools - system, "output": output}


def anatomy(
    request: dict, model: str, usage: dict, at: datetime, prev: dict | None,
    needs_marker: bool, price: Price, list_cost: int, cost_of: Callable[[dict], int],
) -> dict:
    """The forensic columns of one priced call. cost_of prices a usage at list price."""
    fp = fingerprint(request)
    if usage.get("cache_write_extended"):
        fp["ttl"] = "1h"
    found = diagnose(prev, fp, request, model, usage, at, needs_marker)
    parts = split(price, usage, fp, list_cost)
    return {
        "fingerprint": json.dumps(fp, separators=(",", ":")),
        "cache_break": found and found[0],
        "break_cost": max(0, list_cost - cost_of(as_if_read(usage, found[1]))) if found else 0,
        **{f"{part}_cost": amount for part, amount in parts.items()},
    }


def _group(rows: list[dict], key: str, empty: str) -> list[dict]:
    out: dict[str, dict] = {}
    for r in rows:
        g = out.setdefault(r[key] or empty, {key: r[key] or empty, "calls": 0,
                                             "charged_micros": 0, "list_micros": 0})
        g["calls"] += 1
        g["charged_micros"] += r["cost"]
        g["list_micros"] += r["list_cost"]
    return sorted(out.values(), key=lambda g: -g["list_micros"])


def why(ledger: Ledger, task: str) -> dict:
    """Where one task's money went. Amounts are micro-dollars; the parts are at list price,
    so a task on a subscription still shows what its calls were worth."""
    rows = dicts(ledger.db.execute(
        "select * from calls where task = ? order by at, rowid", (task,)
    ))
    by_server: dict[str, int] = {}
    for r in rows:
        groups = json.loads(r["fingerprint"])["tools"]
        size, left = sum(g["bytes"] for g in groups.values()), r["tools_cost"]
        for n, (server, g) in enumerate(groups.items()):
            share = left if n == len(groups) - 1 else r["tools_cost"] * g["bytes"] // max(1, size)
            by_server[server] = by_server.get(server, 0) + share
            left -= share
    return {
        "task": task,
        "calls": len(rows),
        "subscription_calls": sum(r["route"] == "subscription" for r in rows),
        "charged_micros": sum(r["cost"] for r in rows),
        "list_micros": sum(r["list_cost"] for r in rows),
        "cache_miss_micros": sum(r["break_cost"] for r in rows),
        "parts": {f"{p}_micros": sum(r[f"{p}_cost"] for r in rows)
                  for p in ("tools", "system", "history", "output")},
        "tools_by_server": [{"server": s, "micros": m}
                            for s, m in sorted(by_server.items(), key=lambda kv: -kv[1])],
        "by_subagent": _group(rows, "subagent", "main"),
        "by_prompt": _group(rows, "prompt_id", "(none)"),
        "breaks": [
            {"turn": n, "at": r["at"], "subagent": r["subagent"] or "main",
             "model": r["model"], "reason": r["cache_break"], "micros": r["break_cost"]}
            for n, r in enumerate(rows, 1) if r["cache_break"]
        ],
    }


def top_tasks(ledger: Ledger, since: datetime, limit: int = 10, until: datetime | None = None) -> list[dict]:
    """The tasks worth most at list price since a time (and before another), for the dashboard."""
    return dicts(ledger.db.execute(
        "select task, count(*) as calls, sum(cost) as charged, sum(list_cost) as list"
        " from calls where task is not null and at >= ? and (? is null or at < ?) group by task"
        " order by sum(list_cost) desc limit ?",
        (since.isoformat(), *[until and until.isoformat()] * 2, limit),
    ))
