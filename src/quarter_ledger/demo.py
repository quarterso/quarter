"""quarter-ledger demo: the whole loop on this machine, with no vendor key and no money.

In a throwaway folder it starts the mock vendor (mock_upstream.py) and the
gateway on free localhost ports. It sends calls as two agents for two customers, has one
call held for a person and approves it by name, books a sample LiteLLM export through
connect mode, then says where the dashboard is and how to see the journal and audit pack.
It runs from a checkout of this repository, which holds the mock and the sample.
"""

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

from . import mock_upstream
from .proxy import VENDOR_KEY_ENV

SAMPLE = Path(__file__).with_name("samples") / "litellm-sample.csv"
SAMPLE_MONTH = "2026-10"
CLI = (sys.executable, "-c", "from quarter_ledger.cli import main; main()")
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost only, never a proxy
APPROVER = "Dana"
CAPS = [
    {"scope": "agent", "key": "*", "window": "day", "limit_usd": "5", "approve_above_usd": "0.25"},
    {"scope": "customer", "key": "acme", "window": "month", "limit_usd": "50"},
]
# agent, vendor, model, customer, task
CALLS = [
    ("support-bot", "openai", "gpt-4o-mini", "acme", "ticket-481"),
    ("support-bot", "openai", "gpt-4o-mini", "acme", "ticket-481"),
    ("support-bot", "openai", "gpt-4o", "globex", "ticket-482"),
    ("researcher", "anthropic", "claude-haiku-4-5", "globex", "market-scan"),
    ("researcher", "anthropic", "claude-haiku-4-5", "acme", "market-scan"),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_mock() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), mock_upstream.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def write_config(folder: Path, mock_url: str) -> tuple[dict[str, str], dict[str, str], str]:
    """The files a real setup has; the environment every step runs with, the agent keys and
    the approver's sign-in. Every key is made up here and reaches nothing but this machine."""
    agents = {name: f"qa_{secrets.token_hex(16)}" for name in ("support-bot", "researcher")}
    people = {APPROVER: {"key": secrets.token_hex(16), "role": "finance"}}
    files = {
        "caps.json": json.dumps(CAPS, indent=1),
        "upstreams.json": json.dumps({"openai": mock_url, "anthropic": mock_url}),
        "people.json": json.dumps(people, indent=1),
    }
    for name, text in files.items():
        (folder / name).write_text(text + "\n")
    env = {
        "QUARTER_AGENT_KEYS": ",".join(f"{name}={key}" for name, key in agents.items()),
        "QUARTER_PEOPLE": str(folder / "people.json"),
        "QUARTER_CAPS": str(folder / "caps.json"),
        "QUARTER_UPSTREAMS": str(folder / "upstreams.json"),
        "OPENAI_API_KEY": "mock-not-a-real-key",
        "ANTHROPIC_API_KEY": "mock-not-a-real-key",
    }
    fd = os.open(folder / ".env", os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write("".join(f"{k}={v}\n" for k, v in env.items()))
    return env, agents, people[APPROVER]["key"]


def clean_env(env: dict[str, str]) -> dict[str, str]:
    """This shell's environment without its Quarter settings or vendor keys, so no Slack, feed
    or real key of yours is used, plus the demo's own."""
    vendor = set(VENDOR_KEY_ENV.values())
    return {k: v for k, v in os.environ.items() if not k.startswith("QUARTER_") and k not in vendor} | env


def start_gateway(folder: Path, env: dict, port: int) -> subprocess.Popen:
    """In its own session, so Ctrl+C reaches the demo alone, which then stops it."""
    with open(folder / "gateway.log", "w") as log:
        proc = subprocess.Popen([*CLI, "serve", "--host", "127.0.0.1", "--port", str(port)], cwd=folder,
                                env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(100):
        if proc.poll() is not None:
            break
        try:
            LOCAL.open(f"http://127.0.0.1:{port}/healthz", timeout=1).close()
            return proc
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise SystemExit(f"demo: the gateway did not start; see {folder / 'gateway.log'}")


def send(gateway: str, key: str, vendor: str, model: str, customer: str, task: str,
         max_tokens: int = 200, approval: str | None = None) -> tuple[int, dict]:
    if vendor == "openai":
        url, auth = f"{gateway}/openai/v1/chat/completions", {"authorization": f"Bearer {key}"}
    else:
        url, auth = f"{gateway}/anthropic/v1/messages", {"x-api-key": key, "anthropic-version": "2023-06-01"}
    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": f"Work on {task} for {customer}."}]}
    headers = auth | {"content-type": "application/json", "x-quarter-customer": customer, "x-quarter-task": task}
    if approval:
        headers["x-quarter-approval"] = approval
    request = urllib.request.Request(url, json.dumps(body).encode(), headers)
    try:
        with LOCAL.open(request, timeout=10) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def quarter_ledger(folder: Path, env: dict, *argv: str) -> None:
    """One CLI command on the demo ledger, as a person would type it."""
    sys.stdout.flush()
    subprocess.run([*CLI, *argv], cwd=folder, env=env, check=True)


def run(args) -> None:
    folder = Path(args.dir).resolve() if args.dir else Path(tempfile.mkdtemp(prefix="quarter-demo-"))
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "ledger.db").exists():
        sys.exit(f"demo: {folder} already has a ledger; give another --dir")
    mock = start_mock()
    own, agents, login = write_config(folder, f"http://127.0.0.1:{mock.server_address[1]}")
    env = clean_env(own)
    port = free_port()
    gateway = f"http://127.0.0.1:{port}"
    proc = start_gateway(folder, env, port)
    on_term = signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # stop the gateway too
    try:
        print(f"Quarter demo in {folder}\nMock vendor and gateway on localhost. No real keys, no money.\n")
        print("Model calls through the gateway:")
        for agent, vendor, model, customer, task in CALLS:
            status, _ = send(gateway, agents[agent], vendor, model, customer, task)
            print(f"  {agent:<12} {model:<17} for {customer:<7} {status}")

        status, held = send(gateway, agents["researcher"], "anthropic", "claude-sonnet-4-5",
                            "globex", "deep-report", max_tokens=32000)
        if status != 403 or "approval_id" not in held:
            sys.exit(f"demo: expected the large call to wait for a person, got {status} {held}")
        print(f"\nA large call (up to 32,000 tokens of output) waits for a person: {status} {held['error']['code']}")
        print(f"  {held['reason']}")
        quarter_ledger(folder, env, "approve", held["approval_id"], "--as", APPROVER)
        print(f"  approved by {APPROVER}: quarter-ledger approve {held['approval_id']} --as {APPROVER}")
        status, _ = send(gateway, agents["researcher"], "anthropic", "claude-sonnet-4-5",
                         "globex", "deep-report", max_tokens=32000, approval=held["approval_id"])
        print(f"  retried with x-quarter-approval: {status}")

        print("\nConnect mode books last month's LiteLLM bill (a sample file):")
        print("  ", end="", flush=True)
        quarter_ledger(folder, env, "connect", "litellm", "--file", str(SAMPLE), "--no-alerts")

        print()
        quarter_ledger(folder, env, "status", "--caps", str(folder / "caps.json"))
        month = f"{datetime.now(UTC):%Y-%m}"
        print(f"""
Dashboard: {gateway}/dashboard?month={month}
  Last month's bill: {gateway}/dashboard?month={SAMPLE_MONTH}
  Sign in with any username and the password {login} ({APPROVER}, finance)

See the rest, in another terminal:
  cd {folder}
  alias q='uv run --no-project --env-file .env {sys.executable} -m quarter_ledger'
  q report --by customer
  q agents list
  q export --month {SAMPLE_MONTH}
  q export --month {SAMPLE_MONTH} --format quickbooks
  q audit-pack --month {month} --out audit-{month}.zip
  q verify-chain
""")
        if args.no_wait:
            return
        print("The gateway runs until you press Ctrl+C. The folder stays; delete it when done.", flush=True)
        proc.wait()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        mock.shutdown()
        signal.signal(signal.SIGTERM, on_term)
