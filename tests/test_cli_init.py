import json
from unittest.mock import patch

import pytest

from quarter_ledger.cli import init
from quarter_ledger.policy import decide, load_caps


def test_init_writes_a_usable_config_once(tmp_path):
    message = init(tmp_path, "40", "3")
    [cap] = load_caps(json.loads((tmp_path / "caps.json").read_text()))
    assert cap.window == "month" and "$40" in message
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env", "caps.json"]
    env = (tmp_path / ".env").read_text()
    assert f"QUARTER_CAPS={tmp_path.resolve() / 'caps.json'}\n" in env
    assert "# OPENAI_API_KEY=\n" in env
    assert (tmp_path / ".env").stat().st_mode & 0o077 == 0
    with pytest.raises(SystemExit):
        init(tmp_path, "40", "3")


def test_serve_starts_with_no_flags(tmp_path, monkeypatch):
    from quarter_ledger import cli

    for name in ("CAPS", "PRICES", "UPSTREAMS", "VENDOR_KEYS"):
        monkeypatch.delenv(f"QUARTER_{name}", raising=False)
    monkeypatch.setenv("QUARTER_AGENT_KEYS", "bot=k1,k2")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    started, polls = [], []

    class Server:
        def serve_forever(self, poll_interval=0.5):
            polls.append(poll_interval)

    monkeypatch.setattr(cli, "serve", lambda gw, host, port: started.append(gw) or Server())
    cli.main(["--db", str(tmp_path / "l.db"), "serve"])
    [gw] = started
    assert set(gw.upstreams) == {"openai", "anthropic", "gemini", "openrouter", "chatgpt"}
    assert not gw.prices.price("anthropic", "claude-sonnet-4-5").is_flat
    assert gw.caps == [] and gw.agent_keys == {"k1", "k2"}
    assert gw.key_agents == {"k1": "bot"} and gw.vendor_keys["openai"] == "sk-real"
    assert polls == [3600]  # an idle gateway does not wake twice a second


def test_serve_makes_a_signing_key_so_payments_need_no_setup(tmp_path, monkeypatch, capsys):
    from quarter_ledger import cli
    from quarter_ledger.receipts import public_key, verify

    monkeypatch.delenv("QUARTER_SIGNING_KEY", raising=False)
    monkeypatch.delenv("QUARTER_CAPS", raising=False)
    started, polls = [], []

    class Server:
        def serve_forever(self, poll_interval=0.5):
            polls.append(poll_interval)

    monkeypatch.setattr(cli, "serve", lambda gw, host, port: started.append(gw) or Server())
    db = tmp_path / "l.db"
    cli.main(["--db", str(db), "serve"])
    key_file = tmp_path / "l.signing-key"
    key = key_file.read_text().strip()
    assert started[0].signing_key == key and key_file.stat().st_mode & 0o077 == 0
    assert f"payments are authorised with key {public_key(key)}" in capsys.readouterr().out
    cli.main(["--db", str(db), "serve"])  # kept, never replaced
    assert started[1].signing_key == key

    from quarter_ledger.store import Ledger

    ledger = Ledger(str(db))
    _, approval = ledger.hold(load_caps([{"scope": "all", "limit_usd": "1", "approve_above_usd": "0"}]),
                              {"agent": "bot"}, "openai", "m", 10)
    ledger.decide_approval(approval, True, "dana")
    cli.main(["--db", str(db), "receipt", approval])
    token = capsys.readouterr().out.strip().splitlines()[-1]
    assert verify(token, public_key(key))["sub"] == approval


def test_agent_keys_may_name_their_agent():
    from quarter_ledger.cli import agent_keys

    assert agent_keys(" bot=k1, k2 ,,") == {"k1": "bot", "k2": None}


def test_all_scope_caps_every_agent_together(tmp_path):
    from quarter_ledger.store import Ledger

    ledger = Ledger(str(tmp_path / "l.db"))
    caps = load_caps([{"scope": "all", "window": "month", "limit_usd": "1"}])
    for agent in ("a", "b"):
        _, call = ledger.hold(caps, {"agent": agent}, "openai", "m", 600_000)
        if call:
            ledger.settle(call, 600_000)
    assert ledger.spent("all", "all", None) == 600_000
    decision = decide(caps, {"agent": "a"}, 2_000_000, lambda *a: 0)
    assert decision.reason == "all spend: month cap reached"


def test_wrap_points_claude_code_at_the_gateway():
    from quarter_ledger.cli import wrap_env

    env = wrap_env({"QUARTER_AGENT_KEYS": "bot=k1,k2"}, "http://q:8780/", "me")
    assert env["ANTHROPIC_BASE_URL"] == "http://q:8780/anthropic"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "x-quarter-agent: me\nx-quarter-key: k2"
    assert env["CLAUDE_CODE_GATEWAY_HINT_HEADERS"] == "1"
    env = wrap_env({"QUARTER_AGENT_KEYS": "bot=k1,me=k3"}, "http://q:8780/", "me")
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "x-quarter-agent: me\nx-quarter-key: k3"
    env = wrap_env({"QUARTER_AGENT_KEYS": "bot=k1"}, "http://q:8780/", "me")
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "x-quarter-agent: bot\nx-quarter-key: k1"


def test_managed_settings_make_quarter_the_only_way_out_for_claude_code():
    from quarter_ledger.cli import managed_settings

    settings = managed_settings({"QUARTER_AGENT_KEYS": "claude-code=qa_k"}, "https://gw.internal:8780/", "claude-code")
    assert settings["allowedProviders"] == ["customEndpoint"]
    assert settings["env"] == {
        "ANTHROPIC_BASE_URL": "https://gw.internal:8780/anthropic",
        "ANTHROPIC_CUSTOM_HEADERS": "x-quarter-agent: claude-code\nx-quarter-key: qa_k",
        "CLAUDE_CODE_GATEWAY_HINT_HEADERS": "1",
    }


def test_codex_requirements_route_every_session_through_quarter(capsys):
    import tomllib

    from quarter_ledger.cli import main

    env = {"QUARTER_AGENT_KEYS": "codex=qa_c,claude-code=qa_k"}
    with patch.dict("os.environ", env):
        main(["--db", ":memory:", "managed-settings", "--for", "codex", "--url", "https://gw.internal:8780/"])
    out, err = capsys.readouterr()
    toml = tomllib.loads(out)
    assert toml["model_provider"] == "quarter"
    provider = toml["model_providers"]["quarter"]
    assert provider["base_url"] == "https://gw.internal:8780/chatgpt/backend-api/codex"
    assert provider["requires_openai_auth"] is True
    assert provider["http_headers"] == {"x-quarter-agent": "codex", "x-quarter-key": "qa_c"}
    assert "/etc/codex/requirements.toml" in err


def test_serve_reads_max_wait(tmp_path, monkeypatch):
    from quarter_ledger import cli

    started = []

    class Server:
        def serve_forever(self, poll_interval=0.5):
            pass

    monkeypatch.setattr(cli, "serve", lambda gw, host, port: started.append(gw) or Server())
    monkeypatch.delenv("QUARTER_MAX_WAIT", raising=False)
    cli.main(["--db", str(tmp_path / "l.db"), "serve"])
    monkeypatch.setenv("QUARTER_MAX_WAIT", "110")
    cli.main(["--db", str(tmp_path / "l.db"), "serve"])
    assert [gw.max_wait for gw in started] == [300, 110]
    for bad in ("0", "301", "1.5", "soon"):
        monkeypatch.setenv("QUARTER_MAX_WAIT", bad)
        with pytest.raises(SystemExit):
            cli.main(["--db", str(tmp_path / "l.db"), "serve"])
