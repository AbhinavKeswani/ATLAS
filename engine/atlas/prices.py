"""Daily adjusted-close prices for the Picks charts — batched + cached.

We pull once with yfinance's multi-ticker `download` (a few HTTP calls for the whole
S&P 500, not one per name) and cache into the `price_history` table. From then on a
daily job only fills the gap (new sessions since the last stored date), so we stop
re-pulling history. Charts read straight from the cache.

Benchmarks for the pop-out chart: the company's GICS-sector SPDR ETF, SPY (the whole
market), and an equal-weight basket of its sub-industry peers — all normalized to %
change on the client.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .store import Store

log = logging.getLogger("atlas.prices")

# GICS sector → SPDR sector ETF (keyless via yfinance).
SECTOR_ETF = {
    "Information Technology": "XLK",
    "Health Care": "XLV",
    "Financials": "XLF",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Communication Services": "XLC",
    "Industrials": "XLI",
    "Energy": "XLE",
    "Utilities": "XLU",
    "Real Estate": "XLRE",
    "Materials": "XLB",
    # Label variants (older universe used short GICS-ish names; yfinance profiles
    # use their own sector naming for looked-up tickers outside the universe).
    "Technology": "XLK",
    "Health": "XLV",
    "Telecommunication Services": "XLC",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Basic Materials": "XLB",
}
MARKET_ETF = "SPY"

_RANGE_DAYS = {"1mo": 31, "3mo": 93, "6mo": 186, "1y": 372, "2y": 745, "5y": 1830, "max": 20000}


def _cutoff(range_: str) -> str:
    days = _RANGE_DAYS.get(range_, 372)
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def batch_download(symbols: list[str], period: str = "2y", interval: str = "1d", chunk: int = 100) -> dict[str, list[dict]]:
    """Adjusted-close series for many symbols in a few requests. {SYMBOL: [{date, close}]}."""
    try:
        import yfinance as yf
    except ImportError:
        return {}
    syms = sorted({s.upper().strip() for s in symbols if s and s.strip()})
    out: dict[str, list[dict]] = {}
    for i in range(0, len(syms), chunk):
        batch = syms[i:i + chunk]
        yq = {s: s.replace(".", "-") for s in batch}  # Yahoo uses BRK-B, not BRK.B
        try:
            df = yf.download(list(yq.values()), period=period, interval=interval, group_by="ticker",
                             auto_adjust=True, progress=False, threads=True)
        except Exception as e:  # noqa: BLE001
            log.info("batch_download failed (%d syms): %s", len(batch), e)
            continue
        for s in batch:
            try:
                # group_by="ticker" yields a MultiIndex keyed by ticker even for one symbol.
                sub = df[yq[s]] if yq[s] in df.columns.get_level_values(0) else df
                ser = sub["Close"].dropna()
            except Exception:
                continue
            rows = [{"date": idx.strftime("%Y-%m-%d"), "close": round(float(v), 4)} for idx, v in ser.items()]
            if rows:
                out[s] = rows  # keyed by the canonical (dotted) symbol
    return out


def gap_fill(store: Store, symbols: list[str]) -> dict:
    """Fetch only the daily closes newer than what's cached, per symbol. Batched.

    Symbols with no history get a 2y backfill; symbols already tracked get a short
    pull (recent sessions) to fill forward. This is the daily-ingest workhorse.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fresh, stale = [], []
    for s in {x.upper() for x in symbols}:
        last = store.last_price_date(s)
        if last is None:
            fresh.append(s)
        elif last < today:
            stale.append(s)
    filled = 0
    if fresh:
        for sym, rows in batch_download(fresh, period="2y").items():
            filled += store.upsert_prices(sym, rows)
    if stale:
        # 1mo covers any weekend/holiday gap for daily names updated regularly.
        for sym, rows in batch_download(stale, period="1mo").items():
            filled += store.upsert_prices(sym, rows)
    return {"symbols": len(fresh) + len(stale), "backfilled": len(fresh), "updated": len(stale), "rows": filled}


def series(store: Store, symbol: str, range_: str = "1y") -> list[dict]:
    """Cached daily closes for a symbol within `range_`; fetch+cache on a cache miss."""
    symbol = symbol.upper()
    rows = store.get_price_series(symbol, since=_cutoff(range_))
    if not rows:
        dl = batch_download([symbol], period="2y").get(symbol, [])
        if dl:
            store.upsert_prices(symbol, dl)
            rows = store.get_price_series(symbol, since=_cutoff(range_))
    return rows


def benchmark_series(store: Store, sector: str, range_: str) -> dict[str, list[dict]]:
    """Sector SPDR ETF + SPY series (cached like everything else)."""
    out: dict[str, list[dict]] = {}
    etf = SECTOR_ETF.get(sector)
    if etf:
        out[etf] = series(store, etf, range_)
    out[MARKET_ETF] = series(store, MARKET_ETF, range_)
    return out


def peer_basket(store: Store, peers: list[str], range_: str) -> list[dict]:
    """Equal-weight, %-normalized basket of peers we have prices for. [{date, close}] where
    `close` is an index (100 = start) so the client can plot it alongside real series."""
    cutoff = _cutoff(range_)
    serieses = []
    for p in peers:
        s = store.get_price_series(p.upper(), since=cutoff)
        if len(s) > 2:
            serieses.append(s)
    if not serieses:
        return []
    # Align on common dates, rebase each to 100, then average.
    from collections import defaultdict
    norm_by_date: dict[str, list[float]] = defaultdict(list)
    for s in serieses:
        base = s[0]["close"] or 1.0
        for pt in s:
            norm_by_date[pt["date"]].append(pt["close"] / base * 100.0)
    return [{"date": d, "close": round(sum(v) / len(v), 4)} for d, v in sorted(norm_by_date.items())]
