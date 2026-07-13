"""Lineup checks for the Soccer tab (ESPN public scoreboard, no key).

The WC 2026 model's "projected XI" is a caps-based proxy — it doesn't know who
is actually starting today. Before Atlas recommends a bet it checks the real
team sheet: ESPN posts starters ~1h before kickoff and rosters/absence notes
earlier. A match's picks are only *recommendable* (cards ranked up, parlay
eligible) once its lineups clear:

    confirmed  starters posted for both sides and no key player missing
    probable   rosters visible, starters not yet flagged — cleared unless a
               key player (top xG/goals contributor per soccer.json) is absent
    unknown    ESPN has nothing yet / fetch failed — shown, never recommended

Results cache in the `soccer_lineups` setting; the UI re-pulls on demand and
whenever the cache is older than ~15 min around match days.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
import urllib.parse
import urllib.request

from .store import Store

log = logging.getLogger("atlas.soccer_lineups")

_KEY = "soccer_lineups"
_API = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world"
_LOOKAHEAD_DAYS = 2  # only chase lineups for matches kicking off soon

# ESPN naming vs openfootball naming.
_TEAM_ALIASES = {
    "usa": "united states",
    "czechia": "czech republic",
    "bosnia herzegovina": "bosnia and herzegovina",
    "korea republic": "south korea",
    "ir iran": "iran",
    "cote d ivoire": "ivory coast",
}


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return _TEAM_ALIASES.get(s, s)


def _get(url: str, timeout: float = 20.0, retries: int = 2) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Atlas/0.1 (local dashboard)"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except TimeoutError:
            if attempt == retries:
                raise
    return {}


def _scoreboard(date_yyyymmdd: str) -> list[dict]:
    try:
        data = _get(f"{_API}/scoreboard?{urllib.parse.urlencode({'dates': date_yyyymmdd})}")
        return data.get("events", []) or []
    except Exception as e:  # noqa: BLE001 - network best-effort
        log.warning("ESPN scoreboard %s failed: %s", date_yyyymmdd, e)
        return []


def _event_rosters(event_id: str) -> dict[str, dict]:
    """Per team-name-norm: {'starters': [...names...], 'roster': [...names...]}."""
    try:
        data = _get(f"{_API}/summary?event={event_id}")
    except Exception as e:  # noqa: BLE001
        log.warning("ESPN summary %s failed: %s", event_id, e)
        return {}
    out: dict[str, dict] = {}
    for side in data.get("rosters", []) or []:
        team = _norm((side.get("team") or {}).get("displayName", ""))
        roster, starters = [], []
        for entry in side.get("roster", []) or []:
            name = (entry.get("athlete") or {}).get("displayName") or ""
            if not name:
                continue
            roster.append(name)
            if entry.get("starter"):
                starters.append(name)
        if team:
            out[team] = {"roster": roster, "starters": starters}
    return out


def fetch_lineups(matches: list[dict]) -> dict:
    """Pull lineups for upcoming matches (blocking — call via asyncio.to_thread)."""
    today = time.strftime("%Y-%m-%d")
    horizon = time.strftime("%Y-%m-%d", time.localtime(time.time() + _LOOKAHEAD_DAYS * 86400))
    want = {
        (m["date"], _norm(m["home"]), _norm(m["away"])): int(m["match_id"])
        for m in matches
        if today <= m.get("date", "") <= horizon
    }
    lineups: dict[str, dict] = {}
    for date in sorted({d for d, _, _ in want}):
        for ev in _scoreboard(date.replace("-", "")):
            comp = (ev.get("competitions") or [{}])[0]
            sides = {c.get("homeAway"): _norm((c.get("team") or {}).get("displayName", ""))
                     for c in comp.get("competitors", [])}
            mid = want.get((date, sides.get("home", ""), sides.get("away", "")))
            if mid is None:
                continue
            rosters = _event_rosters(str(ev.get("id")))
            home, away = rosters.get(sides["home"], {}), rosters.get(sides["away"], {})
            n_starters = min(len(home.get("starters", [])), len(away.get("starters", [])))
            status = ("confirmed" if n_starters >= 11
                      else "probable" if home.get("roster") and away.get("roster")
                      else "unknown")
            lineups[str(mid)] = {
                "status": status,
                "home": home, "away": away,
                "kickoff_state": ((ev.get("status") or {}).get("type") or {}).get("state"),
            }
    return {"checked_at": time.time(), "matches": lineups}


def cached_lineups(store: Store) -> dict:
    cached = store.get_setting(_KEY, None)
    return cached if isinstance(cached, dict) else {"checked_at": None, "matches": {}}


async def refresh(store: Store, matches: list[dict]) -> dict:
    import asyncio

    result = await asyncio.to_thread(fetch_lineups, matches)
    store.set_setting(_KEY, result)
    return {"checked": len(result["matches"]), "checked_at": result["checked_at"]}


def _key_absences(names: list[dict], sheet: dict) -> list[str]:
    """Key players (from soccer.json) missing from the posted starters/roster."""
    if not sheet:
        return []
    pool = sheet.get("starters") or sheet.get("roster") or []
    pool_norm = {_norm(n) for n in pool}
    # surname fallback: ESPN sometimes lists short names
    pool_last = {n.split(" ")[-1] for n in pool_norm if n}
    out = []
    for kp in names or []:
        n = _norm(kp.get("name", ""))
        if n and n not in pool_norm and (n.split(" ")[-1] not in pool_last):
            out.append(kp.get("name"))
    return out


def annotate(matches: list[dict], lineups: dict) -> list[dict]:
    """Attach per-match lineup status + the recommendability gate.

    cleared = confirmed with no key absences, or probable with no key absences.
    Selections inherit the match gate (a missing key attacker moves totals and
    result markets alike); per-side absences are surfaced for the UI.
    """
    lu = lineups.get("matches", {})
    out = []
    for m in matches:
        info = lu.get(str(m.get("match_id")), {})
        status = info.get("status", "unknown")
        key_out = {
            "home": _key_absences((m.get("key_players") or {}).get("home"), info.get("home", {})),
            "away": _key_absences((m.get("key_players") or {}).get("away"), info.get("away", {})),
        } if status != "unknown" else {"home": [], "away": []}
        cleared = status in ("confirmed", "probable") and not key_out["home"] and not key_out["away"]
        m = {**m, "lineup": {
            "status": status,
            "cleared": cleared,
            "key_out": key_out,
            "starters_posted": status == "confirmed",
        }}
        out.append(m)
    return out
