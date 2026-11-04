from datetime import UTC, datetime, timedelta

from quarter_ledger.insights import forecast
from quarter_ledger.money import to_micros
from quarter_ledger.policy import Cap
from quarter_ledger.store import Ledger


def charge(ledger, agent, customer, usd, at):
    ledger.db.execute(
        "insert into entries (call_id, kind, at, agent, customer, vendor, amount)"
        " values ('c', 'charge', ?, ?, ?, 'openai', ?)",
        (at.isoformat(), agent, customer, to_micros(usd)),
    )


def test_forecast_projects_month_end_and_cap_date(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    now = datetime(2026, 11, 11, 0, 0, tzinfo=UTC)  # 20 days left in November
    for day in range(1, 8):
        charge(ledger, "bot", "acme", 1, now - timedelta(days=day))  # $1 a day
    caps = [Cap("customer", "acme", "month", to_micros(20))]
    out = forecast(ledger, caps, now)
    assert out["so_far"] == to_micros(7)
    assert out["month_end"] == to_micros(27)
    assert out["cap_warnings"] == [
        {
            "cap": "customer acme",
            "limit": to_micros(20),
            "projected": to_micros(27),
            "reached": "2026-11-24",
        }
    ]


def test_init_total_budget_is_forecast_and_shown(tmp_path):
    """The one cap `init` writes (scope all, no key) warns and shows on the dashboard."""
    from quarter_ledger.dashboard import render
    from quarter_ledger.policy import load_caps

    ledger = Ledger(str(tmp_path / "l.db"))
    now = datetime(2026, 11, 11, 0, 0, tzinfo=UTC)
    for day in range(1, 8):
        charge(ledger, "bot", None, 1, now - timedelta(days=day))
    caps = load_caps([{"scope": "all", "window": "month", "limit_usd": "20"}])
    assert forecast(ledger, caps, now)["cap_warnings"] == [
        {"cap": "all spend", "limit": to_micros(20), "projected": to_micros(27),
         "reached": "2026-11-24"}
    ]
    assert "all spend" in render(ledger, caps) and "No named caps" not in render(ledger, caps)


def test_early_month_uses_last_months_days(tmp_path):
    """On 2 November the rate comes from the last seven days, most of them October."""
    ledger = Ledger(str(tmp_path / "l.db"))
    now = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)  # 29 days left in November
    for day in range(1, 8):
        charge(ledger, "bot", "acme", 1, now - timedelta(days=day))  # $1 a day since 26 Oct
    out = forecast(ledger, [], now)
    assert out["so_far"] == to_micros(1)  # only 1 November is this month
    assert out["agents"][0]["per_day"] == to_micros(1)
    assert out["month_end"] == to_micros(30)
    assert out["enough_data"] and out["rate_days"] == 7


def test_brand_new_ledger_has_not_enough_data(tmp_path):
    from quarter_ledger.dashboard import render as dashboard
    from quarter_ledger.status import render as status

    ledger = Ledger(str(tmp_path / "l.db"))
    now = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
    for hour in (3, 2, 1):
        charge(ledger, "bot", "acme", 1, now - timedelta(hours=hour))
    caps = [
        Cap("customer", "acme", "month", to_micros(10)),  # would be "reached" on a guess
        Cap("agent", "bot", "month", to_micros(2)),  # really is reached
    ]
    out = forecast(ledger, caps, now)
    assert out["so_far"] == to_micros(3)
    assert out["month_end"] is None and not out["enough_data"]
    assert out["agents"] == [{"agent": "bot", "so_far": to_micros(3), "per_day": None,
                              "month_end": None}]
    assert out["cap_warnings"] == [
        {"cap": "agent bot", "limit": to_micros(2), "projected": to_micros(3),
         "reached": "already"}
    ]
    assert "Month end: not enough data yet" in status(ledger, caps, now)

    fresh = Ledger(str(tmp_path / "fresh.db"))  # the dashboard always uses the clock
    charge(fresh, "bot", "acme", 1, datetime.now(UTC) - timedelta(hours=1))
    assert "Not enough data yet" in dashboard(fresh, [])


def test_steady_spend_projects_the_simple_rate(tmp_path):
    ledger = Ledger(str(tmp_path / "l.db"))
    now = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)  # 15 days spent, 15 left
    for hours in range(6, 15 * 24 + 1, 6):
        charge(ledger, "bot", "acme", 0.5, now - timedelta(hours=hours))  # $2 a day
    out = forecast(ledger, [], now)
    assert out["so_far"] == to_micros(30)
    assert out["agents"][0]["per_day"] == to_micros(2)
    assert out["month_end"] == to_micros(60)


def test_early_month_burst_does_not_fire_cap_warning(tmp_path):
    """$5 on the first day after a quiet October: no cap date and no Slack warning."""
    ledger = Ledger(str(tmp_path / "l.db"))
    now = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)
    for day in range(2, 8):
        charge(ledger, "bot", "acme", 0.5, now - timedelta(days=day))  # October, $0.50 a day
    charge(ledger, "bot", "acme", 5, datetime(2026, 11, 1, 9, 0, tzinfo=UTC))
    caps = [Cap("customer", "acme", "month", to_micros(50))]
    out = forecast(ledger, caps, now)
    # The month-to-date rate would have been $4.80 a day and $144 at month end.
    assert out["so_far"] == to_micros(5)
    assert out["month_end"] < to_micros(50)
    assert out["cap_warnings"] == []
