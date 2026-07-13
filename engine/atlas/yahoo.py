"""Latest equity prices scraped from Yahoo Finance — on-demand, no API key, no deps.

Yahoo rate-limits (429) requests with Python's urllib fingerprint, but a plain `curl`
gets 200, so we shell out to curl to fetch the chart JSON (the same data the quote page
uses). Called when the user hits "Refresh prices"; the price is stored on each holding's
last_price so valuation stays fast between refreshes.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import urllib.parse
import urllib.request

log = logging.getLogger("atlas.yahoo")

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"


def _fetch(url: str, timeout: float = 12.0) -> str | None:
    """GET a URL, preferring curl (Yahoo 429s urllib); fall back to urllib."""
    if shutil.which("curl"):
        try:
            r = subprocess.run(
                ["curl", "-s", "-m", str(int(timeout)), "-A", _UA, url],
                capture_output=True, timeout=timeout + 4,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout.decode("utf-8", "replace")
        except Exception as e:
            log.info("curl fetch failed: %s", e)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception as e:
        log.info("urllib fetch failed: %s", e)
        return None


def latest_price(symbol: str) -> float | None:
    sym = urllib.parse.quote(symbol.upper().strip())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d"
    body = _fetch(url)
    if not body:
        return None
    try:
        meta = json.loads(body)["chart"]["result"][0]["meta"]
        px = meta.get("regularMarketPrice")
        return float(px) if px is not None else None
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as e:
        log.info("yahoo parse failed for %s: %s", symbol, e)
        return None


def _series_via_yfinance(symbol: str, range_: str, interval: str) -> list[dict]:
    """Adjusted-close series via yfinance. Returns [] if unavailable."""
    try:
        import yfinance as yf
    except ImportError:
        return []
    try:
        hist = yf.Ticker(symbol).history(period=range_, interval=interval, auto_adjust=True)
        if hist is None or hist.empty or "Close" not in hist.columns:
            return []
        stamp = "%Y-%m-%d %H:%M" if interval.endswith(("m", "h")) else "%Y-%m-%d"
        out: list[dict] = []
        for idx, px in hist["Close"].items():
            if px is None:
                continue
            try:
                out.append({"date": idx.strftime(stamp), "close": round(float(px), 4)})
            except (ValueError, TypeError):
                continue
        return out
    except Exception as e:  # noqa: BLE001 - any yfinance failure degrades to the chart fallback
        log.info("yfinance series failed for %s: %s", symbol, e)
        return []


def _series_via_chart(symbol: str, range_: str, interval: str) -> list[dict]:
    """Fallback: Yahoo chart JSON (adjclose track). query1 429s under load → query2 + retry."""
    sym = urllib.parse.quote(symbol.upper().strip())
    body = None
    for attempt in range(3):
        host = "query2" if attempt % 2 else "query1"
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?interval={interval}&range={range_}"
        body = _fetch(url)
        if body and "Too Many Requests" not in body[:64] and '"chart"' in body[:200]:
            break
        body = None
        time.sleep(0.8 * (attempt + 1))
    if not body:
        return []
    try:
        result = json.loads(body)["chart"]["result"][0]
        timestamps = result.get("timestamp") or []
        closes = result["indicators"]["quote"][0].get("close") or []
        adj = result.get("indicators", {}).get("adjclose")
        series = adj[0].get("adjclose") if adj else closes
        intraday = interval.endswith(("m", "h"))
        stamp = "%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"
        out: list[dict] = []
        for ts, px in zip(timestamps, series or closes):
            if ts is None or px is None:
                continue
            out.append({"date": time.strftime(stamp, time.gmtime(ts)), "close": round(float(px), 4)})
        return out
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as e:
        log.info("yahoo chart series failed for %s: %s", symbol, e)
        return []


def daily_series(symbol: str, range_: str = "1y", interval: str = "1d") -> list[dict]:
    """Adjusted-close price series for `symbol` as [{date, close}] (oldest→newest).

    Primary source is yfinance (split/dividend-adjusted close). HYDRA only caches
    1-minute bars locally, so history comes from Yahoo. `interval` defaults to daily
    ("1d"); "1h" gives hourly where Yahoo has it (shorter ranges only). Falls back to
    the Yahoo chart JSON endpoint if yfinance is unavailable or returns nothing.
    """
    symbol = symbol.upper().strip()
    return _series_via_yfinance(symbol, range_, interval) or _series_via_chart(symbol, range_, interval)


def latest_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch each symbol with a small delay + one retry to avoid Yahoo's burst 429s."""
    out: dict[str, float] = {}
    for i, s in enumerate(symbols):
        if i:
            time.sleep(0.5)
        px = latest_price(s)
        if px is None:
            time.sleep(1.2)
            px = latest_price(s)  # one retry after a longer pause
        if px is not None:
            out[s.upper()] = px
    return out
