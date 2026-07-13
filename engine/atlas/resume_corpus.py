"""Build the 'what have I actually built' corpus that grounds resume bullets.

Two best-effort sources, cached in the settings table with a fetched-at stamp:

  GitHub  — the authed `gh` CLI: every repo's description, primary language,
            language byte-breakdown, topics, and a README head.
  Local   — the Desktop project roots in config.RESUME_SCAN_ROOTS: language mix
            (by file extension), dependency manifests, and a README head.

Everything is defensive — a missing `gh`, an unreadable repo, or an absent root
degrades to an empty section rather than raising. The corpus is passed verbatim
into the Claude prompts so it can speak accurately about stacks and surface work
that isn't on the resume yet.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

from .config import GH_BIN, RESUME_SCAN_ROOTS
from .store import Store

log = logging.getLogger("atlas.resume_corpus")

_GH_KEY = "resume_corpus_github"
_LOCAL_KEY = "resume_corpus_local"

# Directories we never descend into while scanning local projects.
_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".next", "target", "data", ".idea", ".vscode",
    "site-packages", ".tox", "coverage", ".cache",
}
# Extension → language label for the local language mix.
_EXT_LANG = {
    ".py": "Python", ".ipynb": "Jupyter", ".js": "JavaScript", ".ts": "TypeScript",
    ".jsx": "React", ".tsx": "React", ".java": "Java", ".cpp": "C++", ".cc": "C++",
    ".c": "C", ".cs": "C#", ".rs": "Rust", ".go": "Go", ".r": "R", ".R": "R",
    ".sql": "SQL", ".sh": "Shell", ".lua": "Lua", ".pine": "PineScript",
    ".html": "HTML", ".css": "CSS", ".swift": "Swift", ".kt": "Kotlin",
    ".m": "MATLAB/ObjC", ".jl": "Julia", ".rb": "Ruby",
}
_README_HEAD_CHARS = 1200
_MAX_GH_READMES = 12       # only fetch READMEs for the N most-recently-pushed repos
_MAX_SCAN_FILES = 20000    # per-root file-walk cap


# --- GitHub ------------------------------------------------------------------

def _gh(*args: str, timeout: float = 30.0) -> str | None:
    """Run a `gh` subcommand, returning stdout text or None on any failure."""
    if shutil.which(GH_BIN) is None and not Path(GH_BIN).exists():
        return None
    try:
        proc = subprocess.run([GH_BIN, *args], capture_output=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        log.warning("gh %s failed: %s", args[0] if args else "", e)
        return None
    if proc.returncode != 0:
        log.warning("gh %s exited %s: %s", args[0] if args else "", proc.returncode,
                    proc.stderr.decode("utf-8", "replace")[:200])
        return None
    return proc.stdout.decode("utf-8", "replace")


def gh_available() -> bool:
    return (shutil.which(GH_BIN) is not None or Path(GH_BIN).exists()) and _gh("auth", "status") is not None


def fetch_github(limit: int = 60) -> dict[str, Any]:
    """Pull repo metadata (+ README heads) via the authed gh CLI."""
    login_raw = _gh("api", "user", "--jq", ".login")
    user = (login_raw or "").strip()
    if not user:
        return {"available": False, "error": "gh not authenticated", "repos": [], "fetched_at": time.time()}

    listing = _gh(
        "repo", "list", user, "--limit", str(limit), "--source", "--no-archived",
        "--json", "name,description,primaryLanguage,pushedAt,isPrivate,repositoryTopics",
    )
    try:
        raw = json.loads(listing) if listing else []
    except json.JSONDecodeError:
        raw = []

    raw.sort(key=lambda r: r.get("pushedAt") or "", reverse=True)
    repos: list[dict[str, Any]] = []
    for i, r in enumerate(raw):
        lang = (r.get("primaryLanguage") or {}).get("name")
        topics = [t.get("name") for t in (r.get("repositoryTopics") or []) if t.get("name")]
        entry: dict[str, Any] = {
            "name": r.get("name"),
            "description": (r.get("description") or "").strip(),
            "primary_language": lang,
            "pushed_at": r.get("pushedAt"),
            "private": bool(r.get("isPrivate")),
            "topics": topics,
        }
        # Language byte-breakdown → ranked list.
        langs_raw = _gh("api", f"repos/{user}/{r['name']}/languages")
        try:
            langs = json.loads(langs_raw) if langs_raw else {}
            entry["languages"] = [k for k, _ in sorted(langs.items(), key=lambda kv: kv[1], reverse=True)]
        except json.JSONDecodeError:
            entry["languages"] = [lang] if lang else []
        # README head for the most-recently-touched repos only.
        if i < _MAX_GH_READMES:
            entry["readme"] = _gh_readme(user, r["name"])
        repos.append(entry)

    return {"available": True, "user": user, "repos": repos, "fetched_at": time.time()}


def _gh_readme(user: str, repo: str) -> str:
    raw = _gh("api", f"repos/{user}/{repo}/readme", "--jq", ".content")
    if not raw:
        return ""
    try:
        text = base64.b64decode(raw.strip()).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    return text[:_README_HEAD_CHARS].strip()


# --- Local projects ----------------------------------------------------------

def _scan_root(root: Path) -> dict[str, Any] | None:
    if not root.is_dir():
        return None
    lang_counts: dict[str, int] = {}
    manifests: list[str] = []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            seen += 1
            if seen > _MAX_SCAN_FILES:
                break
            ext = Path(fn).suffix
            lang = _EXT_LANG.get(ext)
            if lang:
                lang_counts[lang] = lang_counts.get(lang, 0) + 1
            if fn in ("pyproject.toml", "package.json", "requirements.txt", "Cargo.toml",
                      "go.mod", "Gemfile", "pom.xml", "build.gradle"):
                manifests.append(str(Path(dirpath).relative_to(root) / fn))
        if seen > _MAX_SCAN_FILES:
            break

    languages = [l for l, _ in sorted(lang_counts.items(), key=lambda kv: kv[1], reverse=True)][:10]
    return {
        "name": root.name,
        "path": str(root),
        "languages": languages,
        "dependencies": _read_dependencies(root),
        "manifests": manifests[:20],
        "readme": _read_readme(root),
    }


def _read_readme(root: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt", "readme.md", "README"):
        p = root / name
        if p.exists():
            try:
                return p.read_text(encoding="utf-8", errors="replace")[:_README_HEAD_CHARS].strip()
            except OSError:
                return ""
    return ""


def _read_dependencies(root: Path) -> list[str]:
    """Best-effort top-level dependency names from a project's manifest."""
    deps: list[str] = []
    py = root / "pyproject.toml"
    if py.exists():
        try:
            data = tomllib.loads(py.read_text(encoding="utf-8", errors="replace"))
            proj_deps = (data.get("project") or {}).get("dependencies") or []
            deps += [_dep_name(d) for d in proj_deps]
        except Exception:  # noqa: BLE001
            pass
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            deps += list((data.get("dependencies") or {}).keys())
        except Exception:  # noqa: BLE001
            pass
    req = root / "requirements.txt"
    if req.exists() and not deps:
        try:
            for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    deps.append(_dep_name(line))
        except OSError:
            pass
    # De-dup, keep order, cap.
    out, seen = [], set()
    for d in deps:
        if d and d not in seen:
            seen.add(d); out.append(d)
    return out[:30]


def _dep_name(spec: str) -> str:
    return spec.split(";")[0].split("[")[0].split("=")[0].split(">")[0].split("<")[0].split("~")[0].strip()


def fetch_local() -> dict[str, Any]:
    projects = [p for r in RESUME_SCAN_ROOTS if (p := _scan_root(Path(r))) is not None]
    return {"available": bool(projects), "projects": projects, "fetched_at": time.time()}


# --- Cache + public API ------------------------------------------------------

def refresh(store: Store, *, github: bool = True, local: bool = True) -> dict[str, Any]:
    """Re-pull the requested corpus sources and cache them. Blocking (shell-outs)."""
    result: dict[str, Any] = {}
    if github:
        gh = fetch_github()
        store.set_setting(_GH_KEY, gh)
        result["github"] = gh
    if local:
        lo = fetch_local()
        store.set_setting(_LOCAL_KEY, lo)
        result["local"] = lo
    return get(store)


def get(store: Store) -> dict[str, Any]:
    """Read the cached corpus (no fetch)."""
    return {
        "github": store.get_setting(_GH_KEY, {}) or {},
        "local": store.get_setting(_LOCAL_KEY, {}) or {},
    }


def as_prompt_context(store: Store, *, max_chars: int = 9000) -> str:
    """Compact, human-readable rendering of the corpus for a Claude prompt."""
    data = get(store)
    lines: list[str] = []
    gh = data.get("github") or {}
    for r in (gh.get("repos") or [])[:30]:
        langs = ", ".join(r.get("languages") or [])
        desc = r.get("description") or ""
        head = f"- GitHub/{r.get('name')} [{langs}]"
        if desc:
            head += f": {desc}"
        lines.append(head)
        rd = (r.get("readme") or "").replace("\n", " ").strip()
        if rd:
            lines.append(f"    README: {rd[:280]}")
    lo = data.get("local") or {}
    for p in (lo.get("projects") or []):
        langs = ", ".join(p.get("languages") or [])
        deps = ", ".join((p.get("dependencies") or [])[:15])
        lines.append(f"- Local/{p.get('name')} [{langs}]" + (f" — deps: {deps}" if deps else ""))
        rd = (p.get("readme") or "").replace("\n", " ").strip()
        if rd:
            lines.append(f"    README: {rd[:280]}")
    text = "\n".join(lines)
    return text[:max_chars] if text else "(no portfolio corpus cached yet)"
