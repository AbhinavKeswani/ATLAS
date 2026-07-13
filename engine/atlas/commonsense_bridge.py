"""Bridge to the sibling CommonSense fundamentals engine.

CommonSense lives in its own project + venv (it has heavy deps: edgartools,
pandas, yfinance). Rather than import it, Atlas shells into its venv — the same
approach as the Claude bridge — to run the universe screener, then reads the
JSON artifacts it writes:

    data/screener/picks.json        ranked picks (quality + mispricing)
    data/parquet/<TICKER>/scores.json   per-ticker score + multiples

All reads are best-effort: if CommonSense isn't present the Picks tab just shows
an empty state with a hint.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from .config import COMMONSENSE_PYTHON, COMMONSENSE_ROOT

log = logging.getLogger("atlas.commonsense")

# CommonSense writes per-ticker scores and the screener output under its DATA_DIR
# (defaults to data/parquet). The screener writes picks.json to DATA_DIR/screener.
_DATA_DIR = COMMONSENSE_ROOT / "data" / "parquet"
_PICKS_JSON = _DATA_DIR / "screener" / "picks.json"


def available() -> bool:
    """True if the CommonSense project + its venv python are both present."""
    return COMMONSENSE_ROOT.is_dir() and COMMONSENSE_PYTHON.exists()


def read_picks() -> dict[str, Any]:
    """Read the last screener output without running it. Empty dict if none."""
    if not _PICKS_JSON.exists():
        return {}
    try:
        return json.loads(_PICKS_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("could not read picks.json: %s", e)
        return {}


def read_scores(symbol: str) -> dict[str, Any]:
    """Read a ticker's scores.json (score + multiples + metrics). Empty dict if none."""
    path = _DATA_DIR / symbol.upper() / "scores.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("could not read scores for %s: %s", symbol, e)
        return {}


# On-demand MD&A: the bulk screen ingests facts only (fetch_mdna=False), so the
# narrative is fetched lazily the first time a pick's breakout is opened.
_MDNA_SNIPPET = r"""
import json, sys
from pathlib import Path
import pandas as pd
from commonsense.config import DATA_DIR, EDGAR_EMAIL
from commonsense.edgar.mdna import write_mdna_for_filing

ticker = sys.argv[1].upper()
company_dir = Path(DATA_DIR) / ticker
sub_path = company_dir / f"{ticker}_sec_submissions.parquet"
out = {"ticker": ticker, "written": [], "errors": []}
if not sub_path.exists():
    out["errors"].append("no cached submissions (run the screener first)")
else:
    df = pd.read_parquet(sub_path)
    for form in ("10-K", "10-Q", "20-F"):
        rows = df[df["form"] == form].sort_values("filingDate", ascending=False)
        if rows.empty:
            continue
        r = rows.iloc[0]
        base = f"{ticker}_{form}_{str(r['filingDate']).replace('-', '')[:8]}"
        if list(company_dir.glob(base + "_mdna.*")):
            out["written"].append(str(next(iter(company_dir.glob(base + "_mdna.*")))))
            continue
        try:
            p = write_mdna_for_filing(
                cik=int(str(r["cik"]).lstrip("0") or 0),
                accession_no=str(r["accessionNumber"]),
                form=form,
                user_agent=EDGAR_EMAIL,
                company_dir=company_dir,
                base_name=base,
                primary_document=str(r.get("primaryDocument") or "") or None,
            )
            if p is not None:
                out["written"].append(str(p))
            else:
                out["errors"].append(f"{form}: extraction returned nothing")
        except Exception as e:
            out["errors"].append(f"{form}: {e}")
print(json.dumps(out))
"""


def fetch_mdna(symbol: str, timeout: float = 120.0) -> dict[str, Any]:
    """Fetch MD&A for the latest 10-K/10-Q of `symbol` into the CommonSense cache."""
    if not available():
        return {"error": f"CommonSense not found at {COMMONSENSE_ROOT}"}
    env = {**os.environ, "PYTHONPATH": str(COMMONSENSE_ROOT / "src")}
    try:
        proc = subprocess.run(
            [str(COMMONSENSE_PYTHON), "-c", _MDNA_SNIPPET, symbol.upper()],
            cwd=str(COMMONSENSE_ROOT), env=env, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": "MD&A fetch timed out"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"MD&A fetch failed: {e}"}
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        return {"error": err[-400:] or f"MD&A fetch exited {proc.returncode}"}
    try:
        return json.loads(proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"error": "MD&A fetch produced no JSON"}


def read_mdna(symbol: str, max_chars: int = 60000) -> list[dict[str, Any]]:
    """Read cached MD&A texts for `symbol` (newest first), truncated for transport."""
    company_dir = _DATA_DIR / symbol.upper()
    out: list[dict[str, Any]] = []
    for p in sorted(company_dir.glob("*_mdna.*"), reverse=True):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parts = p.stem.split("_")  # e.g. KLAC_10-K_20250807_mdna
        out.append({
            "file": p.name,
            "form": parts[1] if len(parts) > 2 else "",
            "date": parts[2] if len(parts) > 2 else "",
            "chars": len(text),
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
        })
    return out


# Single-ticker lookup: reuse the screener's ensure-data + score pipeline so a
# ticker outside the ranked universe (or not yet ingested) can be analyzed on
# demand from the Picks tab lookup bar.
_LOOKUP_SNIPPET = r"""
import json, sys
from pathlib import Path
from commonsense.config import DATA_DIR, EDGAR_EMAIL
from commonsense.screener import _ensure_ticker_data
from commonsense.analysis import score_company
from commonsense.market.prices import get_quote

ticker = sys.argv[1].upper()
data_dir = Path(DATA_DIR)
try:
    ok = _ensure_ticker_data({"symbol": ticker}, data_dir, EDGAR_EMAIL, ingest=True, force=False)
    if not ok:
        print(json.dumps({"error": f"no SEC facts found for {ticker} — is it a US filer?"}))
        raise SystemExit(0)
    q = get_quote(ticker)
    score = score_company(ticker, data_dir, price=(q.price if q else None), write_json=True)
    print(json.dumps({
        "ok": True,
        "ticker": ticker,
        "quality_score": score.get("quality_score"),
        "verdict": score.get("verdict"),
    }))
except SystemExit:
    raise
except Exception as e:
    print(json.dumps({"error": str(e)[:300]}))
"""


def lookup_ticker(symbol: str, timeout: float = 240.0) -> dict[str, Any]:
    """Ingest (if needed) + analyze + score one ticker on demand. Blocking, ~10-60s."""
    if not available():
        return {"error": f"CommonSense not found at {COMMONSENSE_ROOT}"}
    env = {**os.environ, "PYTHONPATH": str(COMMONSENSE_ROOT / "src")}
    try:
        proc = subprocess.run(
            [str(COMMONSENSE_PYTHON), "-c", _LOOKUP_SNIPPET, symbol.upper()],
            cwd=str(COMMONSENSE_ROOT), env=env, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": "lookup timed out"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"lookup launch failed: {e}"}
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        return {"error": err[-400:] or f"lookup exited {proc.returncode}"}
    try:
        return json.loads(proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"error": "lookup produced no JSON"}


def run_screen(limit: int | None = None, *, ingest: bool = True, timeout: float = 1800.0) -> dict[str, Any]:
    """Run `python -m commonsense.screener` in the CommonSense venv (blocking).

    Screening ingests SEC data per name, so it's slow — call via asyncio.to_thread.
    Returns the parsed picks.json (or an {"error": ...} dict on failure).
    """
    if not available():
        return {"error": f"CommonSense not found at {COMMONSENSE_ROOT}"}

    cmd = [str(COMMONSENSE_PYTHON), "-m", "commonsense.screener"]
    if limit is not None:
        cmd += ["--limit", str(int(limit))]
    if not ingest:
        cmd += ["--no-ingest"]

    env = {**os.environ, "PYTHONPATH": str(COMMONSENSE_ROOT / "src")}
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(COMMONSENSE_ROOT),
            env=env,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": "screener timed out"}
    except Exception as e:  # noqa: BLE001 - surface any launch failure to the UI
        return {"error": f"screener launch failed: {e}"}

    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        log.warning("screener exited %s: %s", proc.returncode, err[-500:])
        return {"error": err[-500:] or f"screener exited {proc.returncode}"}

    return read_picks()
