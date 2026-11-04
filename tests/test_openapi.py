"""openapi.json against the server: every path it lists is answered, every route is listed."""

import ast
import inspect
import json
import re
import textwrap
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from quarter_ledger import proxy
from quarter_ledger.pricing import PriceBook
from quarter_ledger.proxy import Gateway, Handler, serve
from quarter_ledger.slack import Slack
from quarter_ledger.store import Ledger

SPEC = json.loads(proxy.OPENAPI.read_text())
AGENT_KEY, ADMIN_KEY = "agent-secret", "admin-secret"
# Both keys, so a route answers past its sign-in check: the agent key where the vendor key goes
KEYS = {"x-quarter-key": ADMIN_KEY, "authorization": f"Bearer {AGENT_KEY}"}
METHODS = ("get", "post", "put", "patch", "delete")
SAMPLE = {"vendor": "fake", "path": "v1/models"}  # any other path parameter is "x1", or a real id


@pytest.fixture
def url(tmp_path):
    slack = Slack("xoxb-test", "secret", "C1", {})  # /slack/actions is only answered with Slack set up
    ledger = Ledger(str(tmp_path / "l.db"))
    SAMPLE["id"] = ledger.add_standing("a1", "fake", 1, 1, "sam")  # so /standing/{id}/revoke finds one
    gw = Gateway(ledger, [], PriceBook({}), {"fake": "http://127.0.0.1:9"}, {AGENT_KEY}, ADMIN_KEY, slack=slack)
    server = serve(gw, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def send(url, method, path):
    data = b"{}" if method != "GET" else None
    req = urllib.request.Request(url + path, data, KEYS, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def answered(url, method, path) -> bool:
    """Neither a 404 nor what the gateway says to a vendor it does not know."""
    first = path.split("/")[1]
    status, body = send(url, method, path)
    unknown = send(url, method, path.replace(first, "nosuchvendor", 1))
    return status != 404 and (status, body) != (unknown[0], unknown[1].replace(b"nosuchvendor", first.encode()))


def refs(node):
    if isinstance(node, dict):
        yield from [node["$ref"]] if "$ref" in node else []
        for v in node.values():
            yield from refs(v)
    elif isinstance(node, list):
        for v in node:
            yield from refs(v)


def test_the_spec_is_openapi_and_served_without_a_key(url):
    assert SPEC["openapi"].startswith("3.1") and SPEC["paths"]
    for r in refs(SPEC):
        _, kind, name = r.removeprefix("#/").split("/")
        assert name in SPEC["components"][kind], r
    with urllib.request.urlopen(url + "/openapi.json") as resp:
        assert resp.status == 200 and json.loads(resp.read()) == SPEC


def test_every_path_in_the_spec_is_answered(url):
    for template, ops in SPEC["paths"].items():
        path = re.sub(r"\{(\w+)\}", lambda m: SAMPLE.get(m[1], "x1"), template)
        for method in set(ops) & set(METHODS):
            assert answered(url, method.upper(), path), f"{method.upper()} {template} is not a route"


def compared(code, name: str) -> set[str]:
    """The strings `code` compares the variable `name`, or an item of it, with: route == "/x",
    route.startswith("/x"), parts[0] in ("x", "y")."""
    found = set()
    for n in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(code)))):
        if isinstance(n, ast.Compare):
            left, sides = n.left, n.comparators
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            left, sides = n.func.value, n.args
        else:
            continue
        left = left.value if isinstance(left, ast.Subscript) else left
        if isinstance(left, ast.Name) and left.id == name:
            found |= {c.value for side in sides for c in ast.walk(side)
                      if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    return found


def segments(method: str) -> set[str]:
    return {s for p, ops in SPEC["paths"].items() if method in ops for s in p.strip("/").split("/")}


def test_every_route_the_server_answers_is_in_the_spec():
    gets = {p for p, ops in SPEC["paths"].items() if "get" in ops}
    assert proxy.VIEWER_ROUTES | proxy.FINANCE_DOWNLOADS <= gets
    in_get = {s for v in compared(Handler.do_GET, "route") for s in v.strip("/").split("/") if s}
    assert in_get <= segments("get"), in_get - segments("get")
    in_post = compared(Handler, "parts")
    assert in_post <= segments("post"), in_post - segments("post")
    assert proxy.RESERVED <= segments("get") | segments("post")


def test_every_first_segment_of_a_quarter_route_is_reserved():
    """A vendor so named would be hidden by the route, so none may be configured."""
    assert proxy.RESERVED == {p.strip("/").split("/")[0] for p in SPEC["paths"] if not p.startswith("/{")}


def test_the_x_quarter_headers_match_the_code():
    parts = SPEC["components"]
    named = [*parts["parameters"].values(), *parts["securitySchemes"].values()]
    in_spec = {n for n in [*parts["headers"], *(p.get("name") for p in named)] if str(n).startswith("x-quarter-")}
    in_code = set(re.findall(r"x-quarter-[a-z]+(?:-[a-z]+)*", Path(proxy.__file__).read_text()))
    assert in_code == in_spec, (in_code - in_spec, in_spec - in_code)
