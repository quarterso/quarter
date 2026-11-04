"""Write compose.yaml, Caddyfile, litestream.yml and cloudflared.yml for the hosted box.

    python3 deploy/hosted/render.py deploy/hosted/tenants.json <out dir>

Secrets are never written here: each tenant reads env/<name>.env, Litestream reads
env/litestream.env, and cloudflared reads cloudflared/credentials.json, all placed by hand.
See docs/HOSTED.md.
"""

import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).parent
LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
TUNNEL_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MEMORY = re.compile(r"[1-9][0-9]*[mg]")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
RESERVED = {"caddy", "cloudflared", "litestream", "www"}  # services, and the website
DEFAULT_MEMORY = "96m"
MAX_WAIT = 110  # under Cloudflare's 125-second proxy read timeout
CADDY = "caddy:2.11.7@sha256:36c24ad91a7ff7bc4c87fe22bfa8df3c4b18da0773d73fc12ee2c52b98c0bb7e"
CLOUDFLARED = ("cloudflare/cloudflared:2026.10.7"
               "@sha256:4955524aeaa379e154023d5a027c250f7b35041a2e7e950cf13f144bc9b9c5ba")
LITESTREAM = ("litestream/litestream:0.5.17"
              "@sha256:4b02b9859a6b6b4087d8b8944e15f7e984bd7957cba322bbeee38b0e27b9656a")
# The gateway takes plain http only on localhost, so the mock shares the sandbox's network.
MOCK_URL = "http://127.0.0.1:8790"
# The sandbox talks only to its mock, so no vendor key is on the box, and its caps stay small.
SANDBOX_ENV = {
    "QUARTER_UPSTREAMS": json.dumps({"openai": MOCK_URL, "anthropic": MOCK_URL}),
    "QUARTER_VENDOR_KEYS": json.dumps({"openai": "sandbox", "anthropic": "sandbox"}),
    "QUARTER_CAPS": json.dumps([
        {"scope": "agent", "key": "*", "window": "day", "limit_usd": "1", "approve_above_usd": "0.05"},
        {"scope": "all", "window": "day", "limit_usd": "5"},
    ]),
}


def load(text: str) -> dict:
    """tenants.json, checked: a ValueError names the first thing wrong."""
    box = json.loads(text)
    domain, tunnel = box.get("domain", ""), box.get("tunnel", "")
    if not all(LABEL.fullmatch(part) for part in domain.split(".")) or "." not in domain:
        raise ValueError(f"domain must be a lowercase domain name, not {domain!r}")
    if not TUNNEL_ID.fullmatch(tunnel):
        raise ValueError(f"tunnel must be the tunnel's id (a lowercase UUID), not {tunnel!r}")
    if box.setdefault("ipv6_only", False) not in (True, False):
        raise ValueError("ipv6_only must be true or false")
    tenants, seen = box.get("tenants") or [], set()
    if not tenants:
        raise ValueError("tenants must list at least one tenant")
    for t in tenants:
        name = t.get("name", "")
        if not LABEL.fullmatch(name):
            raise ValueError(f"tenant name {name!r} must be a DNS label: a-z, 0-9 and -, at most 63")
        if name in RESERVED or name.endswith("-mock"):
            raise ValueError(f"tenant name {name!r} is reserved")
        if name in seen:
            raise ValueError(f"tenant name {name!r} is used twice")
        seen.add(name)
        if not TAG.fullmatch(t.get("tag", "")):
            raise ValueError(f"{name}: tag must be the quarter-ledger image tag")
        if not MEMORY.fullmatch(t.setdefault("memory", DEFAULT_MEMORY)):
            raise ValueError(f"{name}: memory must look like 96m or 1g, not {t['memory']!r}")
        if t.setdefault("sandbox", False) not in (True, False):
            raise ValueError(f"{name}: sandbox must be true or false")
    return box


def q(value: str) -> str:
    """A YAML string: JSON's double-quoted strings are valid YAML."""
    return json.dumps(value)


def compose(box: dict) -> str:
    sandbox = any(t["sandbox"] for t in box["tenants"])
    # Each tenant on its own network, which only Caddy joins: no tenant reaches another directly.
    own = {t["name"]: "sandbox" if t["sandbox"] else f"t-{t['name']}" for t in box["tenants"]}
    out = ["name: quarter", "", "services:"]
    out += [
        "  cloudflared:",
        f"    image: {CLOUDFLARED}",
        "    restart: unless-stopped",
        "    command: [tunnel, --no-autoupdate, --config, /etc/cloudflared/config.yml, run]",
        "    volumes:",
        "      - ./cloudflared.yml:/etc/cloudflared/config.yml:ro",
        "      - ./cloudflared/credentials.json:/etc/cloudflared/credentials.json:ro",
        "    networks: [edge]",
        "    depends_on: [caddy]",
        "  caddy:",
        f"    image: {CADDY}",
        "    restart: unless-stopped",
        "    volumes: [./Caddyfile:/etc/caddy/Caddyfile:ro]",
        f"    networks: [edge, {', '.join(own.values())}]",
    ]
    for t in box["tenants"]:
        name = t["name"]
        environment = {"QUARTER_MAX_WAIT": str(MAX_WAIT), "QUARTER_CLIENT_IP_HEADER": "cf-connecting-ip",
                       "QUARTER_DASHBOARD_URL": f"https://{name}.{box['domain']}"}
        environment |= SANDBOX_ENV if t["sandbox"] else {}
        out += [
            f"  {name}:",
            f"    image: quarter-ledger:{t['tag']}",
            "    restart: unless-stopped",
            f"    env_file: env/{name}.env",
            "    environment:",
            *(f"      {k}: {q(v)}" for k, v in environment.items()),
            f"    volumes: [{name}:/data]",
            f"    mem_limit: {t['memory']}",
            f"    networks: [{own[name]}]",
        ]
        if t["sandbox"]:
            out += [
                f"  {name}-mock:",
                f"    image: quarter-ledger:{t['tag']}",
                "    restart: unless-stopped",
                "    init: true",
                "    entrypoint: [python, /mock/mock_upstream.py]",
                '    environment: {PORT: "8790"}',
                "    volumes: [./mock_upstream.py:/mock/mock_upstream.py:ro]",
                "    mem_limit: 32m",
                f'    network_mode: "service:{name}"',
            ]
    backed_up = [t["name"] for t in box["tenants"] if not t["sandbox"]]
    if backed_up:  # started after the tenants, so each volume is first made by its own ledger
        out += [
            "  litestream:",
            f"    image: {LITESTREAM}",
            "    restart: unless-stopped",
            "    user: \"10001\"",  # the ledger's user: the databases are owner only
            "    command: [replicate, -config, /etc/litestream.yml]",
            "    env_file: env/litestream.env",
            "    volumes:",
            "      - ./litestream.yml:/etc/litestream.yml:ro",
            *(f"      - {name}:/data/{name}" for name in backed_up),
            f"    depends_on: [{', '.join(backed_up)}]",
            "    networks: [backup]",
            ]
    out += ["", "networks:"]
    for net in ["edge", "backup", *(n for n in own.values() if n != "sandbox")]:
        out.append(f"  {net}:")
        if box["ipv6_only"]:
            out.append("    enable_ipv6: true")
    if sandbox:
        out += ["  sandbox:", "    internal: true"]  # no way out: only Caddy reaches in
    out += ["", "volumes:", *(f"  {t['name']}:" for t in box["tenants"])]
    return "\n".join(out) + "\n"


def caddyfile(box: dict) -> str:
    out = ["# TLS ends at Cloudflare; the tunnel reaches Caddy over plain HTTP.",
           "{", "\tauto_https off", "\tadmin off", "}", ""]
    for t in box["tenants"]:
        out += [f"http://{t['name']}.{box['domain']} {{", f"\treverse_proxy {t['name']}:8780", "}", ""]
    out += ["http:// {", "\trespond 404", "}"]
    return "\n".join(out) + "\n"


def litestream(box: dict) -> str:
    out = [
        "# Bucket and keys come from env/litestream.env.",
        "access-key-id: ${LITESTREAM_ACCESS_KEY_ID}",
        "secret-access-key: ${LITESTREAM_SECRET_ACCESS_KEY}",
        "dbs:",
    ]
    for t in box["tenants"]:
        if not t["sandbox"]:
            out += [
                f"  - path: /data/{t['name']}/ledger.db",
                "    replica:",
                "      type: s3",
                "      bucket: ${LITESTREAM_BUCKET}",
                f"      path: {t['name']}",
                "      endpoint: ${LITESTREAM_ENDPOINT}",
                "      region: ${LITESTREAM_REGION}",
                "      sync-interval: 10s",  # R2 bills each upload; see docs/HOSTED.md
            ]
    return "\n".join(out) + "\n"


def cloudflared(box: dict) -> str:
    out = [f"tunnel: {box['tunnel']}", "credentials-file: /etc/cloudflared/credentials.json"]
    if box["ipv6_only"]:
        out.append('edge-ip-version: "6"')
    out += ["ingress:", f'  - hostname: "*.{box["domain"]}"', "    service: http://caddy:80",
            "  - service: http_status:404"]
    return "\n".join(out) + "\n"


def render(box: dict, out: Path) -> list[str]:
    files = {"compose.yaml": compose(box), "Caddyfile": caddyfile(box),
             "litestream.yml": litestream(box), "cloudflared.yml": cloudflared(box)}
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / name).write_text(text)
    if any(t["sandbox"] for t in box["tenants"]):
        shutil.copy(HERE / "mock_upstream.py", out / "mock_upstream.py")
        files["mock_upstream.py"] = ""
    return sorted(files)


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        sys.exit("usage: render.py <tenants.json> <out dir>")
    try:
        box = load(Path(argv[0]).read_text())
    except (OSError, ValueError) as e:  # json.JSONDecodeError is a ValueError
        sys.exit(f"{argv[0]}: {e}")
    for name in render(box, Path(argv[1])):
        print(Path(argv[1]) / name)


if __name__ == "__main__":
    main(sys.argv[1:])
