"""Google Calendar: pull events into the local cache, plus create/update/delete.

Writes go straight to Google (the user's own account via their OAuth) — each write is
a deliberate UI action. `upcoming` powers reminders for things starting soon.
"""

from __future__ import annotations

import datetime as dt
import logging

from . import google_auth
from .store import Store

log = logging.getLogger("atlas.gcal")


def _parse_event(ev: dict) -> dict:
    start = ev.get("start", {})
    end = ev.get("end", {})
    all_day = "date" in start
    return {
        "gcal_id": ev["id"],
        "calendar_id": ev.get("organizer", {}).get("email"),
        "title": ev.get("summary", "(no title)"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "location": ev.get("location"),
        "description": ev.get("description"),
        "all_day": all_day,
    }


def sync(store: Store, days_back: int = 1, days_ahead: int = 30) -> dict:
    """Pull events from ALL the user's visible calendars into the local cache.

    Previously only `primary` was read, which made the calendar look write-only for
    users whose real events live on other calendars (named personal, work, shared,
    subscribed). We now enumerate calendarList and pull every calendar the user has
    selected in Google Calendar (plus primary, always). Cache is replaced wholesale
    so deletions and moves made in Google are reflected too.
    """
    svc = google_auth.service("calendar", "v3")
    now = dt.datetime.now(dt.timezone.utc)
    time_min = (now - dt.timedelta(days=days_back)).isoformat()
    time_max = (now + dt.timedelta(days=days_ahead)).isoformat()

    try:
        cals = svc.calendarList().list(maxResults=50).execute().get("items", [])
    except Exception as e:
        log.warning("calendarList failed (%s) — falling back to primary only", e)
        cals = []
    cal_ids = [c["id"] for c in cals if c.get("selected") or c.get("primary")]
    if not cal_ids:
        cal_ids = ["primary"]

    uniq: dict[str, dict] = {}
    errors = 0
    for cid in cal_ids:
        try:
            result = svc.events().list(
                calendarId=cid, timeMin=time_min, timeMax=time_max,
                singleEvents=True, orderBy="startTime", maxResults=250,
            ).execute()
        except Exception as e:
            log.warning("event pull failed for calendar %s: %s", cid, e)
            errors += 1
            continue
        for ev in result.get("items", []):
            if ev.get("status") == "cancelled":
                continue
            p = _parse_event(ev)
            p["calendar_id"] = cid
            uniq.setdefault(p["gcal_id"], p)  # invites can appear on several calendars
    store.replace_events(list(uniq.values()))
    return {"synced": len(uniq), "calendars": len(cal_ids), "errors": errors}


def upcoming(store: Store, within_min: int = 30) -> list[dict]:
    """Cached events starting within the next `within_min` minutes (for reminders)."""
    now = dt.datetime.now(dt.timezone.utc)
    soon = now + dt.timedelta(minutes=within_min)
    out = []
    for e in store.list_events():
        if e["all_day"] or not e["start"]:
            continue
        try:
            start = dt.datetime.fromisoformat(e["start"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if now <= start <= soon:
            out.append({**e, "minutes_until": int((start - now).total_seconds() / 60)})
    return out


def _localize(iso: str) -> str:
    """Google requires timezone-aware dateTimes; attach the local offset to naive ones."""
    try:
        d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if d.tzinfo is None:
        d = d.astimezone()  # interpret as local time, attach local offset
    return d.isoformat()


def create_event(store: Store, title: str, start: str, end: str, location: str | None, description: str | None) -> dict:
    svc = google_auth.service("calendar", "v3")
    body = {
        "summary": title,
        "start": {"dateTime": _localize(start)},
        "end": {"dateTime": _localize(end)},
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    ev = svc.events().insert(calendarId="primary", body=body).execute()
    parsed = _parse_event(ev)
    store.upsert_event(parsed)
    return parsed


def _calendar_of(store: Store, gcal_id: str) -> str:
    """Which calendar an event lives on (from the cache) — writes must target it."""
    for e in store.list_events():
        if e.get("gcal_id") == gcal_id:
            return e.get("calendar_id") or "primary"
    return "primary"


def update_event(store: Store, gcal_id: str, **fields: str) -> dict:
    svc = google_auth.service("calendar", "v3")
    cal = _calendar_of(store, gcal_id)
    ev = svc.events().get(calendarId=cal, eventId=gcal_id).execute()
    if fields.get("title"):
        ev["summary"] = fields["title"]
    if fields.get("start"):
        ev["start"] = {"dateTime": _localize(fields["start"])}
    if fields.get("end"):
        ev["end"] = {"dateTime": _localize(fields["end"])}
    if fields.get("location") is not None:
        ev["location"] = fields["location"]
    if fields.get("description") is not None:
        ev["description"] = fields["description"]
    updated = svc.events().update(calendarId=cal, eventId=gcal_id, body=ev).execute()
    parsed = _parse_event(updated)
    parsed["calendar_id"] = cal
    store.upsert_event(parsed)
    return parsed


def find_or_create_event(store: Store, title: str, start: str, end: str | None) -> dict:
    """Create an event only if no matching one exists (same day + similar title)."""
    day = (start or "")[:10]
    key = title.lower().strip()[:14]
    for e in store.list_events():
        if e.get("start", "")[:10] == day and key and key in (e.get("title") or "").lower():
            return {"created": False, "event": e}
    if not end:
        try:
            s = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
            end = (s + dt.timedelta(hours=1)).isoformat()
        except ValueError:
            end = start
    ev = create_event(store, title, start, end, None, "Auto-added by Atlas from an email.")
    return {"created": True, "event": ev}


def delete_event(store: Store, gcal_id: str) -> None:
    svc = google_auth.service("calendar", "v3")
    svc.events().delete(calendarId=_calendar_of(store, gcal_id), eventId=gcal_id).execute()
    store.delete_event(gcal_id)
