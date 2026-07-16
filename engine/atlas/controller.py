"""Atlas Copilot — the global controller agent.

One conversational entry point that can read the whole dashboard's state and act on
any module: update the profile, consolidate résumés, add todos, tweak settings, or
just drive the UI (navigate / refresh a tab). It hands Claude a compact state index
plus the full content of any résumés/profile the request references, and executes a
whitelisted set of actions Claude returns. No blind DB access — every action is typed.
"""

from __future__ import annotations

import json
import logging
import re

from . import gcal, networth, paycheck, profile as profile_mod, resume as resume_mod
from .claude_bridge import ClaudeBridge, ClaudeError
from .store import Store

log = logging.getLogger("atlas.copilot")

VIEW_LABELS = {
    "home": "Home", "todos": "Todos", "money": "Money", "networth": "Net Worth",
    "picks": "Picks", "soccer": "Soccer", "health": "Health", "inbox": "Inbox",
    "meetings": "Meetings", "profile": "Profile", "resume": "Resume", "settings": "Settings",
}

_PROMPT = """You are Atlas Copilot, the control agent for the user's personal life-dashboard "Atlas".
You can read the dashboard's data (below) and act on it. Be decisive and concrete.

The user is currently on the "{view_label}" tab.{tab_note}

=== DASHBOARD STATE ===
{state}

{resources}
=== USER REQUEST ===
"{message}"

Respond with ONLY a JSON object (no prose, no markdown):
{{"reply": str (1-4 sentences, first person as Atlas, saying what you did or asking a needed question),
  "actions": [ ... ],           // the changes to apply; [] if none/just answering
  "navigate": str|null}}         // a tab key to switch the UI to afterward, or null

AVAILABLE ACTIONS (emit only what the request needs):
- {{"action":"profile_update","updates":{{"summary":str?,
     "roles":[{{"company":str,"role":str,"dates":str?,"location":str?,"facts":[str]}}]?,
     "education":[{{"school":str,"degree":str?,"dates":str?}}]?,"skills":[str]?}}}}
- {{"action":"resume_save","category":"swe"|"quant"|"finance","label":str,"latex":str}}   // create a new résumé doc
- {{"action":"resume_update","doc_id":int,"latex":str}}                                    // overwrite an existing doc
- {{"action":"todo_add","title":str,"category":str?,"priority":"low"|"normal"|"high"?,"due":"YYYY-MM-DD"?}}
- {{"action":"todo_complete","id":int}}
- {{"action":"setting_set","key":str,"value":<any>}}

RULES:
- "Consolidate résumé X and Y": read their LaTeX from RESOURCES, merge into ONE cohesive one-page
  résumé — dedupe roles/bullets, keep the strongest phrasing, preserve a valid compilable preamble
  from one of them. Emit resume_save with the FULL merged LaTeX and a label like "X + Y (consolidated)".
- "Fill/update my profile from a résumé": extract roles (company/role/dates + concrete accomplishment
  facts), education, and skills from the résumé LaTeX and emit ONE profile_update. Never invent facts.
- Only use resume doc_ids that appear in the state/resources. NEVER invent ids.
- If the request is ambiguous or you lack the content, ask a short clarifying question in "reply"
  and emit no actions. Prefer doing the obvious thing over asking.
- Keep LaTeX valid; don't include markdown fences inside the latex string."""


def _state(store: Store) -> str:
    prof = profile_mod.get(store)
    docs = store.list_resume_docs()
    pay = paycheck.compute_status(store)
    nw = networth.compute(store)
    todos = [t for t in store.list_todos(include_done=False)]
    state = {
        "profile": {
            "summary": (prof.get("summary") or "")[:200],
            "roles": [f'{r.get("role")} @ {r.get("company")}' for r in prof.get("roles", [])],
            "skills_count": len(prof.get("skills", [])),
            "education": [e.get("school") for e in prof.get("education", [])],
        },
        "resumes": [{"id": d["id"], "label": d["label"], "category": d["category"], "base": bool(d["base"])} for d in docs],
        "todos_open": [{"id": t["id"], "title": t["title"]} for t in todos[:20]],
        "money": {"running_net": pay["running"]["net"], "next_payday": pay["next_payday"]},
        "networth": {"total": nw["total"]},
        "calendar_upcoming": [{"title": e["title"], "start": e["start"]} for e in gcal.upcoming(store, within_min=1440)[:5]],
    }
    return json.dumps(state, indent=1, default=str)


def _relevant_resumes(store: Store, message: str) -> list[dict]:
    """Full LaTeX of résumés the request references (by label token), else recent ones."""
    docs = store.list_resume_docs()
    if not docs:
        return []
    msg = message.lower()
    def toks(s: str) -> set[str]:
        return {t for t in re.findall(r"[a-z0-9]{3,}", (s or "").lower()) if t not in _STOP}
    matched = [d for d in docs if toks(d["label"]) & toks(msg)]
    if not matched and any(w in msg for w in ("resume", "résumé", "profile", "consolidat", "merge", "cv")):
        matched = sorted(docs, key=lambda d: d["updated_at"], reverse=True)[:3]
    return matched[:4]


_STOP = {"the", "and", "for", "resume", "resumes", "with", "into", "from", "consolidate", "merge"}


def _resources(store: Store, message: str) -> str:
    parts: list[str] = []
    rez = _relevant_resumes(store, message)
    if rez:
        parts.append("=== RÉSUMÉ CONTENTS (referenced) ===")
        for d in rez:
            latex = (d.get("latex") or "")[:6000]
            parts.append(f'[resume id={d["id"]} label="{d["label"]}" category={d["category"]}]\n{latex}')
    if any(w in message.lower() for w in ("profile", "résumé", "resume", "career", "role", "skill")):
        parts.append("=== FULL PROFILE (JSON) ===\n" + json.dumps(profile_mod.get(store), default=str)[:4000])
    return ("\n\n".join(parts) + "\n") if parts else ""


async def run(store: Store, claude: ClaudeBridge, message: str, view: str | None, tab_note: str | None = None) -> dict:
    prompt = _PROMPT.format(
        view_label=VIEW_LABELS.get(view or "", "dashboard"),
        tab_note=f" Note about that tab: {tab_note}" if tab_note else "",
        state=_state(store),
        resources=_resources(store, message),
        message=message.replace('"', "'"),
    )
    data = await claude.extract_json(prompt)
    if not isinstance(data, dict):
        return {"reply": str(data), "applied": [], "navigate": None}
    applied, errors = await _apply(store, claude, data.get("actions") or [])
    return {
        "reply": data.get("reply", ""),
        "applied": applied,
        "errors": errors,
        "navigate": data.get("navigate") if data.get("navigate") in VIEW_LABELS else None,
    }


async def _apply(store: Store, claude: ClaudeBridge, actions: list) -> tuple[list[str], list[str]]:
    applied, errors = [], []
    valid_docs = {d["id"] for d in store.list_resume_docs()}
    for a in actions:
        if not isinstance(a, dict):
            continue
        act = a.get("action")
        try:
            if act == "profile_update":
                profile_mod.apply_updates(store, a.get("updates") or {})
                applied.append("updated your profile")
            elif act == "resume_save" and a.get("latex") and a.get("category") in resume_mod.CATEGORIES:
                d = store.add_resume_doc(a["category"], a.get("label") or "Copilot draft", a["latex"])
                applied.append(f'saved résumé “{d["label"]}” (id {d["id"]})')
            elif act == "resume_update" and a.get("doc_id") in valid_docs and a.get("latex"):
                resume_mod.save_doc(store, int(a["doc_id"]), latex=a["latex"])
                applied.append(f'updated résumé id {a["doc_id"]}')
            elif act == "todo_add" and a.get("title"):
                store.add_todo(a["title"], category=a.get("category", "Inbox"),
                               priority=a.get("priority", "normal"), due=a.get("due"), source="claude")
                applied.append(f'added todo “{a["title"]}”')
            elif act == "todo_complete" and a.get("id") is not None:
                store.update_todo(int(a["id"]), done=True)
                applied.append(f'completed todo {a["id"]}')
            elif act == "setting_set" and a.get("key"):
                store.set_setting(a["key"], a.get("value"))
                applied.append(f'set {a["key"]}')
            else:
                errors.append(f"skipped unknown/invalid action: {act}")
        except Exception as e:  # noqa: BLE001 — surface per-action failures, keep going
            errors.append(f"{act}: {e}")
    return applied, errors
