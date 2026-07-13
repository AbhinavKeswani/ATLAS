"""Portfolio valuation: holdings × live HYDRA prices, plus order import.

Holdings can be entered directly or rebuilt from imported order fills. Pricing comes
from hydra_prices; symbols HYDRA doesn't cover are flagged so the UI can offer to
subscribe them. The brokerage total feeds Net Worth.
"""

from __future__ import annotations

import csv
import io
import time

from . import yahoo
from .store import Store


def valuation(store: Store) -> dict:
    """Value positions from each holding's stored price (last refreshed from Yahoo),
    falling back to cost basis. Prices are refreshed on demand via refresh_prices()."""
    holdings = store.list_holdings()
    positions = []
    total_value = total_cost = 0.0
    unpriced = []
    for h in holdings:
        if h.get("last_price"):
            px, source = h["last_price"], "market"
        else:
            px, source = h["cost_basis"], "cost"
            unpriced.append(h["symbol"])
        value = (px or 0.0) * h["qty"]
        cost = (h["cost_basis"] or 0.0) * h["qty"]
        total_value += value
        total_cost += cost
        positions.append({
            **h,
            "price": px,
            "priced": source == "market",
            "source": source,
            "value": round(value, 2),
            "unrealized": round(value - cost, 2) if h["cost_basis"] else None,
        })
    return {
        "positions": positions,
        "total_value": round(total_value, 2),
        "total_cost": round(total_cost, 2),
        "total_unrealized": round(total_value - total_cost, 2) if total_cost else None,
        "unpriced_symbols": sorted(set(unpriced)),
    }


def refresh_prices(store: Store) -> dict:
    """Scrape the latest price for each held symbol from Yahoo Finance and store it."""
    holdings = store.list_holdings()
    symbols = sorted({h["symbol"] for h in holdings})
    if not symbols:
        return {"updated": 0, "failed": []}
    prices = yahoo.latest_prices(symbols)
    updated = 0
    for h in holdings:
        px = prices.get(h["symbol"])
        if px is not None:
            store.upsert_holding(h["symbol"], h["qty"], h["cost_basis"], h["account"], last_price=px)
            updated += 1
    failed = [s for s in symbols if s not in prices]
    return {"updated": updated, "failed": failed, "prices": prices}


def import_orders_csv(store: Store, text: str) -> dict:
    """Import a Merrill-style order CSV. Tolerant header matching.

    Recognized columns (case-insensitive, fuzzy): symbol/ticker, side/action,
    qty/quantity/shares, price, date/trade date.
    """
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return {"imported": 0, "error": "empty or unparseable CSV"}

    def find(*names: str) -> str | None:
        for f in reader.fieldnames or []:
            low = f.strip().lower()
            if any(n in low for n in names):
                return f
        return None

    c_sym = find("symbol", "ticker")
    c_side = find("side", "action", "type")
    c_qty = find("quantity", "qty", "shares")
    c_price = find("price")
    c_date = find("date")
    if not (c_sym and c_qty and c_price):
        return {"imported": 0, "error": "missing symbol/qty/price columns"}

    imported = 0
    for row in reader:
        sym = (row.get(c_sym) or "").strip()
        if not sym:
            continue
        try:
            qty = abs(float(str(row.get(c_qty, "0")).replace(",", "")))
            price = float(str(row.get(c_price, "0")).replace("$", "").replace(",", ""))
        except ValueError:
            continue
        raw_side = (row.get(c_side, "") or "").strip().lower() if c_side else ""
        side = "sell" if "sell" in raw_side or "sld" in raw_side else "buy"
        ts = time.time()
        if c_date and row.get(c_date):
            ts = _parse_ts(row[c_date]) or ts
        store.add_order(sym, side, qty, price, ts, account=None)
        imported += 1

    store.rebuild_holdings_from_orders()
    return {"imported": imported}


def _parse_ts(s: str) -> float | None:
    import datetime as dt

    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).timestamp()
        except ValueError:
            continue
    return None
