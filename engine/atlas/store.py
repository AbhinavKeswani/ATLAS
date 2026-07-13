"""SQLite persistence for Atlas.

The full schema for every module is created up front (todos, time tracking,
portfolio, accounts, health) so later phases are purely additive — no migrations.
A generic `settings` key/value table holds JSON config (pay config, calorie target,
the persisted Claude session id, HYDRA universe additions, etc.).

Single-writer model copied from Vein's store.py: one connection, we serialize
access ourselves from the asyncio loop.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import DB_PATH, ensure_dirs

_SCHEMA = """
-- ---------- Todos ----------
CREATE TABLE IF NOT EXISTS todos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    notes       TEXT,
    category    TEXT NOT NULL DEFAULT 'Inbox',
    priority    TEXT NOT NULL DEFAULT 'normal',   -- low | normal | high
    due         TEXT,                             -- ISO date or NULL
    done        INTEGER NOT NULL DEFAULT 0,
    sort        REAL NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'manual',   -- manual | claude | image
    created_at  REAL NOT NULL,
    closed_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_todos_done ON todos(done);

-- ---------- Time tracking (paycheck) ----------
CREATE TABLE IF NOT EXISTS time_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    work_date   TEXT NOT NULL,                    -- YYYY-MM-DD
    start       TEXT NOT NULL,                    -- HH:MM (24h)
    end         TEXT NOT NULL,                    -- HH:MM (24h)
    break_min   INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT 'manual',   -- manual | schedule
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_time_date ON time_entries(work_date);

-- ---------- Portfolio ----------
CREATE TABLE IF NOT EXISTS holdings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    qty         REAL NOT NULL,
    cost_basis  REAL,                             -- per-share avg cost
    last_price  REAL,                             -- manual snapshot price (used when HYDRA has no live quote)
    account     TEXT,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,                    -- buy | sell
    qty         REAL NOT NULL,
    price       REAL NOT NULL,
    ts          REAL NOT NULL,                    -- fill time (epoch)
    account     TEXT,
    created_at  REAL NOT NULL
);

-- ---------- Net worth ----------
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    type        TEXT NOT NULL,                    -- cash | savings | debt | brokerage | other
    balance     REAL NOT NULL DEFAULT 0,
    manual      INTEGER NOT NULL DEFAULT 1,       -- 0 => derived (e.g. brokerage)
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS networth_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    total       REAL NOT NULL
);

-- ---------- Health ----------
CREATE TABLE IF NOT EXISTS workouts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    type            TEXT NOT NULL,
    duration_min    INTEGER,
    notes           TEXT,
    calories_burned INTEGER
);
CREATE TABLE IF NOT EXISTS food_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    description TEXT NOT NULL,
    calories    INTEGER,
    protein     REAL,
    carbs       REAL,
    fat         REAL,
    source      TEXT NOT NULL DEFAULT 'manual'    -- manual | image | claude | chat
);
CREATE TABLE IF NOT EXISTS bodyweight (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    weight REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lifts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    exercise TEXT NOT NULL,
    weight   REAL NOT NULL,
    reps     INTEGER,
    notes    TEXT
);
CREATE INDEX IF NOT EXISTS idx_lifts_ex ON lifts(exercise);

-- ---------- Gmail ----------
CREATE TABLE IF NOT EXISTS emails (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_id     TEXT UNIQUE NOT NULL,
    thread_id    TEXT,
    sender       TEXT,
    sender_email TEXT,
    subject      TEXT,
    snippet      TEXT,
    body         TEXT,
    received_at  REAL,
    is_unread    INTEGER NOT NULL DEFAULT 1,
    category     TEXT NOT NULL DEFAULT 'general',   -- action|recruiting|job_listing|security|finance|news|general
    summary      TEXT,
    needs_reply  INTEGER NOT NULL DEFAULT 0,
    starred      INTEGER NOT NULL DEFAULT 0,
    important    INTEGER NOT NULL DEFAULT 0,
    archived     INTEGER NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emails_received ON emails(received_at);
CREATE INDEX IF NOT EXISTS idx_emails_category ON emails(category);

CREATE TABLE IF NOT EXISTS email_drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id    INTEGER REFERENCES emails(id),
    to_addr     TEXT,
    subject     TEXT,
    body        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',    -- pending | approved | sent | discarded
    created_at  REAL NOT NULL,
    sent_at     REAL
);

-- Job / recruiting tracking (separate from general mail).
CREATE TABLE IF NOT EXISTS recruiting (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id    INTEGER REFERENCES emails(id),
    company     TEXT,
    role        TEXT,
    recruiter   TEXT,
    stage       TEXT NOT NULL DEFAULT 'inbound',    -- inbound | screen | interview | offer | rejected | archived
    notes       TEXT,
    received_at REAL,
    created_at  REAL NOT NULL
);

-- ---------- Calendar (cache of pulled events) ----------
CREATE TABLE IF NOT EXISTS calendar_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    gcal_id     TEXT UNIQUE NOT NULL,
    calendar_id TEXT,
    title       TEXT,
    start       TEXT,                               -- ISO datetime or date
    end         TEXT,
    location    TEXT,
    description TEXT,
    all_day     INTEGER NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cal_start ON calendar_events(start);

-- ---------- Reminders (fired via AppleScript by the engine scheduler) ----------
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    remind_at   REAL NOT NULL,
    method      TEXT NOT NULL DEFAULT 'notification',  -- notification | imessage
    target      TEXT,                                   -- iMessage handle (self)
    email_id    INTEGER REFERENCES emails(id),
    status      TEXT NOT NULL DEFAULT 'pending',        -- pending | fired | cancelled
    created_at  REAL NOT NULL,
    fired_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, remind_at);

-- ---------- Weekly hours (payroll-style overrides: explicit regular + overtime) ----------
CREATE TABLE IF NOT EXISTS hours_week (
    week_start  TEXT PRIMARY KEY,                       -- ISO date of the week's Sunday
    regular     REAL NOT NULL DEFAULT 0,
    overtime    REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL
);

-- ---------- Daily adjusted-close cache (universe + benchmarks) ----------
CREATE TABLE IF NOT EXISTS price_history (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,                          -- YYYY-MM-DD
    close  REAL NOT NULL,                          -- split/dividend-adjusted close
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_price_symbol ON price_history(symbol);

-- ---------- Resume docs (saved/historical LaTeX resumes) ----------
CREATE TABLE IF NOT EXISTS resume_docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT NOT NULL,                    -- swe | quant | finance
    variant     TEXT,                             -- data-analyst | research-pipelines | NULL
    label       TEXT NOT NULL,                    -- company/role the resume was built for
    latex       TEXT NOT NULL,
    pdf_path    TEXT,                             -- last compiled PDF (under RESUME_DIR)
    base        INTEGER NOT NULL DEFAULT 0,       -- 1 => canonical base template for its category
    parent_id   INTEGER,                          -- lineage: doc this was derived from
    meta        TEXT,                             -- JSON: job desc, fit diagnostics, notes
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resume_cat ON resume_docs(category);

-- ---------- Resume entries (reusable per-job bullet bank) ----------
CREATE TABLE IF NOT EXISTS resume_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    company     TEXT,
    role        TEXT,
    location    TEXT,
    dates       TEXT,
    category    TEXT,
    latex       TEXT NOT NULL,                    -- fitted bullets (LaTeX itemize body)
    raw_contrib TEXT,                             -- the raw description it was distilled from
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);

-- ---------- Generic settings (JSON values) ----------
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        ensure_dirs()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL;")
        self._db.execute("PRAGMA synchronous=NORMAL;")
        self._db.executescript(_SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        """Additive column migrations for DBs created before a column existed."""
        hcols = {r["name"] for r in self._db.execute("PRAGMA table_info(holdings)")}
        if "last_price" not in hcols:
            self._db.execute("ALTER TABLE holdings ADD COLUMN last_price REAL")
        ecols = {r["name"] for r in self._db.execute("PRAGMA table_info(emails)")}
        for col in ("starred", "important", "archived", "trashed"):
            if col not in ecols:
                self._db.execute(f"ALTER TABLE emails ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
        if "body_html" not in ecols:
            self._db.execute("ALTER TABLE emails ADD COLUMN body_html TEXT")

    # --- Settings (JSON kv) --------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self._db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self._db.commit()

    # --- Price history (daily adjusted close cache) --------------------------

    def upsert_prices(self, symbol: str, rows: list[dict[str, Any]]) -> int:
        """Insert/replace daily closes for a symbol. rows: [{date, close}]. Returns count."""
        symbol = symbol.upper()
        payload = [(symbol, r["date"], float(r["close"])) for r in rows if r.get("date") and r.get("close") is not None]
        if not payload:
            return 0
        self._db.executemany(
            "INSERT INTO price_history(symbol, date, close) VALUES(?, ?, ?) "
            "ON CONFLICT(symbol, date) DO UPDATE SET close=excluded.close",
            payload,
        )
        self._db.commit()
        return len(payload)

    def last_price_date(self, symbol: str) -> str | None:
        row = self._db.execute(
            "SELECT MAX(date) AS d FROM price_history WHERE symbol=?", (symbol.upper(),)
        ).fetchone()
        return row["d"] if row and row["d"] else None

    def get_price_series(self, symbol: str, since: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Daily closes for a symbol, oldest→newest, optionally from `since` (YYYY-MM-DD).

        `limit` returns only the most recent N rows (still oldest→newest) — the cheap
        path for day-over-day math across many symbols (market breadth).
        """
        if limit:
            cur = self._db.execute(
                "SELECT date, close FROM price_history WHERE symbol=? ORDER BY date DESC LIMIT ?",
                (symbol.upper(), int(limit)),
            )
            return [{"date": r["date"], "close": r["close"]} for r in reversed(cur.fetchall())]
        if since:
            cur = self._db.execute(
                "SELECT date, close FROM price_history WHERE symbol=? AND date>=? ORDER BY date",
                (symbol.upper(), since),
            )
        else:
            cur = self._db.execute(
                "SELECT date, close FROM price_history WHERE symbol=? ORDER BY date", (symbol.upper(),)
            )
        return [{"date": r["date"], "close": r["close"]} for r in cur.fetchall()]

    def priced_symbols(self) -> set[str]:
        cur = self._db.execute("SELECT DISTINCT symbol FROM price_history")
        return {r["symbol"] for r in cur.fetchall()}

    # --- Todos ---------------------------------------------------------------

    def add_todo(
        self,
        title: str,
        notes: str | None = None,
        category: str = "Inbox",
        priority: str = "normal",
        due: str | None = None,
        source: str = "manual",
    ) -> dict:
        now = time.time()
        cur = self._db.execute(
            "INSERT INTO todos(title, notes, category, priority, due, sort, source, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (title, notes, category, priority, due, now, source, now),
        )
        self._db.commit()
        return self.get_todo(int(cur.lastrowid))  # type: ignore[return-value]

    def get_todo(self, todo_id: int) -> dict | None:
        row = self._db.execute("SELECT * FROM todos WHERE id=?", (todo_id,)).fetchone()
        return dict(row) if row else None

    def list_todos(self, include_done: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM todos"
        if not include_done:
            sql += " WHERE done=0"
        sql += " ORDER BY done, sort, id"
        return [dict(r) for r in self._db.execute(sql).fetchall()]

    def update_todo(self, todo_id: int, **fields: Any) -> dict | None:
        allowed = {"title", "notes", "category", "priority", "due", "done", "sort"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "done" in sets:
            self._db.execute(
                "UPDATE todos SET closed_at=? WHERE id=?",
                (time.time() if sets["done"] else None, todo_id),
            )
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            self._db.execute(f"UPDATE todos SET {cols} WHERE id=?", (*sets.values(), todo_id))
        self._db.commit()
        return self.get_todo(todo_id)

    def delete_todo(self, todo_id: int) -> bool:
        cur = self._db.execute("DELETE FROM todos WHERE id=?", (todo_id,))
        self._db.commit()
        return cur.rowcount > 0

    # --- Time entries --------------------------------------------------------

    def add_time_entry(
        self, work_date: str, start: str, end: str, break_min: int = 0, source: str = "manual"
    ) -> dict:
        cur = self._db.execute(
            "INSERT INTO time_entries(work_date, start, end, break_min, source, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (work_date, start, end, break_min, source, time.time()),
        )
        self._db.commit()
        return self.get_time_entry(int(cur.lastrowid))  # type: ignore[return-value]

    def get_time_entry(self, entry_id: int) -> dict | None:
        row = self._db.execute("SELECT * FROM time_entries WHERE id=?", (entry_id,)).fetchone()
        return dict(row) if row else None

    def list_time_entries(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM time_entries WHERE work_date >= ? AND work_date <= ? "
            "ORDER BY work_date, start",
            (start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]

    def has_time_entry(self, work_date: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM time_entries WHERE work_date=? LIMIT 1", (work_date,)
        ).fetchone()
        return row is not None

    def delete_time_entry(self, entry_id: int) -> bool:
        cur = self._db.execute("DELETE FROM time_entries WHERE id=?", (entry_id,))
        self._db.commit()
        return cur.rowcount > 0

    # --- Holdings / orders ---------------------------------------------------

    def list_holdings(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._db.execute("SELECT * FROM holdings ORDER BY symbol")]

    def upsert_holding(
        self, symbol: str, qty: float, cost_basis: float | None, account: str | None,
        last_price: float | None = None,
    ) -> dict:
        symbol = symbol.upper().strip()
        existing = self._db.execute(
            "SELECT id FROM holdings WHERE symbol=? AND IFNULL(account,'')=IFNULL(?,'')",
            (symbol, account),
        ).fetchone()
        now = time.time()
        if existing:
            self._db.execute(
                "UPDATE holdings SET qty=?, cost_basis=?, last_price=COALESCE(?,last_price), updated_at=? WHERE id=?",
                (qty, cost_basis, last_price, now, existing["id"]),
            )
            hid = existing["id"]
        else:
            cur = self._db.execute(
                "INSERT INTO holdings(symbol, qty, cost_basis, last_price, account, updated_at) VALUES (?,?,?,?,?,?)",
                (symbol, qty, cost_basis, last_price, account, now),
            )
            hid = int(cur.lastrowid)
        self._db.commit()
        return dict(self._db.execute("SELECT * FROM holdings WHERE id=?", (hid,)).fetchone())

    def delete_holding(self, holding_id: int) -> bool:
        cur = self._db.execute("DELETE FROM holdings WHERE id=?", (holding_id,))
        self._db.commit()
        return cur.rowcount > 0

    def add_order(
        self, symbol: str, side: str, qty: float, price: float, ts: float, account: str | None
    ) -> dict:
        cur = self._db.execute(
            "INSERT INTO orders(symbol, side, qty, price, ts, account, created_at) VALUES (?,?,?,?,?,?,?)",
            (symbol.upper().strip(), side.lower(), qty, price, ts, account, time.time()),
        )
        self._db.commit()
        return dict(self._db.execute("SELECT * FROM orders WHERE id=?", (cur.lastrowid,)).fetchone())

    def list_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT * FROM orders ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def rebuild_holdings_from_orders(self) -> None:
        """Collapse all orders into net positions with weighted-avg cost basis."""
        agg: dict[tuple[str, str], dict] = {}
        for o in self._db.execute("SELECT * FROM orders ORDER BY ts"):
            key = (o["symbol"], o["account"] or "")
            a = agg.setdefault(key, {"qty": 0.0, "cost": 0.0})
            signed = o["qty"] if o["side"] == "buy" else -o["qty"]
            if o["side"] == "buy":
                a["cost"] += o["qty"] * o["price"]
            a["qty"] += signed
        # Replace all holdings with the order-derived net positions.
        self._db.execute("DELETE FROM holdings")
        now = time.time()
        for (symbol, account), a in agg.items():
            if abs(a["qty"]) < 1e-9:
                continue
            avg = a["cost"] / a["qty"] if a["qty"] > 0 else None
            self._db.execute(
                "INSERT INTO holdings(symbol, qty, cost_basis, account, updated_at) VALUES (?,?,?,?,?)",
                (symbol, a["qty"], avg, account or None, now),
            )
        self._db.commit()

    # --- Accounts / net worth ------------------------------------------------

    def list_accounts(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._db.execute("SELECT * FROM accounts ORDER BY type, name")]

    def add_account(self, name: str, type: str, balance: float, manual: bool = True) -> dict:
        cur = self._db.execute(
            "INSERT INTO accounts(name, type, balance, manual, updated_at) VALUES (?,?,?,?,?)",
            (name, type, balance, 1 if manual else 0, time.time()),
        )
        self._db.commit()
        return dict(self._db.execute("SELECT * FROM accounts WHERE id=?", (cur.lastrowid,)).fetchone())

    def update_account(self, account_id: int, **fields: Any) -> dict | None:
        allowed = {"name", "type", "balance"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            self._db.execute(
                f"UPDATE accounts SET {cols}, updated_at=? WHERE id=?",
                (*sets.values(), time.time(), account_id),
            )
            self._db.commit()
        row = self._db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return dict(row) if row else None

    def delete_account(self, account_id: int) -> bool:
        cur = self._db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
        self._db.commit()
        return cur.rowcount > 0

    def add_snapshot(self, total: float) -> None:
        self._db.execute(
            "INSERT INTO networth_snapshots(ts, total) VALUES (?,?)", (time.time(), total)
        )
        self._db.commit()

    def snapshots(self, limit: int = 180) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT ts, total FROM networth_snapshots ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # --- Health --------------------------------------------------------------

    def add_workout(
        self, type: str, duration_min: int | None, notes: str | None, calories_burned: int | None
    ) -> dict:
        cur = self._db.execute(
            "INSERT INTO workouts(ts, type, duration_min, notes, calories_burned) VALUES (?,?,?,?,?)",
            (time.time(), type, duration_min, notes, calories_burned),
        )
        self._db.commit()
        return dict(self._db.execute("SELECT * FROM workouts WHERE id=?", (cur.lastrowid,)).fetchone())

    def list_workouts(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT * FROM workouts ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def delete_workout(self, workout_id: int) -> bool:
        cur = self._db.execute("DELETE FROM workouts WHERE id=?", (workout_id,))
        self._db.commit()
        return cur.rowcount > 0

    def add_food(
        self,
        description: str,
        calories: int | None,
        protein: float | None,
        carbs: float | None,
        fat: float | None,
        source: str = "manual",
        ts: float | None = None,
    ) -> dict:
        cur = self._db.execute(
            "INSERT INTO food_log(ts, description, calories, protein, carbs, fat, source) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts or time.time(), description, calories, protein, carbs, fat, source),
        )
        self._db.commit()
        return dict(self._db.execute("SELECT * FROM food_log WHERE id=?", (cur.lastrowid,)).fetchone())

    def list_food(self, since_ts: float) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM food_log WHERE ts >= ? ORDER BY ts DESC", (since_ts,)
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_food(self, food_id: int) -> bool:
        cur = self._db.execute("DELETE FROM food_log WHERE id=?", (food_id,))
        self._db.commit()
        return cur.rowcount > 0

    # --- Bodyweight ----------------------------------------------------------

    def add_bodyweight(self, weight: float, ts: float | None = None) -> dict:
        cur = self._db.execute(
            "INSERT INTO bodyweight(ts, weight) VALUES (?,?)", (ts or time.time(), weight)
        )
        self._db.commit()
        return dict(self._db.execute("SELECT * FROM bodyweight WHERE id=?", (cur.lastrowid,)).fetchone())

    def list_bodyweight(self, limit: int = 180) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT ts, weight FROM bodyweight ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # --- Lifts / strength ----------------------------------------------------

    # Canonical names so "bench", "Bench", and "Bench Press" all track as one exercise.
    _LIFT_ALIASES = {
        "bench": "Bench Press", "bench press": "Bench Press", "bp": "Bench Press",
        "flat bench": "Bench Press",
        "incline bench": "Incline Bench Press", "incline bench press": "Incline Bench Press",
        "ohp": "Overhead Press", "overhead press": "Overhead Press",
        "shoulder press": "Overhead Press", "military press": "Overhead Press",
        "squat": "Squat", "back squat": "Squat", "front squat": "Front Squat",
        "deadlift": "Deadlift", "dl": "Deadlift",
        "rdl": "Romanian Deadlift", "romanian deadlift": "Romanian Deadlift",
        "row": "Barbell Row", "barbell row": "Barbell Row", "bent over row": "Barbell Row",
        "pullup": "Pull-Up", "pull up": "Pull-Up", "pull-up": "Pull-Up",
    }

    @classmethod
    def canonical_exercise(cls, name: str) -> str:
        name = name.strip()
        return cls._LIFT_ALIASES.get(name.lower(), name)

    def add_lift(self, exercise: str, weight: float, reps: int | None, notes: str | None, ts: float | None = None) -> dict:
        cur = self._db.execute(
            "INSERT INTO lifts(ts, exercise, weight, reps, notes) VALUES (?,?,?,?,?)",
            (ts or time.time(), self.canonical_exercise(exercise), weight, reps, notes),
        )
        self._db.commit()
        return dict(self._db.execute("SELECT * FROM lifts WHERE id=?", (cur.lastrowid,)).fetchone())

    def canonicalize_lifts(self) -> int:
        """One-time cleanup: re-map existing rows through the alias table."""
        n = 0
        for r in self._db.execute("SELECT id, exercise FROM lifts").fetchall():
            canon = self.canonical_exercise(r["exercise"])
            if canon != r["exercise"]:
                self._db.execute("UPDATE lifts SET exercise=? WHERE id=?", (canon, r["id"]))
                n += 1
        self._db.commit()
        return n

    def list_lifts(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self._db.execute("SELECT * FROM lifts ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def delete_lift(self, lift_id: int) -> bool:
        cur = self._db.execute("DELETE FROM lifts WHERE id=?", (lift_id,))
        self._db.commit()
        return cur.rowcount > 0

    # --- Gmail ---------------------------------------------------------------

    def upsert_email(self, e: dict) -> dict:
        """Insert an email by gmail_id, or update mutable fields if it exists."""
        row = self._db.execute("SELECT id FROM emails WHERE gmail_id=?", (e["gmail_id"],)).fetchone()
        if row:
            self._db.execute(
                "UPDATE emails SET is_unread=?, category=COALESCE(?,category), summary=COALESCE(?,summary), "
                "needs_reply=COALESCE(?,needs_reply), body=COALESCE(?,body), body_html=COALESCE(?,body_html) WHERE id=?",
                (e.get("is_unread", 1), e.get("category"), e.get("summary"), e.get("needs_reply"),
                 e.get("body"), e.get("body_html"), row["id"]),
            )
            eid = row["id"]
        else:
            cur = self._db.execute(
                "INSERT INTO emails(gmail_id, thread_id, sender, sender_email, subject, snippet, body, body_html, "
                "received_at, is_unread, category, summary, needs_reply, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (e["gmail_id"], e.get("thread_id"), e.get("sender"), e.get("sender_email"), e.get("subject"),
                 e.get("snippet"), e.get("body"), e.get("body_html"), e.get("received_at"), e.get("is_unread", 1),
                 e.get("category", "general"), e.get("summary"), e.get("needs_reply", 0), time.time()),
            )
            eid = int(cur.lastrowid)
        self._db.commit()
        return dict(self._db.execute("SELECT * FROM emails WHERE id=?", (eid,)).fetchone())

    def get_email(self, email_id: int) -> dict | None:
        row = self._db.execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
        return dict(row) if row else None

    def threads_with_drafts(self) -> set[str]:
        """thread_ids that already have ANY draft (pending/approved/sent) — one reply per chain."""
        rows = self._db.execute(
            "SELECT DISTINCT e.thread_id FROM email_drafts d JOIN emails e ON e.id=d.email_id "
            "WHERE e.thread_id IS NOT NULL AND d.status != 'discarded'"
        ).fetchall()
        return {r["thread_id"] for r in rows}

    def clear_needs_reply_thread(self, thread_id: str, except_id: int) -> None:
        """Older messages in a chain shouldn't each demand a reply — keep only the newest."""
        self._db.execute(
            "UPDATE emails SET needs_reply=0 WHERE thread_id=? AND id != ?", (thread_id, except_id)
        )
        self._db.commit()

    def list_emails(self, category: str | None = None, limit: int = 100, include_archived: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM emails WHERE trashed=0"
        params: list[Any] = []
        if category:
            sql += " AND category=?"; params.append(category)
        if not include_archived:
            sql += " AND archived=0"
        sql += " ORDER BY received_at DESC LIMIT ?"; params.append(limit)
        return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def list_emails_collapsed(self, limit: int = 150) -> list[dict[str, Any]]:
        """Chain-collapsed inbox: newest message per thread wins, annotated with
        thread_count; a chain is unread/important/needs_reply if ANY message is.
        This is THE canonical email listing — every consumer (inbox, stats, Home)
        must count conversations through here so numbers agree everywhere."""
        out: list[dict[str, Any]] = []
        seen: dict[str, dict] = {}
        for e in self.list_emails(limit=limit):  # newest-first
            tid = e.get("thread_id")
            if not tid:
                out.append({**e, "thread_count": 1})
                continue
            if tid in seen:
                agg = seen[tid]
                agg["thread_count"] += 1
                agg["needs_reply"] = agg["needs_reply"] or e["needs_reply"]
                agg["important"] = agg["important"] or e["important"]
                agg["is_unread"] = agg["is_unread"] or e["is_unread"]
                agg["starred"] = agg["starred"] or e["starred"]
            else:
                agg = {**e, "thread_count": 1}
                seen[tid] = agg
                out.append(agg)
        return out

    def email_stats(self) -> dict:
        """Reading-stats counters for the dashboard widgets.

        Conversation-level counts come from the collapsed view so they match what
        the Inbox shows; awaiting_analysis and trash stay per-message because they
        drive per-message work queues (analyze batches, purge)."""
        def n(where: str) -> int:
            return int(self._db.execute(f"SELECT COUNT(*) FROM emails WHERE {where}").fetchone()[0] or 0)

        chains = self.list_emails_collapsed(limit=500)
        return {
            "indexed": len(chains),
            "analyzed": n("trashed=0 AND summary IS NOT NULL"),
            "awaiting_analysis": n("trashed=0 AND summary IS NULL"),
            "unread": sum(1 for c in chains if c["is_unread"]),
            "important": sum(1 for c in chains if c["important"]),
            "starred": sum(1 for c in chains if c["starred"]),
            "needs_reply": sum(1 for c in chains if c["needs_reply"] and not c["archived"]),
            "trash": n("trashed=1"),
        }

    # --- Trash pile ------------------------------------------------------------

    def list_trash(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM emails WHERE trashed=1 ORDER BY received_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_trashed(self, email_id: int, trashed: bool) -> dict | None:
        self._db.execute("UPDATE emails SET trashed=? WHERE id=?", (1 if trashed else 0, email_id))
        self._db.commit()
        return self.get_email(email_id)

    def purge_emails(self, ids: list[int]) -> int:
        """Permanently drop specific emails from the local index (Gmail is untouched)."""
        if not ids:
            return 0
        ph = ",".join("?" * len(ids))
        self._db.execute(f"DELETE FROM email_drafts WHERE email_id IN ({ph})", ids)
        self._db.execute(f"DELETE FROM recruiting WHERE email_id IN ({ph})", ids)
        self._db.execute(f"DELETE FROM emails WHERE id IN ({ph})", ids)
        self._db.commit()
        return len(ids)

    def purge_trash(self) -> int:
        """Permanently drop ALL trashed emails from the local index (Gmail is untouched)."""
        ids = [r["id"] for r in self._db.execute("SELECT id FROM emails WHERE trashed=1")]
        return self.purge_emails(ids)

    def email_has_reminder(self, email_id: int) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM reminders WHERE email_id=? AND status='pending' LIMIT 1", (email_id,)
        ).fetchone()
        return row is not None

    def update_email(self, email_id: int, **fields: Any) -> dict | None:
        allowed = {"category", "summary", "needs_reply", "is_unread", "starred", "important", "archived"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            self._db.execute(f"UPDATE emails SET {cols} WHERE id=?", (*sets.values(), email_id))
            self._db.commit()
        return self.get_email(email_id)

    def emails_since(self, since_ts: float) -> int:
        r = self._db.execute("SELECT COUNT(*) FROM emails WHERE received_at >= ?", (since_ts,)).fetchone()
        return int(r[0] or 0)

    # --- Drafts --------------------------------------------------------------

    def add_draft(self, email_id: int | None, to_addr: str | None, subject: str | None, body: str) -> dict:
        cur = self._db.execute(
            "INSERT INTO email_drafts(email_id, to_addr, subject, body, status, created_at) "
            "VALUES (?,?,?,?, 'pending', ?)",
            (email_id, to_addr, subject, body, time.time()),
        )
        self._db.commit()
        return self.get_draft(int(cur.lastrowid))  # type: ignore[return-value]

    def get_draft(self, draft_id: int) -> dict | None:
        row = self._db.execute("SELECT * FROM email_drafts WHERE id=?", (draft_id,)).fetchone()
        return dict(row) if row else None

    def list_drafts(self, status: str | None = "pending") -> list[dict[str, Any]]:
        if status:
            rows = self._db.execute(
                "SELECT d.*, e.subject AS email_subject, e.sender AS email_sender, e.snippet AS email_snippet, "
                "e.body AS email_body, e.summary AS email_summary "
                "FROM email_drafts d LEFT JOIN emails e ON e.id=d.email_id "
                "WHERE d.status=? ORDER BY d.created_at DESC", (status,)
            ).fetchall()
        else:
            rows = self._db.execute("SELECT * FROM email_drafts ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def update_draft(self, draft_id: int, **fields: Any) -> dict | None:
        allowed = {"to_addr", "subject", "body", "status", "sent_at"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            self._db.execute(f"UPDATE email_drafts SET {cols} WHERE id=?", (*sets.values(), draft_id))
            self._db.commit()
        return self.get_draft(draft_id)

    # --- Recruiting ----------------------------------------------------------

    def add_recruiting(self, e: dict) -> dict:
        existing = self._db.execute(
            "SELECT id FROM recruiting WHERE email_id=?", (e.get("email_id"),)
        ).fetchone() if e.get("email_id") else None
        if existing:
            return dict(self._db.execute("SELECT * FROM recruiting WHERE id=?", (existing["id"],)).fetchone())
        cur = self._db.execute(
            "INSERT INTO recruiting(email_id, company, role, recruiter, stage, notes, received_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (e.get("email_id"), e.get("company"), e.get("role"), e.get("recruiter"),
             e.get("stage", "inbound"), e.get("notes"), e.get("received_at"), time.time()),
        )
        self._db.commit()
        return dict(self._db.execute("SELECT * FROM recruiting WHERE id=?", (cur.lastrowid,)).fetchone())

    def list_recruiting(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._db.execute("SELECT * FROM recruiting ORDER BY received_at DESC")]

    def update_recruiting(self, rec_id: int, **fields: Any) -> dict | None:
        allowed = {"company", "role", "recruiter", "stage", "notes"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            self._db.execute(f"UPDATE recruiting SET {cols} WHERE id=?", (*sets.values(), rec_id))
            self._db.commit()
        row = self._db.execute("SELECT * FROM recruiting WHERE id=?", (rec_id,)).fetchone()
        return dict(row) if row else None

    # --- Calendar cache ------------------------------------------------------

    def upsert_event(self, ev: dict) -> None:
        self._db.execute(
            "INSERT INTO calendar_events(gcal_id, calendar_id, title, start, end, location, description, all_day, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(gcal_id) DO UPDATE SET title=excluded.title, start=excluded.start, end=excluded.end, "
            "location=excluded.location, description=excluded.description, all_day=excluded.all_day, updated_at=excluded.updated_at",
            (ev["gcal_id"], ev.get("calendar_id"), ev.get("title"), ev.get("start"), ev.get("end"),
             ev.get("location"), ev.get("description"), 1 if ev.get("all_day") else 0, time.time()),
        )
        self._db.commit()

    def replace_events(self, events: list[dict]) -> None:
        """Refresh the local cache to exactly the pulled set (drops stale/deleted)."""
        self._db.execute("DELETE FROM calendar_events")
        for ev in events:
            self.upsert_event(ev)

    def list_events(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._db.execute("SELECT * FROM calendar_events ORDER BY start")]

    def delete_event(self, gcal_id: str) -> None:
        self._db.execute("DELETE FROM calendar_events WHERE gcal_id=?", (gcal_id,))
        self._db.commit()

    def clear_inbox(self) -> None:
        """Wipe the local email index (emails + derived drafts/recruiting) for reindex."""
        for t in ("email_drafts", "recruiting", "emails"):
            self._db.execute(f"DELETE FROM {t}")
        self._db.commit()

    def known_gmail_ids(self) -> set[str]:
        return {r["gmail_id"] for r in self._db.execute("SELECT gmail_id FROM emails")}

    # --- Reminders -----------------------------------------------------------

    def add_reminder(self, text: str, remind_at: float, method: str, target: str | None, email_id: int | None) -> dict:
        cur = self._db.execute(
            "INSERT INTO reminders(text, remind_at, method, target, email_id, status, created_at) "
            "VALUES (?,?,?,?,?, 'pending', ?)",
            (text, remind_at, method, target, email_id, time.time()),
        )
        self._db.commit()
        return dict(self._db.execute("SELECT * FROM reminders WHERE id=?", (cur.lastrowid,)).fetchone())

    def due_reminders(self, now: float) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM reminders WHERE status='pending' AND remind_at <= ?", (now,)
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_reminder_fired(self, rid: int) -> None:
        self._db.execute(
            "UPDATE reminders SET status='fired', fired_at=? WHERE id=?", (time.time(), rid)
        )
        self._db.commit()

    def list_reminders(self, status: str = "pending") -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM reminders WHERE status=? ORDER BY remind_at", (status,)
        ).fetchall()
        return [dict(r) for r in rows]

    def cancel_reminder(self, rid: int) -> bool:
        cur = self._db.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND status='pending'", (rid,))
        self._db.commit()
        return cur.rowcount > 0

    # --- Weekly hours overrides ----------------------------------------------

    def upsert_hours_week(self, week_start: str, regular: float, overtime: float) -> dict:
        self._db.execute(
            "INSERT INTO hours_week(week_start, regular, overtime, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(week_start) DO UPDATE SET regular=excluded.regular, overtime=excluded.overtime, updated_at=excluded.updated_at",
            (week_start, regular, overtime, time.time()),
        )
        self._db.commit()
        return dict(self._db.execute("SELECT * FROM hours_week WHERE week_start=?", (week_start,)).fetchone())

    def hours_weeks(self) -> dict[str, dict]:
        return {r["week_start"]: dict(r) for r in self._db.execute("SELECT * FROM hours_week")}

    def delete_hours_week(self, week_start: str) -> bool:
        cur = self._db.execute("DELETE FROM hours_week WHERE week_start=?", (week_start,))
        self._db.commit()
        return cur.rowcount > 0

    # --- Resume docs ---------------------------------------------------------

    def add_resume_doc(
        self,
        category: str,
        label: str,
        latex: str,
        variant: str | None = None,
        base: bool = False,
        parent_id: int | None = None,
        meta: dict | None = None,
        pdf_path: str | None = None,
    ) -> dict:
        now = time.time()
        cur = self._db.execute(
            "INSERT INTO resume_docs(category, variant, label, latex, pdf_path, base, parent_id, meta, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (category, variant, label, latex, pdf_path, 1 if base else 0, parent_id,
             json.dumps(meta) if meta is not None else None, now, now),
        )
        self._db.commit()
        return self.get_resume_doc(int(cur.lastrowid))  # type: ignore[return-value]

    def get_resume_doc(self, doc_id: int) -> dict | None:
        row = self._db.execute("SELECT * FROM resume_docs WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
        return d

    def list_resume_docs(self, category: str | None = None) -> list[dict[str, Any]]:
        if category:
            rows = self._db.execute(
                "SELECT * FROM resume_docs WHERE category=? ORDER BY base DESC, updated_at DESC",
                (category,),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM resume_docs ORDER BY category, base DESC, updated_at DESC"
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
            out.append(d)
        return out

    def base_resume_doc(self, category: str) -> dict | None:
        row = self._db.execute(
            "SELECT * FROM resume_docs WHERE category=? AND base=1 ORDER BY updated_at DESC LIMIT 1",
            (category,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
        return d

    def update_resume_doc(self, doc_id: int, **fields: Any) -> dict | None:
        allowed = {"category", "variant", "label", "latex", "pdf_path", "base", "parent_id", "meta"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "meta" in sets and not isinstance(sets["meta"], str):
            sets["meta"] = json.dumps(sets["meta"])
        if "base" in sets:
            sets["base"] = 1 if sets["base"] else 0
        if sets:
            cols = ", ".join(f"{k}=?" for k in sets)
            self._db.execute(
                f"UPDATE resume_docs SET {cols}, updated_at=? WHERE id=?",
                (*sets.values(), time.time(), doc_id),
            )
            self._db.commit()
        return self.get_resume_doc(doc_id)

    def set_base_resume_doc(self, doc_id: int) -> dict | None:
        """Mark one doc as its category's base, clearing the flag on its siblings."""
        doc = self.get_resume_doc(doc_id)
        if not doc:
            return None
        self._db.execute("UPDATE resume_docs SET base=0 WHERE category=?", (doc["category"],))
        self._db.execute("UPDATE resume_docs SET base=1, updated_at=? WHERE id=?", (time.time(), doc_id))
        self._db.commit()
        return self.get_resume_doc(doc_id)

    def delete_resume_doc(self, doc_id: int) -> bool:
        cur = self._db.execute("DELETE FROM resume_docs WHERE id=?", (doc_id,))
        self._db.commit()
        return cur.rowcount > 0

    # --- Resume entries (reusable bullet bank) -------------------------------

    def add_resume_entry(
        self, company: str | None, role: str | None, location: str | None, dates: str | None,
        category: str | None, latex: str, raw_contrib: str | None = None,
    ) -> dict:
        now = time.time()
        cur = self._db.execute(
            "INSERT INTO resume_entries(company, role, location, dates, category, latex, raw_contrib, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (company, role, location, dates, category, latex, raw_contrib, now, now),
        )
        self._db.commit()
        return dict(self._db.execute("SELECT * FROM resume_entries WHERE id=?", (cur.lastrowid,)).fetchone())

    def list_resume_entries(self, category: str | None = None) -> list[dict[str, Any]]:
        if category:
            rows = self._db.execute(
                "SELECT * FROM resume_entries WHERE category=? ORDER BY updated_at DESC", (category,)
            ).fetchall()
        else:
            rows = self._db.execute("SELECT * FROM resume_entries ORDER BY updated_at DESC").fetchall()
        return [dict(r) for r in rows]

    def delete_resume_entry(self, entry_id: int) -> bool:
        cur = self._db.execute("DELETE FROM resume_entries WHERE id=?", (entry_id,))
        self._db.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self._db.close()
