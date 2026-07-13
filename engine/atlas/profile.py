"""The master career PROFILE — a conversational, single source of truth that every
résumé is generated from.

Atlas holds the profile as a settings blob (no schema churn): a short summary, a list
of roles (each with concrete accomplishment `facts`), and a skills list. The user grows
it by talking to Claude ("hey Atlas, I want to add my new role at Seaport…"); the agent
draws out details, then records structured updates. `as_prompt_context` renders it for
the résumé prompts so drafting/refinement is grounded in real, user-confirmed work.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from . import resume_corpus
from .claude_bridge import ClaudeBridge, PROFILE_CHAT
from .store import Store

log = logging.getLogger("atlas.profile")

_PROFILE_KEY = "profile"
_CHAT_KEY = "profile_chat"
_MAX_CHAT = 40


def get(store: Store) -> dict[str, Any]:
    p = store.get_setting(_PROFILE_KEY) or {}
    return {
        "summary": p.get("summary", ""),
        "education": p.get("education", []),
        "roles": p.get("roles", []),
        "skills": p.get("skills", []),
        "updated_at": p.get("updated_at"),
    }


def _save(store: Store, p: dict[str, Any]) -> None:
    p["updated_at"] = time.time()
    store.set_setting(_PROFILE_KEY, p)


def apply_updates(store: Store, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge a Claude `updates` block into the stored profile."""
    p = get(store)
    if not isinstance(updates, dict):
        return p
    # Roles: match on company+role (case-insensitive); append/dedup facts.
    for r in updates.get("roles") or []:
        if not isinstance(r, dict) or not (r.get("company") and r.get("role")):
            continue
        key = (str(r["company"]).strip().lower(), str(r["role"]).strip().lower())
        existing = next(
            (x for x in p["roles"]
             if (x.get("company", "").strip().lower(), x.get("role", "").strip().lower()) == key),
            None,
        )
        facts = [str(f).strip() for f in (r.get("facts") or []) if str(f).strip()]
        if existing:
            have = {f.lower() for f in existing.get("facts", [])}
            existing["facts"] = existing.get("facts", []) + [f for f in facts if f.lower() not in have]
            for fld in ("dates", "location", "category_hint"):
                if r.get(fld):
                    existing[fld] = r[fld]
        else:
            p["roles"].append({
                "company": str(r["company"]).strip(), "role": str(r["role"]).strip(),
                "dates": r.get("dates") or "", "location": r.get("location"),
                "category_hint": r.get("category_hint"), "facts": facts,
            })
    # Education: match on school+degree; update fields.
    for ed in updates.get("education") or []:
        if not isinstance(ed, dict) or not ed.get("school"):
            continue
        key = (str(ed["school"]).strip().lower(), str(ed.get("degree", "")).strip().lower())
        existing = next(
            (x for x in p["education"]
             if (x.get("school", "").strip().lower(), x.get("degree", "").strip().lower()) == key),
            None,
        )
        if existing:
            for fld in ("degree", "dates", "location", "coursework", "honors"):
                if ed.get(fld):
                    existing[fld] = ed[fld]
        else:
            p["education"].append({
                "school": str(ed["school"]).strip(), "degree": ed.get("degree", ""),
                "dates": ed.get("dates", ""), "location": ed.get("location"),
                "coursework": ed.get("coursework", ""), "honors": ed.get("honors", ""),
            })
    # Skills: dedup, preserve order.
    if updates.get("skills"):
        have = {s.lower() for s in p["skills"]}
        for s in updates["skills"]:
            s = str(s).strip()
            if s and s.lower() not in have:
                have.add(s.lower()); p["skills"].append(s)
    if updates.get("summary"):
        p["summary"] = str(updates["summary"]).strip()
    _save(store, p)
    return p


def delete_role(store: Store, idx: int) -> dict[str, Any]:
    p = get(store)
    if 0 <= idx < len(p["roles"]):
        p["roles"].pop(idx)
        _save(store, p)
    return p


def delete_education(store: Store, idx: int) -> dict[str, Any]:
    p = get(store)
    if 0 <= idx < len(p["education"]):
        p["education"].pop(idx)
        _save(store, p)
    return p


def delete_skill(store: Store, skill: str) -> dict[str, Any]:
    p = get(store)
    p["skills"] = [s for s in p["skills"] if s.lower() != skill.strip().lower()]
    _save(store, p)
    return p


# --- Chat --------------------------------------------------------------------

def chat_history(store: Store) -> list[dict[str, str]]:
    return store.get_setting(_CHAT_KEY, []) or []


def _push_chat(store: Store, role: str, text: str) -> None:
    hist = chat_history(store)
    hist.append({"role": role, "text": text})
    store.set_setting(_CHAT_KEY, hist[-_MAX_CHAT:])


def clear_chat(store: Store) -> None:
    store.set_setting(_CHAT_KEY, [])


def _history_text(store: Store, limit: int = 12) -> str:
    hist = chat_history(store)[-limit:]
    if not hist:
        return "(new conversation)"
    return "\n".join(f"{'USER' if m['role'] == 'user' else 'ATLAS'}: {m['text']}" for m in hist)


async def chat(store: Store, claude: ClaudeBridge, message: str) -> dict[str, Any]:
    """One conversational turn: reply + apply any structured profile updates."""
    import json as _json
    prompt = PROFILE_CHAT.format(
        profile=_json.dumps(get(store), indent=2),
        corpus=resume_corpus.as_prompt_context(store, max_chars=5000),
        history=_history_text(store),
        message=message,
    )
    _push_chat(store, "user", message)
    data = await claude.extract_json(prompt)
    reply = ""
    updates: dict[str, Any] = {}
    if isinstance(data, dict):
        reply = str(data.get("reply") or "").strip()
        updates = data.get("updates") or {}
    if not reply:
        reply = "Got it."
    profile = apply_updates(store, updates)
    _push_chat(store, "assistant", reply)
    return {"reply": reply, "profile": profile, "history": chat_history(store)}


# --- Prompt context ----------------------------------------------------------

def as_prompt_context(store: Store, *, max_chars: int = 6000) -> str:
    """Render the profile as a grounding block for résumé prompts."""
    p = get(store)
    if not (p["summary"] or p["roles"] or p["skills"] or p["education"]):
        return "(no profile captured yet)"
    lines: list[str] = ["PROFILE:"]
    if p["summary"]:
        lines.append(f"Summary: {p['summary']}")
    for ed in p["education"]:
        loc = f", {ed['location']}" if ed.get("location") else ""
        lines.append(f"- Education: {ed.get('degree', '')} — {ed['school']}{loc} ({ed.get('dates', '')})")
        if ed.get("coursework"):
            lines.append(f"    Coursework: {ed['coursework']}")
        if ed.get("honors"):
            lines.append(f"    Honors: {ed['honors']}")
    for r in p["roles"]:
        loc = f", {r['location']}" if r.get("location") else ""
        hint = f" [{r['category_hint']}]" if r.get("category_hint") else ""
        lines.append(f"- {r['role']} @ {r['company']}{loc} ({r.get('dates', '')}){hint}")
        for f in r.get("facts", []):
            lines.append(f"    · {f}")
    if p["skills"]:
        lines.append("Skills: " + ", ".join(p["skills"]))
    text = "\n".join(lines)
    return text[:max_chars]
