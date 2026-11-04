"""Drive a local gateway with the official openai and anthropic SDKs, as a customer would.

    uv run --with openai --with anthropic python tools/sdk-check.py

Starts the sandbox mock vendor and a gateway on localhost, then makes plain and streamed calls,
lists models, and gets each refusal: over a cap, held for approval (with and without
x-quarter-wait), a paused agent, a bad key, a vendor the gateway has no key for, a vendor that
hangs up after the request was sent, and a locked-out address. For each it prints what the SDK
raised, its message, and how many requests the gateway read (more than one is the SDK's own
retry). Exits 1 if any check fails. Costs nothing and reaches nothing but this machine.
"""

import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic
import openai

from quarter_ledger import mock_upstream
from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.pricing import PriceBook, with_built_in
from quarter_ledger.proxy import LOGIN_FAILURES, Gateway, serve
from quarter_ledger.store import Ledger

KEYS = {name: f"qa_sdkcheck_{name}" for name in ("ok", "capped", "held", "paused")}
CHAT = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 50}
MESSAGE = {"model": "claude-haiku-4-5", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 50}
failures, sent = [], []  # sent: one entry per request the gateway reads


class HangsUp(BaseHTTPRequestHandler):
    """A vendor that reads the whole request and closes without answering."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self.close_connection = True

    def log_message(self, *args):
        pass


def start(server):
    server.handle_error = lambda *args: None  # the SDKs close keep-alive connections at exit
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def gateway(folder: str) -> tuple[str, Gateway]:
    base = f"http://127.0.0.1:{start(ThreadingHTTPServer(('127.0.0.1', 0), mock_upstream.Handler)).server_port}"
    hangs_up = f"http://127.0.0.1:{start(ThreadingHTTPServer(('127.0.0.1', 0), HangsUp)).server_port}"
    ledger = Ledger(f"{folder}/ledger.db")
    ledger.pause("paused", "sdk-check paused it")
    caps = [
        Cap("agent", "capped", "day", 1),  # one micro-dollar: every call is over it
        Cap("agent", "held", "day", to_micros(100), approve_above=1),
    ]
    keys = {key: name for name, key in KEYS.items()}
    upstreams = {"openai": base, "anthropic": base, "nokey": base, "hangsup": hangs_up}
    gw = Gateway(ledger, caps, PriceBook.load(with_built_in(None)), upstreams, set(keys), key_agents=keys,
                 vendor_keys={"openai": "sk-mock", "anthropic": "sk-mock", "hangsup": "sk-mock"})
    server = serve(gw, port=0)
    counted = server.RequestHandlerClass
    server.RequestHandlerClass = type("Counted", (counted,), {"parse_request": counting(counted.parse_request)})
    return f"http://127.0.0.1:{start(server).server_port}", gw


def counting(parse):
    def parse_request(self):
        sent.append(self.raw_requestline)
        return parse(self)
    return parse_request


def clients(url: str, agent: str, headers: dict | None = None, vendor: str = "openai"):
    """Both SDK clients for an agent, with their default retries."""
    key, extra = KEYS.get(agent, "qa_wrong"), headers or {}
    o = openai.OpenAI(base_url=f"{url}/{vendor}/v1", api_key=key, default_headers=extra)
    a = anthropic.Anthropic(base_url=f"{url}/anthropic", api_key=key, default_headers=extra)
    return o, a


def needed(value):
    if not value:
        raise ValueError("missing")
    return value


def check(name: str, run, want: type | None) -> None:
    sent.clear()
    try:
        error, said = None, run()
    except (openai.APIError, anthropic.APIError, ValueError) as e:  # what the SDK raised is the result
        error, said = e, str(e)
    tries = len(sent)
    ok = (type(error) is want if want else error is None) and tries == 1
    if want and ok:  # a refusal must say it is Quarter's, in a body the SDK read
        ok = "Quarter" in str(error) and isinstance(getattr(error, "body", None), dict)
    failures.extend([] if ok else [name])
    kind = type(error).__name__ if error else "ok"
    print(f"{'PASS' if ok else 'FAIL'}  {name:<34} {kind:<26} requests={tries}  {str(said)[:150]}")


def main() -> int:
    with tempfile.TemporaryDirectory() as folder:
        url, gw = gateway(folder)
        o, a = clients(url, "ok")

        def openai_stream():
            chunks = o.chat.completions.create(**CHAT, stream=True)
            return "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)

        def anthropic_stream():
            with a.messages.stream(**MESSAGE) as s:
                return s.get_final_text()

        check("openai chat", lambda: o.chat.completions.create(**CHAT).choices[0].message.content, None)
        check("openai chat stream", openai_stream, None)
        check("anthropic messages", lambda: a.messages.create(**MESSAGE).content[0].text, None)
        check("anthropic messages stream", anthropic_stream, None)
        check("openai models", lambda: [m.id for m in o.models.list()], None)
        check("anthropic models", lambda: [m.id for m in a.models.list()], None)
        check("openai request id", lambda: needed(o.chat.completions.create(**CHAT)._request_id), None)

        cases = [
            ("capped", None, openai.APIStatusError, anthropic.APIStatusError),
            ("held", None, openai.PermissionDeniedError, anthropic.PermissionDeniedError),
            ("held", {"x-quarter-wait": "1"}, openai.PermissionDeniedError, anthropic.PermissionDeniedError),
            ("paused", None, openai.APIStatusError, anthropic.APIStatusError),
            ("wrong key", None, openai.AuthenticationError, anthropic.AuthenticationError),
        ]
        for agent, headers, o_error, a_error in cases:
            o, a = clients(url, agent, headers)
            label = agent + (" (waits 1 s)" if headers else "")
            check(f"openai {label}", lambda o=o: o.chat.completions.create(**CHAT), o_error)
            check(f"openai {label} stream", lambda o=o: list(o.chat.completions.create(**CHAT, stream=True)), o_error)
            check(f"anthropic {label}", lambda a=a: a.messages.create(**MESSAGE), a_error)

        o, _ = clients(url, "ok", vendor="nokey")
        check("openai no vendor key", lambda: o.chat.completions.create(**CHAT), openai.InternalServerError)
        o, _ = clients(url, "ok", vendor="hangsup")
        check("openai vendor hung up", lambda: o.chat.completions.create(**CHAT), openai.InternalServerError)
        for _ in range(LOGIN_FAILURES):
            gw.failed_login("127.0.0.1")
        o, _ = clients(url, "ok")
        check("openai locked out", lambda: o.models.list(), openai.RateLimitError)
    print(f"\n{len(failures)} failed: {', '.join(failures)}" if failures else "\nall passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
