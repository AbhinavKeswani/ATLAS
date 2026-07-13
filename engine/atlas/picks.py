"""Stock Picks: fundamentals-driven long-term candidates for the dashboard.

The pick DECISION is mathematical — CommonSense scores each company (quality +
mispricing). Atlas assembles a report around that decision in two phases so the
drawer opens instantly:

  fast (pick_detail)   — no LLM: score + methodology, valuation multiples, the
                         yfinance price chart, company profile, and raw news.
  analysis (pick_analysis) — one Claude call, cached: a thesis grounded in the
                         score data, GICS competitors, and a semantic read of each
                         news article in the company's + industry's context.

Ranked picks cache in the `picks` setting; per-symbol fast detail is reassembled
on open (cheap); the Claude analysis caches in `pick_analysis:<SYMBOL>`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from . import commonsense_bridge, news, prices
from .claude_bridge import ClaudeBridge, ClaudeError, PICK_ANALYSIS_FROM_DATA
from .store import Store

log = logging.getLogger("atlas.picks")

_PICKS_KEY = "picks"
# v3: the analysis caches its news SNAPSHOT + aggregated sentiment. Bump the
# prefix whenever the analysis shape changes so old caches regenerate lazily.
# The snapshot is what makes reports deterministic: annotations always render
# against the articles the analysis actually read, not today's live feed.
_ANALYSIS_PREFIX = "pick_analysis_v3:"
_CLOSED_KEY = "picks_closed"        # {SYMBOL: closed_at_epoch} — shown less often
_DAILY_COUNT = 10
_CLOSED_COOLDOWN_DAYS = 30
_NEWS_WINDOW_DAYS = 92              # one quarter of headlines feeds the analysis
_SHORT_SENTIMENT_DAYS = 14

# One generation per symbol at a time — concurrent drawer opens share the result
# instead of racing Claude and caching whichever finished last.
_analysis_locks: dict[str, asyncio.Lock] = {}


def list_picks(store: Store) -> dict:
    """Cached ranked picks (from the last screener run)."""
    cached = store.get_setting(_PICKS_KEY, None)
    if isinstance(cached, dict) and "picks" in cached:
        return cached
    disk = commonsense_bridge.read_picks()
    if disk:
        store.set_setting(_PICKS_KEY, disk)
    return disk or {"count": 0, "picks": [], "commonsense_available": commonsense_bridge.available()}


async def refresh_picks(store: Store, *, limit: int | None = None, ingest: bool = True) -> dict:
    """Re-run the CommonSense screener (heavy: SEC ingest) and cache the ranked result."""
    result = await asyncio.to_thread(commonsense_bridge.run_screen, limit, ingest=ingest)
    if result.get("error"):
        return result
    if "picks" not in result:
        return {"error": "screener produced no picks.json — check CommonSense data path"}
    result["refreshed_at"] = time.time()
    store.set_setting(_PICKS_KEY, result)
    return result


async def daily_series(store: Store, symbol: str, range_: str = "1y") -> list[dict]:
    """Cached adjusted-close series for the drawer chart (price_history, fetch on miss)."""
    return await asyncio.to_thread(prices.series, store, symbol, range_)


def _sector_of(store: Store, symbol: str) -> str:
    for p in list_picks(store).get("picks", []):
        if p.get("symbol") == symbol:
            return p.get("sector", "")
    # Not in the ranked universe (e.g. a looked-up ticker): fall back to the
    # yfinance profile sector — SECTOR_ETF carries aliases for its naming.
    try:
        return news.profile(symbol).get("sector", "")
    except Exception:  # noqa: BLE001
        return ""


def _peers_of(store: Store, symbol: str, limit: int = 12) -> list[str]:
    """Sub-industry peers (fallback: sector) from the ranked universe, for the basket."""
    picks = list_picks(store).get("picks", [])
    me = next((p for p in picks if p.get("symbol") == symbol), None)
    if not me:
        return []
    sub, sec = me.get("sub_industry"), me.get("sector")
    peers = [p["symbol"] for p in picks if p.get("symbol") != symbol and sub and p.get("sub_industry") == sub]
    if len(peers) < 3:
        peers = [p["symbol"] for p in picks if p.get("symbol") != symbol and p.get("sector") == sec]
    return peers[:limit]


async def chart_bundle(store: Store, symbol: str, range_: str = "1y") -> dict:
    """Stock + benchmarks (sector ETF, SPY) + equal-weight peer basket, for the % chart."""
    symbol = symbol.upper()
    sector = _sector_of(store, symbol)
    peers = _peers_of(store, symbol)

    def _build() -> dict:
        stock = prices.series(store, symbol, range_)
        bench = prices.benchmark_series(store, sector, range_)
        basket = prices.peer_basket(store, peers, range_)
        series_map = {symbol: stock, **bench}
        if basket:
            series_map["Peer basket"] = basket
        return {"symbol": symbol, "sector": sector, "range": range_, "series": series_map,
                "peers": peers, "basket_prenormalized": bool(basket)}

    return await asyncio.to_thread(_build)


def _ttl_cached(store: Store, kind: str, symbol: str, ttl_s: float, fetch):
    """Settings-backed TTL cache with stale-grace: serve fresh cache instantly;
    on expiry re-fetch, but if the fetch fails/returns empty (Yahoo 429 backoff),
    fall back to the stale copy rather than making the user wait or see nothing."""
    key = f"pickcache:{kind}:{symbol}"
    cached = store.get_setting(key, None)
    now = time.time()
    if cached and (now - cached.get("at", 0)) < ttl_s:
        return cached.get("data")
    try:
        data = fetch()
    except Exception as e:  # noqa: BLE001
        log.info("%s fetch failed for %s: %s", kind, symbol, e)
        data = None
    if data:
        store.set_setting(key, {"at": now, "data": data})
        return data
    return cached.get("data") if cached else data


async def pick_detail(store: Store, symbol: str) -> dict:
    """FAST assembly (no LLM): score + methodology, chart, profile, raw news.

    Everything network-bound is TTL-cached (profile/officers rarely change; news
    isn't fetched at all when a cached analysis exists — the UI renders the
    analysis's own news snapshot). Repeat opens are then pure local reads.
    """
    symbol = symbol.upper().strip()
    cached_analysis = store.get_setting(_ANALYSIS_PREFIX + symbol, None)

    def _bundle():
        return _ttl_cached(store, "bundle", symbol, 7 * 86400, lambda: news.company_bundle(symbol))

    def _news():
        # The drawer shows the analysis's news snapshot when one exists; live
        # headlines are only needed pre-analysis.
        if cached_analysis and cached_analysis.get("news_snapshot"):
            return []
        return _ttl_cached(store, "news", symbol, 2 * 3600,
                           lambda: news.company_news(symbol, _NEWS_WINDOW_DAYS, 16)) or []

    scores = commonsense_bridge.read_scores(symbol)
    bundle, headlines, series = await asyncio.gather(
        asyncio.to_thread(_bundle),
        asyncio.to_thread(_news),
        daily_series(store, symbol),
    )
    bundle = bundle or {}
    mdna_cached = commonsense_bridge.read_mdna(symbol, max_chars=1)  # presence check only
    return {
        "symbol": symbol,
        "scores": scores,
        "profile": bundle.get("profile") or {},
        "news": headlines,
        "series": series,
        "officers": bundle.get("officers") or [],
        "mdna_available": bool(mdna_cached),
        "analysis": cached_analysis,
        "analysis_pending": cached_analysis is None,
    }


async def pick_mdna(symbol: str) -> dict:
    """MD&A for the drawer: fetch on demand (bulk screen skips it), then read cache."""
    symbol = symbol.upper().strip()
    docs = await asyncio.to_thread(commonsense_bridge.read_mdna, symbol)
    if not docs:
        fetched = await asyncio.to_thread(commonsense_bridge.fetch_mdna, symbol)
        if fetched.get("error"):
            return {"symbol": symbol, "docs": [], "error": fetched["error"]}
        docs = await asyncio.to_thread(commonsense_bridge.read_mdna, symbol)
    return {"symbol": symbol, "docs": docs}


def _held_symbols(store: Store) -> set[str]:
    return {str(h.get("symbol", "")).upper() for h in store.list_holdings() if h.get("symbol")}


def daily_picks(store: Store, *, count: int = _DAILY_COUNT, offset: int = 0) -> dict:
    """Today's fresh recommendations: top-ranked names NOT already owned.

    Owned ranked names are returned separately ("suggestions already owned"). Recently
    closed/dismissed picks are pushed to the back so they surface less often. "Show
    different" pages deeper via `offset`.
    """
    ranked = list_picks(store).get("picks", [])
    held = _held_symbols(store)
    closed = store.get_setting(_CLOSED_KEY, {}) or {}
    cutoff = time.time() - _CLOSED_COOLDOWN_DAYS * 86400
    recently_closed = {s for s, t in closed.items() if isinstance(t, (int, float)) and t >= cutoff}

    owned = [p for p in ranked if p["symbol"] in held]
    candidates = [p for p in ranked if p["symbol"] not in held]
    # Fresh first (rank order), recently-closed appended at the end.
    fresh = [p for p in candidates if p["symbol"] not in recently_closed]
    tail = [p for p in candidates if p["symbol"] in recently_closed]
    pool = fresh + tail
    window = pool[offset:offset + count]
    return {
        "picks": window,
        "owned": owned,
        "offset": offset,
        "count": len(window),
        "total_candidates": len(pool),
        "has_more": offset + count < len(pool),
        "commonsense_available": commonsense_bridge.available(),
    }


def close_pick(store: Store, symbol: str) -> dict:
    """Mark a pick closed/dismissed so it surfaces less often."""
    closed = store.get_setting(_CLOSED_KEY, {}) or {}
    closed[symbol.upper()] = time.time()
    store.set_setting(_CLOSED_KEY, closed)
    return {"closed": symbol.upper()}


# ---- Ticker lookup: reference our system first, pull + analyze on miss ----

def lookup_status(store: Store, symbol: str) -> dict:
    """Is this ticker already analyzed by our system? (No network.)"""
    symbol = symbol.upper().strip()
    scores = commonsense_bridge.read_scores(symbol)
    ranked = next((p for p in list_picks(store).get("picks", []) if p["symbol"] == symbol), None)
    return {
        "symbol": symbol,
        "in_system": bool(scores),
        "ranked": ranked is not None,
        "rank": ranked.get("rank") if ranked else None,
        "quality_score": scores.get("quality_score") if scores else None,
        "verdict": scores.get("verdict") if scores else None,
    }


async def run_lookup(store: Store, symbol: str) -> dict:
    """Pull SEC facts (if missing) + analyze + score one ticker on demand."""
    symbol = symbol.upper().strip()
    result = await asyncio.to_thread(commonsense_bridge.lookup_ticker, symbol)
    if result.get("error"):
        return result
    # Warm the price store so the breakout chart has history immediately.
    await asyncio.to_thread(prices.gap_fill, store, [symbol])
    return {**lookup_status(store, symbol), "pulled": True}


# ---- Watchlist (shared with the Money tab's `watchlist` setting) ----

def watchlist_view(store: Store) -> dict:
    """Watchlist enriched with our analysis state so the Picks tab can render it."""
    items = store.get_setting("watchlist", []) or []
    ranked = {p["symbol"]: p for p in list_picks(store).get("picks", [])}
    out = []
    for it in items:
        sym = str(it.get("symbol", "")).upper()
        if not sym:
            continue
        scores = commonsense_bridge.read_scores(sym)
        r = ranked.get(sym)
        out.append({
            "symbol": sym,
            "note": it.get("note") or "",
            "in_system": bool(scores),
            "quality_score": scores.get("quality_score") if scores else None,
            "verdict": scores.get("verdict") if scores else None,
            "rank": r.get("rank") if r else None,
            "mispricing": bool(r.get("mispricing")) if r else False,
            "price": (r or {}).get("price") or it.get("price"),
        })
    return {"items": out}


def watch(store: Store, symbol: str, note: str = "") -> dict:
    """Add a symbol to the shared watchlist (no-op if present)."""
    symbol = symbol.upper().strip()
    items = store.get_setting("watchlist", []) or []
    if not any(str(i.get("symbol", "")).upper() == symbol for i in items):
        items.append({"symbol": symbol, "note": note or "from picks", "added_at": time.time()})
        store.set_setting("watchlist", items)
    return watchlist_view(store)


def unwatch(store: Store, symbol: str) -> dict:
    symbol = symbol.upper().strip()
    items = [i for i in (store.get_setting("watchlist", []) or [])
             if str(i.get("symbol", "")).upper() != symbol]
    store.set_setting("watchlist", items)
    return watchlist_view(store)


async def run_daily_job(store: Store) -> dict:
    """Daily ingest: gap-fill adjusted-close prices for the universe + holdings +
    benchmarks. Fundamentals change quarterly, so this fills price gaps forward and
    lets daily_picks re-rank on fresh prices."""
    # Prefer the screener's on-disk output (it may be fresher than our cached copy —
    # e.g. after a bulk screen ran outside Atlas) and refresh the cache from it.
    disk = commonsense_bridge.read_picks()
    if disk and disk.get("picks"):
        store.set_setting(_PICKS_KEY, {**disk, "refreshed_at": time.time()})
    ranked = list_picks(store).get("picks", [])
    universe = {p["symbol"] for p in ranked}
    universe |= _held_symbols(store)
    universe |= set(prices.SECTOR_ETF.values()) | {prices.MARKET_ETF}
    fill = await asyncio.to_thread(prices.gap_fill, store, sorted(universe))
    # Generate today's market outlook on fresh prices (cached per day; best-effort).
    outlook_ok = None
    try:
        from . import market_outlook
        from .claude_bridge import ClaudeBridge
        res = await market_outlook.get_outlook(store, ClaudeBridge(store))
        outlook_ok = not (res.get("outlook") or {}).get("error")
    except Exception as e:  # noqa: BLE001 - outlook must never fail the price ingest
        log.warning("daily outlook generation failed: %s", e)
        outlook_ok = False
    return {"ran_at": time.time(), "price_fill": fill, "universe": len(universe), "outlook_generated": outlook_ok}


async def pick_analysis(store: Store, claude: ClaudeBridge, symbol: str, *, regenerate: bool = False) -> dict:
    """Claude analysis (cached): grounded thesis + GICS competitors + semantic news read."""
    symbol = symbol.upper().strip()
    cache_key = _ANALYSIS_PREFIX + symbol
    if not regenerate:
        cached = store.get_setting(cache_key, None)
        if cached:
            return cached

    if not claude.available:
        return {"error": "Claude CLI unavailable — showing scored data only."}

    lock = _analysis_locks.setdefault(symbol, asyncio.Lock())
    async with lock:
        # A concurrent open may have generated while we waited on the lock.
        if not regenerate:
            cached = store.get_setting(cache_key, None)
            if cached:
                return cached

        scores = commonsense_bridge.read_scores(symbol)

        def _bundle():
            return _ttl_cached(store, "bundle", symbol, 7 * 86400, lambda: news.company_bundle(symbol)) or {}

        def _news():
            return _ttl_cached(store, "news", symbol, 2 * 3600,
                               lambda: news.company_news(symbol, _NEWS_WINDOW_DAYS, 16)) or []

        bundle, headlines, mdna = await asyncio.gather(
            asyncio.to_thread(_bundle),
            asyncio.to_thread(_news),
            pick_mdna(symbol),  # fetches on miss so the narrative read has material
        )
        analysis = await _generate_analysis(claude, symbol, scores, bundle.get("profile") or {},
                                            headlines, bundle.get("officers") or [], mdna.get("docs") or [])
        if not analysis.get("error"):
            # Snapshot the exact articles the analysis read (annotation indices
            # refer to THIS list) + deterministic sentiment aggregates.
            analysis["news_snapshot"] = headlines[:16]
            analysis["sentiment"] = _sentiment_scores(headlines[:16], analysis.get("news_analysis") or [])
            analysis["generated_at"] = time.time()
            store.set_setting(cache_key, analysis)
        return analysis


def _sentiment_scores(articles: list, news_analysis: list) -> dict:
    """Aggregate per-article Claude sentiment into quarter + 2-week scores.

    positive=+1 / neutral=0 / negative=-1, weighted by relevance (high 1.0,
    medium 0.6, low 0.3); score = 100 * weighted mean, so -100..+100.
    Deterministic given the same analysis — no model call.
    """
    from datetime import datetime, timedelta, timezone

    sval = {"positive": 1.0, "neutral": 0.0, "negative": -1.0}
    rw = {"high": 1.0, "medium": 0.6, "low": 0.3}
    cutoff_short = datetime.now(timezone.utc) - timedelta(days=_SHORT_SENTIMENT_DAYS)

    def bucket(entries):
        num = den = 0.0
        for v, w in entries:
            num += v * w
            den += w
        return round(100.0 * num / den, 1) if den else None

    long_e, short_e = [], []
    for na in news_analysis:
        try:
            i = int(na.get("index"))
            art = articles[i]
        except (TypeError, ValueError, IndexError):
            continue
        v = sval.get(str(na.get("sentiment", "")).lower())
        if v is None:
            continue
        w = rw.get(str(na.get("relevance", "")).lower(), 0.6)
        long_e.append((v, w))
        try:
            d = datetime.strptime(str(art.get("date", ""))[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if d >= cutoff_short:
                short_e.append((v, w))
        except ValueError:
            pass
    return {
        "long_term": bucket(long_e), "short_term": bucket(short_e),
        "n_quarter": len(long_e), "n_2wk": len(short_e),
        "window_days": _NEWS_WINDOW_DAYS, "short_days": _SHORT_SENTIMENT_DAYS,
    }


async def _generate_analysis(claude: ClaudeBridge, symbol: str, scores: dict, profile: dict,
                             headlines: list, execs: list, mdna_docs: list) -> dict:
    news_lines = "\n".join(
        f"[{i}] {h.get('date','')} · {h.get('source','')}: {h.get('headline','')} — {(h.get('summary','') or '')[:200]}"
        for i, h in enumerate(headlines[:16])
    ) or "(no articles found)"
    prof_str = f"{profile.get('sector','?')} / {profile.get('industry','?')}\n{(profile.get('summary','') or '')[:1400]}"
    officer_lines = "\n".join(
        f"- {o['name']} — {o['title']}" + (f" (age {o['age']})" if o.get("age") else "")
        + (f", pay ${o['total_pay']:,}" if o.get("total_pay") else "")
        for o in execs[:8]
    ) or "(no officer data)"
    # Latest annual MD&A preferred for the narrative check; bounded for the prompt.
    mdna_doc = next((d for d in mdna_docs if d.get("form") == "10-K"), mdna_docs[0] if mdna_docs else None)
    mdna_str = (
        f"[{mdna_doc['form']} filed {mdna_doc['date']}]\n{mdna_doc['text'][:12000]}"
        if mdna_doc else "(no MD&A available)"
    )
    prompt = PICK_ANALYSIS_FROM_DATA.format(
        symbol=symbol,
        scores=json.dumps(scores, default=str)[:6000] if scores else "(no fundamental scores available)",
        profile=prof_str,
        officers=officer_lines,
        mdna=mdna_str,
        news=news_lines,
    )
    try:
        result = await claude.extract_json(prompt)
        return result if isinstance(result, dict) else {"error": "unexpected analysis shape"}
    except ClaudeError as e:
        log.warning("analysis generation failed for %s: %s", symbol, e)
        return {"error": str(e)}
