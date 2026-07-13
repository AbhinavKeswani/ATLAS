"""Company news, peers, and profile for a stock pick.

Two providers, chosen at call time:
  - Finnhub (free tier) when ATLAS_FINNHUB_KEY is set — freshest news + peer list.
  - Yahoo Finance (keyless) fallback — quoteSummary for sector/industry/summary,
    search endpoint for recent headlines.

Everything is best-effort and returns plain dicts/lists; the Claude bridge later
synthesizes these into the pick thesis. No hard dependency: with no key and Yahoo
unreachable, callers just get empty lists.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .config import FINNHUB_KEY

log = logging.getLogger("atlas.news")

_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
_FINNHUB = "https://finnhub.io/api/v1"


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
        except Exception as e:  # noqa: BLE001
            log.info("curl fetch failed: %s", e)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        log.info("urllib fetch failed: %s", e)
        return None


def _json(url: str) -> object | None:
    body = _fetch(url)
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


# --- News --------------------------------------------------------------------


def _finnhub_news(symbol: str, days: int, limit: int) -> list[dict]:
    now = int(time.time())
    frm = datetime.fromtimestamp(now - days * 86400, tz=timezone.utc).strftime("%Y-%m-%d")
    to = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    url = f"{_FINNHUB}/company-news?symbol={urllib.parse.quote(symbol)}&from={frm}&to={to}&token={FINNHUB_KEY}"
    data = _json(url)
    if not isinstance(data, list):
        return []
    out = []
    for a in data[:limit]:
        if not isinstance(a, dict):
            continue
        ts = a.get("datetime")
        out.append({
            "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else "",
            "headline": (a.get("headline") or "").strip(),
            "source": (a.get("source") or "").strip(),
            "url": (a.get("url") or "").strip(),
            "summary": (a.get("summary") or "").strip(),
        })
    return out


def _yahoo_news(symbol: str, limit: int) -> list[dict]:
    url = f"https://query1.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(symbol)}&newsCount={limit}&quotesCount=0"
    data = _json(url)
    if not isinstance(data, dict):
        return []
    out = []
    for a in (data.get("news") or [])[:limit]:
        if not isinstance(a, dict):
            continue
        ts = a.get("providerPublishTime")
        out.append({
            "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else "",
            "headline": (a.get("title") or "").strip(),
            "source": (a.get("publisher") or "").strip(),
            "url": (a.get("link") or "").strip(),
            "summary": "",
        })
    return out


def _yfinance_news(symbol: str, limit: int) -> list[dict]:
    """Recent articles via yfinance (handles both old and new .news shapes)."""
    try:
        import yfinance as yf
    except ImportError:
        return []
    try:
        t = yf.Ticker(symbol)
        # get_news(count=...) reaches further back than the ~8-item .news
        # property — needed for the quarter-window sentiment score.
        try:
            items = t.get_news(count=max(limit, 20)) or []
        except (TypeError, AttributeError):
            items = t.news or []
    except Exception as e:  # noqa: BLE001
        log.info("yfinance news failed for %s: %s", symbol, e)
        return []
    out = []
    for a in items[:limit]:
        c = a.get("content") if isinstance(a, dict) else None
        if isinstance(c, dict):  # newer yfinance shape
            prov = (c.get("provider") or {}).get("displayName", "")
            url = ((c.get("canonicalUrl") or {}).get("url")) or ((c.get("clickThroughUrl") or {}).get("url")) or ""
            out.append({
                "date": (c.get("pubDate") or "")[:10],
                "headline": (c.get("title") or "").strip(),
                "source": prov, "url": url,
                "summary": (c.get("summary") or c.get("description") or "").strip(),
            })
        elif isinstance(a, dict):  # older shape
            ts = a.get("providerPublishTime")
            out.append({
                "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else "",
                "headline": (a.get("title") or "").strip(),
                "source": (a.get("publisher") or "").strip(),
                "url": (a.get("link") or "").strip(), "summary": "",
            })
    return [a for a in out if a["headline"]]


def company_news(symbol: str, days: int = 92, limit: int = 16) -> list[dict]:
    """Headlines for the last `days` (default: one quarter), newest first.

    Finnhub if keyed (true date-range query), then yfinance, then Yahoo search.
    Articles older than the window are dropped; keyless sources may not reach a
    full quarter back — the sentiment aggregation reports how many articles it
    actually had.
    """
    symbol = symbol.upper().strip()
    if FINNHUB_KEY:
        articles = _finnhub_news(symbol, days, limit)
        if articles:
            return articles
    articles = _yfinance_news(symbol, limit) or _yahoo_news(symbol, limit)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    fresh = [a for a in articles if not a.get("date") or a["date"] >= cutoff]
    fresh.sort(key=lambda a: a.get("date") or "", reverse=True)
    return fresh[:limit]


def market_news(limit: int = 15) -> list[dict]:
    """Market-wide headlines for the daily outlook: merge the index-ETF feeds
    (SPY/QQQ/DIA cover broad market, tech, and blue-chip narratives), dedupe by
    headline, newest first."""
    merged: list[dict] = []
    seen: set[str] = set()
    for sym in ("SPY", "QQQ", "DIA"):
        for a in company_news(sym, days=7, limit=limit):
            key = (a.get("headline") or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(a)
    merged.sort(key=lambda a: a.get("date") or "", reverse=True)
    return merged[:limit]


# --- Peers + profile ---------------------------------------------------------


def peers(symbol: str, limit: int = 6) -> list[str]:
    """Peer tickers — Finnhub /stock/peers if keyed, else empty."""
    symbol = symbol.upper().strip()
    if FINNHUB_KEY:
        data = _json(f"{_FINNHUB}/stock/peers?symbol={urllib.parse.quote(symbol)}&token={FINNHUB_KEY}")
        if isinstance(data, list):
            return [p for p in data if isinstance(p, str) and p.upper() != symbol][:limit]
    return []


def officers(symbol: str, limit: int = 8) -> list[dict]:
    """C-suite roster (name, title, age, pay) from yfinance. Career history is
    generated by the Claude analysis layer — this is just the factual roster."""
    try:
        import yfinance as yf
    except ImportError:
        return []
    try:
        info = yf.Ticker(symbol.upper()).info or {}
    except Exception as e:  # noqa: BLE001
        log.info("yfinance officers failed for %s: %s", symbol, e)
        return []
    out = []
    for o in (info.get("companyOfficers") or [])[:limit]:
        if not isinstance(o, dict) or not o.get("name"):
            continue
        out.append({
            "name": str(o.get("name", "")).strip(),
            "title": str(o.get("title", "")).strip(),
            "age": o.get("age"),
            "total_pay": o.get("totalPay"),
        })
    return out


def company_bundle(symbol: str) -> dict:
    """Profile + officers from ONE yfinance .info fetch (it's the same underlying
    scrape — calling profile() and officers() separately pays it twice)."""
    symbol = symbol.upper().strip()
    info = {}
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info or {}
    except Exception as e:  # noqa: BLE001
        log.info("yfinance bundle failed for %s: %s", symbol, e)
    profile_d = {
        "sector": (info.get("sector") or "").strip(),
        "industry": (info.get("industry") or "").strip(),
        "summary": (info.get("longBusinessSummary") or "").strip(),
        "website": (info.get("website") or "").strip(),
    }
    officers_l = []
    for o in (info.get("companyOfficers") or [])[:8]:
        if isinstance(o, dict) and o.get("name"):
            officers_l.append({
                "name": str(o.get("name", "")).strip(),
                "title": str(o.get("title", "")).strip(),
                "age": o.get("age"),
                "total_pay": o.get("totalPay"),
            })
    if not profile_d["sector"] and not profile_d["summary"]:
        profile_d = profile(symbol)  # keyless Yahoo quoteSummary fallback
    return {"profile": profile_d, "officers": officers_l}


def _yfinance_profile(symbol: str) -> dict:
    try:
        import yfinance as yf
    except ImportError:
        return {}
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as e:  # noqa: BLE001
        log.info("yfinance profile failed for %s: %s", symbol, e)
        return {}
    return {
        "sector": (info.get("sector") or "").strip(),
        "industry": (info.get("industry") or "").strip(),
        "summary": (info.get("longBusinessSummary") or "").strip(),
        "website": (info.get("website") or "").strip(),
    }


def profile(symbol: str) -> dict:
    """Company background (sector / industry / business summary) — yfinance, else Yahoo curl."""
    symbol = symbol.upper().strip()
    prof = _yfinance_profile(symbol)
    if prof.get("sector") or prof.get("summary"):
        return prof
    url = (
        f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(symbol)}"
        "?modules=assetProfile"
    )
    data = _json(url)
    try:
        p = data["quoteSummary"]["result"][0]["assetProfile"]  # type: ignore[index]
    except (TypeError, KeyError, IndexError):
        p = {}
    return {
        "sector": (p.get("sector") or "").strip(),
        "industry": (p.get("industry") or "").strip(),
        "summary": (p.get("longBusinessSummary") or "").strip(),
        "website": (p.get("website") or "").strip(),
    }
