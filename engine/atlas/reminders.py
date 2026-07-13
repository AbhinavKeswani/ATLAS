"""Local reminders fired via AppleScript (macOS).

The engine runs a background loop that checks for due reminders every 30s and fires
them as a macOS notification, or (if configured) an iMessage to yourself. Scheduling
a "message to myself" = create a reminder with method="imessage" and your handle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time

log = logging.getLogger("atlas.reminders")


def _osascript(script: str) -> bool:
    if sys.platform != "darwin":
        log.info("reminder (non-macOS, skipping osascript): %s", script[:80])
        return False
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
        if r.returncode != 0:
            log.warning("osascript failed: %s", r.stderr.decode("utf-8", "replace")[:200])
            return False
        return True
    except Exception as e:
        log.warning("osascript error: %s", e)
        return False


def _as_str(s: str) -> str:
    # AppleScript string literal: keep unicode literal (ensure_ascii=False) — \uXXXX escapes
    # are NOT valid AppleScript and cause syntax errors.
    return json.dumps(s, ensure_ascii=False)


def fire(reminder: dict) -> bool:
    text = (reminder.get("text") or "").replace("\n", " ").strip()
    method = reminder.get("method", "notification")
    if method == "imessage" and reminder.get("target"):
        # Send an iMessage to yourself (requires Messages.app configured + automation permission).
        script = (
            'tell application "Messages"\n'
            '  set svc to 1st account whose service type = iMessage\n'
            f'  send {_as_str(text)} to participant {_as_str(reminder["target"])} of svc\n'
            "end tell"
        )
        return _osascript(script)
    # Default: a macOS notification banner.
    return _osascript(f'display notification {_as_str(text)} with title "Atlas reminder" sound name "Glass"')


async def scheduler(store, bus) -> None:
    """Background loop: fire due reminders. Started at engine startup."""
    log.info("reminder scheduler started")
    while True:
        try:
            for rem in store.due_reminders(time.time()):
                ok = fire(rem)
                store.mark_reminder_fired(rem["id"])
                bus.publish("reminder_fired", {"id": rem["id"], "text": rem["text"], "ok": ok})
                log.info("fired reminder %s (ok=%s)", rem["id"], ok)
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(30)
