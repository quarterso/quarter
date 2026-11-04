from quarter_ledger.store import Ledger


def test_config_changes_are_audited_once(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    assert ledger.record_config({"caps": [{"agent": "a", "limit_usd": "5"}], "prices": {}})
    assert not ledger.record_config({"prices": {}, "caps": [{"agent": "a", "limit_usd": "5"}]})
    assert ledger.record_config({"caps": [{"agent": "a", "limit_usd": "50"}], "prices": {}})
    changes = [r["detail"] for r in ledger.audit_log() if r["action"] == "config.changed"]
    assert sorted(changes) == ["caps", "caps, prices"]
    assert ledger.verify_chain()["audit"]["intact"]


def test_owner_cannot_approve_own_agent(tmp_path):
    import pytest

    from quarter_ledger.policy import Cap

    ledger = Ledger(str(tmp_path / "l.db"))
    ledger.set_owner("bot", "Dana")
    caps = [Cap(scope="agent", key="bot", window="total", limit=10, approve_above=1)]
    decision, approval = ledger.hold(caps, {"agent": "bot"}, "openai", "m", 5)
    assert decision.outcome == "approve"
    with pytest.raises(PermissionError):
        ledger.decide_approval(approval, True, by="dana")
    with pytest.raises(PermissionError):
        ledger.decide_approval(approval, True, by=" Dana")
    assert ledger.decide_approval(approval, True, by="Sam")
    assert ledger.approval(approval)["decided_by"] == "Sam"
