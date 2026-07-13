"""Resume tab orchestration: templates, entry drafting, JD tailoring, and the
compile → read-the-PDF → revise auto-fit loop.

The persistent local Claude session (`claude_bridge`) is the writer; Tectonic
(`latex`) is the eyes — it compiles to PDF so Claude can read the result back and
verify it fits one clean page. All heavy work (Claude calls, compilation) is sync
and meant to run under `asyncio.to_thread` / `await` from the server routes.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from . import latex as tex
from . import profile as profile_mod
from . import resume_corpus
from .claude_bridge import (
    ClaudeBridge,
    RESUME_ENTRY_FROM_CONTRIB,
    RESUME_REFINE_CHAT,
    RESUME_STYLE_RULES,
    RESUME_TAILOR_TO_JD,
    RESUME_VISUAL_CRITIQUE,
)
from .config import RESUME_DIR
from .store import Store

log = logging.getLogger("atlas.resume")

CATEGORIES = ["swe", "quant", "finance"]
CATEGORY_LABELS = {"swe": "Software Engineer", "quant": "Quant Dev", "finance": "Finance"}
VARIANTS = {
    "swe": ["", "data-analyst"],
    "quant": ["", "research-pipelines"],
    "finance": [""],
}
# Per-category emphasis so technicality is pitched to the audience — a Finance résumé
# shouldn't read like a systems-internals deep dive.
CATEGORY_EMPHASIS = {
    "swe": "Foreground engineering depth, systems design, scale, and shipped software. "
           "Concrete architecture, tools, and reliability read well here.",
    "quant": "Foreground quantitative methods, data rigor, models, and research process. "
             "Math/stats/markets detail is welcome; keep pure software plumbing lighter.",
    "finance": "Foreground financial reasoning, markets, valuation, risk, and business impact. "
               "De-emphasize deep systems/ML internals — translate technical work into outcomes "
               "a finance reader values; keep jargon accessible.",
}
_MAX_FIT_ITERS = 4
_MAX_DOC_CHAT = 30


# --- Build dirs --------------------------------------------------------------

def _doc_dir(doc_id: int) -> Path:
    d = RESUME_DIR / str(doc_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _preview_dir() -> Path:
    d = RESUME_DIR / "_preview"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _context(store: Store) -> str:
    """Combined grounding block for résumé prompts: the master profile + repo/project corpus."""
    return (
        profile_mod.as_prompt_context(store)
        + "\n\nPORTFOLIO CORPUS (repos + local projects):\n"
        + resume_corpus.as_prompt_context(store)
    )


# --- Overview / reads --------------------------------------------------------

def overview(store: Store) -> dict[str, Any]:
    docs = store.list_resume_docs()
    by_cat: dict[str, list[dict]] = {c: [] for c in CATEGORIES}
    for d in docs:
        by_cat.setdefault(d["category"], []).append(_doc_summary(d))
    corpus = resume_corpus.get(store)
    return {
        "categories": [{"key": c, "label": CATEGORY_LABELS[c], "variants": VARIANTS.get(c, [""])}
                       for c in CATEGORIES],
        "docs": by_cat,
        "tectonic_available": tex.available(),
        "corpus": {
            "github": {
                "available": bool((corpus.get("github") or {}).get("available")),
                "count": len((corpus.get("github") or {}).get("repos") or []),
                "fetched_at": (corpus.get("github") or {}).get("fetched_at"),
            },
            "local": {
                "available": bool((corpus.get("local") or {}).get("available")),
                "count": len((corpus.get("local") or {}).get("projects") or []),
                "fetched_at": (corpus.get("local") or {}).get("fetched_at"),
            },
        },
    }


def _doc_summary(d: dict) -> dict:
    return {
        "id": d["id"], "category": d["category"], "variant": d.get("variant"),
        "label": d["label"], "base": bool(d["base"]), "parent_id": d.get("parent_id"),
        "updated_at": d["updated_at"], "created_at": d["created_at"],
        "fit": (d.get("meta") or {}).get("fit"),
    }


def get_doc(store: Store, doc_id: int) -> dict | None:
    return store.get_resume_doc(doc_id)


def save_doc(store: Store, doc_id: int, **fields: Any) -> dict | None:
    doc = store.update_resume_doc(doc_id, **fields)
    # Editing the source invalidates the cached PDF.
    if doc and "latex" in fields:
        store.update_resume_doc(doc_id, pdf_path=None)
        doc = store.get_resume_doc(doc_id)
    return doc


def delete_doc(store: Store, doc_id: int) -> bool:
    return store.delete_resume_doc(doc_id)


def set_base(store: Store, doc_id: int) -> dict | None:
    return store.set_base_resume_doc(doc_id)


# --- Compilation -------------------------------------------------------------

def compile_doc(store: Store, doc_id: int) -> dict[str, Any]:
    """Compile a saved doc into its own build dir; persist pdf_path + fit diag."""
    doc = store.get_resume_doc(doc_id)
    if not doc:
        return {"ok": False, "error": "doc not found"}
    diag = tex.compile(doc["latex"], _doc_dir(doc_id))
    meta = doc.get("meta") or {}
    meta["fit"] = _fit_summary(diag)
    store.update_resume_doc(doc_id, pdf_path=diag.get("pdf_path"), meta=meta)
    return diag


def ensure_pdf(store: Store, doc_id: int) -> str | None:
    """Return a current compiled PDF path for the doc, compiling if stale/missing."""
    doc = store.get_resume_doc(doc_id)
    if not doc:
        return None
    pdf = doc.get("pdf_path")
    if pdf and Path(pdf).exists():
        return pdf
    diag = compile_doc(store, doc_id)
    return diag.get("pdf_path")


def compile_preview(latex: str) -> dict[str, Any]:
    """Compile ad-hoc LaTeX from the editor into the shared preview dir."""
    return tex.compile(latex, _preview_dir())


def _fit_summary(diag: dict) -> dict:
    return {"ok": diag.get("ok"), "page_count": diag.get("page_count"),
            "overfull": len(diag.get("overfull") or []), "error": diag.get("error")}


# --- Claude-driven drafting --------------------------------------------------

async def write_entry(
    store: Store, claude: ClaudeBridge, *, company: str, role: str, dates: str,
    contributions: str, category: str, n_bullets: int = 3, save: bool = True,
    location: str | None = None,
) -> dict[str, Any]:
    """Distil raw contributions into fitted LaTeX bullets for one role."""
    corpus = _context(store)
    prompt = RESUME_ENTRY_FROM_CONTRIB.format(
        category=CATEGORY_LABELS.get(category, category), company=company, role=role,
        dates=dates, contributions=contributions, corpus=corpus, style=RESUME_STYLE_RULES,
        n_bullets=n_bullets,
    )
    data = await claude.extract_json(prompt)
    bullets = data.get("bullets") if isinstance(data, dict) else None
    if not isinstance(bullets, list):
        return {"error": "Claude did not return bullets", "bullets": []}
    bullets = [str(b).strip() for b in bullets if str(b).strip()]
    block_latex = _entry_block_latex(company, role, dates, location, bullets)
    entry = None
    if save:
        entry = store.add_resume_entry(
            company=company, role=role, location=location, dates=dates, category=category,
            latex=block_latex, raw_contrib=contributions,
        )
    return {"bullets": bullets, "block": block_latex, "entry": entry}


def _entry_block_latex(company: str, role: str, dates: str, location: str | None,
                       bullets: list[str]) -> str:
    """Assemble bullets into a full experience block matching the base template."""
    loc = f" | {location}" if location else ""
    items = "\n".join(f"    \\item {b}" for b in bullets)
    return (
        f"\\textbf{{{company}}}{loc} \\hfill {dates} \\\\\n"
        f"\\textit{{{role}}}\n"
        f"\\begin{{itemize}}\n{items}\n\\end{{itemize}}"
    )


async def tailor(
    store: Store, claude: ClaudeBridge, *, base_doc: dict, job_description: str,
    label: str, variant: str | None = None,
) -> dict[str, Any]:
    """Tailor a base resume to a JD, auto-fit to one page, save as a new labeled doc."""
    cat = base_doc["category"]
    style = RESUME_STYLE_RULES + "\n\nCATEGORY EMPHASIS (" + CATEGORY_LABELS.get(cat, cat) + "): " + \
        CATEGORY_EMPHASIS.get(cat, "")
    prompt = RESUME_TAILOR_TO_JD.format(
        label=label, job_description=job_description, corpus=_context(store),
        latex=base_doc["latex"], style=style,
    )
    raw = await claude.ask(prompt, timeout=300.0)
    src = _extract_latex(raw) or base_doc["latex"]

    doc = store.add_resume_doc(
        category=base_doc["category"], variant=variant if variant is not None else base_doc.get("variant"),
        label=label, latex=src, base=False, parent_id=base_doc["id"],
        meta={"job_description": job_description[:4000]},
    )
    diag = await _fit_doc(store, claude, doc["id"])
    return {"doc": store.get_resume_doc(doc["id"]), "fit": _fit_summary(diag)}


async def refine_doc(store: Store, claude: ClaudeBridge, doc_id: int) -> dict[str, Any]:
    """Run the auto-fit loop on an existing doc (e.g. after inserting an entry)."""
    diag = await _fit_doc(store, claude, doc_id)
    return {"doc": store.get_resume_doc(doc_id), "fit": _fit_summary(diag)}


# --- Refinement chat (natural-language edits, raw LaTeX stays hidden) ---------

def _chat_key(doc_id: int) -> str:
    return f"resume_chat:{doc_id}"


def doc_chat_history(store: Store, doc_id: int) -> list[dict[str, str]]:
    return store.get_setting(_chat_key(doc_id), []) or []


def _push_doc_chat(store: Store, doc_id: int, role: str, text: str) -> None:
    hist = doc_chat_history(store, doc_id)
    hist.append({"role": role, "text": text})
    store.set_setting(_chat_key(doc_id), hist[-_MAX_DOC_CHAT:])


def _doc_history_text(store: Store, doc_id: int, limit: int = 8) -> str:
    hist = doc_chat_history(store, doc_id)[-limit:]
    if not hist:
        return "(new conversation)"
    return "\n".join(f"{'USER' if m['role'] == 'user' else 'ATLAS'}: {m['text']}" for m in hist)


async def refine_chat(store: Store, claude: ClaudeBridge, doc_id: int, message: str) -> dict[str, Any]:
    """Apply a natural-language refinement to a résumé, recompile, and keep it one page.

    The model returns `<reply> %%%LATEX%%% <full source>` — a delimiter, not JSON, so the
    large LaTeX body never has to survive JSON escaping. We fit-loop the new source and save.
    """
    doc = store.get_resume_doc(doc_id)
    if not doc:
        return {"error": "doc not found"}
    cat = doc["category"]
    prompt = RESUME_REFINE_CHAT.format(
        category=CATEGORY_LABELS.get(cat, cat),
        category_note=CATEGORY_EMPHASIS.get(cat, ""),
        message=message,
        history=_doc_history_text(store, doc_id),
        context=_context(store),
        latex=doc["latex"],
        style=RESUME_STYLE_RULES,
    )
    _push_doc_chat(store, doc_id, "user", message)
    raw = await claude.ask(prompt, timeout=300.0)
    reply, new_src = _split_refine(raw)
    if new_src:
        store.update_resume_doc(doc_id, latex=new_src)
        diag = await _fit_doc(store, claude, doc_id)
        fit = _fit_summary(diag)
    else:
        fit = (store.get_resume_doc(doc_id) or {}).get("meta", {}).get("fit")
    _push_doc_chat(store, doc_id, "assistant", reply)
    return {"reply": reply, "fit": fit, "history": doc_chat_history(store, doc_id),
            "doc": store.get_resume_doc(doc_id)}


def _split_refine(raw: str) -> tuple[str, str | None]:
    """Split a refine response into (reply, latex) on the %%%LATEX%%% delimiter."""
    if "%%%LATEX%%%" in raw:
        head, _, tail = raw.partition("%%%LATEX%%%")
        reply = head.strip() or "Updated."
        return reply, _extract_latex(tail)
    # No delimiter — maybe the whole thing is LaTeX, or just a chat reply.
    tex_only = _extract_latex(raw)
    if tex_only:
        return "Updated the résumé.", tex_only
    return raw.strip() or "Done.", None


async def _fit_doc(store: Store, claude: ClaudeBridge, doc_id: int) -> dict[str, Any]:
    """Compile → read PDF → revise, up to _MAX_FIT_ITERS, persisting the result."""
    build = _doc_dir(doc_id)
    doc = store.get_resume_doc(doc_id)
    src = doc["latex"]
    diag = await asyncio.to_thread(tex.compile, src, build)

    for _ in range(_MAX_FIT_ITERS):
        pdf = diag.get("pdf_path")
        prompt = RESUME_VISUAL_CRITIQUE.format(
            pdf_path=pdf or "(compile failed — no PDF; fix the LaTeX error below)",
            page_count=diag.get("page_count"),
            overfull=diag.get("overfull") or ("compile error: " + str(diag.get("error")) if not pdf else "none"),
            latex=src,
        )
        extra = [build] if pdf else None
        try:
            crit = await claude.extract_json(prompt, extra_dirs=extra)
        except Exception as e:  # noqa: BLE001 - keep best-effort result on a bad critique
            log.warning("visual critique failed: %s", e)
            break
        if not isinstance(crit, dict):
            break
        if crit.get("ok") and diag.get("ok"):
            break
        new_src = crit.get("latex")
        if not new_src or not isinstance(new_src, str) or new_src.strip() == src.strip():
            break
        src = _extract_latex(new_src) or new_src
        diag = await asyncio.to_thread(tex.compile, src, build)

    meta = (store.get_resume_doc(doc_id) or {}).get("meta") or {}
    meta["fit"] = _fit_summary(diag)
    store.update_resume_doc(doc_id, latex=src, pdf_path=diag.get("pdf_path"), meta=meta)
    return diag


_DOC_RE = re.compile(r"\\documentclass.*\\end\{document\}", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:latex|tex)?\s*|\s*```$", re.MULTILINE)


def _extract_latex(raw: str) -> str | None:
    """Pull the \\documentclass…\\end{document} span out of a model response."""
    if not raw:
        return None
    cleaned = _FENCE_RE.sub("", raw)
    m = _DOC_RE.search(cleaned)
    return m.group(0).strip() if m else None
