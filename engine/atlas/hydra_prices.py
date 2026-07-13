"""Read latest equity prices from the local HYDRA caches — read-only.

HYDRA streams Databento 1-min OHLCV for a 30-symbol NASDAQ universe and tees the
bars to disk as `.dbn.zst` files under data/databento/XNAS.ITCH/ohlcv-1m/. We read
the most recent bar's close per symbol. If the `databento` package isn't installed
(it's an optional extra), pricing degrades gracefully to "unavailable" and the UI
falls back to last-known / cost basis.

We never write HYDRA state. The one mutation we offer is *subscribing* a held ticker
by appending it to HYDRA's universe.json so its bars start getting cached.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import HYDRA_OHLCV_1M, HYDRA_ROOT, HYDRA_UNIVERSE

log = logging.getLogger("atlas.hydra")


def hydra_present() -> bool:
    return HYDRA_ROOT.exists()


def universe_symbols() -> list[str]:
    if not HYDRA_UNIVERSE.exists():
        return []
    try:
        data = json.loads(HYDRA_UNIVERSE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    # universe.json may be a bare list or {"symbols": [...]} / {"universe": [...]}.
    if isinstance(data, list):
        syms = data
    elif isinstance(data, dict):
        syms = data.get("symbols") or data.get("universe") or data.get("equities") or []
    else:
        syms = []
    return [str(s).upper() for s in syms]


def subscribe_symbols(symbols: list[str]) -> list[str]:
    """Append new tickers to HYDRA's universe.json. Returns the symbols actually added.

    Only handles the bare-list and {"symbols": [...]} shapes; for any other shape we
    refuse to guess and return [] so we never corrupt HYDRA's config.
    """
    if not HYDRA_UNIVERSE.exists():
        return []
    existing = set(universe_symbols())
    to_add = [s.upper() for s in symbols if s.upper() not in existing]
    if not to_add:
        return []
    data = json.loads(HYDRA_UNIVERSE.read_text())
    if isinstance(data, list):
        data = sorted(set(data) | set(to_add))
    elif isinstance(data, dict) and "symbols" in data and isinstance(data["symbols"], list):
        data["symbols"] = sorted(set(data["symbols"]) | set(to_add))
    else:
        return []
    HYDRA_UNIVERSE.write_text(json.dumps(data, indent=2))
    log.info("subscribed HYDRA symbols: %s", to_add)
    return to_add


def _latest_close_from_dbn(symbol: str) -> float | None:
    """Read the last close from the newest cached 1-min DBN file for `symbol`."""
    try:
        import databento as db  # optional extra
    except ImportError:
        return None
    if not HYDRA_OHLCV_1M.exists():
        return None
    # Cache files look like <DATE>_<DATE+1>_<SYM>_1sym.dbn.zst — match the symbol token.
    candidates = sorted(HYDRA_OHLCV_1M.glob(f"*_{symbol.upper()}_*.dbn.zst"))
    if not candidates:
        candidates = sorted(HYDRA_OHLCV_1M.glob(f"*{symbol.upper()}*.dbn.zst"))
    if not candidates:
        return None
    newest = candidates[-1]
    try:
        store = db.DBNStore.from_file(newest)
        df = store.to_df()
        if df is None or df.empty or "close" not in df.columns:
            return None
        return float(df["close"].iloc[-1])
    except Exception as e:  # databento read errors shouldn't crash valuation
        log.warning("dbn read failed for %s: %s", symbol, e)
        return None


def latest_prices(symbols: list[str]) -> dict[str, float]:
    """Best-effort latest close per symbol. Missing symbols are simply absent."""
    out: dict[str, float] = {}
    for s in symbols:
        px = _latest_close_from_dbn(s)
        if px is not None:
            out[s.upper()] = px
    return out
