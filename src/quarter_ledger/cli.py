"""quarter-ledger serve | status | mcp | why | report | summary | approvals [import | export | verify]
| approve | deny | cost | export
| shadow-report | replay | agents | ingest-fail-open | connect
| sanctions [update | check | cleared | forget].

Keys come from the environment, never from files in the repository:
  QUARTER_AGENT_KEYS  comma-separated agent keys, each `key` or `name=key` (names the agent)
  QUARTER_ADMIN_KEY   the dashboard password and admin key, signing in as an admin named "admin"
  QUARTER_PEOPLE      {"name": {"key": "...", "role": "viewer|approver|finance|admin"}},
                      JSON text or a path: each person signs in with their own key
  OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY, and
  QUARTER_VENDOR_KEYS {"vendor": "key"} or `vendor=key,vendor:agent=key`: sent to the
  vendor when an agent key arrives as the vendor key; vendor:agent is that named agent's own

A host can set everything else from the environment too. Prices and upstreams for openai,
anthropic, gemini and openrouter are built in; these files extend or override them:
  QUARTER_CAPS, QUARTER_PRICES, QUARTER_UPSTREAMS  JSON text or a path
  QUARTER_RULES       what agents may do on each upstream (see rules.py), JSON text or a path
  QUARTER_APPROVAL_WEBHOOK  https URL told about each waiting approval (e.g. Slack)
  QUARTER_DASHBOARD_URL     public base URL, so that message links to the dashboard
  QUARTER_SIGNING_KEY       signs payment authorisations, approval receipts and statements;
                      unset, serve makes an ed25519 key beside the ledger (<db>.signing-key)
  QUARTER_MAX_WAIT    the longest x-quarter-wait in seconds, 300 unless set lower (behind a
                      proxy that cuts requests sooner, such as Cloudflare at 125 s)
  QUARTER_ORG         the company's domain; set, payments also get a spend mandate sellers check
  QUARTER_MERCURY_TOKEN, QUARTER_MERCURY_ACCOUNT  your Mercury API token and account id, to pay bills
  QUARTER_MERCURY_READ_TOKEN  a Read Only Mercury token for `cards reconcile` (else QUARTER_MERCURY_TOKEN)
  QUARTER_RAMP_CLIENT_ID, QUARTER_RAMP_CLIENT_SECRET  a Ramp app with transactions:read, for
                      `cards reconcile`; QUARTER_RAMP_URL for the sandbox
  OPENAI_ADMIN_KEY, ANTHROPIC_ADMIN_KEY  your own admin keys, read only by reconcile and connect
                      to fetch the vendor's costs
  STRIPE_REVENUE_KEY  a restricted Stripe key (rk_) that can read invoices and customers, for
                      `margin --stripe`; Quarter refuses a full secret key
  QUARTER_LITELLM_URL, QUARTER_LITELLM_KEY, QUARTER_OPENROUTER_MANAGEMENT_KEY,
  QUARTER_CLOUDFLARE_TOKEN, QUARTER_CLOUDFLARE_ACCOUNT, QUARTER_CLOUDFLARE_GATEWAY  read-only
                      access for `connect` (sources.py)
  QUARTER_SLACK_BOT_TOKEN, QUARTER_SLACK_SIGNING_SECRET, QUARTER_SLACK_CHANNEL  approvals
                      in Slack with Approve and Deny buttons; QUARTER_SLACK_USERS is
                      {"Slack user id": "person"}, the only people who may click them
  QUARTER_STRIPE_APPROVALS_SECRET  the whsec_ signing secret of a Stripe thin event destination
                      sending v2.core.approval_request.* to /approvals/stripe; each event goes
                      into the approval record (`approvals export`). Quarter never decides them
  QUARTER_RUNAWAY     unset for defaults, "off", or {"window_minutes", "multiple", "floor_usd",
                      "repeats", "tool_repeats", "mode"}
  QUARTER_MODE        "shadow" lets through every call that caps, payee lists, pauses and loop
                      stops would hold, and records it (would_hold); "enforce" is the default.
                      A cap, QUARTER_PAYEES or QUARTER_RUNAWAY can take "mode": "shadow" alone
  QUARTER_REGISTER    "on" sends the next call or payment of an agent with no owner, unused for
                      30 days, or past its quarterly review to a person; "shadow" records it
                      only; or {"dormant_days", "grace_days", "mode"}. Unset or "off": none
  QUARTER_FAIL_OPEN   "model" lets plain model calls through while the ledger cannot be written,
                      logged to QUARTER_FAIL_OPEN_LOG (default <db>.fail-open.jsonl) for
                      `ingest-fail-open`; everything that could ask a person stays closed.
                      Unset or "off": every call is refused while the ledger is down
  QUARTER_FALLBACKS   {"vendor": {"model": "cheaper model"}}, JSON text or a path
  QUARTER_PAYEES      {"x402": ["0x..."]}: payees allowed per payment method; a payment to
                      anyone else waits for a person, or is refused with "unlisted": "deny"
  QUARTER_BASE_RPC    a Base JSON-RPC endpoint for `wallets reconcile`, kept out of shell
                      history when it holds a provider's key; unset, Base's public one
  QUARTER_AUTO_CACHE  "off" stops the gateway adding Anthropic's cache marker to conversations
  QUARTER_FEED, QUARTER_FEED_KEY  an https address of the Quarter feed and its ed25519-pub key:
                      prices and tool rules kept current daily, under the customer's own (feed.py)
  QUARTER_RETENTION_DAYS  days the ledger promises to keep every entry; recorded in the
                      audit log and the audit pack (nothing in Quarter deletes entries)
  QUARTER_SANCTIONS   the folder `sanctions update` keeps the OFAC lists in (default
                      <db>.sanctions); with lists there, every payee is screened (sanctions.py)
  QUARTER_SANCTIONS_STALE_DAYS  days after a download that the lists count as stale, 7 unless
                      set: then new payees wait for a person, payees paid before go on
  QUARTER_FREE_DECISIONS  decisions a UTC month includes (hosted free tier); past them every
                      call is still decided and its reply says x-quarter-free-limit: exceeded
  QUARTER_DB, HOST, PORT
"""

import argparse
import csv
import getpass
import hashlib
import hmac
import json
import os
import signal
import sqlite3
import sys
import urllib.request
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from . import (
    anchor,
    approval_record,
    bills,
    cards,
    demo,
    forensics,
    incident,
    learn,
    mandate,
    mcp,
    sanctions,
    sources,
    status,
    tax,
    underwriting,
    wallets,
)
from .capitalise import capitalised, read_projects, reclass
from .evidence import audit_pack
from .export import (
    FORMATS,
    LINE_LIMITS,
    centre_headers,
    focus,
    focus_rows,
    journal,
    lines,
    read_centres,
    unbilled,
)
from .failopen import FailOpenLog, ingest
from .feed import Feed, sign_feed
from .insights import forecast
from .margin import STRIPE_KEY_ENV, margins, read_revenue, stripe_revenue
from .money import to_micros, usd_display, usd_shown
from .outcomes import cost_per_outcome, read_outcomes
from .policy import load_caps
from .pricing import PriceBook, with_built_in
from .proxy import MAX_WAIT, UPSTREAMS, VENDOR_KEY_ENV, Gateway, serve, show_decision_in_slack
from .receipts import public_key, sign, verify
from .reconcile import KEY_ENV, VENDORS, compare, fetch, quarter_side, read_costs
from .register import listing, load_register, review
from .rules import Rules, load_rules
from .runaway import load_runaway
from .shadow import replay, shadow_report
from .slack import Slack
from .store import Ledger, agent_summary, month_range


def config(value: str) -> dict | list:
    """JSON text, or the path of a JSON file."""
    try:
        if value.lstrip().startswith(("{", "[")):
            return json.loads(value)
        return json.loads(Path(value).read_text())
    except FileNotFoundError:
        raise SystemExit(f"config file not found: {value}") from None
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"not valid JSON ({e.msg}): {value[:80]}\n"
            "In a .env file read by uv, wrap JSON in single quotes: QUARTER_CAPS='[...]'"
        ) from None


def env_config(name: str, default=None):
    """The setting in environment variable `name` (JSON text or a path), or the default."""
    return config(os.environ[name]) if os.environ.get(name) else default


def caps_from(value: str | None) -> list:
    return load_caps(config(value)) if value else []


def rules_from(value: str | None) -> Rules:
    try:
        return load_rules(config(value)) if value else Rules()
    except (ValueError, TypeError) as e:
        raise SystemExit(f"rules: {e}") from None


def vendor_keys_from(value: str | None) -> dict[str, str]:
    """QUARTER_VENDOR_KEYS: JSON text or a path, or `vendor=key,vendor:agent=key`."""
    if value and "=" in value and not value.lstrip().startswith("{"):
        items = (item.partition("=") for item in value.split(","))
        return {name.strip(): key.strip() for name, _, key in items if name.strip() and key.strip()}
    return config(value) if value else {}


def register_from(value: str | None):
    try:
        return load_register(value)
    except ValueError as e:
        raise SystemExit(f"QUARTER_REGISTER: {e}") from None


def approvers(environ) -> set[str]:
    """Everyone who may decide approvals: people above viewer, Slack deciders, and admin."""
    people = env_config("QUARTER_PEOPLE", {})
    names = {n for n, p in people.items() if isinstance(p, dict) and p.get("role") not in (None, "viewer")}
    names |= set(json.loads(environ.get("QUARTER_SLACK_USERS") or "{}").values())
    return names | ({"admin"} if environ.get("QUARTER_ADMIN_KEY") else set())


def roles(people: dict) -> dict[str, str]:
    """Names and roles only: a key never reaches the audit log or the audit pack."""
    return {name: p.get("role") for name, p in people.items() if isinstance(p, dict)}


def retention_days(value: str | None) -> int | None:
    """QUARTER_RETENTION_DAYS as a whole number of days, or None when unset."""
    if not value:
        return None
    if not value.strip().isdigit() or int(value) < 1:
        sys.exit("QUARTER_RETENTION_DAYS must be a whole number of days")
    return int(value)


def free_decisions(value: str | None) -> int | None:
    """QUARTER_FREE_DECISIONS as a whole number of decisions a month, or None when unset."""
    if not value:
        return None
    if not value.strip().isdigit():
        sys.exit("QUARTER_FREE_DECISIONS must be a whole number of decisions")
    return int(value)


def max_wait(value: str | None) -> int:
    """QUARTER_MAX_WAIT as whole seconds from 1 to 300, the longest a call may wait for a person."""
    if not value:
        return MAX_WAIT
    if not value.strip().isdigit() or not 1 <= int(value) <= MAX_WAIT:
        sys.exit(f"QUARTER_MAX_WAIT must be a whole number of seconds from 1 to {MAX_WAIT}")
    return int(value)


def agent_keys(value: str) -> dict[str, str | None]:
    """QUARTER_AGENT_KEYS as {key: agent name or None}; an item is `key` or `name=key`."""
    keys = {}
    for item in (i.strip() for i in value.split(",")):
        name, sep, key = item.partition("=")
        if sep and name and key:
            keys[key] = name
        elif item:
            keys[item] = None
    return keys


def key_for(environ, agent: str) -> tuple[str, str]:
    """The agent key to hand an agent, and the name it goes by. A named key is only good for
    its own agent: prefer the agent's key, then an unnamed one, then any, under its own name."""
    keys = agent_keys(environ.get("QUARTER_AGENT_KEYS", ""))
    key = next((k for k, n in keys.items() if n == agent), None) or next(
        (k for k, n in keys.items() if n is None), ""
    )
    if not key and keys:
        key, agent = next(iter(keys.items()))
    return key, agent


def wrap_env(environ, url: str, agent: str) -> dict:
    """Environment that sends an Anthropic client through the Quarter gateway."""
    key, agent = key_for(environ, agent)
    headers = [f"x-quarter-agent: {agent}"] + ([f"x-quarter-key: {key}"] if key else [])
    return {
        **environ,
        "ANTHROPIC_BASE_URL": url.rstrip("/") + "/anthropic",
        "ANTHROPIC_CUSTOM_HEADERS": "\n".join(headers),
        # Subagent, prompt and request-class headers, so each subagent's cost shows apart.
        "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1",
    }


# Where Claude Code reads managed settings, which no user, project or shell value overrides.
MANAGED_PATHS = {
    "macOS": "/Library/Application Support/ClaudeCode/managed-settings.json",
    "Linux and WSL": "/etc/claude-code/managed-settings.json",
    "Windows": r"C:\Program Files\ClaudeCode\managed-settings.json",
}


def managed_settings(environ, url: str, agent: str) -> dict:
    """Claude Code managed settings that send every session on a machine through Quarter and
    refuse any other destination: Anthropic directly, or a developer's own proxy (Claude Code
    v2.1.285 or later). A claude.ai login stays the credential, so subscriptions keep working."""
    env = wrap_env(environ, url, agent)
    keep = ("ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_GATEWAY_HINT_HEADERS")
    return {"allowedProviders": ["customEndpoint"], "env": {k: env[k] for k in keep}}


# Where Codex reads requirements, which no user, project or session value overrides.
CODEX_PATHS = {
    "macOS and Linux": "/etc/codex/requirements.toml",
    "Windows": r"%ProgramData%\OpenAI\Codex\requirements.toml",
    "macOS MDM": "com.openai.codex, key requirements_toml_base64",
}


def codex_requirements(environ, url: str, agent: str) -> str:
    """Codex requirements that send every session on a machine through Quarter, the route
    OpenAI's gateway guide gives: a ChatGPT sign-in stays the credential, so plans keep
    working, and Quarter records the calls at no cost."""
    key, agent = key_for(environ, agent)
    headers = ", ".join(f'"{k}" = "{v}"' for k, v in (("x-quarter-agent", agent), ("x-quarter-key", key)) if v)
    return (
        'model_provider = "quarter"\n\n'
        "[model_providers.quarter]\n"
        'name = "Quarter"\n'
        f'base_url = "{url.rstrip("/")}/chatgpt/backend-api/codex"\n'
        "requires_openai_auth = true\n"
        f"http_headers = {{ {headers} }}\n"
    )


def run_managed_settings(args, ledger):
    agent = args.agent or args.client
    if args.client == "codex":
        print(codex_requirements(os.environ, args.url, agent), end="")
        name, paths = "requirements.toml", CODEX_PATHS
    else:
        print(json.dumps(managed_settings(os.environ, args.url, agent), indent=2))
        name, paths = "managed-settings.json", MANAGED_PATHS
    where = "\n".join(f"  {system}: {path}" for system, path in paths.items())
    print(f"Save as {name}, or push it by MDM, on each machine:\n{where}\n"
          "Every user on the machine can read it, agent key included. That key only reaches"
          " Quarter, which applies its caps and rules.", file=sys.stderr)


def init(folder: Path, monthly: str, approve_above: str) -> str:
    """Write a .env with fresh keys and a caps.json with one total budget. Never overwrites."""
    import secrets

    vendor_keys = "".join(f"# {env}=\n" for env in VENDOR_KEY_ENV.values())
    files = {
        "caps.json": json.dumps(
            [{"scope": "all", "window": "month", "limit_usd": monthly, "approve_above_usd": approve_above,
              "over": "approve"}],
            indent=1,
        ),
        ".env": f"QUARTER_AGENT_KEYS=first-agent=qa_{secrets.token_hex(16)}\n"
        f"QUARTER_ADMIN_KEY={secrets.token_hex(16)}\n"
        "QUARTER_CAPS=caps.json\n"  # relative, so the folder can move or be mounted at /app
        f"{vendor_keys}",
    }
    folder.mkdir(parents=True, exist_ok=True)
    taken = [n for n in files if (folder / n).exists()]
    if taken:
        raise SystemExit(f"already there, not overwritten: {', '.join(taken)}")
    for name, text in files.items():  # .env is created owner-only, never readable for a moment
        mode = 0o600 if name == ".env" else 0o644
        fd = os.open(folder / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
    return (
        f"Your agents may spend ${monthly} a month in total; calls over ${approve_above}, and any"
        " past that budget, wait for you.\n"
        "Keys are in .env; add your vendor keys there. Start, from this folder, with:\n"
        "  quarter-ledger --env-file .env serve\n"
        "The dashboard's password is QUARTER_ADMIN_KEY in .env (any username)."
    )


def check_chain(ledger: Ledger) -> None:
    """Print both hash chains' check; exit 1 if either is broken."""
    result = ledger.verify_chain()
    print(json.dumps(result, indent=2))
    if not all(t["intact"] for t in result.values()):
        sys.exit(1)


def csv_rows(path: str) -> list[dict]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


# One function per command, each given the parsed arguments and the ledger.


def run_wrap(args, ledger):
    cmd = [c for c in args.cmd if c != "--"] or ["claude"]
    env = wrap_env(os.environ, args.url, args.agent)
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("Note: ANTHROPIC_API_KEY is set, so Claude Code bills that key, not your subscription.", file=sys.stderr)
    try:
        os.execvpe(cmd[0], cmd, env)
    except FileNotFoundError:
        sys.exit(f"{cmd[0]}: command not found. Install it, or name another: wrap -- <command>")


def run_serve(args, ledger):
    keys = agent_keys(os.environ.get("QUARTER_AGENT_KEYS", ""))
    admin_key = os.environ.get("QUARTER_ADMIN_KEY") or None
    people = env_config("QUARTER_PEOPLE", {})
    retention_days(os.environ.get("QUARTER_RETENTION_DAYS"))
    if args.host != "127.0.0.1" and not keys:
        sys.exit("refusing to listen beyond localhost without QUARTER_AGENT_KEYS")
    person_keys = [str(p.get("key", "")) for p in people.values() if isinstance(p, dict)]
    weak = [k for k in [*keys, admin_key or "", *person_keys] if k and len(k) < 24]
    if args.host != "127.0.0.1" and weak:
        sys.exit("keys must be at least 24 characters to listen beyond localhost")
    caps = config(args.caps) if args.caps else []
    prices = with_built_in(config(args.prices) if args.prices else None)
    upstreams = UPSTREAMS | (config(args.upstreams) if args.upstreams else {})
    vendor_keys = {v: os.environ[env] for v, env in VENDOR_KEY_ENV.items() if os.environ.get(env)}
    vendor_keys |= vendor_keys_from(os.environ.get("QUARTER_VENDOR_KEYS"))
    egress = rules_from(args.rules)
    unlisted = env_config("QUARTER_PAYEES", {}).get("unlisted", "approve")
    if unlisted not in ("approve", "deny"):
        sys.exit(f'QUARTER_PAYEES: "unlisted" is "approve" or "deny", not {unlisted!r}')
    mode = os.environ.get("QUARTER_MODE", "").strip().lower() or "enforce"
    if mode not in ("enforce", "shadow"):
        sys.exit(f'QUARTER_MODE is "enforce" or "shadow", not {mode!r}')
    fail_open = os.environ.get("QUARTER_FAIL_OPEN", "").strip().lower() or "off"
    if fail_open not in ("off", "model"):
        sys.exit(f'QUARTER_FAIL_OPEN is "model" or "off", not {fail_open!r}')
    key = signing_key(ledger, make=True)
    org = os.environ.get("QUARTER_ORG", "").strip().lower() or None
    if org and not mandate.DOMAIN.match(org):
        sys.exit(f"QUARTER_ORG must be the company's domain, such as example.com, not {org!r}")
    if org and not (key or "").startswith("ed25519:"):
        sys.exit("QUARTER_ORG needs an ed25519: signing key to sign spend mandates")
    settings = {"caps": caps, "prices": prices, "upstreams": upstreams}
    if args.rules:
        settings["rules"] = config(args.rules)
    if people:
        settings["people"] = roles(people)
    ledger.record_config(settings | {
        env: os.environ[f"QUARTER_{env.upper()}"]
        for env in ("runaway", "payees", "retention_days", "mode", "register", "max_wait", "fail_open",
                    "free_decisions")
        if os.environ.get(f"QUARTER_{env.upper()}")
    })
    ledger.register = register_from(os.environ.get("QUARTER_REGISTER"))
    gw = Gateway(
        ledger,
        load_caps(caps),
        PriceBook.load(prices),
        upstreams,
        set(keys),
        admin_key,
        approval_webhook=os.environ.get("QUARTER_APPROVAL_WEBHOOK") or None,
        dashboard_url=os.environ.get("QUARTER_DASHBOARD_URL") or None,
        signing_key=key,
        stripe_secret=os.environ.get("QUARTER_STRIPE_SECRET") or None,
        stripe_approvals_secret=os.environ.get("QUARTER_STRIPE_APPROVALS_SECRET") or None,
        stripe_revenue_key=os.environ.get(STRIPE_KEY_ENV) or None,
        org=org,
        mercury=mercury_from_env(),
        runaway=load_runaway(os.environ.get("QUARTER_RUNAWAY")),
        payees=env_config("QUARTER_PAYEES", {}),
        fallbacks=env_config("QUARTER_FALLBACKS", {}),
        auto_cache=os.environ.get("QUARTER_AUTO_CACHE", "").lower() != "off",
        key_agents={k: name for k, name in keys.items() if name},
        vendor_keys=vendor_keys,
        slack=Slack.from_env(os.environ),
        people=people,
        rules=egress,
        mode=mode,
        max_wait=max_wait(os.environ.get("QUARTER_MAX_WAIT")),
        fail_open=FailOpenLog(fail_open_path(args)) if fail_open == "model" else None,
        client_ip_header=os.environ.get("QUARTER_CLIENT_IP_HEADER", "").strip().lower() or None,
        centres_path=os.environ.get("QUARTER_COST_CENTRES") or f"{args.db}.centres.json",
        retention_days=retention_days(os.environ.get("QUARTER_RETENTION_DAYS")),
        rules_config=settings.get("rules"),
        sanctions=screen_from(args),
        **({"redact_key": redaction_key(key)} if key else {}),
    )
    print(f"gateway on http://{args.host}:{args.port}", flush=True)
    if not caps:
        print("no caps set: spend is recorded, not limited", flush=True)
    for rule in gw.in_shadow():
        print(f"SHADOW MODE: {rule} let every call through and only record what they would have held"
              " (quarter-ledger shadow-report)", flush=True)
    print(gw.sanctions.summary(), flush=True)
    if ledger.free_decisions is not None:
        print(f"free tier: {ledger.free_decisions} decisions a month, then replies say x-quarter-free-limit: exceeded",
              flush=True)
    if gw.fail_open:
        print(f"FAIL-OPEN: if the ledger cannot be written, plain model calls go through, logged to"
              f" {gw.fail_open.path} (quarter-ledger ingest-fail-open)", flush=True)
    if key and key.startswith("ed25519:"):
        print(f"payments are authorised with key {public_key(key)}", flush=True)
    if admin_key or people:
        print(f"dashboard on http://{args.host}:{args.port}/dashboard (any username; the password is"
              f" {'QUARTER_ADMIN_KEY' if admin_key else 'your own key from QUARTER_PEOPLE'})", flush=True)
    if os.environ.get("QUARTER_FEED"):
        own_prices = config(args.prices) if args.prices else None
        try:
            Feed(gw, os.environ["QUARTER_FEED"], os.environ.get("QUARTER_FEED_KEY", ""), own_prices, egress).start()
        except ValueError as e:
            sys.exit(f"QUARTER_FEED: {e}")
        print(f"prices and rules kept current from {os.environ['QUARTER_FEED']}", flush=True)
    # As PID 1 in a container, SIGTERM is ignored unless handled, and `docker stop` waits 10 s
    # before killing. Exit as Ctrl-C does: a call mid-write rolls back, since each is a transaction.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # Only shutdown() needs the poll, and nothing here calls it: an idle gateway wakes once an hour.
    serve(gw, args.host, args.port).serve_forever(poll_interval=3600)


def run_agents(args, ledger):
    if args.agents_command == "review":
        try:
            done = review(ledger, args.quarter, args.agent, args.by, args.keep, approvers(os.environ), args.note)
        except (PermissionError, ValueError) as e:
            sys.exit(f"agents review: {e}")
        print(f"{done['agent']}: {done['result']} for {done['quarter']}, reviewed by {done['reviewer']}")
        if done["result"] == "retire":
            print(f"{done['agent']} is paused: its key is refused until someone resumes it."
                  " Remove its key from QUARTER_AGENT_KEYS too.")
        return
    names = {n for n in agent_keys(os.environ.get("QUARTER_AGENT_KEYS", "")).values() if n}
    rows = listing(ledger, names, caps_from(args.caps), env_config("QUARTER_PAYEES", {}),
                   register_from(os.environ.get("QUARTER_REGISTER")))
    columns = ("agent", "owner", "caps", "payees", "first_seen", "last_call", "last_payment",
               "month_to_date", "review_quarter", "reviewed_at", "reviewer", "review_result", "status")
    if args.csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(columns)
        for r in rows:
            r = r | {"month_to_date": usd_display(r["month_to_date"])}
            writer.writerow(["; ".join(v) if isinstance(v, list) else v for v in (r[c] for c in columns)])
        return
    def day(at):
        return at[:10] if at else "never"

    for r in rows:
        review_text = f"{r['review_quarter']} {r['review_result']} by {r['reviewer']}" if r["reviewer"] else "none"
        print(f"{r['agent']:<20} owner {r['owner'] or '-':<12} call {day(r['last_call'])}"
              f"  paid {day(r['last_payment'])}"
              + (f"  observed {day(r['last_observed'])}" if r["last_observed"] else "")
              + f"  month {usd_shown(r['month_to_date'])}"
              f"  review {review_text}  {', '.join(r['status'])}")
        print(f"{'':<20} caps: {'; '.join(r['caps']) or 'none'}  payees: {'; '.join(r['payees']) or 'any'}")


def run_feed_sign(args, ledger):
    """For Quarter's own publishing: sign a feed body with QUARTER_FEED_SIGNING_KEY."""
    key = os.environ.get("QUARTER_FEED_SIGNING_KEY", "")
    try:
        envelope = sign_feed(json.loads(Path(args.body).read_text()), key)
    except (OSError, ValueError) as e:
        sys.exit(f"feed-sign: {e}")
    print(json.dumps(envelope, indent=1))
    print(f"verify key: {public_key(key)}", file=sys.stderr)


def run_report(args, ledger):
    since, until = month_range(args.month) if args.month else (None, None)
    rows = [(w or "(none)", v, t) for w, v, t in ledger.totals(args.by, since, until)]
    if args.csv:
        writer = csv.writer(sys.stdout)
        writer.writerow([args.by, "vendor", "usd"])
        writer.writerows((who, vendor, usd_display(t)) for who, vendor, t in rows)
    else:
        for who, vendor, t in rows:
            print(f"{who:<24} {vendor:<16} {usd_shown(t)}")


def run_summary(args, ledger):
    for row in agent_summary(ledger, args.month):
        print(
            f"{row['agent']:<24} by ${usd_display(row['by']):>10}  on ${usd_display(row['on']):>10}"
            f"  total ${usd_display(row['by'] + row['on']):>10}"
        )


def run_forecast(args, ledger):
    out = forecast(ledger, caps_from(args.caps))
    so_far = f"{out['month']}: ${usd_display(out['so_far'])} so far"
    if not out["enough_data"]:
        print(f"{so_far}, not enough data yet for a month-end forecast (less than a day of spend)")
    else:
        print(f"{so_far}, ${usd_display(out['month_end'])} at month end")
        for r in out["agents"]:
            print(f"  {r['agent']:<22} ${usd_display(r['per_day'])}/day  month end ${usd_display(r['month_end'])}")
    for w in out["cap_warnings"]:
        when = "already reached" if w["reached"] == "already" else f"reached {w['reached']}"
        print(f"  cap {w['cap']}: ${usd_display(w['limit'])}, {when}")


def run_underwriting(args, ledger):
    rules = rules_from(args.rules)
    out = underwriting.statement(ledger, args.month, caps_from(args.caps), rules.listed(),
                                 retention_days(os.environ.get("QUARTER_RETENTION_DAYS")))
    key = signing_key(ledger)
    if key:
        out = {"statement": out, "signed": underwriting.signed(out, key)}
    text = json.dumps(out, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f"wrote {args.out}" + ("" if key else "; unsigned: run serve once, or set QUARTER_SIGNING_KEY"))
    else:
        print(text)


def utc(text: str) -> str:
    """A date or time as the ledger stores it: ISO 8601 in UTC. Without a zone, UTC."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        sys.exit(f"not a date or time: {text}")
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(UTC).isoformat()


def run_incident(args, ledger):
    start, end = utc(args.start), utc(args.end)
    if end <= start:
        sys.exit("--to must come after --from")
    out = incident.pack(ledger, start, end, args.agent, args.task)
    Path(args.out).write_text(json.dumps(out) + "\n")
    rows = {t: len(c["rows"]) for t, c in out["chains"].items()}
    scope = {t: len(ids) for t, ids in out["in_scope"].items()}
    print(f"wrote {args.out}: {scope['entries']} entries and {scope['audit']} audit rows in scope,"
          f" {len(out['calls'])} calls; the chains run on to now ({rows['entries']} and"
          f" {rows['audit']} rows) so they reach a head an insurer already holds")


def statement_heads(path: str) -> tuple[dict, dict]:
    """The heads one statement file commits to, and what was checked about it."""
    data = json.loads(Path(path).read_text())
    if "signed" in data:
        key = os.environ.get("QUARTER_SIGNING_KEY")
        if not key:
            sys.exit(f"{path} is signed: set QUARTER_SIGNING_KEY to the public key to check it")
        claims = verify(data["signed"], key)
        if claims is None:
            sys.exit(f"{path}: the signature does not match")
        data, signed = claims, True
    else:
        data, signed = data.get("statement", data), False
    note = {"file": path, "month": data.get("period", {}).get("month"),
            "made_at": data.get("made_at"), "signed": signed}
    return incident.heads_of(data), note


def run_check_incident(args, ledger):
    pack = json.loads(Path(args.pack).read_text())
    if pack.get("schema") != incident.SCHEMA:
        sys.exit(f"not an incident pack: {args.pack}")
    heads, notes = {}, []
    for path in args.statement:
        found, note = statement_heads(path)
        for table, hashes in found.items():
            heads.setdefault(table, set()).update(hashes)
        notes.append(note)
    chains = incident.check(pack, heads)
    print(json.dumps({"window": pack["window"], "scope": pack["scope"], "statements": notes,
                      "chains": chains}, indent=2))
    if not all(c["intact"] for c in chains.values()):
        sys.exit(1)


def run_anchor(args, ledger):
    if args.anchor_command == "verify":
        try:
            result = anchor.verify(args.anchor, args.file)
        except (OSError, ValueError, KeyError) as e:
            sys.exit(f"cannot check: {e}")
        print(json.dumps(result, indent=2))
        if not result["ok"]:
            sys.exit(1)
        return
    if not args.out:
        sys.exit("--out is needed: the anchor's JSON file")
    record = anchor.make(ledger, args.out, args.submit or None)
    print(f"wrote {args.out}; digest {record['digest']}; keep a copy outside Quarter")
    if record["ots"]["status"] == "not submitted":
        print(f"warning: not submitted to {args.submit}: {record['ots']['reason']}", file=sys.stderr)
    elif record["ots"]["status"] == "submitted":
        proof = record["ots"]["file"]
        print(f"pending timestamp in {proof}; in a few hours run: ots upgrade {proof}")


def run_audit_pack(args, ledger):
    people = env_config("QUARTER_PEOPLE")
    rules_from(args.rules)  # a bad rules file is refused before the pack is written
    summary = audit_pack(
        ledger,
        args.month,
        args.out,
        caps_from(args.caps),
        os.environ.get("QUARTER_SIGNING_KEY"),
        retention_days(os.environ.get("QUARTER_RETENTION_DAYS")),
        roles(people) if people is not None else None,
        config(args.rules) if args.rules else None,
        register_from(os.environ.get("QUARTER_REGISTER")),
    )
    print(json.dumps(summary["chain"]))
    for warning in summary["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"wrote {args.out}; record this head hash elsewhere: {summary['heads']['entries']['hash']}")


def screen_from(args) -> sanctions.Screen:
    days = os.environ.get("QUARTER_SANCTIONS_STALE_DAYS", "").strip()
    if days and not days.isdigit():
        sys.exit("QUARTER_SANCTIONS_STALE_DAYS must be a whole number of days")
    folder = os.environ.get("QUARTER_SANCTIONS") or f"{args.db}.sanctions"
    return sanctions.Screen(folder, int(days) if days else sanctions.STALE_DAYS)


def run_sanctions(args, ledger):
    screen = screen_from(args)
    if args.sanctions_command == "update":
        try:
            lists = sanctions.update(screen.folder)
        except (OSError, ValueError, SyntaxError) as e:  # SyntaxError: XML that does not parse
            ledger.audit("sanctions_update_failed", getpass.getuser(), str(e)[:200])
            sys.exit(f"sanctions update: {e}; the lists kept before are unchanged")
        for which, info in lists.items():
            ledger.audit("sanctions_update", getpass.getuser(), json.dumps({"list": which} | info, sort_keys=True))
            print(f"{which}: published {info['published']}, {info['records']} records, sha256 {info['sha256'][:16]}")
        print(f"kept in {screen.folder}")
        for c in screen.expire(ledger, getpass.getuser()):
            print(f"{c['payee']}: no longer cleared, {c['list']} entry {c['uid']} changed or left the list")
        return
    if args.sanctions_command == "cleared":
        for c in sanctions.cleared(ledger):
            print(f"{c['payee']}  {c['list']} entry {c['uid']}  cleared by {c['cleared_by']}"
                  f" {c['cleared_at'][:16]} ({c['approval_id']})")
        return
    if args.sanctions_command == "forget":
        gone = sanctions.forget(ledger, args.name, getpass.getuser())
        print(f"{args.name}: {'forgotten, its next payment waits for a person' if gone else 'not cleared'}")
        return
    if not screen.stored():
        sys.exit(f"no OFAC list in {screen.folder}: run quarter-ledger sanctions update")
    m = screen.check(args.name)
    print(screen.summary())
    if m.outcome == "clear":
        print(f"{args.name}: no match")
    else:
        verdict = "refused" if m.refused else "waits for a person"
        print(f"{args.name}: {'exact' if m.outcome == 'match' else 'close'} match, {verdict}\n  {m.describe()}")


def fail_open_path(args) -> str:
    return os.environ.get("QUARTER_FAIL_OPEN_LOG") or f"{args.db}.fail-open.jsonl"


def run_ingest_fail_open(args, ledger):
    path = args.file or fail_open_path(args)
    if not os.path.exists(path):
        sys.exit(f"nothing to ingest: {path} does not exist")
    print(json.dumps(ingest(ledger, path), indent=2))


def run_backup(args, ledger):
    os.makedirs(os.path.dirname(args.path) or ".", mode=0o700, exist_ok=True)
    try:
        os.close(os.open(args.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        sys.exit(f"already there, not overwritten: {args.path}")
    with closing(sqlite3.connect(args.path)) as copy:
        ledger.db.backup(copy)  # one consistent snapshot, even with writers running
    check_chain(Ledger(args.path))
    print(f"wrote {args.path}")


def run_resume(args, ledger):
    resume = ledger.resume_task if args.task else ledger.resume
    if not resume(args.name, by="cli"):
        sys.exit(f"{'task ' * args.task}{args.name} is not paused")
    ledger.audit("resumed", "cli", f"{'task ' * args.task}{args.name}")


def run_why(args, ledger):
    r = forensics.why(ledger, args.task)
    if not r["calls"]:
        sys.exit(f"no priced calls recorded for task {args.task}")

    def usd(micros: int) -> str:
        return f"${usd_display(micros)}"

    observed = ledger.db.execute(
        "select count(*) from calls where task = ? and request_class = 'observed'", (args.task,)).fetchone()[0]
    if observed == r["calls"]:  # only observed calls: the prompt was never seen
        print(f"Task {r['task']}: {r['calls']} calls, ${usd_display(r['charged_micros'])}, observed by"
              " connect. The parts and cache breaks need the prompt, which only the gateway sees.")
        return
    sub = r["subscription_calls"]
    print(f"Task {r['task']}: {r['calls']} calls, {usd(r['charged_micros'])} charged"
          + (f" ({sub} on the subscription)" if sub else "")
          + f", {usd(r['list_micros'])} at list price.")
    print(f"Cache misses a stable prefix would have saved: {usd(r['cache_miss_micros'])}.")
    whole = max(1, r["list_micros"])
    print("\nWhere it went (at list price)")
    names = {"tools": "tool definitions", "system": "system prompt",
             "history": "history", "output": "output"}
    for part, name in names.items():
        micros = r["parts"][f"{part}_micros"]
        print(f"  {name:<20} {usd(micros):>10} {micros / whole:>5.0%}")
        if part == "tools":
            for row in r["tools_by_server"]:
                print(f"    {row['server']:<18} {usd(row['micros']):>10}")
    for title, key, rows in (("By subagent", "subagent", r["by_subagent"]),
                             ("By prompt", "prompt_id", r["by_prompt"][:10])):
        print(f"\n{title}")
        for row in rows:
            print(f"  {row[key][:24]:<24} {row['calls']:>4} calls {usd(row['list_micros']):>10}")
    print(f"\nCache breaks: {len(r['breaks'])}")
    for b in r["breaks"][:20]:
        print(f"  turn {b['turn']:<4} {b['subagent'][:12]:<12} {usd(b['micros']):>8}  {b['reason']}")
    if len(r["breaks"]) > 20:
        print(f"  and {len(r['breaks']) - 20} more")


def signing_key(ledger: Ledger, make: bool = False) -> str | None:
    """QUARTER_SIGNING_KEY, else the key kept beside the ledger, made here when `make` is
    set (owner-only, never overwritten): payments work with nothing to configure."""
    key = os.environ.get("QUARTER_SIGNING_KEY")
    if key or ledger.path == ":memory:":
        return key or None
    path = Path(ledger.path).with_suffix(".signing-key")
    if make and not path.exists():
        import secrets

        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass  # another process made it first
        else:
            with os.fdopen(fd, "w") as fh:
                fh.write(f"ed25519:{secrets.token_hex(32)}\n")
    return path.read_text().strip() if path.exists() else None


def redaction_key(signing: str) -> bytes:
    """Derived from the signing key, so a value keeps its placeholder across restarts (and the
    prompt cache keeps hitting) while the key itself never leaves the gateway."""
    return hmac.new(signing.encode(), b"quarter redaction placeholders", hashlib.sha256).digest()


def needs_signing_key(ledger: Ledger) -> str:
    key = signing_key(ledger)
    if not key:
        sys.exit("no signing key: set QUARTER_SIGNING_KEY, or run serve once to make one")
    return key


def run_verify(args, ledger):
    claims = verify(args.token, needs_signing_key(ledger))
    if claims is None:
        sys.exit("invalid: the signature does not match")
    print(json.dumps(claims, indent=2))


def run_receipt(args, ledger):
    key = needs_signing_key(ledger)
    approval = ledger.approval(args.id)
    if not approval or approval["status"] == "pending":
        sys.exit("no decided approval with that id")
    print(sign(approval, key))


def run_cost(args, ledger):
    if args.cost_command == "add":
        cost_id = ledger.add_cost(args.category, args.vendor, to_micros(args.amount_usd), args.month,
                                  args.monthly, args.agent, args.customer)
        print(f"cost {cost_id} recorded")
    elif args.cost_command == "end":
        ok = ledger.end_cost(args.id, args.last_month)
        sys.exit(0 if ok else f"cost {args.id} is not a monthly cost started by then")
    else:
        for cost in ledger.costs_in(args.month):
            print(
                f"{cost['id']:>4}  {cost['agent'] or '(company)':<20} {cost['category']:<14}"
                f" {cost['vendor']:<16} ${usd_display(cost['amount'])}"
                + ("  monthly" if cost["monthly"] else "")
            )


def run_approvals(args, ledger):
    if args.approvals_command == "import":
        try:
            with open(args.file) as fh:
                print(json.dumps(approval_record.import_lines(ledger, fh)))
        except ValueError as e:
            sys.exit(f"{args.file}: {e}")
        return
    if args.approvals_command == "export":
        summary = approval_record.export(ledger, args.out, args.month)
        print(json.dumps(summary["chain"]))
        print(f"wrote {args.out}: {summary['approvals']} approvals;"
              f" record this head hash elsewhere: {summary['audit_head']['hash']}")
        sys.exit(0 if all(t["intact"] for t in summary["chain"].values()) else 1)
    if args.approvals_command == "verify":
        result = approval_record.verify(args.file)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["intact"] else 1)
    for a in ledger.approvals():
        print(f"{a['id']}  {a['agent']}  {a['vendor']}  ${usd_display(a['estimate'])}  {a['reason']}")


def run_decide(args, ledger):
    try:
        ok = ledger.decide_approval(args.id, args.command == "approve", by=args.by)
    except PermissionError as e:
        sys.exit(str(e))
    if ok:  # as the gateway does, so the decision is in the chain
        ledger.audit(args.command, f"{args.by}@cli", args.id)
        print(f"{args.id} {'approved' if args.command == 'approve' else 'denied'} by {args.by}")
    slack = Slack.from_env(os.environ)
    if ok and slack:  # no gateway runs here, so update its Slack message directly
        try:
            show_decision_in_slack(slack, ledger, args.id)
        except (OSError, ValueError) as e:
            ledger.audit("webhook_failed", None, f"{args.id} {e}")
            print(f"decided, but the Slack message was not updated: {e}", file=sys.stderr)
    sys.exit(0 if ok else f"{args.id} is not waiting for approval")


def run_focus(args, ledger):
    if args.month and (args.since or args.until):
        sys.exit("export: give --month or --from and --to, not both")
    start, end = month_range(args.month) if args.month else (None, None)
    start = datetime.fromisoformat(utc(args.since)) if args.since else start
    end = datetime.fromisoformat(utc(args.until)) if args.until else end
    accounts = {}
    for given in args.account:
        vendor, _, account = given.partition("=")
        if not vendor or not account:
            sys.exit(f"export: --account {given}: give it as VENDOR=ID")
        accounts[vendor] = account
    text = focus(focus_rows(ledger, start, end, accounts))
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            f.write(text)
    else:
        sys.stdout.write(text)


def run_export(args, ledger):
    if args.format == "focus":
        return run_focus(args, ledger)
    if not args.month:
        sys.exit(f"export: --format {args.format} needs --month")
    accounts = (args.expense_account, args.offset_account, args.revenue_cost_account)
    centres = None
    if args.cost_centres:
        try:
            centres = read_centres(Path(args.cost_centres).read_text())
            centre_headers(args.format, list(centres))
        except (OSError, ValueError) as e:  # json.JSONDecodeError is a ValueError
            sys.exit(f"export: {args.cost_centres}: {e}")
    if args.billed and not args.unbilled:
        sys.exit("export: --billed needs --unbilled")
    rows, number, narration, reversal = lines(ledger, args.month, *accounts, centres), None, None, None
    if args.unbilled:
        billed = {}
        for given in args.billed:
            vendor, _, path = given.partition("=")
            if not vendor or not path:
                sys.exit(f"export: --billed {given}: give it as VENDOR=FILE")
            try:
                billed[vendor] = sum(read_costs(Path(path).read_text(), args.month).values())
            except (OSError, ValueError) as e:
                sys.exit(f"export: {path}: {e}")
        rows = unbilled(ledger, args.month, billed, args.expense_account, args.accrued_account,
                        args.revenue_cost_account, centres)
        number, reversal = f"AGENT-ACCR-{args.month}", f"AGENT-REV-{args.month}"
        narration = f"Quarter unbilled agent spend {args.month}"
        recorded = {vendor for _, vendor, _ in ledger.totals("customer", *month_range(args.month))}
        for vendor in billed.keys() - recorded:
            print(f"export: Quarter recorded no {vendor} spend in {args.month}; check the vendor name",
                  file=sys.stderr)
    limit = LINE_LIMITS.get(args.format)
    if limit and len(rows) * (2 if reversal else 1) > limit:
        print(f"{args.format} imports at most {limit} lines in one file; this journal has more,"
              " so split it or post it from the csv format", file=sys.stderr)
    sys.stdout.write(journal(ledger, args.month, *accounts, fmt=args.format,
                             date_format=args.date_format, customer_names=args.customer_names,
                             tax_rate=args.tax_rate, rows=rows, number=number,
                             narration=narration, centres=centres, reversal=reversal))


def run_capitalise(args, ledger):
    try:
        projects = read_projects(Path(args.projects).read_text())
    except (OSError, ValueError) as e:  # json.JSONDecodeError is a ValueError
        sys.exit(f"capitalise: {args.projects}: {e}")
    out = capitalised(ledger, args.month, projects)
    if args.evidence:
        with open(args.evidence, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["project", "task", "agent", "vendor", "model", "calls", "amount_usd", "first", "last"])
            for t in out["tasks"]:
                writer.writerow([t["project"], t["task"], t["agent"], t["vendor"], t["model"], t["calls"],
                                 usd_display(t["amount"]), t["first"], t["last"]])
    for c in out["conflicts"]:
        print(f"capitalise: call {c['call_id']} matches {' and '.join(c['projects'])}; counted in neither",
              file=sys.stderr)
    if args.journal:
        rows = reclass(out, args.asset_account, args.expense_account, args.revenue_cost_account)
        sys.stdout.write(journal(ledger, args.month, fmt=args.journal, date_format=args.date_format, rows=rows,
                                 number=f"AGENT-CAP-{args.month}",
                                 narration=f"Quarter agent spend capitalised {args.month}"))
        return
    print(f"{out['month']}: ${usd_display(out['total'])} of agent spend attributable to {len(projects)} project(s)")
    for p in out["projects"]:
        print(f"  {p['project']:<30} ${usd_display(p['amount']):>12}")
    if out["conflicts"]:
        print(f"  {len(out['conflicts'])} call(s) matched more than one project and were left out")


def run_margin(args, ledger):
    notes = None
    if args.stripe:
        key = os.environ.get(STRIPE_KEY_ENV)
        if not key:
            sys.exit(f"margin: set {STRIPE_KEY_ENV} to a restricted Stripe key that can read invoices and customers")
        try:
            revenue, notes = stripe_revenue(key, args.month)
        except ValueError as e:
            sys.exit(f"margin: Stripe {e}")
    else:
        try:
            revenue = read_revenue(Path(args.revenue).read_text())
        except (OSError, ValueError) as e:
            sys.exit(f"margin: {args.revenue}: {e}")
    out = margins(ledger, args.month, revenue)

    def signed(micros):  # usd_display rounds negatives the wrong way
        return ("-" if micros < 0 else "") + usd_display(abs(micros))

    def usd(micros):
        return ("-$" if micros < 0 else "$") + usd_display(abs(micros))

    def pct(value):
        return "" if value is None else f"{value:.1f}%"

    if args.csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(["customer", "revenue_usd", "ai_cost_usd", "margin_usd", "margin_pct"])
        for r in out["rows"]:
            writer.writerow([r["customer"], signed(r["revenue"]), signed(r["ai_cost"]), signed(r["margin"]),
                             pct(r["margin_pct"]).rstrip("%")])
        return
    if notes:
        print(f"Stripe: {notes['invoices']} invoice(s) paid in {out['month']}, excluding tax, less credit notes"
              + (f"; {notes['skipped_currency']} not in USD left out" if notes["skipped_currency"] else "")
              + ("; TEST MODE data" if notes["test_mode"] else ""))
    print(f"{out['month']}: revenue {usd(out['revenue'])}, AI cost {usd(out['ai_cost'])},"
          f" margin after AI cost {usd(out['margin'])} {pct(out['margin_pct'])}".rstrip())
    for r in out["rows"]:
        flag = "  no revenue" if r["revenue"] <= 0 < r["ai_cost"] else (
            "  costs more than it pays" if r["margin"] < 0 else "")
        print(f"  {r['customer']:<22} revenue {usd(r['revenue']):>12}  AI {usd(r['ai_cost']):>11}"
              f"  margin {usd(r['margin']):>12} {pct(r['margin_pct']):>7}{flag}")
    if out["unbilled_customers"]:
        print(f"  {out['unbilled_customers']} customer(s) with AI cost and no revenue: {usd(out['unbilled_cost'])}")
    if out["heavy_share_pct"] is not None:
        print(f"  heaviest {out['heavy_customers']} customer(s) by AI cost: {out['heavy_share_pct']:.0f}% of it;"
              f" median AI cost per customer {usd(out['median_cost'])}")
    if out["unassigned_cost"]:
        print(f"  {usd(out['unassigned_cost'])} of AI spend named no customer (operating expense, not in margin)")


def run_outcomes(args, ledger):
    if args.add:
        try:
            found = read_outcomes(Path(args.add).read_text())
        except (OSError, ValueError) as e:
            sys.exit(f"outcomes: {args.add}: {e}")
        added = sum(ledger.add_outcome(o["kind"], o["task"], at=o["at"], ref=o["ref"], agent=o["agent"],
                                       customer=o["customer"], value=o["value"], reported_by="import")[1]
                    for o in found)
        print(f"{added} outcome(s) recorded, {len(found) - added} already there")
    out = cost_per_outcome(ledger, args.month)

    def usd(micros):
        return "" if micros is None else "$" + usd_display(micros)

    if args.csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(["kind", "outcomes", "ai_cost_usd", "cost_per_outcome_usd", "value_usd", "value_per_ai_dollar"])
        for r in out["rows"]:
            writer.writerow([r["kind"], r["count"], usd(r["cost"]).lstrip("$"), usd(r["per_outcome"]).lstrip("$"),
                             usd(r["value"]).lstrip("$") if r["valued"] else "",
                             "" if r["value_per_dollar"] is None else f"{r['value_per_dollar']:.2f}"])
        return
    print(f"{out['month']}: {out['outcomes']} outcome(s), AI cost {usd(out['cost'])}")
    for r in out["rows"]:
        line = f"  {r['kind']:<24} {r['count']:>6}  AI {usd(r['cost']):>11}  per outcome {usd(r['per_outcome']) or 'n/a':>10}"
        if r["value_per_dollar"] is not None:
            line += f"  returns {r['value_per_dollar']:.1f}x"
        if r["unpriced"]:
            line += f"  ({r['unpriced']} with no spend on their task)"
        print(line)
    if out["idle_tasks"]:
        print(f"  {out['idle_tasks']} task(s) spent {usd(out['idle_cost'])} and reported no outcome")


def run_reconcile(args, ledger):
    where = args.file or f"the {args.vendor} cost API"
    try:
        if args.file:
            billed = read_costs(Path(args.file).read_text(), args.month)
        else:
            key = os.environ.get(KEY_ENV[args.vendor])
            if not key:
                sys.exit(f"reconcile: set {KEY_ENV[args.vendor]} to your {args.vendor} admin key,"
                         " or pass --file with the cost export from the vendor's console")
            billed = fetch(args.vendor, key, args.month,
                           os.environ.get(f"QUARTER_{args.vendor.upper()}_ADMIN_URL"))
        seen, subscription = quarter_side(ledger, args.vendor, args.month)
    except (OSError, ValueError) as e:  # a bad month is a ValueError too
        sys.exit(f"reconcile: {where}: {e}")
    out = compare(billed, seen, args.tolerance_pct)

    def signed(micros):  # usd_display rounds negatives the wrong way
        return ("-" if micros < 0 else "") + usd_display(abs(micros))

    def usd(micros):
        return ("-$" if micros < 0 else "$") + usd_display(abs(micros))

    def pct(value):
        return "" if value is None else f"{value:.1f}%"

    flags = {
        "outside": "billed, and Quarter never saw it: spend outside the gateway",
        "unbilled": "Quarter saw it, and the vendor did not bill it: a credit, or not billed yet",
        "over": f"differs by more than {args.tolerance_pct:g}%",
    }
    if args.csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(["model", "vendor_usd", "quarter_usd", "difference_usd", "difference_pct", "flag"])
        for r in [*out["rows"], out["total"]]:
            writer.writerow([r["model"], signed(r["vendor"]), signed(r["quarter"]), signed(r["difference"]),
                             pct(r["difference_pct"]).rstrip("%"), r["flag"]])
        return
    t = out["total"]
    print(f"{args.month} {args.vendor}: vendor {usd(t['vendor'])}, Quarter {usd(t['quarter'])},"
          f" difference {usd(t['difference'])} {pct(t['difference_pct'])}".rstrip()
          + (f"  {flags[t['flag']]}" if t["flag"] else ""))
    for r in out["rows"]:
        flag = f"  {flags[r['flag']]}" if r["flag"] else ""
        print(f"  {r['model']:<32} vendor {usd(r['vendor']):>11}  Quarter {usd(r['quarter']):>11}"
              f"  difference {usd(r['difference']):>11} {pct(r['difference_pct']):>7}{flag}")
    if subscription["calls"]:
        print(f"  {subscription['calls']} call(s) went on a subscription and are recorded at $0"
              f" ({usd(subscription['list_cost'])} at list price): the subscription is billed apart"
              " from API usage, so they are in neither column")


def run_mandate_keys(args, ledger):
    try:
        print(json.dumps(mandate.keys(needs_signing_key(ledger)), indent=1))
    except ValueError as e:
        sys.exit(f"mandate-keys: {e}")
    print(f"publish this at https://<your domain>{mandate.KEYS_PATH}", file=sys.stderr)


def run_verify_mandate(args, ledger):
    domain = mandate.issuer(args.token)
    try:
        key_set = json.loads(Path(args.keys).read_text()) if args.keys else mandate.fetch_keys(domain or "")
    except (OSError, ValueError) as e:
        sys.exit(f"verify-mandate: no keys for {domain or 'this token'}: {e}")
    try:
        amount = to_micros(args.amount_usd) if args.amount_usd else None
    except ArithmeticError:
        sys.exit(f"verify-mandate: {args.amount_usd!r} is not an amount")
    claims = mandate.verified(args.token, key_set, args.payee, amount)
    if claims is None:
        sys.exit("not valid: wrong key, expired, another payee, or over the amount")
    print(json.dumps(claims, indent=1))


def mercury_from_env() -> dict | None:
    token, account = os.environ.get("QUARTER_MERCURY_TOKEN"), os.environ.get("QUARTER_MERCURY_ACCOUNT")
    if not (token and account):
        return None
    return {"token": token, "account": account, "url": os.environ.get("QUARTER_MERCURY_URL")}


def show_bill(b: dict) -> str:
    due = f"due {b['due']}" if b["due"] else "no due date"
    line = (f"{b['id']}  {b['payee']:<20} {b['invoice']:<14} ${usd_display(b['amount']):>10}"
            f"  {due:<14} {b['status']}")
    line += f": {b['reason']}" if b["status"] in ("waiting", "refused") and b["reason"] else ""
    return line + ("  (no W-9)" if b.get("no_w9") else "")


def run_bill(args, ledger):
    caps = caps_from(args.caps)
    if args.bill_command == "add":
        ids = {"agent": args.agent, "task": None, "customer": args.customer}
        try:
            b = bills.add(ledger, caps, env_config("QUARTER_PAYEES", {}), ids, args.payee, args.invoice,
                          to_micros(args.amount_usd), args.account, args.due, args.memo, screen_from(args))
        except (ValueError, ArithmeticError) as e:
            sys.exit(f"bill: {e}")
        print(show_bill(b))
    elif args.bill_command == "list":
        for b in bills.listed(ledger, args.status):
            print(show_bill(b))
    elif args.bill_command == "payee-tax":
        tin = None
        if args.entity != "foreign":  # never an argument, so it stays out of shell history
            tin = getpass.getpass("TIN: ") if sys.stdin.isatty() else sys.stdin.readline()
        try:
            t = tax.record(ledger, args.payee, args.form_date, args.entity, tin_key(ledger), tin,
                           args.pays_for, args.by)
        except ValueError as e:
            sys.exit(f"payee-tax: {e}")
        tin_line = f", TIN ending {t['tin_end']}" if t["tin_end"] else ""
        print(f"{args.payee}: {tax.ENTITIES[t['entity']]}, form received {t['w9_at']}{tin_line}")
    else:
        try:
            ids = list(args.ids) + (bills.due_by(ledger, args.due_by) if args.due_by else [])
        except ValueError as e:
            sys.exit(f"bill pay: {e}")
        if not ids:
            sys.exit("bill pay: name bills, or --due-by YYYY-MM-DD")
        failed = 0
        for bill_id in ids:
            try:
                b = bills.pay(ledger, caps, bill_id, args.by, args.method, args.ref, mercury_from_env(),
                              args.payment_method)
                print(f"paid   {show_bill(b)}  ref {b['ref']}")
            except (KeyError, PermissionError, ValueError) as e:
                failed += 1
                print(f"unpaid {bill_id}: {str(e).strip(chr(39))}", file=sys.stderr)
        sys.exit(1 if failed else 0)


def tin_key(ledger: Ledger) -> bytes:
    """Derived from the signing key, so a TIN fingerprint cannot be reversed from the
    ledger alone."""
    signing = signing_key(ledger, make=True) or "memory"
    return hmac.new(signing.encode(), b"quarter tin fingerprints", hashlib.sha256).digest()


def run_tax_1099(args, ledger):
    rows = tax.report(ledger, args.year)
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["payee", "entity", "tin_last4", "bills", "total_usd", "reportable",
                             "why", "w9_missing"])
            for r in rows:
                writer.writerow([r["payee"], r["entity"] or "", r["tin_end"] or "", r["bills"],
                                 usd_display(r["total"]), r["reportable"], r["why"],
                                 "yes" if r["w9_missing"] else "no"])
    print(f"{args.year}: {len(rows)} payee(s) paid; 1099-NEC/MISC threshold"
          f" ${usd_display(tax.threshold(args.year))}")
    for r in rows:
        w9 = "  NO W-9" if r["w9_missing"] else ""
        why = f" ({r['why']})" if r["why"] else ""
        print(f"  {r['payee']:<24} ${usd_display(r['total']):>12}  {r['bills']:>3} bill(s)"
              f"  1099: {r['reportable']}{why}{w9}")
    print(tax.NOTE)


def run_suggest(args, ledger):
    out = learn.suggestions(ledger, args.days)
    wait = f", median wait {out['median_wait_minutes']} minutes" if out["median_wait_minutes"] is not None else ""
    print(f"{out['decisions']} decisions in the last {out['days']} days{wait}.")
    if out["would_not_ask"]:
        print(f"Accepting the suggestions below would have spared {out['would_not_ask']} of them.")
    for s in out["suggestions"]:
        print(f"\n{s['text']}.")
        if s["kind"] == "standing":
            print(f"  largest ${usd_display(s['largest'])}. Let calls up to ${usd_display(s['up_to'])} through,"
                  f" at most ${usd_display(s['monthly'])} a month:")
            print(f"  quarter-ledger standing add --agent {s['agent']} --vendor '{s['vendor']}'"
                  f" --up-to-usd {usd_display(s['up_to'])} --monthly-usd {usd_display(s['monthly'])} --by <you>")
    if not out["suggestions"]:
        print("No suggestions: nothing repeats often enough, or a standing approval already covers it.")


def run_standing(args, ledger):
    if args.standing_command == "add":
        try:
            rule_id = ledger.add_standing(args.agent, args.vendor, to_micros(args.up_to_usd),
                                          to_micros(args.monthly_usd), args.by, args.days)
        except (PermissionError, ValueError, ArithmeticError) as e:
            sys.exit(f"standing: {e}")
        print(f"{rule_id}: {args.agent} may spend up to ${args.up_to_usd} a call with {args.vendor},"
              f" ${args.monthly_usd} a month, for {args.days} days, without asking")
    elif args.standing_command == "list":
        for r in ledger.standing(active=not args.all):
            state = f"revoked by {r['revoked_by']}" if r["revoked_at"] else f"until {r['ends'][:10]}"
            print(f"{r['id']}  {r['agent']:<20} {r['vendor']:<24} up to ${usd_display(r['up_to'])}"
                  f"  ${usd_display(r['monthly'])}/month  by {r['created_by']}  {state}")
    else:
        sys.exit(0 if ledger.revoke_standing(args.id, args.by) else f"no active standing approval {args.id}")


def show_transfer(t: dict) -> str:
    return (f"{t['at']:%Y-%m-%d %H:%M}  {t['from']} -> {t['to']}  ${usd_display(t['amount']):>10}"
            f"  tx {t['tx']}")


def run_wallets(args, ledger):
    if args.wallets_command == "policy":
        payees = env_config("QUARTER_PAYEES", {})
        try:
            export = wallets.privy_policy if args.format == "privy" else wallets.cdp_policy
            doc = export(caps_from(args.caps), payees, args.agent)
        except ValueError as e:
            sys.exit(f"wallets policy: {e}")
        if "x402" in payees and payees.get("unlisted", "approve") != "deny":
            print("Note: Quarter lets a person approve a payee off the list; this wallet will not."
                  " Add such payees to QUARTER_PAYEES first.", file=sys.stderr)
        print(json.dumps(doc, indent=1))
        return
    try:
        found = wallets.reconcile(ledger, wallets.read_wallets(config(args.wallets)), args.month,
                                  wallets.Rpc(args.rpc))
    except (ValueError, wallets.RpcError) as e:
        sys.exit(f"wallets reconcile: {e}")
    first, last = found["blocks"]
    print(f"{found['month']}: {len(found['transfers'])} USDC transfers out of the wallets on Base"
          f" (blocks {first} to {last}), {len(found['matched'])} match Quarter's x402 settlements.")
    if found["outside"]:
        print("\nPaid outside Quarter (no authorisation in the ledger):")
        for t in found["outside"]:
            print("  " + show_transfer(t))
    if found["missing"]:
        print("\nSettled in Quarter with no transfer on chain:")
        for s in found["missing"]:
            print(f"  {s['at']:%Y-%m-%d %H:%M}  {s['agent']} -> {s['payee']}"
                  f"  ${usd_display(s['amount']):>10}  call {s['call_id']}")
    if found["mismatched"]:
        print("\nOn chain differs from what was settled:")
        for t, s in found["mismatched"]:
            print(f"  {show_transfer(t)}\n    settled {s['call_id']}: {s['payee']}"
                  f" ${usd_display(s['amount'])}")
    sys.exit(1 if found["outside"] or found["missing"] or found["mismatched"] else 0)


def run_cards(args, ledger):
    env = os.environ
    try:
        agent_cards = cards.read_cards(config(args.cards))
        if args.source == "mercury":
            token = env.get("QUARTER_MERCURY_READ_TOKEN") or env.get("QUARTER_MERCURY_TOKEN")
            if not token:
                sys.exit("cards reconcile: set QUARTER_MERCURY_READ_TOKEN to a Read Only Mercury token")
            base = env.get("QUARTER_MERCURY_URL")
            feed = lambda start, end: cards.mercury(token, start, end, base)
        else:
            client, secret = env.get("QUARTER_RAMP_CLIENT_ID"), env.get("QUARTER_RAMP_CLIENT_SECRET")
            if not (client and secret):
                sys.exit("cards reconcile: set QUARTER_RAMP_CLIENT_ID and QUARTER_RAMP_CLIENT_SECRET"
                         " to a Ramp app allowed transactions:read")
            base = env.get("QUARTER_RAMP_URL")
            token = cards.ramp_token(client, secret, base)
            feed = lambda start, end: cards.ramp(token, start, end, base)
        found = cards.reconcile(ledger, agent_cards, args.month, feed)
    except ValueError as e:  # a bad month is a ValueError too
        sys.exit(f"cards reconcile: {args.source}: {e}")

    def charge(c):
        state = "  pending" if c["pending"] else ""
        return (f"{c['at']:%Y-%m-%d %H:%M}  {c['agent']:<16} {c['merchant'][:28]:<28}"
                f" ${usd_display(c['amount']):>10}  {args.source} {c['id']}{state}")

    def decision(a):
        who = f", approved by {a['approver']}" if a["approver"] else ""
        return (f"call {a['call_id']}: {a['payee']} ${usd_display(a['amount'])}, task {a['task'] or '-'},"
                f" customer {a['customer'] or '-'}{who}")

    print(f"{found['month']} {args.source}: {found['charges']} charges on agent cards,"
          f" {len(found['matched'])} match a Quarter card authorisation.")
    if found["matched"]:
        print("\nMatched:")
        for c, a in found["matched"]:
            note = f"  over by ${usd_display(c['amount'] - a['amount'])}, within tolerance" \
                if c["amount"] > a["amount"] else ""
            print(f"  {charge(c)}\n    {decision(a)}{note}")
    if found["outside"]:
        print("\nCharged outside Quarter (no card authorisation in the ledger):")
        for c in found["outside"]:
            print("  " + charge(c))
    if found["over"]:
        print("\nCharged more than authorised:")
        for c, a in found["over"]:
            print(f"  {charge(c)}\n    {decision(a)}")
    if found["refunds"]:
        print("\nRefunds and credits (not matched; the charge they reverse stays as it was):")
        for c in found["refunds"]:
            print("  " + charge(c) + (f"  refunds {c['original']}" if c["original"] else ""))
    if found["never_charged"]:
        print("\nAuthorised and not charged this month:")
        for a in found["never_charged"]:
            print(f"  {a['at']:%Y-%m-%d %H:%M}  {a['agent']:<16} {decision(a)}")
    if args.settle:
        n = cards.settle(ledger, found["matched"], args.source)
        print(f"\nRecorded {n} matched charge(s) as settlements at the charged amount.")
    sys.exit(1 if found["outside"] or found["over"] else 0)


def run_shadow_report(args, ledger):
    out = shadow_report(ledger, datetime.fromisoformat(utc(args.since)))
    if args.csv:
        w = csv.writer(sys.stdout)
        w.writerow(["rule", "outcome", "calls", "amount_usd", "would_ask"])
        for g in out["by_rule"]:
            w.writerow([g["rule"], g["outcome"], g["count"], usd_display(g["amount"]), " ".join(g["asked"])])
        return
    print(f"Since {args.since}: {out['calls']} would have been held, ${usd_display(out['amount'])} in all.")
    for g in out["by_rule"]:
        asked = f"  would ask {', '.join(g['asked'])}" if g["asked"] else ""
        print(f"  {g['rule'][:48]:<48} {g['outcome']:<7} {g['count']:>5} calls ${usd_display(g['amount']):>10}{asked}")
    for s in out["standing"]:
        what = s["id"] if s["from"] == "in force" else "suggested (quarter-ledger suggest)"
        print(f"A standing approval for {s['agent']} with {s['vendor']} up to ${usd_display(s['up_to'])}, {what},"
              f" would have answered {s['count']} of these asks, ${usd_display(s['amount'])}.")


def run_replay(args, ledger):
    live_payees = env_config("QUARTER_PAYEES", {})
    out = replay(
        ledger, (caps_from(args.live_caps), live_payees),
        (caps_from(args.caps), config(args.payees) if args.payees else live_payees), args.days,
    )
    check = out["self_check"]
    print(f"Replayed {len(out['calls'])} calls from the last {args.days} days, read only.")
    print(f"Self-check: the live rules give the recorded decision for {check['matched']} of {check['compared']}"
          " calls (a rule changed in the period, or a call moved to a cheaper model, explains a difference).")
    for name, label in (("newly_held", "Newly held"), ("no_longer_held", "No longer held"),
                        ("changed", "Held another way (refused instead of asked, or the reverse)")):
        print(f"{label}: {out[name]['calls']} calls, ${usd_display(out[name]['amount'])}")
    if out["newly_held"]["calls"]:
        print(f"  refused {out['newly_held']['block']}, asked {out['newly_held']['approve']}; by rule:")
        for r in out["by_rule"]:
            print(f"  {r['rule'][:48]:<48} {r['calls']:>5} calls ${usd_display(r['amount']):>10}")
    print("Windows hold what was recorded: a call the proposed rules would refuse still counts toward later windows.")
    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["at", "agent", "task", "customer", "vendor", "model", "amount_usd", "recorded", "live",
                        "proposed", "proposed_reason"])
            for c in out["calls"]:
                w.writerow([c["at"], c["agent"], c["task"], c["customer"], c["vendor"], c["model"],
                            usd_display(c["amount"]), c["recorded"], c["live"], c["proposed"], c["proposed_reason"]])


def run_paused(args, ledger):
    for p in ledger.paused_agents():
        who = p["agent"] + (f" task {p['task']}" if p["task"] else "")
        print(f"{who:<24} since {p['at'][:16]}  {p['reason']}")


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="quarter-ledger")
    top.add_argument("--env-file", metavar="FILE", help="read settings from FILE first, such as the .env init writes")
    top.add_argument("--version", action="version", version=f"%(prog)s {package_version()}")
    top.add_argument("--db", default=os.environ.get("QUARTER_DB", "ledger.db"))
    sub = top.add_subparsers(dest="command", required=True)

    def command(name, run, help=None):
        p = sub.add_parser(name, help=help) if help else sub.add_parser(name)
        p.set_defaults(run=run)
        return p

    def month(p, help="YYYY-MM"):
        p.add_argument("--month", required=True, help=help)

    def caps(p):
        p.add_argument("--caps", default=os.environ.get("QUARTER_CAPS"))

    def rules(p):
        p.add_argument("--rules", default=os.environ.get("QUARTER_RULES"))

    dm = command("demo", lambda a: demo.run(a),
                 "try the whole loop on this machine: mock vendor, gateway, a hold, connect")
    dm.add_argument("--dir", help="folder to run in (default: a new temporary folder)")
    dm.add_argument("--no-wait", action="store_true", help="stop when done instead of serving the dashboard")

    it = command("init", lambda a, ledger: print(init(Path(a.dir), a.monthly, a.approve_above)),
                 "starter config for one person or a small team")
    it.add_argument("--monthly", default="100", help="USD all agents together may spend a month")
    it.add_argument("--approve-above", default="5", help="USD above which a call waits for you")
    it.add_argument("--dir", default=".")

    wr = command("wrap", run_wrap, "run Claude Code (or any Anthropic client) through Quarter")
    wr.add_argument("--agent", default="claude-code")
    wr.add_argument("--url", default=os.environ.get("QUARTER_URL", "http://127.0.0.1:8780"))
    wr.add_argument("cmd", nargs=argparse.REMAINDER, help="the command, e.g. claude")

    fs = command("feed-sign", run_feed_sign, "sign a Quarter feed body (Quarter's own publishing)")
    fs.add_argument("body", help="the feed body: version, issued, prices, rules")

    ms = command("managed-settings", run_managed_settings,
                 "print Claude Code or Codex managed settings that make Quarter the only way out of a machine")
    ms.add_argument("--for", dest="client", choices=("claude-code", "codex"), default="claude-code")
    ms.add_argument("--agent", help="the agent name calls are recorded under; defaults to --for")
    ms.add_argument("--url", default=os.environ.get("QUARTER_URL", "http://127.0.0.1:8780"))

    s = command("serve", run_serve, "run the gateway and dashboard")
    caps(s)
    rules(s)
    s.add_argument("--prices", default=os.environ.get("QUARTER_PRICES"))
    s.add_argument("--upstreams", default=os.environ.get("QUARTER_UPSTREAMS"))
    s.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8780")))

    st = command("status", lambda a, ledger: sys.stdout.write(
        status.render(ledger, caps_from(a.caps), color=sys.stdout.isatty(),
                      rules=rules_from(a.rules).listed(), sanctions=screen_from(a))
    ), "spend today and this month, caps, approvals, paused agents, rules, sanctions lists")
    caps(st)
    rules(st)
    caps(command("mcp", lambda a, ledger: mcp.run(ledger, caps_from(a.caps), sys.stdin, sys.stdout),
                 "read-only MCP server on stdio: an agent's budget and approvals"))

    r = command("report", run_report, "spend so far")
    r.add_argument("--by", choices=["agent", "task", "customer", "vendor"], default="customer")
    r.add_argument("--csv", action="store_true")
    r.add_argument("--month", help="YYYY-MM, otherwise all time")

    month(command("summary", run_summary, "per agent: spent by it and spent on it"))

    cp = command("capitalise", run_capitalise,
                 "agent spend attributable to software projects, with evidence and the reclass journal")
    month(cp)
    cp.add_argument("--projects", required=True, help="projects.json: name, from, to, agents, tasks")
    cp.add_argument("--evidence", help="write one row per task behind each project to this CSV")
    cp.add_argument("--journal", choices=FORMATS, help="print the entry that moves the spend into the asset")
    cp.add_argument("--asset-account", default="Capitalised software")
    cp.add_argument("--expense-account", default="AI vendor costs")
    cp.add_argument("--revenue-cost-account", default="Cost of revenue: AI")
    cp.add_argument("--date-format", help="strftime format for the journal date")

    oc = command("outcomes", run_outcomes, "cost per outcome: what agents' work produced, against its AI cost")
    month(oc)
    oc.add_argument("--add", metavar="CSV", help="record outcomes first from a CSV: kind, task and optionally"
                    " at, id, customer, agent, value_usd")
    oc.add_argument("--csv", action="store_true")

    mg = command("margin", run_margin, "margin after AI cost, by customer")
    month(mg)
    source = mg.add_mutually_exclusive_group(required=True)
    source.add_argument("--revenue", help="CSV of revenue that month: a customer column and an amount column")
    source.add_argument("--stripe", action="store_true",
                        help=f"read revenue from Stripe: invoices paid that month ({STRIPE_KEY_ENV})")
    mg.add_argument("--csv", action="store_true")

    cn = command("connect", lambda a, ledger: sources.run(a, ledger, caps_from(a.caps)),
                 "book spend from where it already is (vendor cost APIs, LiteLLM, OpenRouter,"
                 " Cloudflare AI Gateway, CSV) as observed entries")
    sources.add_arguments(cn)
    caps(cn)

    rc = command("reconcile", run_reconcile, "the vendor's bill for a month against what Quarter recorded")
    month(rc)
    rc.add_argument("--vendor", required=True, choices=VENDORS)
    rc.add_argument("--file", help="CSV of the vendor's costs from its console: a date column, an"
                    " optional model or line item column and an amount column; no admin key needed")
    rc.add_argument("--tolerance-pct", type=float, default=2.0,
                    help="flag a model whose difference is over this share of the vendor's figure")
    rc.add_argument("--csv", action="store_true")

    command("suggest", run_suggest, "what past approvals suggest: standing approvals, always-denied spend").add_argument(
        "--days", type=int, default=90)
    ssub = command("standing", run_standing, "approvals agreed ahead of time").add_subparsers(
        dest="standing_command", required=True)
    sa = ssub.add_parser("add", help="let an agent spend up to an amount with one vendor without asking")
    sa.add_argument("--agent", required=True)
    sa.add_argument("--vendor", required=True, help="as in reports: openai, invoice:Acme, x402:0x...")
    sa.add_argument("--up-to-usd", required=True, help="most for one call or payment")
    sa.add_argument("--monthly-usd", required=True, help="most a month, counting everything to that vendor")
    sa.add_argument("--by", required=True, help="who agrees to it")
    sa.add_argument("--days", type=int, default=90, help="it ends after this many days (at most 366)")
    ssub.add_parser("list", help="standing approvals in force").add_argument("--all", action="store_true")
    sr = ssub.add_parser("revoke", help="end one now")
    sr.add_argument("id")
    sr.add_argument("--by", required=True)

    bsub = command("bill", run_bill, "bills paid from your own account, with invoice checks").add_subparsers(
        dest="bill_command", required=True)
    ba = bsub.add_parser("add", help="enter a bill")
    caps(ba)
    ba.add_argument("--payee", required=True)
    ba.add_argument("--invoice", required=True, help="the invoice number")
    ba.add_argument("--amount-usd", required=True)
    ba.add_argument("--account", required=True,
                    help="the account it is paid to: a Mercury recipient id, or the bank details on the invoice")
    ba.add_argument("--due", help="YYYY-MM-DD")
    ba.add_argument("--memo", default="")
    ba.add_argument("--agent", default="accounts-payable", help="who entered it, for caps and reports")
    ba.add_argument("--customer")
    bl = bsub.add_parser("list", help="bills and their status")
    caps(bl)
    bl.add_argument("--status", choices=("waiting", "approved", "refused", "denied", "paying", "paid"))
    bp = bsub.add_parser("pay", help="pay approved bills")
    caps(bp)
    bp.add_argument("ids", nargs="*")
    bp.add_argument("--due-by", help="also every bill due on or before YYYY-MM-DD")
    bp.add_argument("--by", required=True, help="who is paying (not who approved)")
    bp.add_argument("--method", choices=("mercury", "manual"), default="mercury")
    bp.add_argument("--ref", help="for manual: the bank's reference for the payment you made")
    bp.add_argument("--payment-method", choices=bills.MERCURY_METHODS, default="ach")
    bt = bsub.add_parser("payee-tax", help="record a payee's W-9 (W-8 if foreign); asks for the TIN")
    caps(bt)
    bt.add_argument("payee")
    bt.add_argument("--form-date", required=True, help="YYYY-MM-DD the W-9 or W-8 was received")
    bt.add_argument("--entity", required=True, choices=tax.ENTITIES)
    bt.add_argument("--pays-for", choices=tax.PAYS_FOR,
                    help="what you pay them for; for a corporation, attorney or medical is reported")
    bt.add_argument("--by", help="who recorded it")
    tx = command("tax-1099", run_tax_1099, "payees paid in a year and whether each needs a 1099")
    tx.add_argument("--year", type=int, required=True)
    tx.add_argument("--csv", help="also write the rows to this CSV file")

    ksub = command("cost", run_cost, "record what is spent on agents").add_subparsers(
        dest="cost_command", required=True)
    ka = ksub.add_parser("add")
    ka.add_argument("--category", required=True, help="e.g. seat, subscription, compute, people")
    ka.add_argument("--vendor", required=True)
    ka.add_argument("--amount-usd", required=True)
    month(ka, "YYYY-MM it starts")
    ka.add_argument("--monthly", action="store_true", help="repeats every month until ended")
    ka.add_argument("--agent")
    ka.add_argument("--customer")
    ke = ksub.add_parser("end")
    ke.add_argument("id", type=int)
    ke.add_argument("--last-month", required=True, help="YYYY-MM it last counts in")
    month(ksub.add_parser("list"), None)

    caps(command("forecast", run_forecast, "where this month will land, and caps it will reach"))

    command("receipt", run_receipt, "signed proof of a decided approval").add_argument("id")
    command("verify", run_verify, "check a receipt's signature and print it").add_argument("token")
    command("mandate-keys", run_mandate_keys, "the public keys sellers check spend mandates against")
    vm = command("verify-mandate", run_verify_mandate, "check a spend mandate as a seller would")
    vm.add_argument("token")
    vm.add_argument("--keys", help="a key set file; otherwise fetched from the issuer's domain")
    vm.add_argument("--payee")
    vm.add_argument("--amount-usd")

    ap = command("audit-pack", run_audit_pack, "zip of everything an auditor asks for, for a month")
    month(ap)
    ap.add_argument("--out", required=True, help="path of the zip to write")
    caps(ap)
    rules(ap)
    uw = command("underwriting", run_underwriting,
                 "a month's agent risk as counts an insurer can rate on, signed if a key is set")
    month(uw)
    uw.add_argument("--out", help="file to write; prints to stdout without it")
    caps(uw)
    rules(uw)
    ic = command("incident", run_incident,
                 "the record around a loss, with the chain on to a head an insurer holds")
    ic.add_argument("--from", dest="start", required=True, help="date or ISO time, UTC if no zone")
    ic.add_argument("--to", dest="end", required=True, help="date or ISO time, UTC if no zone")
    ic.add_argument("--agent")
    ic.add_argument("--task")
    ic.add_argument("--out", required=True, help="path of the JSON to write")
    ci = command("check-incident", run_check_incident,
                 "recompute an incident pack against underwriting statements already held")
    ci.add_argument("pack")
    ci.add_argument("--statement", action="append", default=[],
                    help="an underwriting statement file; repeat for each month held")
    command("verify-chain", lambda a, ledger: check_chain(ledger), "recompute the ledger's hash chains")
    scsub = command("sanctions", run_sanctions, "the OFAC sanctions lists every payee is screened against"
                    ).add_subparsers(dest="sanctions_command", required=True)
    scsub.add_parser("update", help="download the SDN and Consolidated lists from OFAC and keep them")
    scsub.add_parser("check", help="screen one name or wallet address, as a payment would be"
                     ).add_argument("name")
    scsub.add_parser("cleared", help="payees a person cleared of a close match, and by whom")
    scsub.add_parser("forget", help="hold a cleared payee for a person again").add_argument("name")
    an = command("anchor", run_anchor,
                 "record both chain heads in the audit log and a JSON file to keep elsewhere")
    an.add_argument("--out", help="path of the anchor JSON to write")
    an.add_argument("--submit", nargs="?", const=anchor.CALENDAR,
                    default=os.environ.get("QUARTER_ANCHOR_CALENDAR"),
                    metavar="CALENDAR", help="also send the digest, and nothing else, to an OpenTimestamps"
                    f" calendar (default {anchor.CALENDAR}); off unless given or QUARTER_ANCHOR_CALENDAR is set")
    av = an.add_subparsers(dest="anchor_command").add_parser(
        "verify", help="check, offline, that a ledger, backup, audit pack or incident pack holds an anchor's heads")
    av.add_argument("file", help="the ledger or backup (.db), audit pack (.zip) or incident pack (.json)")
    av.add_argument("--anchor", required=True, help="the anchor JSON")
    fo = command("ingest-fail-open", run_ingest_fail_open,
                 "book the calls QUARTER_FAIL_OPEN let through while the ledger was down")
    fo.add_argument("--file", help="the fallback file; default QUARTER_FAIL_OPEN_LOG or <db>.fail-open.jsonl")
    command("backup", run_backup, "copy the ledger while the gateway runs, then check the copy"
            ).add_argument("path", help="file to write; never overwritten")

    wsub = command("wallets", run_wallets,
                   "the company's own agent wallets: reconcile on chain, export a wallet policy"
                   ).add_subparsers(dest="wallets_command", required=True)
    wc = wsub.add_parser("reconcile", help="match a month of USDC transfers on Base to x402 settlements")
    month(wc)
    wc.add_argument("--wallets", required=True, help='wallets.json: {"0x... address": "agent name"}')
    wc.add_argument("--rpc", default=os.environ.get("QUARTER_BASE_RPC", wallets.PUBLIC_RPC),
                    help="a Base JSON-RPC endpoint (default the public one, or QUARTER_BASE_RPC)")
    wp = wsub.add_parser("policy", help="a wallet policy that refuses what Quarter would refuse")
    wp.add_argument("--format", choices=("cdp", "privy"), default="cdp")
    wp.add_argument("--agent", help="an account policy for this agent's wallet, with its own caps")
    caps(wp)

    csub = command("cards", run_cards, "the company's own agent cards on Mercury or Ramp: reconcile"
                   ).add_subparsers(dest="cards_command", required=True)
    cr = csub.add_parser("reconcile", help="match a month of agent card charges to Quarter card authorisations")
    month(cr)
    cr.add_argument("--source", required=True, choices=cards.SOURCES)
    cr.add_argument("--cards", required=True, help='agent-cards.json: {"card id": "agent name"}')
    cr.add_argument("--settle", action="store_true",
                    help="record matched charges whose authorisation is still open as settlements")

    command("why", run_why, "where one task's money went, and each cache break").add_argument(
        "task", help="the task id: x-quarter-task, or Claude Code's session id")
    sh = command("shadow-report", run_shadow_report, "what rules in shadow mode would have held")
    sh.add_argument("--since", required=True, help="YYYY-MM-DD, or an ISO time (UTC if no zone)")
    sh.add_argument("--csv", action="store_true")
    rp = command("replay", run_replay, "a proposed rule set run over recorded calls, against the live one")
    rp.add_argument("--caps", required=True, help="the proposed caps, JSON text or a path")
    rp.add_argument("--payees", help="the proposed payee lists; the live ones (QUARTER_PAYEES) without it")
    rp.add_argument("--live-caps", default=os.environ.get("QUARTER_CAPS"), help="default QUARTER_CAPS")
    rp.add_argument("--days", type=int, default=30)
    rp.add_argument("--csv", help="also write each call's decisions to this CSV file")
    command("paused", run_paused, "agents and tasks paused by a loop stop")
    rs = command("resume", run_resume, "let a paused agent, or with --task a task, spend again")
    rs.add_argument("name", help="the agent, or the task with --task")
    rs.add_argument("--task", action="store_true", help="resume a paused task")

    asub = command("approvals", run_approvals, "calls waiting for approval, and the approval record"
                   ).add_subparsers(dest="approvals_command")
    asub.add_parser("import", help="add approval events from another source, one JSON object a line"
                    ).add_argument("file", help="JSON lines: source, id, decision, at, and optionally"
                                   " requester, approver, action, ref, amount_usd, reason")
    ax = asub.add_parser("export", help="every approval from every source, with chain hashes, for an auditor")
    ax.add_argument("--out", required=True, help="path of the zip to write")
    ax.add_argument("--month", help="YYYY-MM; every approval without it")
    asub.add_parser("verify", help="recompute the chain hashes of an approval export or audit pack from the file alone"
                    ).add_argument("file")
    for name in ("approve", "deny"):
        a = command(name, run_decide)
        a.add_argument("id")
        a.add_argument("--as", dest="by", default=getpass.getuser(),
                       help="your name, recorded as the approver (default: your login name)")
    ow = command("owner", lambda a, ledger: ledger.set_owner(a.agent, a.owner),
                 "name the person responsible for an agent")
    ow.add_argument("agent")
    ow.add_argument("owner")
    gsub = command("agents", run_agents, "the agent register and the quarterly owner review").add_subparsers(
        dest="agents_command", required=True)
    gl = gsub.add_parser("list", help="every agent: owner, caps, payees, last call and payment, review")
    caps(gl)
    gl.add_argument("--csv", action="store_true")
    gr = gsub.add_parser("review", help="confirm an agent for a quarter, or retire it")
    gr.add_argument("--quarter", required=True, help="YYYY-Qn")
    gr.add_argument("--agent", required=True)
    gr.add_argument("--by", required=True, help="the owner, or an approver")
    keep = gr.add_mutually_exclusive_group(required=True)
    keep.add_argument("--keep", action="store_true", help="still needed and rightly owned")
    keep.add_argument("--retire", action="store_true", help="no longer needed: pause it for good")
    gr.add_argument("--note", default="")

    e = command("export", run_export, "month-end journal CSV, or FOCUS cost and usage rows")
    e.add_argument("--month", help="YYYY-MM; needed for every format but focus")
    e.add_argument("--expense-account", default="AI vendor costs")
    e.add_argument("--offset-account", default="Accrued AI vendor costs")
    e.add_argument("--revenue-cost-account", default="Cost of revenue: AI",
                   help="account for spend that served a customer")
    e.add_argument("--format", choices=(*FORMATS, "focus"), default="csv",
                   help="lay the file out for that system's journal import; focus: one FOCUS 1.4 row"
                   " per charge")
    e.add_argument("--date-format", help="strftime format, if the system expects another (default"
                   " MM/DD/YYYY for the three systems, YYYY-MM-DD for csv)")
    e.add_argument("--customer-names", action="store_true",
                   help="put the customer in the Name column; it must match a customer there")
    e.add_argument("--tax-rate", default="Tax Exempt", help="Xero tax rate for every line")
    e.add_argument("--cost-centres", help="centres.json: each agent's department, class or location;"
                   " splits the expense lines by them")
    e.add_argument("--unbilled", action="store_true",
                   help="only spend the vendors have not billed yet, as an accrual reversed on the 1st")
    e.add_argument("--billed", action="append", default=[], metavar="VENDOR=FILE",
                   help="with --unbilled: the vendor's bill for the month, as reconcile --file reads it")
    e.add_argument("--accrued-account", default="Accrued liabilities",
                   help="with --unbilled: the account credited")
    e.add_argument("--from", dest="since", help="focus: from this date or ISO time (UTC if no zone)")
    e.add_argument("--to", dest="until", help="focus: up to, not including, this date or time")
    e.add_argument("--account", action="append", default=[], metavar="VENDOR=ID",
                   help="focus: the vendor's billing account id (default: the vendor name)")
    e.add_argument("--csv", help="focus: write the rows to this file instead of standard output")
    return top


def package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("quarter-ledger")
    except PackageNotFoundError:
        return "unknown"


def load_env_file(argv: list[str]) -> None:
    """--env-file, read before the parser, whose defaults come from the environment. KEY=VALUE
    lines; a value may be wrapped in single or double quotes; # starts a comment line. A variable
    already set in the environment wins, as with uv and docker."""
    for i, arg in enumerate(argv):
        if arg == "--env-file" and i + 1 < len(argv):
            path = argv[i + 1]
        elif arg.startswith("--env-file="):
            path = arg.split("=", 1)[1]
        else:
            continue
        try:
            lines = Path(path).read_text().splitlines()
        except FileNotFoundError:
            raise SystemExit(f"env file not found: {path}") from None
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.removeprefix("export ").partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            os.environ.setdefault(name.strip(), value)
        return


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    load_env_file(argv)
    # Python's default user agent is refused by some sites (Cloudflare's Browser Integrity Check)
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", f"quarter-ledger/{package_version()}")]
    urllib.request.install_opener(opener)
    args = parser().parse_args(argv)
    if args.command == "verify-chain" and not os.path.exists(args.db):
        sys.exit(f"no ledger at {args.db}")  # opening it would make an empty one, which checks out
    if args.command == "demo":  # it makes its own folder and ledger
        return args.run(args)
    ledger = Ledger(args.db)
    ledger.free_decisions = free_decisions(os.environ.get("QUARTER_FREE_DECISIONS"))
    args.run(args, ledger)
