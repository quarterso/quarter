"""Egress rules: what each agent may do on the APIs it reaches through the gateway.

QUARTER_RULES is a JSON list. Each item is one of:
  {"agent", "upstream", "method", "path", "graphql_operation", "action"}  a call rule
  {"upstream", "default": "deny"}  calls no rule matches on that upstream are denied
  {"agent", "tool", "command", "action"}  a tool call in a model's reply
  {"agent", "customer", "upstream", "to"}  a route: such calls go to upstream `to` instead
  {"agent", "customer", "upstream", "redact", "patterns"}  values masked before a model sees them
The first call rule that matches decides; an upstream with no default allows the rest.
A route is how a rule on where data may go (an end customer's data stays in the EU, say) is
met without refusing the call: the first route that matches sends it to another upstream,
such as the vendor's regional endpoint. Rules and prices still go by the upstream named.
A redaction is the same idea for what data may go: the values it names (card numbers, say)
are masked in model calls and put back in the reply (redact.py). Every redaction that matches
applies, and `upstream` is a glob that defaults to *.
Globs and the command pattern (a regular expression) match without regard to case.

A GraphQL call is checked once per root field, since each is an action the server runs,
and takes the strictest answer. The operation name is a label the client picks, so it is
used only when no query is sent (a persisted query).

The Quarter feed adds HTTP rules (feed/FORMAT.md, version 2). They match where a call really
goes (host, method, decoded path, query, body, GraphQL root fields), only ask a person, and
apply only to calls no rule of the customer's matched.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Literal
from urllib.parse import parse_qs

from .redact import KINDS, Redactor, redactor

Action = Literal["allow", "approve", "deny"]
STRICTNESS = ("allow", "approve", "deny")
CALL_KEYS = {"agent", "upstream", "method", "path", "graphql_operation", "action"}
TOOL_KEYS = {"agent", "tool", "command", "action", "why"}
ROUTE_KEYS = {"agent", "customer", "upstream", "to"}
REDACT_KEYS = {"agent", "customer", "upstream", "redact", "patterns"}
HTTP_KEYS = {"id", "service", "host", "method", "path", "query", "body", "graphql", "action", "why", "source"}
LABEL = re.compile(r"[a-z][a-z0-9_]*")
# GraphQL tokens: strings and comments are matched so their contents are never read as names.
TOKEN = re.compile(r'"""(?:[^"\\]|\\.|"(?!""))*"""|"(?:[^"\\\n]|\\.)*"|#[^\n]*|\.\.\.|[_A-Za-z]\w*|[{}():@]')
TOOL_ITEMS = ("function_call", "custom_tool_call", "local_shell_call", "shell_call")  # OpenAI Responses


def _glob(value: str, pattern: str) -> bool:
    return fnmatchcase(value.lower(), pattern.lower())


@dataclass(frozen=True)
class CallRule:
    upstream: str
    action: Action
    agent: str = "*"
    methods: tuple[str, ...] = ("*",)
    path: str = "*"
    graphql_operation: str | None = None
    number: int = 0  # its place in the file, from 1

    def applies(self, agent: str, upstream: str, method: str, path: str) -> bool:
        """Everything but the GraphQL operation matches."""
        return (
            upstream == self.upstream
            and _glob(agent, self.agent)
            and ("*" in self.methods or method in self.methods)
            and _glob(path, self.path)
        )

    def matches(self, agent: str, upstream: str, method: str, path: str, operation: str | None) -> bool:
        return self.applies(agent, upstream, method, path) and (
            self.graphql_operation is None
            or (operation is not None and _glob(operation, self.graphql_operation))
        )

    def __str__(self) -> str:
        what = f"{self.upstream} {','.join(self.methods)} {self.path}"
        what += f" graphql {self.graphql_operation}" if self.graphql_operation else ""
        who = "" if self.agent == "*" else f" for agent {self.agent}"
        return f"rule {self.number}: {self.action} {what}{who}"


@dataclass(frozen=True)
class ToolRule:
    tool: str
    action: Action  # approve or deny
    agent: str = "*"
    command: str | None = None  # a regular expression searched in the call's input
    number: int = 0
    origin: str = "rule"  # "feed rule" for one from the Quarter feed (feed.py)
    why: str | None = None  # what the rule catches, in words, shown instead of the pattern

    def matches(self, agent: str, name: str, text: str) -> bool:
        return (
            _glob(agent, self.agent)
            and _glob(name, self.tool)
            and (self.command is None or re.search(self.command, text, re.IGNORECASE) is not None)
        )

    def __str__(self) -> str:
        if self.why:
            return f"{self.origin} {self.number}: {self.action}, {self.why}"
        what = f"tool {self.tool}" + (f" command {self.command}" if self.command else "")
        who = "" if self.agent == "*" else f" for agent {self.agent}"
        return f"{self.origin} {self.number}: {self.action} {what}{who}"


@dataclass(frozen=True)
class HttpRule:
    """A feed rule on a call to any API, by the host it really goes to. It only asks."""
    id: str
    why: str
    hosts: tuple[str, ...]
    methods: tuple[str, ...]
    path: str  # regular expressions, searched ignoring case
    query: str | None = None
    body: str | None = None
    graphql: tuple[str, ...] | None = None  # globs on root field names

    def applies(self, method: str, host: str, path: str) -> bool:
        """Everything but the query, the body and the GraphQL fields matches."""
        return (
            any(_glob(host, h) for h in self.hosts)
            and ("*" in self.methods or method in self.methods)
            and re.search(self.path, path, re.IGNORECASE) is not None
        )

    def matches(self, method: str, host: str, path: str, query: str, body: str,
                operations: list[str] | None) -> bool:
        return (
            self.applies(method, host, path)
            and (self.query is None or re.search(self.query, query, re.IGNORECASE) is not None)
            and (self.body is None or re.search(self.body, body, re.IGNORECASE) is not None)
            and (self.graphql is None or any(_glob(op, g) for op in operations or [] for g in self.graphql))
        )

    def __str__(self) -> str:
        return f"feed rule {self.id}: approve, {self.why}"


@dataclass(frozen=True)
class Route:
    upstream: str
    to: str
    agent: str = "*"
    customer: str = "*"
    number: int = 0

    def matches(self, agent: str, customer: str | None, upstream: str) -> bool:
        return upstream == self.upstream and _glob(agent, self.agent) and _glob(customer or "", self.customer)

    def __str__(self) -> str:
        who = "".join(f" for {k} {v}" for k, v in (("agent", self.agent), ("customer", self.customer)) if v != "*")
        return f"rule {self.number}: route {self.upstream} to {self.to}{who}"


@dataclass(frozen=True)
class Redaction:
    kinds: tuple[str, ...]
    patterns: tuple[tuple[str, str], ...] = ()  # (label, regular expression)
    upstream: str = "*"
    agent: str = "*"
    customer: str = "*"
    number: int = 0

    def matches(self, agent: str, customer: str | None, upstream: str) -> bool:
        return _glob(upstream, self.upstream) and _glob(agent, self.agent) and _glob(customer or "", self.customer)

    def __str__(self) -> str:
        what = ", ".join([*self.kinds, *(label for label, _ in self.patterns)])
        who = "".join(f" for {k} {v}" for k, v in (("agent", self.agent), ("customer", self.customer)) if v != "*")
        return f"rule {self.number}: redact {what} on {self.upstream}{who}"


@dataclass
class Rules:
    calls: list[CallRule] = field(default_factory=list)
    defaults: dict[str, str] = field(default_factory=dict)  # upstream: "allow" or "deny"
    tools: list[ToolRule] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    redactions: list[Redaction] = field(default_factory=list)
    feed_calls: list[HttpRule] = field(default_factory=list)  # from the feed, after all of the above
    feed_payees: list = field(default_factory=list)  # feed.Payee: payees the feed lists (version 4)

    def redactor(self, agent: str, customer: str | None, upstream: str, key: bytes) -> Redactor | None:
        """What to mask in this call, from every redaction that matches, or None."""
        found = [r for r in self.redactions if r.matches(agent, customer, upstream)]
        if not found:
            return None
        return redactor([k for r in found for k in r.kinds], dict(p for r in found for p in r.patterns), key)

    def route(self, agent: str, customer: str | None, upstream: str) -> Route | None:
        return next((r for r in self.routes if r.matches(agent, customer, upstream)), None)

    def upstreams(self) -> set[str]:
        return {r.upstream for r in self.calls} | self.defaults.keys()

    def reads_graphql(self, agent: str, upstream: str, method: str, path: str) -> bool:
        """Whether a rule on GraphQL operations could apply to this call."""
        return any(r.graphql_operation and r.applies(agent, upstream, method, path) for r in self.calls)

    def decide(self, agent: str, upstream: str, method: str, path: str,
               operations: list[str] | None) -> tuple[Action, str | None]:
        """The action for a call and the rule that set it (None: allowed, as no rule matched).
        The strictest over its GraphQL root fields; a body that could not be read ("") waits
        for a person at least."""
        answers: list[tuple[Action, str | None]] = []
        if "" in (operations or []):
            answers.append(("approve", f"the GraphQL call to {upstream} could not be read"))
        for op in operations or [None]:
            rule = next((r for r in self.calls if r.matches(agent, upstream, method, path, op)), None)
            if rule:
                answers.append((rule.action, str(rule)))
            elif self.defaults.get(upstream) == "deny":
                answers.append(("deny", f"no rule matched; {upstream} defaults to deny"))
            else:
                answers.append(("allow", None))
        return max(answers, key=lambda a: STRICTNESS.index(a[0]))

    def feed_reads_graphql(self, method: str, host: str, path: str) -> bool:
        return any(r.graphql and r.applies(method, host, path) for r in self.feed_calls)

    def feed_hold(self, method: str, host: str, path: str, query: str, body: str,
                  operations: list[str] | None) -> str | None:
        """Why a feed rule asks about a call, or None. A GraphQL call it cannot read asks."""
        if "" in (operations or []) and self.feed_reads_graphql(method, host, path):
            return f"the GraphQL call to {host} could not be read"
        rule = next((r for r in self.feed_calls if r.matches(method, host, path, query, body, operations)), None)
        return str(rule) if rule else None

    def holds_tools(self, agent: str) -> bool:
        """Whether any tool rule covers this agent's replies."""
        return any(_glob(agent, r.agent) for r in self.tools)

    def held(self, agent: str, reply: dict) -> list[tuple[tuple, str, str, ToolRule]]:
        """(where, tool name, input text, rule) for each tool call in a reply a rule holds."""
        out = []
        for where, name, value in tool_calls(reply):
            text = _text(value)
            rule = next((r for r in self.tools if r.matches(agent, name, text)), None)
            if rule:
                out.append((where, name, text, rule))
        return out

    def listed(self) -> list[str]:
        """Each rule as a line, in file order, for status and the audit pack."""
        lines = [(getattr(r, "origin", "rule") != "rule", r.number, str(r))
                 for r in [*self.calls, *self.tools, *self.routes, *self.redactions]]
        defaults = [f"{u} defaults to {d}" for u, d in sorted(self.defaults.items())]
        feed = [str(r) for r in self.feed_calls]
        return defaults + [line for *_, line in sorted(lines)] + feed  # the customer's, then the feed's


def load_rules(raw: list) -> Rules:
    """Rules from the parsed file. Raises ValueError or TypeError naming the first bad item."""
    if not isinstance(raw, list):
        raise TypeError("the rules file must be a JSON list")
    rules = Rules()
    for n, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise TypeError(f"rule {n} is not an object")
        if "default" in item:
            if set(item) != {"upstream", "default"} or item["default"] not in ("allow", "deny"):
                raise ValueError(f'rule {n}: a default is {{"upstream", "default": "allow" or "deny"}}')
            rules.defaults[str(item["upstream"])] = item["default"]
            continue
        if "to" in item:
            unknown = set(item) - ROUTE_KEYS
            if unknown or not item.get("upstream") or not item.get("to"):
                raise ValueError(f'rule {n}: a route is {{"upstream", "to"}}, optionally "agent" and "customer"')
            rules.routes.append(Route(str(item["upstream"]), str(item["to"]), str(item.get("agent", "*")),
                                      str(item.get("customer", "*")), n))
            continue
        if "redact" in item:
            rules.redactions.append(_redaction(item, n))
            continue
        tool = "tool" in item
        unknown = set(item) - (TOOL_KEYS if tool else CALL_KEYS)
        if unknown:
            raise ValueError(f"rule {n}: unknown {', '.join(sorted(unknown))}")
        allowed = ("approve", "deny") if tool else STRICTNESS
        if item.get("action") not in allowed:
            raise ValueError(f"rule {n}: action must be {' or '.join(allowed)}")
        agent = str(item.get("agent", "*"))
        if tool:
            command = item.get("command")
            try:
                re.compile(command or "")
            except re.error as e:
                raise ValueError(f"rule {n}: bad command pattern: {e}") from None
            why = item.get("why")
            rules.tools.append(ToolRule(str(item["tool"]), item["action"], agent, command, n,
                                        why=str(why) if why else None))
            continue
        if not item.get("upstream"):
            raise ValueError(f"rule {n}: needs an upstream (or a tool)")
        methods = item.get("method", "*")
        methods = [methods] if isinstance(methods, str) else methods
        if not isinstance(methods, list) or not all(isinstance(m, str) for m in methods):
            raise ValueError(f'rule {n}: method is "*" or a list of methods')
        operation = item.get("graphql_operation")
        rules.calls.append(CallRule(
            str(item["upstream"]), item["action"], agent, tuple(m.upper() for m in methods),
            str(item.get("path", "*")), str(operation) if operation else None, n,
        ))
    return rules


def load_http_rules(raw: list) -> list[HttpRule]:
    """The feed's HTTP rules. A rule with a field this version does not know is skipped, since
    the field may narrow what it matches. Raises ValueError naming the first bad rule."""
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ValueError("http is a list of rules")
    rules = []
    for item in raw:
        name = item.get("id")
        if set(item) - HTTP_KEYS:
            continue
        if (not isinstance(name, str) or not name or not isinstance(item.get("path"), str)
                or item.get("action") != "approve" or not item.get("why")):
            raise ValueError(f"http rule {name}: needs an id, a path, a why, and action approve")
        hosts, methods, graphql = (item.get(k) for k in ("host", "method", "graphql"))
        hosts = [hosts] if isinstance(hosts, str) else hosts
        if not _strings(hosts) or not _strings(methods) or (graphql is not None and not _strings(graphql)):
            raise ValueError(f"http rule {name}: host, method and graphql are lists of strings")
        patterns = {k: item.get(k) for k in ("path", "query", "body")}
        for key, pattern in patterns.items():
            try:
                re.compile(pattern or "")
            except (re.error, TypeError) as e:
                raise ValueError(f"http rule {name}: bad {key} pattern: {e}") from None
        rules.append(HttpRule(name, str(item["why"]), tuple(hosts), tuple(m.upper() for m in methods),
                              patterns["path"], patterns["query"], patterns["body"],
                              tuple(graphql) if graphql is not None else None))
    return rules


def _strings(value) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) for v in value)


def _redaction(item: dict, n: int) -> Redaction:
    shape = f'rule {n}: a redaction is {{"redact": [{", ".join(KINDS)}]}}, optionally "patterns",' \
            ' "upstream", "agent" and "customer"'
    kinds, patterns = item["redact"], item.get("patterns", {})
    if set(item) - REDACT_KEYS or not isinstance(kinds, list) or not isinstance(patterns, dict):
        raise ValueError(shape)
    unknown = [k for k in kinds if k not in KINDS]
    if unknown or not (kinds or patterns):
        raise ValueError(f"rule {n}: unknown kind {unknown}" if unknown else shape)
    for label, pattern in patterns.items():
        if not LABEL.fullmatch(label) or not isinstance(pattern, str):
            raise ValueError(f'rule {n}: patterns are {{"name": "regular expression"}}, names in a-z, 0-9 and _')
        try:
            empty = re.compile(pattern).fullmatch("") is not None
        except re.error as e:
            raise ValueError(f"rule {n}: bad pattern {label}: {e}") from None
        if empty:
            raise ValueError(f"rule {n}: pattern {label} matches nothing at all")
    return Redaction(tuple(kinds), tuple(patterns.items()), str(item.get("upstream", "*")),
                     str(item.get("agent", "*")), str(item.get("customer", "*")), n)


# GraphQL


def graphql_operations(raw: bytes, query: str, content_type: str | None) -> list[str] | None:
    """The root fields a GraphQL call runs, from the body and the query string (servers read
    both), or None when it is not GraphQL. A body or query that cannot be read (multipart,
    say) gives [""]."""
    ctype = content_type or ""
    if "application/graphql" in ctype:
        return root_fields(raw.decode(errors="replace")) or [""]
    if "x-www-form-urlencoded" in ctype:
        body = _form(raw.decode(errors="replace"))
    else:
        try:
            body = json.loads(raw) if raw else []
        except ValueError:
            return [""]
    items = [b for b in [*(body if isinstance(body, list) else [body]), _form(query)]
             if isinstance(b, dict) and ("query" in b or "operationName" in b)]
    if not items:
        return None
    names = []
    for item in items:
        text, label = item.get("query"), item.get("operationName")
        if isinstance(text, str):
            names += root_fields(text) or [""]
        else:
            names.append(label if isinstance(label, str) else "")
    return names


def _form(text: str) -> dict:
    return {k: v[0] for k, v in parse_qs(text).items()}


def root_fields(document: str) -> list[str]:
    """The fields every operation in a document runs at its root. Aliases, arguments,
    directives and nested selections are skipped; fragments spread at the root count."""
    tokens = [t for t in TOKEN.findall(document) if t[0] not in '#"']
    fields: dict[str, list[str]] = {"": []}  # "": the operations; else a fragment's root
    owner, header, parens, inline = "", "", 0, False
    stack: list[bool] = []  # per open brace: is it a root selection?
    prev2 = prev = ""
    for t in tokens:
        if parens:
            parens += (t == "(") - (t == ")")
        elif t == "(":
            parens = 1
        elif t == "{":
            if not stack:
                owner, header = header, ""
                fields.setdefault(owner, [])
            stack.append(not stack or (stack[-1] and (inline or prev == "...")))
            inline = False
        elif t == "}":
            if stack:
                stack.pop()
        elif not stack:
            if prev == "fragment":
                header = t
            elif t in ("query", "mutation", "subscription"):
                header = ""
        elif stack[-1] and prev == "..." and t == "@":
            inline = True  # an inline fragment with a directive and no type
        elif stack[-1] and t not in ("...", ":", "@") and prev != "@":
            if prev == "...":
                if t == "on":
                    inline = True
                else:
                    fields[owner].append("..." + t)
            elif prev == "on" and prev2 == "...":
                pass  # the type an inline fragment applies to
            elif prev == ":" and fields[owner]:
                fields[owner][-1] = t  # what came before was an alias
            else:
                fields[owner].append(t)
        prev2, prev = prev, t

    def expand(names: list[str], seen: frozenset) -> list[str]:
        out = []
        for name in names:
            if not name.startswith("..."):
                out.append(name)
            elif name[3:] in fields and name[3:] not in seen:
                out += expand(fields[name[3:]], seen | {name[3:]})
        return out

    return expand(fields[""], frozenset())


# Tool calls in a model's reply


def tool_calls(reply: dict) -> list[tuple[tuple, str, object]]:
    """(where, name, input) for each tool call in a non-streamed Anthropic Messages,
    OpenAI Chat Completions or OpenAI Responses reply."""
    found = []
    for i, block in enumerate(_list(reply.get("content"))):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            found.append((("content", i), str(block.get("name", "")), block.get("input")))
    for c, choice in enumerate(_list(reply.get("choices"))):
        message = choice.get("message") if isinstance(choice, dict) else None
        for j, call in enumerate(_list((message or {}).get("tool_calls"))):
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                found.append((("choices", c, j), str(fn.get("name", "")), _parsed(fn.get("arguments"))))
    for i, item in enumerate(_list(reply.get("output"))):
        if isinstance(item, dict) and item.get("type") in TOOL_ITEMS:
            value = next((item[k] for k in ("arguments", "input", "action") if k in item), None)
            found.append((("output", i), str(item.get("name") or item["type"]), _parsed(value)))
    return found


def without_tool_calls(reply: dict, held: list[tuple], note: str) -> dict:
    """The reply with the held tool calls taken out and a note in their place, so the agent
    reads why and goes on without them."""
    out = json.loads(json.dumps(reply))
    gone = {where for where, *_ in held}
    if any(w[0] == "content" for w in gone):
        kept = [b for i, b in enumerate(out["content"]) if ("content", i) not in gone]
        out["content"] = kept + [{"type": "text", "text": note}]
        if not any(isinstance(b, dict) and b.get("type") == "tool_use" for b in kept):
            out["stop_reason"] = "end_turn"
    for c, choice in enumerate(_list(out.get("choices"))):
        message = choice.get("message") if isinstance(choice, dict) else None
        calls = _list((message or {}).get("tool_calls"))
        kept = [call for j, call in enumerate(calls) if ("choices", c, j) not in gone]
        if len(kept) == len(calls):
            continue
        if kept:
            message["tool_calls"] = kept
        else:
            message.pop("tool_calls", None)
            choice["finish_reason"] = "stop"
        if isinstance(message.get("content"), str | None):
            message["content"] = ((message.get("content") or "") + "\n\n" + note).strip()
    if any(w[0] == "output" for w in gone):
        kept = [item for i, item in enumerate(out["output"]) if ("output", i) not in gone]
        text = {"type": "output_text", "text": note, "annotations": []}
        out["output"] = kept + [{"type": "message", "role": "assistant", "content": [text]}]
    return out


# A held command is shown to a person, so anything that looks like a credential is hidden first:
# a value after password=, token: or Bearer, and any run of 24 or more letters and digits mixed.
SECRET = re.compile(
    r"(?i)((?:bearer|password|passwd|pwd|token|secret|api[_-]?key)[\s=:\"']+)[^\s\"']+"
    r"|(?=[\w\-]*\d)(?=[\w\-]*[a-z])[\w\-]{24,}"
)


def hide_secrets(text: str) -> str:
    return SECRET.sub(lambda m: (m.group(1) or "") + "[hidden]", text)


def fingerprint(held: list[tuple]) -> str:
    """Names the exact tool calls held, so an approval covers those and no others."""
    calls = json.dumps([(name, text) for _, name, text, _ in held])
    return hashlib.sha256(calls.encode()).hexdigest()[:16]


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _parsed(value):
    """OpenAI sends a tool call's arguments as JSON text."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _text(value) -> str:
    """Every string in a tool call's input, joined, for the command pattern to search."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return " ".join(_text(v) for v in value)
    return ""
