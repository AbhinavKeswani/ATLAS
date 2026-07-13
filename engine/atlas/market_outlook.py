"""Daily Market Outlook for the Picks tab.

Answers "what does the business/finance world look like today?" with the same
discipline as the per-pick analysis: everything Claude sees is EXTRACTED data —
computed market analytics from our own price store plus real market headlines —
and it is instructed to narrate only from that material. The sentiment score is
NOT Claude's: it is computed deterministically from Claude's per-article reads
(positive/negative weighted by impact), and each day's score is appended to a
persisted history so the trend can be graphed over time.

Cache: one outlook per calendar day (`market_outlook:<date>` setting), history in
`market_outlook_history`. Regeneration is explicit (?regenerate=true) or via the
daily job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from datetime import datetime, timezone

from . import news, prices
from .claude_bridge import ClaudeBridge, ClaudeError, MARKET_OUTLOOK_FROM_DATA
from .store import Store

log = logging.getLogger("atlas.outlook")

_HISTORY_KEY = "market_outlook_history"
_DAY_PREFIX = "market_outlook:"
_lock = asyncio.Lock()

_IMPACT_W = {"high": 1.0, "medium": 0.6, "low": 0.3}
_SENT_V = {"positive": 1.0, "neutral": 0.0, "negative": -1.0}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _day_change(rows: list[dict]) -> float | None:
    """Percent change between the last two cached closes."""
    if len(rows) < 2:
        return None
    prev, last = rows[-2]["close"], rows[-1]["close"]
    if not prev:
        return None
    return round((last / prev - 1) * 100, 2)


def market_stats(store: Store) -> dict:
    """Computed market analytics from OUR OWN price data — no model, no news.

    SPY + per-sector ETF day moves, universe breadth (share of ranked names
    advancing on the latest session), and the current picks profile.
    """
    spy_rows = prices.series(store, prices.MARKET_ETF, "1mo")
    spy_chg = _day_change(spy_rows)
    as_of = spy_rows[-1]["date"] if spy_rows else _today()

    sectors: dict[str, float] = {}
    seen_etfs = set()
    for sector, etf in prices.SECTOR_ETF.items():
        if etf in seen_etfs:
            continue
        seen_etfs.add(etf)
        chg = _day_change(prices.series(store, etf, "1mo"))
        if chg is not None:
            sectors[sector] = chg
    best = max(sectors.items(), key=lambda kv: kv[1]) if sectors else None
    worst = min(sectors.items(), key=lambda kv: kv[1]) if sectors else None

    # Breadth across the ranked universe from cached closes (fast local reads).
    from . import picks as picks_mod
    ranked = picks_mod.list_picks(store).get("picks", [])
    moves: list[float] = []
    for p in ranked:
        rows = store.get_price_series(p["symbol"], since=None, limit=2)
        chg = _day_change(rows)
        if chg is not None:
            moves.append(chg)
    advancing = sum(1 for m in moves if m > 0)
    breadth_pct = round(100 * advancing / len(moves), 1) if moves else None
    median_move = round(statistics.median(moves), 2) if moves else None

    top10 = ranked[:10]
    return {
        "as_of": as_of,
        "spy_day_pct": spy_chg,
        "sector_day_pct": dict(sorted(sectors.items(), key=lambda kv: -kv[1])),
        "best_sector": {"name": best[0], "pct": best[1]} if best else None,
        "worst_sector": {"name": worst[0], "pct": worst[1]} if worst else None,
        "universe_size": len(moves),
        "breadth_advancing_pct": breadth_pct,
        "median_universe_move_pct": median_move,
        "mispriced_count": sum(1 for p in ranked if p.get("mispricing")),
        "top10_avg_quality": round(sum(p.get("quality_score") or 0 for p in top10) / len(top10), 1) if top10 else None,
    }


def _score(articles: list, news_analysis: list) -> dict:
    """Deterministic market-sentiment score: ±1 per article, weighted by impact,
    scaled to -100..+100. The graph the user tracks is THIS number over days."""
    num = den = 0.0
    pos = neg = neu = 0
    for na in news_analysis:
        try:
            _ = articles[int(na.get("index"))]
        except (TypeError, ValueError, IndexError):
            continue
        v = _SENT_V.get(str(na.get("sentiment", "")).lower())
        if v is None:
            continue
        w = _IMPACT_W.get(str(na.get("impact", "")).lower(), 0.6)
        num += v * w
        den += w
        pos += v > 0
        neg += v < 0
        neu += v == 0
    return {
        "score": round(100 * num / den, 1) if den else None,
        "n_articles": pos + neg + neu,
        "n_positive": pos, "n_negative": neg, "n_neutral": neu,
    }


def _append_history(store: Store, entry: dict) -> list[dict]:
    """Upsert today's point into the score history (idempotent per date)."""
    hist = [h for h in (store.get_setting(_HISTORY_KEY, []) or []) if h.get("date") != entry["date"]]
    hist.append(entry)
    hist.sort(key=lambda h: h["date"])
    hist = hist[-365:]
    store.set_setting(_HISTORY_KEY, hist)
    return hist


def history(store: Store) -> list[dict]:
    return store.get_setting(_HISTORY_KEY, []) or []


async def get_outlook(store: Store, claude: ClaudeBridge, *, regenerate: bool = False) -> dict:
    """Today's outlook (cached per day) + the score history for the trend graph."""
    day = _today()
    cache_key = _DAY_PREFIX + day
    if not regenerate:
        cached = store.get_setting(cache_key, None)
        if cached:
            return {"outlook": cached, "history": history(store)}

    async with _lock:
        if not regenerate:
            cached = store.get_setting(cache_key, None)
            if cached:
                return {"outlook": cached, "history": history(store)}

        stats, articles = await asyncio.gather(
            asyncio.to_thread(market_stats, store),
            asyncio.to_thread(news.market_news, 15),
        )
        if not articles:
            return {"outlook": {"error": "no market news available right now"}, "history": history(store)}
        if not claude.available:
            return {"outlook": {"error": "Claude CLI unavailable"}, "history": history(store)}

        news_lines = "\n".join(
            f"[{i}] {a.get('date','')} · {a.get('source','')}: {a.get('headline','')} — {(a.get('summary','') or '')[:220]}"
            for i, a in enumerate(articles)
        )
        prompt = MARKET_OUTLOOK_FROM_DATA.format(
            date=day, stats=json.dumps(stats, default=str), news=news_lines,
        )
        # The shared Claude session occasionally returns a degraded shape (empty
        # news_analysis) — validate and retry once rather than caching garbage.
        result = None
        for attempt in range(2):
            try:
                candidate = await claude.extract_json(prompt)
            except ClaudeError as e:
                log.warning("outlook generation failed (attempt %d): %s", attempt + 1, e)
                continue
            if isinstance(candidate, dict) and candidate.get("headline") and (candidate.get("news_analysis") or []):
                result = candidate
                break
            log.warning("outlook attempt %d returned degraded shape (keys=%s) — retrying",
                        attempt + 1, list(candidate.keys()) if isinstance(candidate, dict) else type(candidate).__name__)
        if result is None:
            return {"outlook": {"error": "outlook generation returned no usable analysis — try regenerate"}, "history": history(store)}

        sentiment = _score(articles, result.get("news_analysis") or [])
        if sentiment.get("score") is None:
            return {"outlook": {"error": "no scorable article reads — try regenerate"}, "history": history(store)}
        outlook = {
            **result,
            "date": day,
            "stats": stats,
            "news_snapshot": articles,      # determinism: annotations refer to THIS list
            "sentiment": sentiment,
            "generated_at": time.time(),
        }
        store.set_setting(cache_key, outlook)
        _append_history(store, {
            "date": day,
            "score": sentiment.get("score"),
            "n_articles": sentiment.get("n_articles"),
            "spy_day_pct": stats.get("spy_day_pct"),
            "breadth_pct": stats.get("breadth_advancing_pct"),
        })
        return {"outlook": outlook, "history": history(store)}
