import re
import socket

from quarter_ledger.cli import main
from quarter_ledger.money import to_micros
from quarter_ledger.store import Ledger


def test_demo_runs_the_whole_loop_and_stops(tmp_path, capfd):
    main(["demo", "--dir", str(tmp_path), "--no-wait"])
    out = capfd.readouterr().out
    assert out.count(" 200\n") == 6  # five calls, and the held one once approved
    assert "403 approval_required" in out and "approved by Dana" in out
    assert "6 new, 0 revised, 0 already booked; $47.55 added as observed spend" in out
    assert "Spent today <$0.01" in out
    assert "Sign in with any username and the password" in out and "q audit-pack --month" in out

    ledger = Ledger(str(tmp_path / "ledger.db"))
    assert ledger.db.execute("select status, decided_by from approvals").fetchall() == [("used", "Dana")]
    observed = ledger.db.execute("select sum(amount) from entries where call_id like 'obs:%'").fetchone()[0]
    assert observed == to_micros("47.55")
    assert all(t["intact"] for t in ledger.verify_chain().values())

    port = int(re.search(r"127\.0\.0\.1:(\d+)/dashboard", out).group(1))
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", port)) != 0  # the gateway was stopped


def test_demo_refuses_a_folder_with_a_ledger(tmp_path):
    Ledger(str(tmp_path / "ledger.db"))
    try:
        main(["demo", "--dir", str(tmp_path), "--no-wait"])
    except SystemExit as e:
        assert "already has a ledger" in str(e)
    else:
        raise AssertionError("the demo ran over an existing ledger")
