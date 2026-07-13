"""Health: today's food totals vs. target, and recent workouts."""

from __future__ import annotations

import collections
import datetime as dt
import time

from .claude_bridge import ClaudeBridge, ClaudeError
from .store import Store

DEFAULT_CAL_TARGET = 2200


def _start_of_today() -> float:
    return dt.datetime.combine(dt.date.today(), dt.time.min).timestamp()


def today_summary(store: Store) -> dict:
    items = store.list_food(_start_of_today())
    cals = sum(i["calories"] or 0 for i in items)
    protein = sum(i["protein"] or 0 for i in items)
    carbs = sum(i["carbs"] or 0 for i in items)
    fat = sum(i["fat"] or 0 for i in items)
    target = store.get_setting("calorie_target", DEFAULT_CAL_TARGET)
    return {
        "date": dt.date.today().isoformat(),
        "calories": cals,
        "target": target,
        "remaining": target - cals,
        "protein": round(protein, 1),
        "carbs": round(carbs, 1),
        "fat": round(fat, 1),
        "items": items,
    }


def recent_workouts(store: Store, limit: int = 20) -> dict:
    workouts = store.list_workouts(limit)
    week_ago = time.time() - 7 * 86400
    this_week = [w for w in workouts if w["ts"] >= week_ago]
    return {
        "workouts": workouts,
        "week_count": len(this_week),
        "week_minutes": sum(w["duration_min"] or 0 for w in this_week),
    }


# --- Bodyweight ---------------------------------------------------------------


def bodyweight(store: Store) -> dict:
    series = store.list_bodyweight()
    latest = series[-1]["weight"] if series else None
    change = round(series[-1]["weight"] - series[0]["weight"], 1) if len(series) >= 2 else None
    return {"series": series, "latest": latest, "change": change}


# --- Strength progression -----------------------------------------------------


def _week_start(ts: float) -> str:
    d = dt.date.fromtimestamp(ts)
    return (d - dt.timedelta(days=(d.weekday() + 1) % 7)).isoformat()


def _suggest(series: list[dict]) -> dict | None:
    if not series:
        return None
    last = series[-1]["max_weight"]
    inc = 5 if last >= 100 else 2.5
    if len(series) >= 2 and series[-1]["max_weight"] == series[-2]["max_weight"]:
        return {"weight": last, "note": "Stalled — hold weight, add 1–2 reps or a set."}
    return {"weight": round(last + inc, 1), "note": f"Progressing — try +{inc:g} lb next session."}


def strength_progress(store: Store) -> list[dict]:
    """Per exercise: weekly max weight over time + a next-week suggestion."""
    by_ex: dict[str, list] = collections.defaultdict(list)
    for l in store.list_lifts():
        by_ex[l["exercise"]].append(l)
    out = []
    for ex, items in by_ex.items():
        weeks: dict[str, dict] = {}
        for l in items:
            wk = _week_start(l["ts"])
            w = weeks.setdefault(wk, {"week": wk, "max_weight": 0.0, "top_reps": None})
            if l["weight"] > w["max_weight"]:
                w["max_weight"] = l["weight"]; w["top_reps"] = l["reps"]
        series = [weeks[k] for k in sorted(weeks)]
        out.append({
            "exercise": ex, "series": series,
            "current": series[-1]["max_weight"] if series else None,
            "suggestion": _suggest(series),
        })
    out.sort(key=lambda e: e["exercise"].lower())
    return out


# --- Recovery signals from workout logs (feed the coach) -----------------------


def add_recovery_note(store: Store, rec: dict) -> None:
    """Append a workout-parsed recovery signal to a rolling log (last 14 kept)."""
    notes = store.get_setting("recovery_notes", []) or []
    parts = [f"{k}: {rec[k]}" for k in ("soreness", "sleep", "energy", "note") if rec.get(k)]
    if not parts:
        return
    notes.append({"ts": time.time(), "text": "; ".join(str(p) for p in parts)})
    store.set_setting("recovery_notes", notes[-14:])


def recent_recovery_notes(store: Store, days: float = 7.0) -> list[str]:
    cutoff = time.time() - days * 86400
    return [n["text"] for n in (store.get_setting("recovery_notes", []) or []) if n["ts"] >= cutoff]


# --- Recovery insight (Claude) ------------------------------------------------

_RECOVERY = """You are a concise fitness recovery + nutrition coach. Using this week's data,
respond with ONLY a JSON object (no prose, no markdown):
{{"insight": str (2-3 sentences on recovery & nutrition status),
  "recovery": "good"|"moderate"|"low",
  "workout_suggestion": str (what to train next and at what intensity, 1-2 sentences)}}

Bodyweight: {bw}
This week: {wk_count} workouts ({wk_min} min total). Today's intake: {cals}/{target} kcal, {protein} g protein.
Recent workouts: {recent_workouts}
Recent lifts (exercise@weight): {recent_lifts}
Recovery signals from workout logs (soreness/sleep/energy): {recovery_signals}
Extra context the user texted: {notes}"""


async def recovery(store: Store, claude: ClaudeBridge) -> dict:
    food = today_summary(store)
    wk = recent_workouts(store, limit=8)
    bw = bodyweight(store)
    lifts = store.list_lifts(limit=12)
    notes = " | ".join(m["text"] for m in (store.get_setting("health_chat", []) or [])[-6:] if m["role"] == "user") or "none"
    prompt = _RECOVERY.format(
        bw=f"{bw['latest']} lb (Δ{bw['change']})" if bw["latest"] else "not logged",
        wk_count=wk["week_count"], wk_min=wk["week_minutes"],
        cals=food["calories"], target=food["target"], protein=food["protein"],
        recent_workouts=", ".join(f"{w['type']}" for w in wk["workouts"][:5]) or "none",
        recent_lifts=", ".join(f"{l['exercise']}@{l['weight']}" for l in lifts[:8]) or "none",
        recovery_signals=" | ".join(recent_recovery_notes(store)) or "none",
        notes=notes,
    )
    try:
        data = await claude.extract_json(prompt)
    except ClaudeError as e:
        return {"insight": f"(Could not generate: {e})", "recovery": "moderate", "workout_suggestion": ""}
    if not isinstance(data, dict):
        data = {"insight": str(data), "recovery": "moderate", "workout_suggestion": ""}
    data["at"] = time.time()
    store.set_setting("recovery_latest", data)
    return data


def get_recovery(store: Store) -> dict | None:
    return store.get_setting("recovery_latest")


# --- Health AI chat (log meals by text / ask for advice) ----------------------

_CHAT = """You are Atlas's health assistant. The user may tell you what they ate (log it) or ask
for recovery/nutrition/training advice. Respond with ONLY a JSON object (no prose, no markdown):
{{"reply": str (friendly, concise, 1-3 sentences),
  "foods": [{{"description": str, "calories": int, "protein": number, "carbs": number, "fat": number}}]}}
If the message describes food the user ate, fill `foods` with your best estimate (one entry per item
or meal) and acknowledge it in `reply`. Otherwise `foods` is [] and `reply` answers their question.

Context — today: {cals}/{target} kcal, {protein} g protein; this week {wk} workouts.
User: {message}"""


async def chat(store: Store, claude: ClaudeBridge, message: str) -> dict:
    food = today_summary(store)
    wk = recent_workouts(store, limit=8)
    try:
        data = await claude.extract_json(_CHAT.format(
            cals=food["calories"], target=food["target"], protein=food["protein"],
            wk=wk["week_count"], message=message,
        ))
    except ClaudeError as e:
        raise
    if not isinstance(data, dict):
        data = {"reply": str(data), "foods": []}
    logged = []
    for f in data.get("foods", []) or []:
        if isinstance(f, dict) and f.get("description"):
            store.add_food(f["description"], f.get("calories"), f.get("protein"), f.get("carbs"), f.get("fat"), source="chat")
            logged.append(f["description"])
    hist = store.get_setting("health_chat", []) or []
    hist.append({"role": "user", "text": message, "ts": time.time()})
    hist.append({"role": "ai", "text": data.get("reply", ""), "ts": time.time()})
    store.set_setting("health_chat", hist[-40:])
    return {"reply": data.get("reply", ""), "logged": logged, "history": hist[-40:]}


def chat_history(store: Store) -> list[dict]:
    return store.get_setting("health_chat", []) or []
