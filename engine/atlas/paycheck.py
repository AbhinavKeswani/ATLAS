"""Paycheck engine: hours → gross → withholding estimate → net, plus next payday.

Hours come from manual `time_entries`; any gap day that matches the default work
schedule is auto-filled (so the running total accrues without daily data entry).
Overtime is weekly (>40 h at 1.5×). Withholding is an ESTIMATE via the annualized
percentage method (federal + FICA + NY State + optional NYC) — not payroll-exact.

Tax constants below are 2025 figures used as a baseline; they live here, clearly
labelled, and are trivially editable when 2026 tables are finalized.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .store import Store

# Default schedule: weekday (Mon=0) -> (start "HH:MM", end "HH:MM").
DEFAULT_SCHEDULE: dict[int, tuple[str, str]] = {
    0: ("08:00", "20:00"),  # Mon
    1: ("08:00", "20:00"),  # Tue
    2: ("08:00", "20:00"),  # Wed
    3: ("08:00", "20:00"),  # Thu
    4: ("08:00", "16:00"),  # Fri
}

PERIODS_PER_YEAR = 26  # biweekly

# --- 2025 tax baseline (editable) --------------------------------------------

FED_STD_DEDUCTION_SINGLE = 15_000.0
FED_BRACKETS_SINGLE = [  # (upper bound of bracket, marginal rate)
    (11_925, 0.10), (48_475, 0.12), (103_350, 0.22), (197_300, 0.24),
    (250_525, 0.32), (626_350, 0.35), (float("inf"), 0.37),
]
SS_RATE = 0.062
SS_WAGE_BASE = 176_100.0
MEDICARE_RATE = 0.0145

NY_STD_DEDUCTION_SINGLE = 8_000.0
NY_BRACKETS_SINGLE = [
    (8_500, 0.04), (11_700, 0.045), (13_900, 0.0525), (80_650, 0.055),
    (215_400, 0.06), (1_077_550, 0.0685), (5_000_000, 0.0965),
    (25_000_000, 0.103), (float("inf"), 0.109),
]
NYC_BRACKETS_SINGLE = [
    (12_000, 0.03078), (25_000, 0.03762), (50_000, 0.03819), (float("inf"), 0.03876),
]


def _progressive_tax(taxable: float, brackets: list[tuple[float, float]]) -> float:
    if taxable <= 0:
        return 0.0
    tax = 0.0
    lower = 0.0
    for upper, rate in brackets:
        if taxable <= lower:
            break
        tax += (min(taxable, upper) - lower) * rate
        lower = upper
    return tax


@dataclass
class PayConfig:
    rate: float = 17.0
    ot_multiplier: float = 1.5
    ot_threshold: float = 40.0
    anchor_payday: str | None = None  # biweekly only: a known payday, YYYY-MM-DD
    period_days: int = 14
    # Days between a period ending and its payday landing (payroll lag).
    pay_lag_days: int = 0
    # "biweekly" (anchor + 14d) or "semimonthly" (work 1–15 paid on the 15th,
    # work 16–EOM paid on the 30th, clamped to month end).
    pay_schedule: str = "biweekly"
    state: str = "NY"
    nyc_resident: bool = False
    # Flat all-in effective withholding rate (0–1) from an actual paystub. When set,
    # it replaces the bracket estimate: tax = gross × rate. This is what actually gets
    # withheld (fed + FICA + NY State + NYC), so it's more accurate than estimating.
    effective_tax_rate: float | None = None
    default_break_min: int = 0
    use_default_schedule: bool = True

    @property
    def periods_per_year(self) -> int:
        return 24 if self.pay_schedule == "semimonthly" else 26

    @classmethod
    def load(cls, store: Store) -> "PayConfig":
        raw = store.get_setting("pay_config", {}) or {}
        cfg = cls()
        for k, v in raw.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg

    def save(self, store: Store) -> None:
        store.set_setting("pay_config", self.__dict__)


# --- Hours -------------------------------------------------------------------


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _entry_hours(start: str, end: str, break_min: int) -> float:
    mins = _minutes(end) - _minutes(start) - break_min
    return max(0.0, mins / 60.0)


def hours_for_day(day: dt.date, entries: dict[str, list[dict]], cfg: PayConfig) -> float:
    """Manual entries win; otherwise auto-fill from the default schedule."""
    key = day.isoformat()
    if key in entries:
        return sum(_entry_hours(e["start"], e["end"], e["break_min"]) for e in entries[key])
    if cfg.use_default_schedule and day.weekday() in DEFAULT_SCHEDULE:
        s, e = DEFAULT_SCHEDULE[day.weekday()]
        return _entry_hours(s, e, cfg.default_break_min)
    return 0.0


def _week_start(day: dt.date) -> dt.date:
    """The Sunday that begins `day`'s payroll week (Sun–Sat)."""
    return day - dt.timedelta(days=(day.weekday() + 1) % 7)


# --- Withholding -------------------------------------------------------------


def estimate_withholding(gross_period: float, cfg: PayConfig) -> dict:
    """Per-period withholding. A flat effective rate (from a real paystub) wins;
    otherwise fall back to the annualized bracket estimate."""
    if cfg.effective_tax_rate is not None:
        total = round(gross_period * cfg.effective_tax_rate, 2)
        return {
            "mode": "effective", "effective_rate": cfg.effective_tax_rate,
            "federal": None, "fica": None, "state": None, "city": None,
            "total_tax": total, "net": round(gross_period - total, 2),
        }
    n_periods = cfg.periods_per_year
    annual = gross_period * n_periods

    fed_taxable = max(0.0, annual - FED_STD_DEDUCTION_SINGLE)
    fed = _progressive_tax(fed_taxable, FED_BRACKETS_SINGLE)
    ss = min(annual, SS_WAGE_BASE) * SS_RATE
    medicare = annual * MEDICARE_RATE

    ny_taxable = max(0.0, annual - NY_STD_DEDUCTION_SINGLE)
    ny = _progressive_tax(ny_taxable, NY_BRACKETS_SINGLE) if cfg.state == "NY" else 0.0
    nyc = _progressive_tax(ny_taxable, NYC_BRACKETS_SINGLE) if cfg.nyc_resident else 0.0

    per = lambda annual_amt: round(annual_amt / n_periods, 2)
    federal = per(fed)
    fica = per(ss + medicare)
    state = per(ny)
    city = per(nyc)
    total = round(federal + fica + state + city, 2)
    return {
        "mode": "estimate",
        "effective_rate": round(total / gross_period, 4) if gross_period else 0.0,
        "federal": federal,
        "fica": fica,
        "state": state,
        "city": city,
        "total_tax": total,
        "net": round(gross_period - total, 2),
    }


# --- Period math -------------------------------------------------------------


def _default_anchor(today: dt.date) -> dt.date:
    """If the user hasn't set a payday yet, anchor to the most recent Friday."""
    return today - dt.timedelta(days=(today.weekday() - 4) % 7)


def current_period(cfg: PayConfig, today: dt.date) -> tuple[dt.date, dt.date, dt.date]:
    """Return (period_start, period_end_exclusive, next_payday) for `today`.

    Paydays fall on anchor + 14·k. The anchor is treated as a period boundary;
    the running period is the 14-day block containing today, paid on its end date.
    """
    if cfg.pay_schedule == "semimonthly":
        return _semimonthly_period(today)
    anchor = dt.date.fromisoformat(cfg.anchor_payday) if cfg.anchor_payday else _default_anchor(today)
    span = cfg.period_days
    delta = (today - anchor).days
    k = delta // span  # floor, works for negatives too
    period_start = anchor + dt.timedelta(days=span * k)
    period_end = period_start + dt.timedelta(days=span)  # exclusive boundary
    payday = period_end + dt.timedelta(days=cfg.pay_lag_days)
    return period_start, period_end, payday


def _month_end(d: dt.date) -> dt.date:
    nxt = dt.date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
    return nxt - dt.timedelta(days=1)


def _semimonthly_period(today: dt.date) -> tuple[dt.date, dt.date, dt.date]:
    """Work 1–15 pays on the 15th; work 16–EOM pays on the 30th (clamped to EOM)."""
    if today.day <= 15:
        start = today.replace(day=1)
        end = today.replace(day=16)                    # exclusive
        payday = today.replace(day=15)
    else:
        start = today.replace(day=16)
        eom = _month_end(today)
        end = eom + dt.timedelta(days=1)               # exclusive
        payday = min(today.replace(day=min(30, eom.day)), eom)
    return start, end, payday


def prev_period_start(cfg: PayConfig, start: dt.date) -> dt.date:
    """The start of the pay period immediately before the one starting at `start`."""
    if cfg.pay_schedule == "semimonthly":
        if start.day == 16:
            return start.replace(day=1)
        prev_month_last = start - dt.timedelta(days=1)
        return prev_month_last.replace(day=16)
    return start - dt.timedelta(days=cfg.period_days)


def period_bounds(cfg: PayConfig, start: dt.date) -> tuple[dt.date, dt.date]:
    """(end_exclusive, payday) for the period starting at `start`."""
    if cfg.pay_schedule == "semimonthly":
        if start.day == 1:
            return start.replace(day=16), start.replace(day=15)
        eom = _month_end(start)
        return eom + dt.timedelta(days=1), min(start.replace(day=min(30, eom.day)), eom)
    end = start + dt.timedelta(days=cfg.period_days)
    return end, end + dt.timedelta(days=cfg.pay_lag_days)


def _accrue(cfg: PayConfig, entries: dict[str, list[dict]], overrides: dict[str, dict],
            start: dt.date, end: dt.date, running_cutoff: dt.date | None = None) -> dict:
    """Sum hours by payroll week. A week with a manual override uses its explicit
    regular/overtime totals; otherwise hours come from daily entries + auto-fill and
    are split at the 40h threshold.

    Two partial-week modes:
    - Period boundary (running_cutoff=None): a Sun–Sat week only partly inside
      [start, end) — e.g. a semi-monthly cut — is prorated by its days-in-window so
      hours aren't double-counted across adjacent checks.
    - Running-to-date (running_cutoff=today): override hours are ACTUAL hours worked
      so far, so an override week that has started counts IN FULL (no calendar
      proration); non-override days still only count through today via the day loop."""
    weeks: dict[dt.date, float] = {}
    days_in: dict[dt.date, int] = {}
    for i in range((end - start).days):
        day = start + dt.timedelta(days=i)
        wk = _week_start(day)
        weeks[wk] = weeks.get(wk, 0.0) + hours_for_day(day, entries, cfg)
        days_in[wk] = days_in.get(wk, 0) + 1
    reg = ot = 0.0
    for wk, hrs in weeks.items():
        ov = overrides.get(wk.isoformat())
        if ov:
            if running_cutoff is not None:
                frac = 1.0 if wk <= running_cutoff else 0.0
            else:
                frac = min(1.0, days_in[wk] / 7.0)
            reg += ov["regular"] * frac
            ot += ov["overtime"] * frac
        else:
            reg += min(cfg.ot_threshold, hrs)
            ot += max(0.0, hrs - cfg.ot_threshold)
    gross = round(reg * cfg.rate + ot * cfg.rate * cfg.ot_multiplier, 2)
    return {"reg_hours": round(reg, 2), "ot_hours": round(ot, 2), "gross": gross}


def compute_status(store: Store, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    cfg = PayConfig.load(store)
    start, end, next_payday = current_period(cfg, today)

    rows = store.list_time_entries(start.isoformat(), (end - dt.timedelta(days=1)).isoformat())
    entries: dict[str, list[dict]] = {}
    for r in rows:
        entries.setdefault(r["work_date"], []).append(r)
    overrides = store.hours_weeks()

    # Running = actual hours to date (override weeks that have started count in full);
    # projected = the whole period.
    through = min(today + dt.timedelta(days=1), end)
    running = _accrue(cfg, entries, overrides, start, through, running_cutoff=today)
    projected = _accrue(cfg, entries, overrides, start, end)

    run_tax = estimate_withholding(running["gross"], cfg)
    proj_tax = estimate_withholding(projected["gross"], cfg)

    # Semi-monthly paydays sit INSIDE the period (the 15th/30th); if today is past
    # this period's payday, the next check the user sees is the following period's.
    if next_payday < today:
        nxt_start = end
        _, next_payday = period_bounds(cfg, nxt_start)

    return {
        "period_start": start.isoformat(),
        "period_end": (end - dt.timedelta(days=1)).isoformat(),
        "next_payday": next_payday.isoformat(),
        "days_until_payday": (next_payday - today).days,
        "rate": cfg.rate,
        "running": {**running, **run_tax},
        "projected": {**projected, **proj_tax},
        "config": cfg.__dict__,
    }


# --- Unpaid checks (per pay period, from stored hours) -------------------------


def _period_net(store: Store, cfg: PayConfig, start: dt.date, end: dt.date) -> dict:
    rows = store.list_time_entries(start.isoformat(), (end - dt.timedelta(days=1)).isoformat())
    entries: dict[str, list[dict]] = {}
    for r in rows:
        entries.setdefault(r["work_date"], []).append(r)
    acc = _accrue(cfg, entries, store.hours_weeks(), start, end)
    return {**acc, **estimate_withholding(acc["gross"], cfg)}


def pay_checks(store: Store, today: dt.date | None = None, lookback: int = 3) -> dict:
    """Recent pay periods as 'checks': completed ones are unpaid until deposited;
    the current period shows as accruing. Deposit status lives in settings.check_status."""
    today = today or dt.date.today()
    cfg = PayConfig.load(store)
    cur_start, cur_end, _ = current_period(cfg, today)
    status: dict = store.get_setting("check_status", {}) or {}

    # Walk back `lookback` completed periods using schedule-aware boundaries.
    starts: list[dt.date] = []
    s = cur_start
    for _ in range(lookback):
        s = prev_period_start(cfg, s)
        starts.append(s)
    checks = []
    for start in reversed(starts):             # oldest completed first
        end, payday = period_bounds(cfg, start)
        acc = _period_net(store, cfg, start, end)
        if acc["gross"] <= 0:
            continue                            # no hours that period — not a check
        st = status.get(start.isoformat())
        checks.append({
            "period_start": start.isoformat(),
            "period_end": (end - dt.timedelta(days=1)).isoformat(),
            "payday": payday.isoformat(),
            **acc,
            "state": "deposited" if st else "unpaid",
            "deposit": st,
        })
    cur = _period_net(store, cfg, cur_start, cur_end)
    _, cur_payday = period_bounds(cfg, cur_start)
    current = {
        "period_start": cur_start.isoformat(),
        "period_end": (cur_end - dt.timedelta(days=1)).isoformat(),
        "payday": cur_payday.isoformat(),
        **cur,
        "state": "accruing",
    }
    unpaid_total = round(sum(c["net"] for c in checks if c["state"] == "unpaid"), 2)
    return {"checks": checks, "current": current, "unpaid_total": unpaid_total}


def deposit_check(store: Store, period_start: str, account_id: int | None,
                  amount: float | None = None, gross: float | None = None) -> dict:
    """Mark a period's check as hit-the-bank: credit a cash account, record status.

    `amount` overrides the computed estimate with the ACTUAL net from a paystub
    (Atlas's withholding is only an estimate), and `gross` records the actual gross."""
    import time as _t

    cfg = PayConfig.load(store)
    start = dt.date.fromisoformat(period_start)
    end, _payday = period_bounds(cfg, start)
    est = _period_net(store, cfg, start, end)
    net = round(float(amount), 2) if amount is not None else est["net"]
    status: dict = store.get_setting("check_status", {}) or {}
    if period_start in status:
        raise ValueError("already deposited")
    accounts = store.list_accounts()
    acct = next((a for a in accounts if a["id"] == account_id), None) if account_id \
        else next((a for a in accounts if a["type"] == "cash"), None)
    if acct is None:
        raise ValueError("no cash account to deposit into — add one on the Net Worth tab")
    store.update_account(acct["id"], balance=round(acct["balance"] + net, 2))
    status[period_start] = {
        "amount": net, "gross": gross if gross is not None else est["gross"],
        "actual": amount is not None, "account_id": acct["id"], "account": acct["name"], "ts": _t.time(),
    }
    store.set_setting("check_status", status)
    return {"deposited": net, "account": acct["name"], "actual": amount is not None}


def undo_deposit(store: Store, period_start: str) -> dict:
    """Reverse a mistaken deposit: subtract the recorded amount, clear status."""
    status: dict = store.get_setting("check_status", {}) or {}
    st = status.pop(period_start, None)
    if not st:
        raise ValueError("not deposited")
    acct = next((a for a in store.list_accounts() if a["id"] == st["account_id"]), None)
    if acct:
        store.update_account(acct["id"], balance=round(acct["balance"] - st["amount"], 2))
    store.set_setting("check_status", status)
    return {"reversed": st["amount"]}
