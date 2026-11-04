"""A read-only MCP server over stdio, so an agent can see its own budget.

One JSON-RPC 2.0 message per line on stdin, one reply per line on stdout. It speaks the
initialize handshake (protocol 2025-11-25 and earlier). A client that first probes with
server/discover gets "method not found" and falls back to initialize, as the 2026-07-28
revision tells it to. No tool approves, changes a cap or spends.
"""

import json
import sqlite3
from datetime import UTC, datetime

from . import forensics, status
from .money import usd_display
from .policy import Cap, window_start
from .store import Ledger

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
INTERNAL_ERROR = -32603

TEXT = {"type": "string"}
TOOLS = [
    {
        "name": "budget_left",
        "title": "Budget left",
        "description": "Each spending cap that applies to an agent's calls, what is used and "
        "what is left (USD). Calls over approve_above wait for a person. Pass task and "
        "customer to include caps on those.",
        "inputSchema": {
            "type": "object",
            "properties": {"agent": TEXT, "task": TEXT, "customer": TEXT},
            "required": ["agent"],
            "additionalProperties": False,
        },
    },
    {
        "name": "spend",
        "title": "Spend",
        "description": "What was spent today or this month (UTC), in USD, per agent, task or "
        "customer.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "by": {"type": "string", "enum": ["agent", "task", "customer"], "default": "agent"},
                "period": {"type": "string", "enum": ["today", "month"], "default": "month"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "approval_status",
        "title": "Approval status",
        "description": "Whether a call sent for approval is pending, approved or denied. The id "
        "is the apr_... the gateway returned.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": TEXT},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "why",
        "title": "Why a task cost that",
        "description": "Where one task's money went (USD): tool definitions per MCP server, "
        "system prompt, history and output, cost per subagent and per prompt, and each "
        "prompt-cache break with its turn, cause and cost.",
        "inputSchema": {
            "type": "object",
            "properties": {"task": TEXT},
            "required": ["task"],
            "additionalProperties": False,
        },
    },
]
for _tool in TOOLS:
    _tool["annotations"] = {"readOnlyHint": True, "openWorldHint": False}


class ToolError(Exception):
    """A tool ran but could not answer; the agent sees the message and may retry."""


def text(name: str, args: dict) -> str | None:
    value = args.get(name)
    if value is not None and not isinstance(value, str):
        raise ToolError(f"{name} must be a string")
    return value


def budget_left(ledger: Ledger, caps: list[Cap], args: dict) -> dict:
    agent = text("agent", args)
    if not agent:
        raise ToolError("agent is required")
    ids = {"agent": agent, "task": text("task", args), "customer": text("customer", args)}
    rows = status.budget_left(ledger, caps, ids, datetime.now(UTC))
    return {
        "agent": agent,
        "paused": ledger.paused(agent),
        "caps": [
            {
                "cap": r["cap"],
                "window": r["window"],
                "limit_usd": usd_display(r["limit"]),
                "used_usd": usd_display(r["used"]),
                "left_usd": usd_display(r["left"]),
                "approve_above_usd": None if r["approve_above"] is None
                else usd_display(r["approve_above"]),
            }
            for r in rows
        ],
    }


def spend(ledger: Ledger, caps: list[Cap], args: dict) -> dict:
    by, period = text("by", args) or "agent", text("period", args) or "month"
    if by not in ("agent", "task", "customer"):
        raise ToolError("by must be agent, task or customer")
    if period not in ("today", "month"):
        raise ToolError("period must be today or month")
    since = window_start("day" if period == "today" else "month", datetime.now(UTC))
    totals = ledger.spend_by(by, since)
    return {
        "by": by,
        "period": period,
        "since": since.isoformat(),
        "total_usd": usd_display(sum(totals.values())),
        "rows": [{by: k, "usd": usd_display(v)} for k, v in sorted(totals.items())],
    }


def approval_status(ledger: Ledger, caps: list[Cap], args: dict) -> dict:
    approval_id = text("id", args)
    row = ledger.approval(approval_id) if approval_id else None
    if row is None:
        raise ToolError(f"no approval {approval_id}")
    return {
        "id": row["id"],
        "status": row["status"],
        "agent": row["agent"],
        "estimate_usd": usd_display(row["estimate"]),
        "reason": row["reason"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
        "decided_by": row["decided_by"],
    }


def why(ledger: Ledger, caps: list[Cap], args: dict) -> dict:
    task = text("task", args)
    if not task:
        raise ToolError("task is required")
    return in_usd(forensics.why(ledger, task))


def in_usd(value: object) -> object:
    """Every *_micros amount as *_usd, and a bare micros as usd."""
    if isinstance(value, list):
        return [in_usd(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for k, v in value.items():
        if k == "micros" or k.endswith("_micros"):
            out[k.removesuffix("micros") + "usd"] = usd_display(v)
        else:
            out[k] = in_usd(v)
    return out


HANDLERS = {
    "budget_left": budget_left, "spend": spend, "approval_status": approval_status, "why": why,
}


def error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(ledger: Ledger, caps: list[Cap], message) -> dict | None:
    """The reply to one parsed message, or None for a notification."""
    if not isinstance(message, dict):
        return error(None, INVALID_REQUEST, "expected one JSON-RPC request object")
    msg_id = message.get("id")
    if not isinstance(msg_id, (str, int)) or isinstance(msg_id, bool):
        msg_id = None
    method, params = message.get("method"), message.get("params", {})
    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return error(msg_id, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
    if "id" not in message:
        return None  # notifications, such as notifications/initialized, need no reply
    if msg_id is None:
        return error(None, INVALID_REQUEST, "id must be a string or an integer")
    if not isinstance(params, dict):
        return error(msg_id, INVALID_PARAMS, "params must be an object")

    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "quarter-ledger", "title": "Quarter ledger", "version": "0.1.0"},
            "instructions": "Read-only. Check budget_left before a costly call, and "
            "approval_status for a call that is waiting for a person.",
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments", {})
        if name not in HANDLERS:
            return error(msg_id, INVALID_PARAMS, f"Unknown tool: {name}")
        if not isinstance(args, dict):
            return error(msg_id, INVALID_PARAMS, "arguments must be an object")
        try:
            data = HANDLERS[name](ledger, caps, args)
            result = {"content": [{"type": "text", "text": json.dumps(data)}],
                      "structuredContent": data, "isError": False}
        except ToolError as e:
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
    else:
        return error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def run(ledger: Ledger, caps: list[Cap], stdin, stdout) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            reply = handle(ledger, caps, json.loads(line))
        except json.JSONDecodeError:
            reply = error(None, PARSE_ERROR, "Parse error")
        except sqlite3.Error as e:  # a locked or broken ledger fails this request, not the session
            reply = error(None, INTERNAL_ERROR, f"Internal error: {e}")
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()
