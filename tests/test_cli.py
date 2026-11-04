from quarter_ledger.cli import config


def test_config_reads_json_text_or_a_file(tmp_path):
    path = tmp_path / "upstreams.json"
    path.write_text('{"openai": "http://vendor"}')
    assert config('{"openai": "http://vendor"}') == config(str(path)) == {"openai": "http://vendor"}
    assert config(' [{"scope": "agent"}]') == [{"scope": "agent"}]


def test_receipt_command(tmp_path, monkeypatch, capsys):
    from quarter_ledger.cli import main
    from quarter_ledger.receipts import verify
    from quarter_ledger.store import Ledger

    db = str(tmp_path / "l.db")
    ledger = Ledger(db)
    ledger.hold([], {"agent": "a"}, "v", "m", 1)
    ledger.db.execute(
        "insert into approvals (id, created_at, agent, vendor, estimate, reason, status)"
        " values ('apr_1', 'now', 'a', 'v', 5, 'big', 'approved')"
    )
    monkeypatch.setenv("QUARTER_SIGNING_KEY", "k" * 32)
    main(["--db", db, "receipt", "apr_1"])
    assert verify(capsys.readouterr().out.strip(), "k" * 32)["sub"] == "apr_1"


def test_config_errors_say_what_is_wrong(tmp_path):
    import pytest

    with pytest.raises(SystemExit, match="single quotes"):
        config("{openai:http://vendor}")  # what uv makes of unquoted JSON in .env
    with pytest.raises(SystemExit, match="not found: .*missing.json"):
        config(str(tmp_path / "missing.json"))


def test_wrap_names_a_missing_command(tmp_path):
    import pytest

    from quarter_ledger.cli import main

    with pytest.raises(SystemExit, match="no-such-agent-cli: command not found"):
        main(["--db", str(tmp_path / "l.db"), "wrap", "--", "no-such-agent-cli"])


def test_new_commands_name_a_bad_amount_or_date(tmp_path):
    import pytest

    from quarter_ledger.cli import main

    db = str(tmp_path / "l.db")
    keys = tmp_path / "keys.json"
    keys.write_text('{"keys": []}')
    for args, message in (
        (["bill", "add", "--payee", "A", "--invoice", "1", "--amount-usd", "abc", "--account", "x1"], "bill:"),
        (["standing", "add", "--agent", "a", "--vendor", "v", "--up-to-usd", "abc", "--monthly-usd", "5",
          "--by", "s"], "standing:"),
        (["bill", "pay", "--due-by", "nope", "--by", "s"], "bill pay:"),
        (["verify-mandate", "a.b.c", "--keys", str(keys), "--amount-usd", "abc"], "verify-mandate:"),
    ):
        with pytest.raises(SystemExit, match=message):
            main(["--db", db, *args])
