"""Soccer tab: WC 2026 betting model bridge + pick assembly.

Same pattern as the CommonSense bridge: the model lives in its own project +
venv (pandas, scipy, penaltyblog), so Atlas shells into that venv to run the
pipeline stages and an export script, then reads the soccer.json artifact:

    <WC_ROOT>/data/built/soccer.json    per-match probs, per-book prices,
                                        scoreline grids, timing buckets

The assembled payload served to the UI layers on top of that:
  - lineup status per match (ESPN, soccer_lineups.py) gating recommendations
  - three 4-leg parlays (safe/medium/spicier, soccer_parlay.py)

The pick DECISION is mathematical (Dixon-Coles Poisson over Elo + player-xG
blends, calibrated on 2021-24 internationals); Atlas only presents it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

from . import soccer_lineups, soccer_parlay
from .config import WC_PYTHON, WC_ROOT
from .store import Store

log = logging.getLogger("atlas.soccer")

_SOCCER_KEY = "soccer"
_SOCCER_JSON = WC_ROOT / "data" / "built" / "soccer.json"
_FRESH_SECS = 24 * 3600  # skip ingest/features when built tables are newer than this


def available() -> bool:
    """True if the WC 2026 project + its venv python are both present."""
    return WC_ROOT.is_dir() and WC_PYTHON.exists()


def has_odds_key() -> bool:
    """True if The Odds API key is reachable (env or the model project's .env)."""
    if os.environ.get("ODDS_API_KEY"):
        return True
    env = WC_ROOT / ".env"
    if env.exists():
        try:
            return any(
                line.strip().startswith("ODDS_API_KEY=") and line.split("=", 1)[1].strip()
                for line in env.read_text().splitlines()
            )
        except OSError:
            return False
    return False


def read_soccer_json() -> dict:
    """Read the last export without running anything. Empty dict if none."""
    if not _SOCCER_JSON.exists():
        return {}
    try:
        return json.loads(_SOCCER_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("could not read soccer.json: %s", e)
        return {}


def _wc(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run a wc2026 CLI command inside the model's venv."""
    cmd = [str(WC_PYTHON), "-c", "from wc2026.cli import app; app()", *args]
    return subprocess.run(cmd, cwd=str(WC_ROOT), capture_output=True, timeout=timeout)


def _stale(name: str) -> bool:
    p = WC_ROOT / "data" / "built" / name
    return not p.exists() or (time.time() - p.stat().st_mtime) > _FRESH_SECS


def run_pipeline(*, demo: bool | None = None, full: bool = False) -> dict:
    """Run the model pipeline + export (blocking — call via asyncio.to_thread).

    ingest/features (slow: Understat, Wikipedia, Elo scrapes) only run when the
    built tables are >24h old or `full` is set; model/odds/export always run.
    With no Odds API key the odds stage falls back to demo (synthetic) prices.
    """
    if not available():
        return {"error": f"WC 2026 model not found at {WC_ROOT} — set ATLAS_WC_ROOT"}
    if demo is None:
        demo = not has_odds_key()

    stages: list[tuple[str, list[str], float]] = []
    if full or _stale("fixtures.parquet"):
        stages.append(("ingest", ["ingest"], 1200))
        stages.append(("features", ["features"], 300))
    elif _stale("team_features.parquet"):
        stages.append(("features", ["features"], 300))
    stages.append(("model", ["model"], 300))
    stages.append(("odds", ["odds", "--demo"] if demo else ["odds"], 300))

    for name, args, timeout in stages:
        try:
            proc = _wc(args, timeout)
        except subprocess.TimeoutExpired:
            return {"error": f"{name} stage timed out"}
        except Exception as e:  # noqa: BLE001 - surface any launch failure to the UI
            return {"error": f"{name} stage launch failed: {e}"}
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            log.warning("wc2026 %s exited %s: %s", name, proc.returncode, err[-500:])
            return {"error": f"{name} stage failed: {err[-300:] or proc.returncode}"}

    try:
        proc = subprocess.run(
            [str(WC_PYTHON), str(Path("scripts") / "atlas_export.py")],
            cwd=str(WC_ROOT), capture_output=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return {"error": "export timed out"}
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        return {"error": f"export failed: {err[-300:] or proc.returncode}"}

    data = read_soccer_json()
    if not data:
        return {"error": "export produced no soccer.json"}
    data["demo_odds"] = data.get("demo_odds", False) or demo
    return data


async def refresh(store: Store, *, demo: bool | None = None, full: bool = False) -> dict:
    """Re-run the pipeline and cache the export in the settings table."""
    import asyncio

    result = await asyncio.to_thread(run_pipeline, demo=demo, full=full)
    if result.get("error"):
        return result
    result["refreshed_at"] = time.time()
    store.set_setting(_SOCCER_KEY, result)
    return {"matches": len(result.get("matches", [])), "demo_odds": result.get("demo_odds")}


def _cached(store: Store) -> dict:
    cached = store.get_setting(_SOCCER_KEY, None)
    if isinstance(cached, dict) and "matches" in cached:
        return cached
    disk = read_soccer_json()
    if disk:
        store.set_setting(_SOCCER_KEY, disk)
    return disk


def overview(store: Store) -> dict:
    """Everything the Soccer tab renders: matches + lineups + parlays."""
    data = _cached(store)
    if not data:
        return {"available": available(), "matches": [], "books": [], "parlays": []}
    lineups = soccer_lineups.cached_lineups(store)
    matches = soccer_lineups.annotate(data.get("matches", []), lineups)
    parlays = soccer_parlay.build_parlays(matches)
    return {
        "available": True,
        "generated_at": data.get("generated_at"),
        "refreshed_at": data.get("refreshed_at"),
        "demo_odds": data.get("demo_odds", False),
        "books": data.get("books", []),
        "model_params": data.get("model_params", {}),
        "calibration": data.get("calibration", {}),
        "lineups_checked_at": lineups.get("checked_at"),
        "matches": matches,
        "parlays": parlays,
    }


def match_detail(store: Store, match_id: int) -> dict | None:
    """Full payload for the math modal (score grid, timing, params)."""
    data = _cached(store)
    for m in data.get("matches", []):
        if int(m.get("match_id", -1)) == match_id:
            lineups = soccer_lineups.cached_lineups(store)
            (annotated,) = soccer_lineups.annotate([m], lineups)
            return {
                "match": annotated,
                "model_params": data.get("model_params", {}),
                "calibration": data.get("calibration", {}),
            }
    return None
