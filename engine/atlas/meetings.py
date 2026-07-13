"""Vein meeting notes, surfaced read-only.

Primary path: open Vein's vein.db read-only (works even when Vein isn't running).
Fallback: if the DB isn't present but a Vein engine is up, hit its HTTP API. Atlas
deliberately does NOT absorb Vein's audio/STT pipeline (~8-10 GB of models).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.request

from .config import VEIN_API, VEIN_DB

log = logging.getLogger("atlas.meetings")


def available() -> dict:
    if VEIN_DB.exists():
        return {"source": "db", "path": str(VEIN_DB)}
    if _http_ok():
        return {"source": "http", "path": VEIN_API}
    return {"source": "none", "path": str(VEIN_DB)}


def _http_ok() -> bool:
    try:
        with urllib.request.urlopen(f"{VEIN_API}/status", timeout=1.5):
            return True
    except Exception:
        return False


def _connect() -> sqlite3.Connection:
    # Read-only URI so we never lock or mutate Vein's DB.
    db = sqlite3.connect(f"file:{VEIN_DB}?mode=ro", uri=True, check_same_thread=False, timeout=3)
    db.row_factory = sqlite3.Row
    return db


def _parse_summary(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"gist": raw}


def list_meetings(limit: int = 100) -> list[dict]:
    if VEIN_DB.exists():
        return _list_from_db(limit)
    if _http_ok():
        return _http_get("/meetings") or []
    return []


def _list_from_db(limit: int) -> list[dict]:
    db = _connect()
    try:
        rows = db.execute(
            """
            SELECT m.*,
                   (SELECT COUNT(*) FROM utterances u WHERE u.meeting_id=m.id) AS n_utterances,
                   (SELECT COUNT(*) FROM action_items a WHERE a.meeting_id=m.id) AS n_todos,
                   (SELECT body FROM summaries s WHERE s.meeting_id=m.id ORDER BY id DESC LIMIT 1) AS summary
            FROM meetings m ORDER BY m.started_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["summary"] = _parse_summary(d.get("summary"))
            d["people"] = [
                p["name"] for p in db.execute(
                    "SELECT name FROM participants WHERE meeting_id=? ORDER BY id", (r["id"],)
                ).fetchall()
            ]
            out.append(d)
        return out
    finally:
        db.close()


def meeting_detail(meeting_id: int) -> dict | None:
    if VEIN_DB.exists():
        return _detail_from_db(meeting_id)
    if _http_ok():
        return _http_get(f"/meetings/{meeting_id}")
    return None


def _detail_from_db(meeting_id: int) -> dict | None:
    db = _connect()
    try:
        m = db.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        if not m:
            return None
        d = dict(m)
        raw = db.execute(
            "SELECT body FROM summaries WHERE meeting_id=? ORDER BY id DESC LIMIT 1", (meeting_id,)
        ).fetchone()
        d["summary"] = _parse_summary(raw["body"] if raw else None)
        d["todos"] = [dict(r) for r in db.execute(
            "SELECT * FROM action_items WHERE meeting_id=? ORDER BY id", (meeting_id,)
        ).fetchall()]
        d["people"] = [r["name"] for r in db.execute(
            "SELECT name FROM participants WHERE meeting_id=? ORDER BY id", (meeting_id,)
        ).fetchall()]
        d["transcript"] = [r["text"] for r in db.execute(
            "SELECT text FROM utterances WHERE meeting_id=? ORDER BY id", (meeting_id,)
        ).fetchall()]
        return d
    finally:
        db.close()


def _http_get(path: str):
    try:
        with urllib.request.urlopen(f"{VEIN_API}{path}", timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning("vein http %s failed: %s", path, e)
        return None
