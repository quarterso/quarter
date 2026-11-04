"""Caps, and the decision for one call.

A cap limits spend for one scope (an agent, a task, an end customer, or "all" spend) over a window.
"approve_above" sends any single call costing more than that to a person instead.
"over" says what happens to a call that would take spend past the cap: "block" (the default)
refuses it; "approve" sends it to a person, who may let that one call through.
"mode": "shadow" makes a cap record what it would have held instead of holding it (see
store.Ledger.hold); "enforce" is the default.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .money import to_micros

Scope = Literal["agent", "task", "customer", "all"]
Window = Literal["day", "month", "total"]
Over = Literal["block", "approve"]
Mode = Literal["enforce", "shadow"]


@dataclass(frozen=True)
class Cap:
    scope: Scope
    key: str  # an id, or "*" for every agent, task or customer
    window: Window
    limit: int  # micro-dollars
    approve_above: int | None = None
    over: Over = "block"
    mode: Mode = "enforce"

    def applies_to(self, scope_ids: dict[str, str | None]) -> str | None:
        """The id this cap counts against for a call, or None if it doesn't apply."""
        if self.scope == "all":
            return "all"
        value = scope_ids.get(self.scope)
        if value is None or self.key not in ("*", value):
            return None
        return value


@dataclass(frozen=True)
class Decision:
    outcome: Literal["allow", "block", "approve"]
    reason: str = ""
    fallback_to: str | None = None  # the cheaper model the call was moved to
    would_hold: "Decision | None" = None  # allowed in shadow: what enforcing would have done


@dataclass(frozen=True)
class Shadow:
    """What a hold lets through and records instead of refusing, beyond caps whose own mode
    is "shadow". everything: QUARTER_MODE=shadow, so caps, payee lists and pauses; payees:
    QUARTER_PAYEES "mode": "shadow". approvers: who would be asked, for the record."""
    everything: bool = False
    payees: bool = False
    approvers: tuple[str, ...] = ()


def cap_label(cap: Cap, target: str) -> str:
    return "all spend" if cap.scope == "all" else f"{cap.scope} {target}"


def window_start(window: Window, now: datetime) -> datetime | None:
    if window == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return None


def load_caps(raw: list[dict]) -> list[Cap]:
    for c in raw:
        if c.get("over", "block") not in ("block", "approve"):
            raise ValueError(f'a cap\'s "over" is "block" or "approve", not {c["over"]!r}')
        if c.get("mode", "enforce") not in ("enforce", "shadow"):
            raise ValueError(f'a cap\'s "mode" is "enforce" or "shadow", not {c["mode"]!r}')
    return [
        Cap(
            scope=c["scope"],
            key=c.get("key", "*"),
            window=c.get("window", "month"),
            limit=to_micros(c["limit_usd"]),
            approve_above=to_micros(c["approve_above_usd"]) if "approve_above_usd" in c else None,
            over=c.get("over", "block"),
            mode=c.get("mode", "enforce"),
        )
        for c in raw
    ]


def decide(
    caps: list[Cap],
    scope_ids: dict[str, str | None],
    estimate: int,
    spent,
    skip_approval: bool = False,
    skip_line: bool = False,
    now: datetime | None = None,
) -> Decision:
    """spent(scope, id, since) returns micro-dollars already spent or held.

    skip_approval: a person already approved this call. Caps still apply, except a cap
    whose "over" is "approve": that approval was for going past it.
    skip_line: a standing approval covers this call. Only "approve_above" is skipped;
    going past a cap still blocks or asks.
    A cap that blocks wins over any that would ask; the person asked sees every reason.
    now: the moment the windows are measured from (replay passes a call's own time).
    """
    now = now or datetime.now(UTC)
    reasons = []
    for cap in caps:
        target = cap.applies_to(scope_ids)
        if target is None:
            continue
        label = cap_label(cap, target)
        over = spent(cap.scope, target, window_start(cap.window, now)) + estimate > cap.limit
        # Fixed text, no amounts: the shadow report groups reasons by their exact words.
        why = f"{label}: one call over the whole {cap.window} cap" if estimate > cap.limit else f"{label}: {cap.window} cap reached"
        if over and cap.over == "block":
            return Decision("block", why)
        if skip_approval:
            continue
        if over:
            reasons.append(why)
        elif cap.approve_above is not None and estimate > cap.approve_above and not skip_line:
            reasons.append(f"{label}: call over approval limit")
    return Decision("approve", "; ".join(reasons)) if reasons else Decision("allow")
