"""Daily Picks job — run standalone (cron), no server required.

  1. Re-score the cached S&P 500 universe with freshly batched prices (no SEC ingest —
     fundamentals change quarterly; this just refreshes valuation/ranking).
  2. Gap-fill the daily adjusted-close cache for the universe + your holdings +
     benchmark ETFs, so the pop-out charts stay current.

Today's 10 fresh recommendations are then served on demand by /api/picks/daily
(top-ranked names you don't already own). Run:

    cd engine && .venv/bin/python -m atlas.daily_job
"""

from __future__ import annotations

import time

from . import commonsense_bridge, prices
from .store import Store


def run() -> dict:
    store = Store()

    # 1. Refresh scores/ranking on fresh prices (cached facts only — cheap, no SEC).
    screen = commonsense_bridge.run_screen(None, ingest=False)
    ranked = screen.get("picks") or []
    if "picks" in screen:
        store.set_setting("picks", {**screen, "refreshed_at": time.time()})

    # 2. Gap-fill daily adjusted-close prices for everything the charts need.
    universe = {p["symbol"] for p in ranked}
    universe |= {str(h.get("symbol", "")).upper() for h in store.list_holdings() if h.get("symbol")}
    universe |= set(prices.SECTOR_ETF.values()) | {prices.MARKET_ETF}
    fill = prices.gap_fill(store, sorted(universe))

    summary = {"ran_at": time.time(), "ranked": len(ranked), "universe": len(universe), "price_fill": fill}
    print(f"[daily_job] ranked={len(ranked)} universe={len(universe)} price_fill={fill}")
    return summary


if __name__ == "__main__":
    run()
