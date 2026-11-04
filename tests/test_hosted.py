"""The hosted kit: deploy/hosted/render.py and the sandbox's mock upstream."""

import importlib.util
import json
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from test_proxy import ADMIN_KEY, AGENT_KEY, start

from quarter_ledger import mock_upstream as mock
from quarter_ledger.pricing import PriceBook, with_built_in
from quarter_ledger.proxy import Gateway, serve
from quarter_ledger.store import Ledger

KIT = Path(__file__).parent.parent / "deploy" / "hosted"


def load(name):
    spec = importlib.util.spec_from_file_location(name, KIT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render = load("render")
TUNNEL = "6f1c2b8e-0d3a-4c5b-9e7f-1a2b3c4d5e6f"


def box(*tenants, **top):
    return json.dumps({"domain": "quarterfi.com", "tunnel": TUNNEL, "tenants": list(tenants)} | top)


def test_example_renders_every_file(tmp_path):
    loaded = render.load((KIT / "tenants.example.json").read_text())
    written = render.render(loaded, tmp_path)
    recipient = ["backup-recipient.txt"] if (KIT / "backup-recipient.txt").exists() else []
    assert written == sorted(["caddy/Caddyfile", "nightly_backup.py", "cloudflared.yml", "compose.yaml",
                              "litestream.yml", *recipient])
    compose = (tmp_path / "compose.yaml").read_text()
    assert "env_file: env/acme.env" in compose and "mem_limit: 96m" in compose
    assert 'QUARTER_MAX_WAIT: "110"' in compose and "enable_ipv6: true" in compose
    assert 'network_mode: "service:sandbox-mock"' in compose and "internal: true" in compose


def test_no_tenant_shares_a_network_with_another():
    loaded = render.load(box({"name": "acme", "tag": "t"}, {"name": "beta", "tag": "t"}))
    services, name = {}, None
    for line in render.compose(loaded).splitlines():
        if line.startswith("  ") and not line.startswith("   ") and line.endswith(":"):
            name = line.strip(" :")
        elif line.startswith("    networks: ["):
            services[name] = set(line.split("[")[1].rstrip("]").split(", "))
    assert services["acme"] == {"t-acme"} and services["beta"] == {"t-beta"}
    assert services["caddy"] == {"edge", "t-acme", "t-beta"}
    assert not services["litestream"] & {"t-acme", "t-beta"}
    assert not services["cloudflared"] & {"t-acme", "t-beta"}
    assert 'QUARTER_CLIENT_IP_HEADER: "cf-connecting-ip"' in render.compose(loaded)


def test_each_tenant_gets_its_own_host_volume_and_limit():
    loaded = render.load(box({"name": "acme", "tag": "v1"}, {"name": "beta", "tag": "v2", "memory": "128m"}))
    compose, caddy = render.compose(loaded), render.caddyfile(loaded)
    assert "image: quarter-ledger:v2" in compose and "mem_limit: 128m" in compose
    assert "volumes: [beta:/data]" in compose and "      - beta:/data/beta" in compose
    assert "http://beta.quarterfi.com {\n\treverse_proxy beta:8780\n}" in caddy
    assert caddy.endswith("http:// {\n\trespond 404\n}\n")  # unknown hosts
    assert "sandbox" not in compose and "enable_ipv6" not in compose
    assert "edge-ip-version" not in render.cloudflared(loaded)


def test_the_sandbox_is_not_backed_up_and_reaches_only_its_mock():
    loaded = render.load(box({"name": "sandbox", "tag": "v1", "sandbox": True}, {"name": "acme", "tag": "v1"}))
    backups = render.litestream(loaded)
    assert "/data/acme/ledger.db" in backups and "sandbox" not in backups
    assert backups.count("monitor-interval: 1m") == 1 and "l0-retention-check-interval: 5m" in backups
    compose = render.compose(loaded)
    upstreams = json.loads(json.loads(compose.split("QUARTER_UPSTREAMS: ")[1].split("\n")[0]))
    assert upstreams == {"openai": "http://127.0.0.1:8790", "anthropic": "http://127.0.0.1:8790"}
    assert "      - sandbox:/data/sandbox" not in compose


def test_only_sandboxes_means_no_litestream():
    loaded = render.load(box({"name": "sandbox", "tag": "v1", "sandbox": True}))
    assert "litestream" not in render.compose(loaded)


@pytest.mark.parametrize("tenants, error", [
    ([{"name": "Acme", "tag": "v1"}], "DNS label"),
    ([{"name": "acme-", "tag": "v1"}], "DNS label"),
    ([{"name": "a" * 64, "tag": "v1"}], "DNS label"),
    ([{"name": "acme", "tag": "v1"}, {"name": "acme", "tag": "v1"}], "used twice"),
    ([{"name": "caddy", "tag": "v1"}], "reserved"),
    ([{"name": "demo-mock", "tag": "v1"}], "reserved"),
    ([{"name": "acme"}], "tag"),
    ([{"name": "acme", "tag": "v1", "memory": "lots"}], "memory"),
    ([{"name": "acme", "tag": "v1", "sandbox": "yes"}], "sandbox"),
    ([], "at least one"),
])
def test_bad_tenants_are_refused(tenants, error):
    with pytest.raises(ValueError, match=error):
        render.load(box(*tenants))


def test_bad_domain_or_tunnel_is_refused():
    with pytest.raises(ValueError, match="domain"):
        render.load(box({"name": "a", "tag": "v1"}, domain="Quarterfi.com"))
    with pytest.raises(ValueError, match="tunnel"):
        render.load(box({"name": "a", "tag": "v1"}, tunnel="<tunnel id>"))


def test_main_names_the_file_and_the_problem(tmp_path, capsys):
    bad = tmp_path / "tenants.json"
    bad.write_text(box({"name": "A", "tag": "v1"}))
    with pytest.raises(SystemExit, match="tenants.json: tenant name 'A'"):
        render.main([str(bad), str(tmp_path / "out")])
    assert not (tmp_path / "out").exists()


@pytest.fixture
def sandbox(tmp_path):
    upstream = start(ThreadingHTTPServer(("127.0.0.1", 0), mock.Handler))
    base = f"http://127.0.0.1:{upstream.server_port}"
    ledger = Ledger(str(tmp_path / "ledger.db"))
    gw = Gateway(ledger, [], PriceBook.load(with_built_in(None)), {"openai": base, "anthropic": base},
                 {AGENT_KEY}, ADMIN_KEY, vendor_keys={"openai": "sandbox", "anthropic": "sandbox"})
    proxy = start(serve(gw, port=0))
    yield f"http://127.0.0.1:{proxy.server_port}", base, ledger
    proxy.shutdown()
    upstream.shutdown()


def post(url, body, headers):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=5) as reply:
        return reply.headers["content-type"], reply.read()


def test_mock_answers_openai_and_anthropic_with_usage(sandbox):
    _, base, _ = sandbox
    _, body = post(base + "/v1/chat/completions", {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "x" * 400}]}, {})
    reply = json.loads(body)
    assert reply["usage"]["prompt_tokens"] > 100 and reply["usage"]["completion_tokens"] > 0
    assert reply["choices"][0]["message"]["content"] == mock.REPLY
    _, body = post(base + "/v1/messages", {"model": "claude-haiku-4-5", "messages": []}, {})
    assert json.loads(body)["usage"]["output_tokens"] == mock.tokens(mock.REPLY)
    kind, body = post(base + "/v1/messages", {"model": "m", "stream": True, "messages": []}, {})
    assert kind == "text/event-stream" and b"event: message_stop" in body
    with pytest.raises(urllib.error.HTTPError) as other:
        post(base + "/v1/embeddings", {}, {})
    assert other.value.code == 404


def test_sandbox_calls_through_the_gateway_are_priced(sandbox):
    url, _, ledger = sandbox
    request = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]}
    post(url + "/openai/v1/chat/completions", request, {"authorization": f"Bearer {AGENT_KEY}"})
    stream = {"model": "claude-haiku-4-5", "max_tokens": 50, "stream": True, "messages": []}
    kind, _ = post(url + "/anthropic/v1/messages", stream, {"x-api-key": AGENT_KEY, "anthropic-version": "2023-06-01"})
    assert kind.startswith("text/event-stream")
    charges = ledger.db.execute("select amount from entries where kind = 'charge'").fetchall()
    assert len(charges) == 2 and all(amount > 0 for (amount,) in charges)


def test_sandbox_lists_models_and_relays_a_request_id_through_the_gateway(sandbox):
    url, _, _ = sandbox
    for headers, field in (({}, "object"), ({"anthropic-version": "2023-06-01"}, "has_more")):
        req = urllib.request.Request(url + "/openai/v1/models", headers={"x-quarter-key": AGENT_KEY, **headers})
        with urllib.request.urlopen(req, timeout=5) as reply:
            body = json.loads(reply.read())
            assert reply.headers["x-request-id"] == "req_sandbox"  # the SDKs read it from the vendor
        assert field in body and "gpt-4o-mini" in [m["id"] for m in body["data"]]


def test_secrets_backup_signs_s3_requests_like_aws(monkeypatch):
    # AWS's published Signature Version 4 example key, so the signature is checkable by hand.
    import datetime
    import importlib.util
    spec = importlib.util.spec_from_file_location("nightly_backup", KIT / "nightly_backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen = {}

    class Resp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(req, timeout):
        seen["url"], seen["headers"], seen["method"] = req.full_url, dict(req.header_items()), req.get_method()
        return Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake)
    env = {"LITESTREAM_ENDPOINT": "https://ns.compat.objectstorage.us-phoenix-1.oraclecloud.com",
           "LITESTREAM_REGION": "us-phoenix-1", "LITESTREAM_BUCKET": "b",
           "LITESTREAM_ACCESS_KEY_ID": "AKIDEXAMPLE", "LITESTREAM_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"}
    now = datetime.datetime(2026, 11, 3, 12, 0, tzinfo=datetime.UTC)
    assert mod.s3("PUT", env, "secrets/2026-11-03.tar.age", b"sealed", now) == 200
    assert seen["method"] == "PUT"
    assert seen["url"] == "https://ns.compat.objectstorage.us-phoenix-1.oraclecloud.com/b/secrets/2026-11-03.tar.age"
    auth = seen["headers"]["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261103/us-phoenix-1/s3/aws4_request, "
                           "SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=")
    assert "wJalr" not in auth


def test_secrets_archive_leaves_out_the_databases(tmp_path, monkeypatch):
    import importlib.util
    import io
    import tarfile
    spec = importlib.util.spec_from_file_location("nightly_backup", KIT / "nightly_backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "acme.env").write_text("QUARTER_ADMIN_KEY=x\n")
    vol = tmp_path / "vol"
    (vol / ".ledger.db-litestream").mkdir(parents=True)
    for name in ["ledger.db", "ledger.db-wal", "ledger.db-shm", "ledger.signing-key"]:
        (vol / name).write_text("x")
    monkeypatch.setattr(mod, "volumes", lambda: iter([("acme", vol)]))
    monkeypatch.setattr(mod, "snapshot", lambda root, tenant: (vol / mod.COPY).write_text("checked copy"))
    with tarfile.open(fileobj=io.BytesIO(mod.archive(tmp_path))) as tar:
        names = tar.getnames()
    assert names == ["env/acme.env", "volumes/acme/ledger.signing-key", "volumes/acme/nightly-backup.db"]
    assert not (vol / mod.COPY).exists()  # the copy never stays on the box


def test_nightly_pings_fail_when_an_upload_fails(tmp_path, monkeypatch):
    import importlib.util
    import io
    spec = importlib.util.spec_from_file_location("nightly_backup", KIT / "nightly_backup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (tmp_path / "env").mkdir()
    (tmp_path / "env" / "litestream.env").write_text("LITESTREAM_BUCKET=a\n")
    (tmp_path / "env" / "b2.env").write_text("LITESTREAM_BUCKET=b\n")
    (tmp_path / "env" / "healthchecks.env").write_text("HC_BACKUP_URL=https://hc-ping.example/u\n")
    pinged, put = [], []
    monkeypatch.setattr(mod, "archive", lambda root: b"tar")
    monkeypatch.setattr(mod, "encrypt", lambda data, r: b"sealed")
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda url, timeout: pinged.append(url) or io.BytesIO())

    def s3(method, env, key, body=b""):
        if method == "PUT":
            put.append(env["LITESTREAM_BUCKET"])
            if env["LITESTREAM_BUCKET"] == "b":
                raise OSError("b2 down")
    monkeypatch.setattr(mod, "s3", s3)
    monkeypatch.setattr(mod.sys, "argv", ["x", str(tmp_path)])
    try:
        mod.main()
    except OSError:
        pass
    assert put == ["a", "b"] and pinged == ["https://hc-ping.example/u/fail"]


def test_free_plan_counts_decisions_and_paid_does_not():
    loaded = render.load(box({"name": "acme", "tag": "t"}, {"name": "beta", "tag": "t", "plan": "paid"}))
    compose = render.compose(loaded)
    acme, beta = compose.split("  beta:")[0].split("  acme:")[1], compose.split("  beta:")[1]
    assert f'QUARTER_FREE_DECISIONS: "{render.FREE_DECISIONS}"' in acme
    assert "QUARTER_FREE_DECISIONS" not in beta
    with pytest.raises(ValueError, match="plan must be"):
        render.load(box({"name": "acme", "tag": "t", "plan": "gold"}))


def test_the_litestream_check_fails_when_litestream_is_not_running(monkeypatch, tmp_path):
    import subprocess
    nightly_backup = load("nightly_backup")
    out = {"ps": "demo\ncaddy\n", "logs": "level=INFO msg=ok\n"}

    def run(cmd, **_):
        return subprocess.CompletedProcess(cmd, 0, out["ps" if "ps" in cmd else "logs"], "")

    monkeypatch.setattr(nightly_backup.subprocess, "run", run)
    assert not nightly_backup.litestream_ok(tmp_path)
    out["ps"] += "litestream\n"
    assert nightly_backup.litestream_ok(tmp_path)
    out["logs"] += "level=ERROR msg=403\n"
    assert not nightly_backup.litestream_ok(tmp_path)
