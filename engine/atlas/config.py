"""Runtime configuration and well-known paths for the Atlas engine.

Pattern mirrors Vein's config.py: a per-OS app-data dir holds the SQLite DB,
logs, and uploaded images. Everything binds to localhost — nothing leaves the box.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --- Paths (per-OS app-data dir) ---------------------------------------------


def _app_support() -> Path:
    # ATLAS_APP_DIR points the whole data dir (DB, logs, uploads, tokens) somewhere
    # else — used for demo/dev instances that must not touch the real database.
    override = os.environ.get("ATLAS_APP_DIR", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "Atlas"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Atlas"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "Atlas"


APP_SUPPORT = _app_support()
DB_PATH = APP_SUPPORT / "atlas.db"
LOG_DIR = APP_SUPPORT / "logs"
UPLOAD_DIR = APP_SUPPORT / "uploads"
RESUME_DIR = APP_SUPPORT / "resumes"    # compiled .tex/.pdf per saved resume doc

# Hand-editable user config: theme, rail icons, widget layout, intro behavior.
# The server watches its mtime and hot-pushes changes to the UI over the WS bus.
USER_CONFIG = Path(os.environ.get("ATLAS_USER_CONFIG", str(APP_SUPPORT / "user_config.json")))

# Google OAuth: user drops a Desktop-app OAuth client here; token is cached alongside.
GOOGLE_CREDENTIALS = Path(os.environ.get("ATLAS_GOOGLE_CREDENTIALS", str(APP_SUPPORT / "google_credentials.json")))
GOOGLE_TOKEN = APP_SUPPORT / "google_token.json"


def ensure_dirs() -> None:
    for d in (APP_SUPPORT, LOG_DIR, UPLOAD_DIR, RESUME_DIR):
        d.mkdir(parents=True, exist_ok=True)


# --- Server ------------------------------------------------------------------

HOST = os.environ.get("ATLAS_HOST", "127.0.0.1")
PORT = int(os.environ.get("ATLAS_PORT", "8770"))  # Vein owns 8765

# --- Claude bridge -----------------------------------------------------------

# The local `claude` CLI binary. Overridable if it isn't on PATH.
CLAUDE_BIN = os.environ.get("ATLAS_CLAUDE_BIN", "claude")

# --- Resume tab --------------------------------------------------------------

# Tectonic: self-contained XeLaTeX-compatible engine used to compile resumes and
# feed the compiled PDF back to Claude for visual verification. Install once with
# `brew install tectonic`. Overridable if it isn't on PATH.
TECTONIC_BIN = os.environ.get("ATLAS_TECTONIC_BIN", "tectonic")

# The `gh` CLI (already authed) is used to pull the GitHub half of the portfolio
# corpus. Overridable if it isn't on PATH.
GH_BIN = os.environ.get("ATLAS_GH_BIN", "gh")

# --- CommonSense (fundamentals engine for stock Picks) -----------------------

# The sibling CommonSense project produces per-ticker scores + a ranked screener.
# Atlas shells into its own venv (like the Claude bridge) to run the screener.
COMMONSENSE_ROOT = Path(
    os.environ.get(
        "ATLAS_COMMONSENSE_ROOT",
        str(Path.home() / "Desktop" / "Desktop - Abhinav’s MacBook Pro" / "CommonSense"),
    )
)
COMMONSENSE_PYTHON = Path(
    os.environ.get("ATLAS_COMMONSENSE_PYTHON", str(COMMONSENSE_ROOT / ".venv" / "bin" / "python"))
)

# Optional Finnhub key for fresh company news + peers (free tier). Falls back to
# keyless Yahoo when unset. Set ATLAS_FINNHUB_KEY in the environment.
FINNHUB_KEY = os.environ.get("ATLAS_FINNHUB_KEY", "").strip()

# --- WC 2026 (soccer betting model for the Soccer tab) ------------------------

# The sibling World Cup model produces per-match probabilities + per-book odds.
# Atlas shells into its venv (same pattern as CommonSense) and reads soccer.json.
WC_ROOT = Path(os.environ.get("ATLAS_WC_ROOT", str(Path.home() / "Desktop" / "WC 2026")))
WC_PYTHON = Path(os.environ.get("ATLAS_WC_PYTHON", str(WC_ROOT / ".venv" / "bin" / "python")))

# --- HYDRA (live equity data) ------------------------------------------------

HYDRA_ROOT = Path(os.environ.get("ATLAS_HYDRA_ROOT", str(Path.home() / "Desktop" / "HYDRA")))
HYDRA_UNIVERSE = HYDRA_ROOT / "live_paper_trading" / "config" / "universe.json"
HYDRA_OHLCV_1M = HYDRA_ROOT / "data" / "databento" / "XNAS.ITCH" / "ohlcv-1m"

# --- Vein (meeting notes, read-only) -----------------------------------------


def _vein_db() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "Vein" / "vein.db"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Vein" / "vein.db"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "Vein" / "vein.db"


VEIN_DB = Path(os.environ.get("ATLAS_VEIN_DB", str(_vein_db())))
VEIN_API = os.environ.get("ATLAS_VEIN_API", "http://127.0.0.1:8765")

# --- Resume corpus: local project roots to scan ------------------------------

# Local Desktop projects Atlas scans (tech stack, dependency files, README) to
# ground resume bullets. Non-existent paths are skipped at scan time. Override
# with ATLAS_RESUME_ROOTS (a `:`-separated list of absolute paths).
_ATLAS_ROOT = Path(__file__).resolve().parent.parent.parent  # .../Atlas
_DEFAULT_RESUME_ROOTS = [
    HYDRA_ROOT,
    COMMONSENSE_ROOT,
    WC_ROOT,
    _ATLAS_ROOT,
    Path.home() / "Desktop" / "Vein",
]
_env_roots = os.environ.get("ATLAS_RESUME_ROOTS", "").strip()
RESUME_SCAN_ROOTS: list[Path] = (
    [Path(p) for p in _env_roots.split(os.pathsep) if p]
    if _env_roots
    else _DEFAULT_RESUME_ROOTS
)
