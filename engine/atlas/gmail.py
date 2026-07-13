"""Gmail: pull inbox, summarize + classify with Claude, brief, draft, send.

Reads via the Gmail API (requires google connection). Summaries/classification/drafts
go through the local Claude bridge. Sending is explicit: a draft is created 'pending'
and only leaves the machine when the user approves it in the UI (send_draft).
"""

from __future__ import annotations

import base64
import json
import logging
import time
from email.mime.text import MIMEText

from . import google_auth
from .claude_bridge import ClaudeBridge, ClaudeError
from .store import Store

log = logging.getLogger("atlas.gmail")


# --- Pull --------------------------------------------------------------------


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _split_addr(from_val: str) -> tuple[str, str]:
    """'Alice Smith <alice@x.com>' -> ('Alice Smith', 'alice@x.com')."""
    if "<" in from_val and ">" in from_val:
        name = from_val.split("<")[0].strip().strip('"')
        email = from_val.split("<")[1].split(">")[0].strip()
        return name or email, email
    return from_val, from_val


def _decode_bodies(payload: dict) -> tuple[str, str | None]:
    """Walk the MIME tree; return (plain_text, html) — html kept verbatim for as-sent rendering."""
    found = {"plain": None, "html": None}

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain" and found["plain"] is None:
            found["plain"] = base64.urlsafe_b64decode(data).decode("utf-8", "replace")
        elif data and mime == "text/html" and found["html"] is None:
            found["html"] = base64.urlsafe_b64decode(data).decode("utf-8", "replace")
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    plain = found["plain"] or ""
    return plain[:8000], (found["html"][:300000] if found["html"] else None)


def _decode_body(payload: dict) -> str:
    return _decode_bodies(payload)[0]


def gmail_unread_estimate() -> int | None:
    """How many unread inbox messages exist on Gmail (server-side estimate)."""
    try:
        svc = google_auth.service("gmail", "v1")
        r = svc.users().messages().list(userId="me", q="is:unread in:inbox", maxResults=1).execute()
        return int(r.get("resultSizeEstimate", 0))
    except Exception as e:
        log.warning("unread estimate failed: %s", e)
        return None


def sync(store: Store, max_results: int = 100, unread_only: bool = False, skip_known: bool = True) -> dict:
    """Pull recent messages into the local DB (fast, no LLM).

    unread_only → pull only unread mail (used after reindex to grab the next batch).
    skip_known  → don't re-fetch messages already indexed locally.
    """
    svc = google_auth.service("gmail", "v1")
    kwargs = {"userId": "me", "maxResults": max_results}
    if unread_only:
        kwargs["q"] = "is:unread in:inbox"
    else:
        kwargs["labelIds"] = ["INBOX"]
    listing = svc.users().messages().list(**kwargs).execute()
    ids = [m["id"] for m in listing.get("messages", [])]
    known = store.known_gmail_ids() if skip_known else set()
    pulled = 0
    for gid in ids:
        if gid in known:
            continue
        msg = svc.users().messages().get(userId="me", id=gid, format="full").execute()
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])
        name, email = _split_addr(_header(headers, "From"))
        plain, html = _decode_bodies(payload)
        store.upsert_email({
            "gmail_id": gid,
            "thread_id": msg.get("threadId"),
            "sender": name,
            "sender_email": email,
            "subject": _header(headers, "Subject"),
            "snippet": msg.get("snippet", ""),
            "body": plain,
            "body_html": html,
            "received_at": int(msg.get("internalDate", "0")) / 1000.0,
            "is_unread": 1 if "UNREAD" in msg.get("labelIds", []) else 0,
        })
        pulled += 1
    return {"pulled": pulled}


def mark_read(store: Store, email_id: int) -> dict | None:
    """Mark an email read locally AND on Gmail (removes the UNREAD label)."""
    e = store.get_email(email_id)
    if not e:
        return None
    if e["is_unread"]:
        try:
            svc = google_auth.service("gmail", "v1")
            svc.users().messages().modify(userId="me", id=e["gmail_id"], body={"removeLabelIds": ["UNREAD"]}).execute()
        except Exception as ex:
            log.warning("mark_read failed on Gmail for %s: %s", e["gmail_id"], ex)
    return store.update_email(email_id, is_unread=0)


# --- Analyze (Claude) --------------------------------------------------------

CATEGORIES = {"action", "recruiting", "job_listing", "security", "finance", "news", "general"}

_ANALYZE = """Analyze this email. Respond with ONLY a JSON object (no prose, no markdown):
{{"summary": str (1-2 sentences),
  "category": "action"|"recruiting"|"job_listing"|"security"|"finance"|"news"|"general",
  "importance": "high"|"normal",
  "needs_reply": bool,
  "company": str|null, "role": str|null, "recruiter": str|null,
  "event": null OR {{"title": str, "start": "YYYY-MM-DDTHH:MM:SS", "end": "YYYY-MM-DDTHH:MM:SS", "in_person": bool}} }}

Category rules (pick the single best):
- "action": needs YOU to do something concrete, or is a personal obligation — a meeting, an
  appointment (dentist/doctor), a deadline, an RSVP, a bill due.
- "recruiting": a REAL update on YOUR job search — a recruiter about a specific role, an
  application status, interview scheduling, or an offer. NOT generic postings.
- "job_listing": automated job postings/digests from boards (Indeed, LinkedIn Jobs,
  ZipRecruiter, Lensa, Glassdoor) — lists of openings you didn't apply to.
- "security": account/login/security alerts, 2FA, suspicious access.
- "finance": banking, payments, invoices, receipts, brokerage/trading, subscriptions.
- "news": newsletters, product updates, announcements.
- "general": anything else, including marketing/promotions.

"event": fill ONLY when the email states a specific scheduled meeting or in-person appointment
with a concrete date AND time (e.g. "dentist appointment July 8 at 2pm", "let's meet Thursday 3pm").
Assume year {year} if omitted; default a 60-minute duration if no end time. Otherwise null.

Today is {today}.
From: {sender} <{email}>
Subject: {subject}
Body:
{body}"""


async def analyze_unprocessed(store: Store, claude: ClaudeBridge, limit: int = 12) -> dict:
    """Summarize + classify emails without a summary. Returns detected calendar events."""
    import datetime as dt
    today = dt.date.today()
    todo = [e for e in store.list_emails(limit=200) if not e.get("summary")][:limit]
    processed = 0
    events: list[dict] = []
    for e in todo:
        try:
            data = await claude.extract_json(_ANALYZE.format(
                sender=e["sender"], email=e["sender_email"], subject=e["subject"],
                today=today.isoformat(), year=today.year,
                body=(e["body"] or e["snippet"])[:4000],
            ))
        except ClaudeError as ex:
            log.warning("analyze failed for email %s: %s", e["id"], ex)
            continue
        if not isinstance(data, dict):
            continue
        category = data.get("category") if data.get("category") in CATEGORIES else "general"
        store.update_email(
            e["id"], summary=data.get("summary"), category=category,
            needs_reply=1 if data.get("needs_reply") else 0,
            important=1 if data.get("importance") == "high" else 0,
        )
        if category == "recruiting":
            store.add_recruiting({
                "email_id": e["id"], "company": data.get("company"), "role": data.get("role"),
                "recruiter": data.get("recruiter") or e["sender"], "received_at": e["received_at"],
            })
        ev = data.get("event")
        if isinstance(ev, dict) and ev.get("title") and ev.get("start"):
            events.append({**ev, "email_id": e["id"]})
        processed += 1
    return {"processed": processed, "events": events}


_BRIEF = """You are preparing a morning briefing from a batch of {n} recent emails.
Format your response as MARKDOWN using these sections, IN THIS EXACT ORDER. Use a `## ` header
for each section and `- ` bullets under it. OMIT any section that has nothing — do not write
"none". Keep bullets specific and short. Ignore pure marketing/promotions unless notable.

## Action Items
Things that need YOU to personally act — reply, decide, verify, confirm, or a deadline. This
section is the most important; always put it first. Prefix time-sensitive items with "**(urgent)**".

## Security
Account/login/security alerts, suspicious access, password or 2FA notices.

## Personal Finances
Banking, payments, invoices, receipts, trading/brokerage, subscriptions, money movement.

## Job Communications
REAL updates on your job search — recruiter outreach, interviews, applications, offers.

## Job Listings
Automated openings from job boards (Indeed, LinkedIn Jobs, etc.) — group briefly, don't list each.

## News
Newsletters, product updates, announcements, or anything else worth knowing.

Emails:
{digest}"""


async def briefing(store: Store, claude: ClaudeBridge) -> dict:
    # Collapsed view: one digest line per conversation, so chains don't repeat as bullets.
    emails = store.list_emails_collapsed(limit=60)[:40]
    if not emails:
        return {"briefing": "No emails synced yet.", "count": 0}
    def line(e: dict) -> str:
        chain = f" ({e['thread_count']} msgs in thread)" if e.get("thread_count", 1) > 1 else ""
        return f"- [{e['category']}] {e['sender']}: {e['subject']}{chain} — {e.get('summary') or e['snippet']}"

    digest = "\n".join(line(e) for e in emails)
    try:
        text = await claude.ask(_BRIEF.format(n=len(emails), digest=digest[:6000]))
    except ClaudeError as e:
        return {"briefing": f"(Could not generate briefing: {e})", "count": len(emails)}
    store.set_setting("latest_briefing", {"text": text, "at": time.time(), "count": len(emails)})
    path = _save_report(text, len(emails))
    return {"briefing": text, "count": len(emails), "report": path}


def _save_report(text: str, count: int) -> str:
    """Persist each briefing to a timestamped text file for future reference."""
    import datetime as dt

    from .config import APP_SUPPORT

    reports = APP_SUPPORT / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    path = reports / f"briefing_{stamp}.txt"
    header = f"Atlas briefing — {dt.datetime.now().strftime('%A %B %d, %Y %I:%M %p')} — {count} emails\n{'='*60}\n\n"
    path.write_text(header + text)
    return str(path)


def list_reports() -> list[dict]:
    import datetime as dt

    from .config import APP_SUPPORT

    reports = APP_SUPPORT / "reports"
    if not reports.exists():
        return []
    out = []
    for p in sorted(reports.glob("briefing_*.txt"), reverse=True):
        out.append({"name": p.name, "path": str(p), "when": dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat()})
    return out


_DRAFT = """Draft a reply to this email in the user's voice: warm, concise, professional. Respond with
ONLY the reply body text (no subject, no "Dear", no markdown, no signature block).

From: {sender}
Subject: {subject}
Body:
{body}"""


async def draft_reply(store: Store, claude: ClaudeBridge, email_id: int) -> dict:
    e = store.get_email(email_id)
    if not e:
        raise ClaudeError("email not found")
    # One reply per chain: if this thread already has a live draft, hand that back.
    if e.get("thread_id"):
        for d in store.list_drafts(status="pending"):
            other = store.get_email(d["email_id"]) if d.get("email_id") else None
            if other and other.get("thread_id") == e["thread_id"]:
                return d
    body = await claude.ask(_DRAFT.format(sender=e["sender"], subject=e["subject"], body=(e["body"] or e["snippet"])[:4000]))
    subject = e["subject"] or ""
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    return store.add_draft(email_id, e["sender_email"], subject, body.strip())


async def draft_all_needing_reply(store: Store, claude: ClaudeBridge, limit: int = 8) -> dict:
    """One drafted reply PER THREAD (the newest message), never one per chain message."""
    existing_ids = {d["email_id"] for d in store.list_drafts(status=None)}
    drafted_threads = store.threads_with_drafts()
    candidates = [e for e in store.list_emails(limit=100) if e["needs_reply"] and e["id"] not in existing_ids]
    # Collapse to the newest message per thread; drop threads that already have a draft.
    by_thread: dict[str, dict] = {}
    singles: list[dict] = []
    for e in candidates:
        tid = e.get("thread_id")
        if not tid:
            singles.append(e)
            continue
        if tid in drafted_threads:
            store.clear_needs_reply_thread(tid, -1)  # chain already handled — quiet the whole thread
            continue
        cur = by_thread.get(tid)
        if cur is None or (e["received_at"] or 0) > (cur["received_at"] or 0):
            by_thread[tid] = e
    targets = (list(by_thread.values()) + singles)[:limit]
    made = 0
    for e in targets:
        try:
            await draft_reply(store, claude, e["id"])
            if e.get("thread_id"):
                store.clear_needs_reply_thread(e["thread_id"], e["id"])
            made += 1
        except ClaudeError:
            continue
    return {"drafted": made}


# --- Send (only on explicit approval) ----------------------------------------


def send_draft(store: Store, draft_id: int) -> dict:
    """Send an approved draft via Gmail. Called only after the user approves."""
    d = store.get_draft(draft_id)
    if not d:
        raise RuntimeError("draft not found")
    if d["status"] == "sent":
        return d
    if not d.get("to_addr"):
        raise RuntimeError("draft has no recipient")
    svc = google_auth.service("gmail", "v1")
    mime = MIMEText(d["body"])
    mime["To"] = d["to_addr"]
    mime["Subject"] = d["subject"] or ""
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
    payload = {"raw": raw}
    if d.get("email_id"):
        e = store.get_email(d["email_id"])
        if e and e.get("thread_id"):
            payload["threadId"] = e["thread_id"]
    svc.users().messages().send(userId="me", body=payload).execute()
    updated = store.update_draft(draft_id, status="sent", sent_at=time.time())
    if d.get("email_id"):
        store.update_email(d["email_id"], needs_reply=0)
    return updated  # type: ignore[return-value]


# --- Trash review (Claude decides: keep vs genuinely delete) -------------------

_TRASH_REVIEW = """You are the final reviewer of emails the user queued for deletion from their
local index. For EVERY email below, decide whether it should be KEPT or can GENUINELY be
deleted. Your verdicts are executed immediately — keeps are restored to the inbox, deletes
are purged — so judge each one.

KEEP anything with real consequence if lost:
- appointments/meetings, deadlines, RSVPs
- personal correspondence from real people
- job-search updates (recruiters, applications, interviews, offers)
- financial/legal/tax notices, receipts that may be needed for returns or expensing
- security alerts and account-access notices

DELETE the genuinely disposable:
- marketing, promos, sales, newsletters, product announcements
- OTP/verification codes (long expired), automated digests, social notifications
- duplicate chain messages whose newest copy is kept elsewhere

If you are UNSURE, verdict "keep" — a wrong delete is worse than a wrong keep.

Respond with ONLY a JSON object (no prose, no markdown):
{{"verdicts": [{{"id": int, "action": "keep"|"delete", "reason": str (short, <12 words)}}]}}
Include a verdict for every id listed.

Queued for deletion:
{digest}"""


async def review_trash(store: Store, claude: ClaudeBridge) -> dict:
    """Claude issues a keep/delete verdict per queued email and both are executed:
    keeps are restored (marked important), deletes are purged from the local index
    (Gmail itself is never touched). Emails Claude doesn't rule on stay in the pile."""
    pile = store.list_trash()
    if not pile:
        return {"reviewed": 0, "kept": [], "deleted": 0, "unruled": 0}
    valid_ids = {e["id"] for e in pile}
    by_id = {e["id"]: e for e in pile}
    digest = "\n".join(
        f"[{e['id']}] from {e['sender']} — {e['subject']} :: {(e['summary'] or e['snippet'] or '')[:160]}"
        for e in pile
    )
    data = await claude.extract_json(_TRASH_REVIEW.format(digest=digest[:6000]))
    verdicts = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(verdicts, list):
        raise ClaudeError("trash review returned no verdicts")

    kept, delete_ids = [], []
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        try:
            eid = int(v.get("id"))
        except (TypeError, ValueError):
            continue
        if eid not in valid_ids:
            continue  # never act on ids Claude invented
        if v.get("action") == "keep":
            store.set_trashed(eid, False)
            store.update_email(eid, important=1)  # resurface prominently
            kept.append({"id": eid, "subject": by_id[eid]["subject"], "reason": v.get("reason", "")})
        elif v.get("action") == "delete":
            delete_ids.append(eid)
    deleted = store.purge_emails(delete_ids)
    ruled = {k["id"] for k in kept} | set(delete_ids)
    return {"reviewed": len(pile), "kept": kept, "deleted": deleted,
            "unruled": len(valid_ids - ruled)}
