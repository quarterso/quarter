"""The finance page: a month's spend, caps used, approvals waiting, and for finance the
month's files and the cost centre map."""

from datetime import UTC, datetime, timedelta
from html import escape
from urllib.parse import quote

from .forensics import top_tasks
from .insights import forecast
from .learn import suggestions
from .money import to_cents, usd_shown
from .outcomes import cost_per_outcome
from .policy import Cap, cap_label, window_start
from .register import listing
from .store import Ledger, agent_summary, month_range

DIMENSIONS = ("department", "class", "location")  # export.DIMENSIONS, in the order shown
DOWNLOADS = (  # label, path; each takes ?month=
    ("Journal (CSV)", "/export?format=csv"),
    ("QuickBooks journal", "/export?format=quickbooks"),
    ("Xero journal", "/export?format=xero"),
    ("NetSuite journal", "/export?format=netsuite"),
    ("FOCUS cost rows", "/export?format=focus"),
    ("Audit pack (zip)", "/audit-pack"),
    ("Approvals record (zip)", "/approvals/export"),
)


def default_month(now: datetime) -> str:
    """Last month in a month's first 7 days, when the close is being done; else this month."""
    return f"{now - timedelta(days=7):%Y-%m}" if now.day <= 7 else f"{now:%Y-%m}"


def usd(micros: int) -> str:
    """Dollars with thousands separators, for reading on screen only."""
    return escape(usd_shown(micros, lambda m: f"{to_cents(m) / 100:,.2f}"))

STYLE = """
:root{--ink:#0B1426;--ink2:#4A5568;--ink3:#718096;--line:#E4E8EF;--bg:#F5F7FA;--blue:#0866FF;
  --blue-soft:#EAF1FF;--warn:#B42318;--warn-soft:#FEF3F2}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.5 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;font-feature-settings:"tnum"}
.top{background:#fff;border-bottom:1px solid var(--line)}
.top div{max-width:1120px;margin:0 auto;padding:14px 24px;display:flex;align-items:center;gap:10px}
.mark{width:18px;height:18px;background:var(--blue);border-radius:0 0 0 18px}
.top b{font-size:15px;letter-spacing:-.01em}.top span{color:var(--ink3);margin-left:auto}
main{max-width:1120px;margin:0 auto;padding:28px 24px 64px}
h1{font-size:26px;letter-spacing:-.02em;margin:0} p.sub{color:var(--ink2);margin:2px 0 0}
h2{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--ink3);margin:36px 0 10px}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:22px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:18px 20px;color:var(--ink2)}
.card b{display:block;font-size:26px;font-weight:600;letter-spacing:-.02em;color:var(--ink);margin-bottom:2px}
.card:first-child b{color:var(--blue)}
table{width:100%;border-collapse:separate;border-spacing:0;background:#fff;border:1px solid var(--line);
  border-radius:12px;overflow:hidden}
th,td{text-align:left;padding:11px 16px;border-bottom:1px solid var(--line)}
th{font-size:12px;font-weight:600;color:var(--ink3);background:#FAFBFC}
tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:6px;background:#EEF1F6;border-radius:3px;min-width:120px}
.bar i{display:block;height:6px;border-radius:3px;background:var(--blue)}
.bar i.over{background:var(--warn)}
form{display:inline} button{font:inherit;font-size:13px;font-weight:500;border:1px solid var(--line);
  background:#fff;color:var(--ink);border-radius:8px;padding:5px 12px;cursor:pointer}
button.ok{background:var(--blue);border-color:var(--blue);color:#fff}
.empty{color:var(--ink3);background:#fff;border:1px dashed var(--line);border-radius:12px;padding:14px 16px;margin:0}
td.neg{color:var(--warn);font-weight:600}
.shadow{background:var(--warn-soft);border:1px solid var(--warn);border-radius:12px;padding:14px 16px;margin:22px 0 0}
.head{display:flex;align-items:flex-end;gap:12px;flex-wrap:wrap}.head form{margin-left:auto}
select,input[type=text]{font:inherit;font-size:13px;border:1px solid var(--line);border-radius:8px;padding:5px 8px;
  background:#fff;color:var(--ink)} td input[type=text]{width:100%}
.files{display:flex;flex-wrap:wrap;gap:8px}.files a{background:#fff;border:1px solid var(--line);border-radius:8px;
  padding:6px 12px;color:var(--ink);text-decoration:none;font-size:13px;font-weight:500}
.files a:hover{border-color:var(--blue);color:var(--blue)}
@media (max-width:760px){.cards{grid-template-columns:1fr 1fr}table{display:block;overflow-x:auto}}
"""


def _table(title: str, totals: dict[str, int]) -> str:
    if not totals:
        return f"<h2>{title}</h2><p class=empty>No spend yet.</p>"
    body = "".join(
        f"<tr><td>{escape(k)}</td><td class=n>{usd(v)}</td></tr>"
        for k, v in sorted(totals.items(), key=lambda kv: -kv[1])
    )
    return f"<h2>{title}</h2><table><tr><th>Name</th><th class=n>Spend</th></tr>{body}</table>"


def _agents(summary: list[dict]) -> str:
    if not summary:
        return "<p class=empty>No spend yet.</p>"
    rows = "".join(
        f"<tr><td>{escape(r['agent'])}</td><td class=n>{usd(r['by'])}</td>"
        f"<td class=n>{usd(r['on'])}</td><td class=n><b>{usd(r['by'] + r['on'])}</b></td></tr>"
        for r in summary
    )
    return (
        "<table><tr><th>Agent</th><th class=n>Spent by it</th><th class=n>Spent on it</th>"
        f"<th class=n>Total</th></tr>{rows}</table>"
    )


def _paused(rows: list[dict], can_decide: bool) -> str:
    if not rows:
        return ""
    body = "".join(
        f"<tr><td>{escape(p['agent'])}{escape(' task ' + p['task'] if p['task'] else '')}</td>"
        f"<td>{escape(p['at'][:16].replace('T', ' '))} UTC</td>"
        f"<td>{escape(p['reason'])}</td><td>"
        + (
            f"<form method=post action='/{'tasks' if p['task'] else 'agents'}/"
            f"{escape(quote(p['task'] or p['agent'], safe=''))}/resume'>"
            "<input type=hidden name=form value=1><button class=ok>Resume</button></form>"
            if can_decide
            else ""
        )
        + "</td></tr>"
        for p in rows
    )
    return (
        "<h2>Paused agents</h2><table><tr><th>Agent</th><th>Since</th><th>Why</th><th></th></tr>"
        f"{body}</table>"
    )


def _tasks(rows: list[dict]) -> str:
    if not rows:
        return ""
    body = "".join(
        f"<tr><td>{escape(r['task'])}</td><td class=n>{r['calls']}</td>"
        f"<td class=n>{usd(r['charged'])}</td><td class=n>{usd(r['list'])}</td>"
        f"<td><a href='/tasks/{escape(quote(r['task'], safe=''))}/why'>Why</a></td></tr>"
        for r in rows
    )
    head = ("<tr><th>Task</th><th class=n>Calls</th><th class=n>Charged</th>"
            "<th class=n>At list price</th><th></th></tr>")
    return f"<h2>Costliest tasks</h2><table>{head}{body}</table>"


def _forecast(f: dict) -> str:
    warn = "".join(
        f"<tr><td>{escape(w['cap'])}</td><td class=n>{usd(w['limit'])}</td>"
        f"<td class='n neg'>{usd(w['projected'])}</td><td>{'Reached' if w['reached'] == 'already' else 'Reached on ' + w['reached']}</td></tr>"
        for w in f["cap_warnings"]
    )
    if warn:
        return (
            "<table><tr><th>Cap</th><th class=n>Limit</th><th class=n>Month end at this rate</th><th></th></tr>"
            + warn
            + "</table>"
        )
    if not f["enough_data"]:
        return "<p class=empty>Not enough data yet: the forecast starts after a day of spend.</p>"
    return "<p class=empty>No monthly cap will be reached at the current rate.</p>"


def _caps(ledger: Ledger, caps: list[Cap], now: datetime) -> str:
    rows = []
    for cap in caps:
        if cap.key == "*" and cap.scope != "all":
            continue  # wildcard caps apply per id; shown per agent in a later version
        used = ledger.spent(cap.scope, cap.key, window_start(cap.window, now))
        share = min(used / cap.limit, 1) if cap.limit else 1
        over = " class=over" if share >= 0.9 else ""
        rows.append(
            f"<tr><td>{escape(cap_label(cap, cap.key))}{' (shadow)' if cap.mode == 'shadow' else ''}</td><td>{cap.window}</td>"
            f"<td class=n>{usd(used)} of {usd(cap.limit)}</td>"
            f"<td><div class=bar><i{over} style='width:{share * 100:.0f}%'></i></div></td></tr>"
        )
    if not rows:
        return "<p class=empty>No named caps.</p>"
    head = "<tr><th>Cap</th><th>Window</th><th class=n>Used</th><th></th></tr>"
    return f"<table>{head}{''.join(rows)}</table>"


def _approvals(pending: list[dict], can_decide: bool) -> str:
    if not pending:
        return "<p class=empty>Nothing waiting.</p>"
    rows = "".join(
        f"<tr><td>{escape(a['agent'])}</td><td>{escape(a['vendor'])}"
        f" {escape(a['model'] or '')}</td><td class=n>{usd(a['estimate'])}</td>"
        f"<td>{escape(a['reason'])}</td><td>"
        + (
            f"<form method=post action='/approvals/{escape(a['id'])}/approve'>"
            "<input type=hidden name=form value=1><button class=ok>Approve</button></form> "
            f"<form method=post action='/approvals/{escape(a['id'])}/deny'>"
            "<input type=hidden name=form value=1><button>Deny</button></form>"
            if can_decide
            else ""
        )
        + "</td></tr>"
        for a in pending
    )
    head = "<tr><th>Agent</th><th>Call</th><th class=n>Up to</th><th>Why</th><th></th></tr>"
    return f"<table>{head}{rows}</table>"


def _learned(ledger: Ledger) -> str:
    """What past approvals suggest, and the standing approvals in force. Shown only once
    there is something to show."""
    found = suggestions(ledger)["suggestions"]
    standing = ledger.standing()
    if not found and not standing:
        return ""
    items = "".join(
        f"<tr><td>{escape(s['text'])}</td><td class=n>"
        + (f"up to {usd(s['up_to'])}, {usd(s['monthly'])}/month" if s["kind"] == "standing"
           else "refuse with a rule")
        + "</td></tr>"
        for s in found
    )
    rules = "".join(
        f"<tr><td>{escape(r['agent'])} with {escape(r['vendor'])}, agreed by {escape(r['created_by'])}"
        f" until {r['ends'][:10]}</td><td class=n>up to {usd(r['up_to'])},"
        f" {usd(r['monthly'])}/month</td></tr>"
        for r in standing
    )
    out = (f"<h2>Suggested from your approvals</h2><table>{items}</table><p class=sub>Accept one with"
           " <code>quarter-ledger standing add</code> (see <code>quarter-ledger suggest</code>).</p>") if items else ""
    return out + (f"<h2>Standing approvals</h2><table>{rules}</table>" if rules else "")


def _register(ledger: Ledger, keys, now: datetime) -> str:
    """Each agent's owner, last use and quarterly review: the register in short."""
    rows = listing(ledger, keys, rule=ledger.register, now=now)
    if not rows:
        return ""

    def row(r: dict) -> str:
        used = max(filter(None, (r["last_call"], r["last_payment"], r["last_observed"])), default="never")[:10]
        review = f"{r['review_quarter']} {r['review_result']} by {r['reviewer']}" if r["reviewer"] else "none"
        flag = "" if r["status"] == ["ok"] else " class=neg"
        return (f"<tr><td>{escape(r['agent'])}</td><td>{escape(r['owner'] or 'none')}</td><td>{escape(used)}</td>"
                f"<td>{escape(review)}</td><td{flag}>{escape(', '.join(r['status']))}</td></tr>")

    body = "".join(row(r) for r in rows)
    head = "<tr><th>Agent</th><th>Owner</th><th>Last used</th><th>Last review</th><th>Status</th></tr>"
    return (f"<h2>Agent register</h2><table>{head}{body}</table><p class=sub>Review with"
            " <code>quarter-ledger agents review</code>; the full list is <code>quarter-ledger agents list</code>.</p>")


def _shadow(ledger: Ledger, shadow: list[str], start: datetime, end: datetime) -> str:
    if not shadow:
        return ""
    held = ledger.db.execute(
        "select count(*) from audit where action = 'would_hold' and at >= ? and at < ?",
        (start.isoformat(), end.isoformat()),
    ).fetchone()[0]
    return (
        f"<p class=shadow><b>Shadow mode.</b> These rules let every call through and only record what"
        f" they would have held: {escape('; '.join(shadow))}. In {start:%B} {held} would have been held."
        " See <code>quarter-ledger shadow-report</code>.</p>"
    )


def _free_limit(ledger: Ledger, now: datetime) -> str:
    if not ledger.over_free_limit():
        return ""
    used = ledger.decisions(f"{now:%Y-%m}")
    return (f"<p class=shadow><b>Over the free limit.</b> {used} decisions this month, {ledger.free_decisions}"
            " free. Calls are still decided; each reply carries <code>x-quarter-free-limit: exceeded</code>.</p>")


def _outcomes(o: dict) -> str:
    """Cost per outcome for the month, shown once any outcome has been reported."""
    if not o["outcomes"]:
        return ""

    def row(r: dict) -> str:
        per = "" if r["per_outcome"] is None else usd(r["per_outcome"])
        ratio = "" if r["value_per_dollar"] is None else f"{r['value_per_dollar']:.1f}x"
        return (f"<tr><td>{escape(r['kind'])}</td><td class=n>{r['count']}</td><td class=n>{usd(r['cost'])}</td>"
                f"<td class=n>{per}</td><td class=n>{ratio}</td></tr>")

    rows = "".join(row(r) for r in o["rows"])
    idle = (f"<p class=sub>{o['idle_tasks']} task(s) spent {usd(o['idle_cost'])} and reported no outcome.</p>"
            if o["idle_tasks"] else "")
    table = (f"<table><tr><th>Outcome</th><th class=n>Count</th><th class=n>AI cost</th>"
             f"<th class=n>Per outcome</th><th class=n>Value per AI $</th></tr>{rows}</table>" if rows else "")
    return f"<h2>Cost per outcome</h2>{table}{idle}"


def _margin(m: dict | str | None) -> str:
    """Revenue from Stripe against AI cost, per customer, worst margin first."""
    if m is None:
        return ""
    if isinstance(m, str):
        return f"<h2>Margin after AI cost</h2><p class=empty>{escape(m)}</p>"

    def money(micros: int) -> str:
        return ("-" if micros < 0 else "") + usd(abs(micros))

    def pct(value) -> str:
        return "" if value is None else f"{value:.1f}%"

    actual = m.get("actual")
    rows = "".join(
        f"<tr><td>{escape(r['customer'])}</td><td class=n>{money(r['revenue'])}</td>"
        f"<td class=n>{money(r['ai_cost'])}</td><td class='n{' neg' if r['margin'] < 0 else ''}'>{money(r['margin'])}</td>"
        f"<td class=n>{pct(r['margin_pct'])}</td>"
        + (f"<td class=n>{money(r['actual_cost'])}</td>" if actual else "") + "</tr>" for r in m["rows"])
    notes = m.get("stripe") or {}
    said = [f"{notes.get('invoices', 0)} Stripe invoice(s) paid this month, excluding tax"]
    if notes.get("skipped_currency"):
        said.append(f"{notes['skipped_currency']} not in USD left out")
    if notes.get("test_mode"):
        said.append("test mode data")
    if m["unassigned_cost"]:
        said.append(f"{usd(m['unassigned_cost'])} of AI spend named no customer")
    if actual:
        said.append(f"at the vendors' own figures ({', '.join(actual['vendors'])}), AI cost {money(actual['ai_cost'])}")
        if actual["unattributed"]:
            said.append(f"{usd(actual['unattributed'])} billed past what Quarter saw, given to no customer")
    return (f"<h2>Margin after AI cost</h2><div class=cards>"
            f"<div class=card><b>{money(m['revenue'])}</b>Revenue</div>"
            f"<div class=card><b>{money(m['ai_cost'])}</b>AI cost to serve</div>"
            f"<div class=card><b>{money(m['margin'])} {pct(m['margin_pct'])}</b>Margin after AI cost</div></div>"
            + ("<table><tr><th>Customer</th><th class=n>Revenue</th><th class=n>AI cost</th>"
               "<th class=n>Margin</th><th class=n>%</th>"
               + ("<th class=n>At vendor's figures</th>" if actual else "") + f"</tr>{rows}</table>" if rows else "")
            + f"<p class=sub>{escape('; '.join(said))}.</p>")


def _picker(ledger: Ledger, chosen: str, now: datetime) -> str:
    """A month to show: each from the first one with spend to this one, newest first."""
    first = ledger.db.execute("select min(at) from entries").fetchone()[0]
    months, at = [], window_start("month", now)
    while len(months) < 36:
        months.append(f"{at:%Y-%m}")
        if not first or months[-1] <= first[:7]:
            break
        at = (at - timedelta(days=1)).replace(day=1)
    if chosen not in months:
        months.append(chosen)
    options = "".join(
        f"<option value='{escape(m)}'{' selected' if m == chosen else ''}>{datetime.strptime(m, '%Y-%m'):%B %Y}</option>"
        for m in sorted(months, reverse=True)
    )
    return (f"<form method=get action='/dashboard'><select name=month aria-label=Month>{options}</select>"
            " <button>Show</button></form>")


def _downloads(month: str) -> str:
    links = "".join(f"<a href='{path}{'&amp;' if '?' in path else '?'}month={escape(month)}' download>{label}</a>" for label, path in DOWNLOADS)
    return (f"<h2>Files for {datetime.strptime(month, '%Y-%m'):%B %Y}</h2><div class=files>{links}</div>"
            "<p class=sub>The journals split by the cost centres below.</p>")


def _centres(centres: dict[str, dict[str, str]], agents: set[str], version: str) -> str:
    """The cost centre map as a form: one row per agent, and a blank row to add one."""
    names = sorted(agents | {a for c in centres.values() for a in c}, key=lambda a: (a == "*", a))
    rows = "".join(
        f"<tr><td><input type=text name=agent value='{escape(a)}' aria-label=Agent></td>"
        + "".join(f"<td><input type=text name={d} value='{escape(centres.get(d, {}).get(a, ''))}'"
                  f" aria-label={d.title()}></td>" for d in DIMENSIONS)
        + "</tr>"
        for a in [*names, ""]
    )
    head = "<tr><th>Agent</th>" + "".join(f"<th>{d.title()}</th>" for d in DIMENSIONS) + "</tr>"
    return (
        "<h2>Cost centres</h2><form method=post action='/attribution' style='display:block'>"
        f"<input type=hidden name=form value=1><input type=hidden name=version value='{escape(version)}'>"
        f"<table>{head}{rows}</table><p class=sub>Department is the team. Use * for every agent not listed."
        " Clear a box to remove it. Each change is kept in the audit log.</p>"
        "<p><button class=ok>Save</button></p></form>"
    )


def render(ledger: Ledger, caps: list[Cap], signed_in: str = "", can_decide: bool = True,
           shadow: list[str] | None = None, keys=(), month: str | None = None, finance: bool = False,
           centres: dict[str, dict[str, str]] | None = None, centres_version: str = "",
           margin: dict | str | None = None) -> str:
    """Without can_decide, the Approve, Deny and Resume buttons are left out. shadow names
    the rules that record instead of refusing (Gateway.in_shadow). keys: the agents with a
    named key, for the register. month (YYYY-MM, this month if None) is the month every spend
    figure covers; caps and the forecast are shown for this month only, and what waits for a
    person always. finance adds the month's files and, with centres, the cost centre form.
    margin: margin.margins for the month with Stripe's notes, or why Stripe could not be read."""
    who = f"Signed in as {escape(signed_in)}" if signed_in else "Ledger"
    now = datetime.now(UTC)
    month = month or f"{now:%Y-%m}"
    current = month == f"{now:%Y-%m}"
    start, end = month_range(month)
    by_agent = ledger.spend_by("agent", start, end)
    by_customer = ledger.spend_by("customer", start, end)
    by_vendor = ledger.spend_by("vendor", start, end)
    total = sum(by_vendor.values())
    summary = agent_summary(ledger, month)
    spent_on = sum(r["on"] for r in summary)
    pending = ledger.approvals("pending")
    paused = ledger.paused_agents()
    outlook = forecast(ledger, caps, now)
    month_end = (usd(outlook["month_end"]) if outlook["enough_data"]
                 else "Not enough data yet")
    third = (f"<b>{month_end}</b>Month end at this rate" if current
             else f"<b>{usd(total + spent_on)}</b>Month total")
    live = (f"<h2>Caps</h2>{_caps(ledger, caps, now)}<h2>Forecast</h2>{_forecast(outlook)}" if current
            else "<p class=sub>Caps and the forecast are shown for this month only.</p>")
    files = _downloads(month) if finance else ""
    if finance and centres is not None:
        files += _centres(centres, set(by_agent) - {"(none)"} | set(keys), centres_version)

    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Quarter ledger</title><style>{STYLE}</style></head><body>
<div class=top><div><i class=mark></i><b>Quarter</b><span>{who}</span></div></div><main>
<div class=head><div><h1>Agent spend</h1><p class=sub>{start:%B %Y}, {"charges so far" if current else "the whole month"}</p></div>
{_picker(ledger, month, now)}</div>
{_shadow(ledger, shadow or [], start, end)}
{_free_limit(ledger, now)}
<div class=cards>
<div class=card><b>{usd(total)}</b>Spent by agents</div>
<div class=card><b>{usd(spent_on)}</b>Spent on agents</div>
<div class=card>{third}</div>
<div class=card><b>{len(by_agent)}</b>Agents spending</div>
<div class=card><b>{len(by_customer)}</b>Customers served</div>
<div class=card><b>{len(pending)}</b>Waiting for approval</div>
</div>
{_paused(paused, can_decide)}
<h2>Approvals</h2>{_approvals(pending, can_decide)}
{_learned(ledger)}
{live}
<h2>Cost per agent</h2>{_agents(summary)}
{_register(ledger, keys, now)}
{_tasks(top_tasks(ledger, start, until=end))}
{_outcomes(cost_per_outcome(ledger, month))}
{_margin(margin)}
{_table("By customer", by_customer)}
{_table("By agent", by_agent)}
{_table("By vendor", by_vendor)}
{files}
</main></body></html>"""
