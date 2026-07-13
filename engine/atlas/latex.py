"""Compile LaTeX to PDF with Tectonic — the visual-verification substrate.

Tectonic is a self-contained XeLaTeX-compatible engine (single binary, fetches
packages on demand). We shell out to it the same way the Claude and CommonSense
bridges shell out to their tools: `subprocess.run`, a timeout, stderr surfaced.

The compile result carries the two signals the resume fit-loop needs:
  - `page_count`  — from the engine's "Output written on … (N pages …)" log line
  - `overfull`    — the `Overfull \\hbox` warnings Tectonic prints to stderr
`ok` is the one-page, no-overflow condition. If Tectonic isn't installed the tab
still edits/saves LaTeX; it just can't compile or auto-verify (graceful, like the
HYDRA/CommonSense extras).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import TECTONIC_BIN

log = logging.getLogger("atlas.latex")

_OVERFULL_RE = re.compile(r"Overfull \\hbox \(([0-9.]+)pt too wide\)[^\n]*", re.MULTILINE)
_PAGES_RE = re.compile(r"Output written on [^\n(]*\((\d+) pages?", re.MULTILINE)


def available() -> bool:
    """True if the Tectonic binary is on PATH (or at the configured path)."""
    return shutil.which(TECTONIC_BIN) is not None or Path(TECTONIC_BIN).exists()


def compile(latex: str, build_dir: Path, *, timeout: float = 120.0) -> dict[str, Any]:
    """Compile `latex` in `build_dir`. Returns a diagnostics dict.

    Shape:
        {ok, pdf_path, page_count, overfull: [str], error, log_tail}
    `ok` is True iff a PDF was produced on exactly one page with no overfull hboxes.
    Never raises — failures come back as {ok: False, error: "..."}.
    """
    if not available():
        return {
            "ok": False, "pdf_path": None, "page_count": None, "overfull": [],
            "error": "Tectonic not installed. Run `brew install tectonic`.", "log_tail": "",
        }
    build_dir.mkdir(parents=True, exist_ok=True)
    tex_path = build_dir / "main.tex"
    tex_path.write_text(latex, encoding="utf-8")
    pdf_path = build_dir / "main.pdf"
    log_path = build_dir / "main.log"

    try:
        proc = subprocess.run(
            [TECTONIC_BIN, "-X", "compile", str(tex_path), "--outdir", str(build_dir), "--keep-logs"],
            cwd=str(build_dir), capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "pdf_path": None, "page_count": None, "overfull": [],
                "error": "Tectonic timed out.", "log_tail": ""}
    except Exception as e:  # noqa: BLE001 - surface any launch failure to the UI
        return {"ok": False, "pdf_path": None, "page_count": None, "overfull": [],
                "error": f"Tectonic launch failed: {e}", "log_tail": ""}

    stderr = proc.stderr.decode("utf-8", "replace")
    overfull = [m.group(0).strip() for m in _OVERFULL_RE.finditer(stderr)]

    if proc.returncode != 0 or not pdf_path.exists():
        # A real LaTeX error. Pull the most useful tail of the log for the model.
        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else stderr
        return {
            "ok": False, "pdf_path": None, "page_count": None, "overfull": overfull,
            "error": _first_tex_error(log_text) or f"tectonic exited {proc.returncode}",
            "log_tail": log_text[-1500:],
        }

    page_count = None
    if log_path.exists():
        m = _PAGES_RE.search(log_path.read_text(encoding="utf-8", errors="replace"))
        if m:
            page_count = int(m.group(1))

    return {
        "ok": (page_count == 1) and not overfull,
        "pdf_path": str(pdf_path),
        "page_count": page_count,
        "overfull": overfull,
        "error": None,
        "log_tail": stderr[-1500:],
    }


def _first_tex_error(log_text: str) -> str | None:
    """Extract the first `! ...` TeX error line (+ a little context) from a log."""
    lines = log_text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("!"):
            ctx = lines[i : i + 4]
            return " ".join(s.strip() for s in ctx if s.strip())[:400]
    return None
