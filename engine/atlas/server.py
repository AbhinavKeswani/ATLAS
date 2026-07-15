"""Atlas engine: FastAPI REST + WebSocket, serving the glass dashboard.

Binds 127.0.0.1 only. One process owns the SQLite store, the event bus, and the
Claude bridge; the dashboard is a static SPA served from web/.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import gcal, gmail, google_auth, health, market_outlook, meetings, networth, paycheck, picks, portfolio, profile, profile_seed, reminders, resume, resume_corpus, resume_templates, soccer, soccer_lineups, yahoo
from .bus import EventBus
from .claude_bridge import (
    CALENDAR_COMMAND, ClaudeBridge, ClaudeError, DRAFT_REVISE,
    FINANCE_FROM_IMAGE, FOOD_FROM_IMAGE, FOOD_FROM_TEXT,
    TIMECARD_FROM_IMAGE, TODO_FROM_IMAGE, WORKOUT_FROM_TEXT,
)
from .config import HOST, LOG_DIR, PORT, UPLOAD_DIR, ensure_dirs
from .store import Store

log = logging.getLogger("atlas.server")
_WEB_DIR = Path(__file__).parent / "web"


# --- Request bodies ----------------------------------------------------------


class TodoBody(BaseModel):
    title: str
    notes: str | None = None
    category: str = "Inbox"
    priority: str = "normal"
    due: str | None = None


class TodoPatch(BaseModel):
    title: str | None = None
    notes: str | None = None
    category: str | None = None
    priority: str | None = None
    due: str | None = None
    done: bool | None = None


class TimeEntryBody(BaseModel):
    work_date: str
    start: str
    end: str
    break_min: int = 0


class PayConfigBody(BaseModel):
    rate: float | None = None
    ot_multiplier: float | None = None
    anchor_payday: str | None = None
    pay_schedule: str | None = None   # "biweekly" | "semimonthly"
    pay_lag_days: int | None = None
    nyc_resident: bool | None = None
    effective_tax_rate: float | None = None
    default_break_min: int | None = None
    use_default_schedule: bool | None = None


class HoldingBody(BaseModel):
    symbol: str
    qty: float
    cost_basis: float | None = None
    last_price: float | None = None
    account: str | None = None


class WatchBody(BaseModel):
    symbol: str
    note: str | None = None


class PicksRefreshBody(BaseModel):
    limit: int | None = None
    ingest: bool = True


class SoccerRefreshBody(BaseModel):
    demo: bool | None = None   # None = auto (demo when no ODDS_API_KEY)
    full: bool = False         # force ingest/features even if built tables are fresh


class OrderBody(BaseModel):
    symbol: str
    side: str
    qty: float
    price: float
    account: str | None = None


class CsvBody(BaseModel):
    csv: str


class AccountBody(BaseModel):
    name: str
    type: str
    balance: float = 0.0


class AccountPatch(BaseModel):
    name: str | None = None
    type: str | None = None
    balance: float | None = None


class WorkoutBody(BaseModel):
    type: str
    duration_min: int | None = None
    notes: str | None = None
    calories_burned: int | None = None


class WorkoutTextBody(BaseModel):
    text: str


class FoodBody(BaseModel):
    description: str
    calories: int | None = None
    protein: float | None = None
    carbs: float | None = None
    fat: float | None = None


class BodyweightBody(BaseModel):
    weight: float


class LiftBody(BaseModel):
    exercise: str
    weight: float
    reps: int | None = None
    notes: str | None = None


class HealthChatBody(BaseModel):
    message: str


class SettingBody(BaseModel):
    key: str
    value: object


class SubscribeBody(BaseModel):
    symbols: list[str]


class DraftReplyBody(BaseModel):
    email_id: int


class EmailFlags(BaseModel):
    starred: bool | None = None
    important: bool | None = None
    archived: bool | None = None
    category: str | None = None


class ReminderBody(BaseModel):
    text: str
    remind_at: float           # epoch seconds
    method: str = "notification"  # notification | imessage
    target: str | None = None
    email_id: int | None = None


class HoursWeekBody(BaseModel):
    week_start: str            # ISO Sunday date
    regular: float = 0.0
    overtime: float = 0.0


class CheckDepositBody(BaseModel):
    period_start: str          # ISO date of the pay period start
    account_id: int | None = None
    amount: float | None = None   # actual net from a paystub (overrides estimate)
    gross: float | None = None


class DraftPatch(BaseModel):
    to_addr: str | None = None
    subject: str | None = None
    body: str | None = None


class DraftReviseBody(BaseModel):
    message: str                     # the instruction ("make it shorter", "add availability Friday")
    to_addr: str | None = None       # current (possibly unsaved) field values from the modal
    subject: str | None = None
    body: str | None = None


class RecruitingPatch(BaseModel):
    company: str | None = None
    role: str | None = None
    recruiter: str | None = None
    stage: str | None = None
    notes: str | None = None


class EventBody(BaseModel):
    title: str
    start: str  # ISO datetime
    end: str
    location: str | None = None
    description: str | None = None


class EventPatch(BaseModel):
    title: str | None = None
    start: str | None = None
    end: str | None = None
    location: str | None = None
    description: str | None = None


class ResumeCompileBody(BaseModel):
    latex: str


class ResumeEntryBody(BaseModel):
    company: str
    role: str
    dates: str
    contributions: str
    category: str
    location: str | None = None
    n_bullets: int = 3
    save: bool = True


class ResumeTailorBody(BaseModel):
    base_id: int                    # the base (or any) doc to tailor from
    job_description: str
    label: str                      # company/role this iteration is for
    variant: str | None = None


class ResumeDocPatch(BaseModel):
    label: str | None = None
    category: str | None = None
    variant: str | None = None
    latex: str | None = None


class ResumeCorpusBody(BaseModel):
    github: bool = True
    local: bool = True


class ResumeChatBody(BaseModel):
    message: str


class ProfileChatBody(BaseModel):
    message: str


def create_app() -> FastAPI:
    ensure_dirs()
    store = Store()
    bus = EventBus()
    claude = ClaudeBridge(store)
    app = FastAPI(title="Atlas Engine", version="0.1.0")
    app.state.store = store
    app.state.bus = bus
    app.state.claude = claude

    # ---- Overview ----
    @app.get("/api/overview")
    async def overview() -> dict:
        nw = networth.compute(store)
        # Thread-aware counts from the same source as the Inbox — never recount raw rows.
        mail = store.email_stats()
        return {
            "pay": paycheck.compute_status(store),
            "networth": {"total": nw["total"], "series": nw["series"], "brokerage": nw["brokerage"]},
            "todos_open": len(store.list_todos(include_done=False)),
            "food": health.today_summary(store),
            "workouts": health.recent_workouts(store, limit=1),
            "claude_available": claude.available,
            "inbox": {
                "unread": mail["unread"],
                "needs_reply": mail["needs_reply"],
                "drafts_pending": len(store.list_drafts(status="pending")),
            },
            "upcoming": gcal.upcoming(store, within_min=120),
        }

    # ---- Todos ----
    @app.get("/api/todos")
    async def list_todos() -> list[dict]:
        return store.list_todos()

    @app.post("/api/todos")
    async def add_todo(body: TodoBody) -> dict:
        t = store.add_todo(body.title, body.notes, body.category, body.priority, body.due)
        bus.publish("todo_added", t)
        return t

    @app.patch("/api/todos/{todo_id}")
    async def patch_todo(todo_id: int, body: TodoPatch) -> dict:
        t = store.update_todo(todo_id, **body.model_dump(exclude_none=True))
        if t is None:
            raise HTTPException(404, "todo not found")
        bus.publish("todo_updated", t)
        return t

    @app.delete("/api/todos/{todo_id}")
    async def delete_todo(todo_id: int) -> dict:
        if not store.delete_todo(todo_id):
            raise HTTPException(404, "todo not found")
        bus.publish("todo_removed", {"id": todo_id})
        return {"deleted": todo_id}

    @app.post("/api/todos/ingest-image")
    async def ingest_image(file: UploadFile) -> dict:
        path = await _save_upload(file)
        bus.publish("claude_busy", {"task": "todos"})
        try:
            items = await claude.extract_json(TODO_FROM_IMAGE.format(path=path))
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        finally:
            bus.publish("claude_idle", {})
        if not isinstance(items, list):
            raise HTTPException(502, "Claude did not return a list")
        created = []
        for it in items:
            if not isinstance(it, dict) or not it.get("title"):
                continue
            t = store.add_todo(
                it["title"], category=it.get("category", "Inbox"),
                priority=it.get("priority", "normal"), due=it.get("due"), source="image",
            )
            bus.publish("todo_added", t)
            created.append(t)
        return {"created": created}

    # ---- Money / paycheck ----
    @app.get("/api/pay")
    async def pay_status() -> dict:
        return paycheck.compute_status(store)

    @app.put("/api/pay/config")
    async def set_pay_config(body: PayConfigBody) -> dict:
        cfg = paycheck.PayConfig.load(store)
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(cfg, k, v)
        cfg.save(store)
        bus.publish("pay_updated", {})
        return paycheck.compute_status(store)

    @app.get("/api/time")
    async def list_time(start: str, end: str) -> list[dict]:
        return store.list_time_entries(start, end)

    @app.post("/api/time")
    async def add_time(body: TimeEntryBody) -> dict:
        e = store.add_time_entry(body.work_date, body.start, body.end, body.break_min)
        bus.publish("pay_updated", {})
        return e

    @app.delete("/api/time/{entry_id}")
    async def delete_time(entry_id: int) -> dict:
        if not store.delete_time_entry(entry_id):
            raise HTTPException(404, "entry not found")
        bus.publish("pay_updated", {})
        return {"deleted": entry_id}

    # ---- Portfolio ----
    @app.get("/api/portfolio")
    async def get_portfolio() -> dict:
        return {"valuation": portfolio.valuation(store), "orders": store.list_orders()}

    @app.post("/api/portfolio/holdings")
    async def add_holding(body: HoldingBody) -> dict:
        h = store.upsert_holding(body.symbol, body.qty, body.cost_basis, body.account, body.last_price)
        bus.publish("networth_updated", {})
        return h

    @app.delete("/api/portfolio/holdings/{holding_id}")
    async def delete_holding(holding_id: int) -> dict:
        if not store.delete_holding(holding_id):
            raise HTTPException(404, "holding not found")
        bus.publish("networth_updated", {})
        return {"deleted": holding_id}

    @app.post("/api/portfolio/orders")
    async def add_order(body: OrderBody) -> dict:
        o = store.add_order(body.symbol, body.side, body.qty, body.price, time.time(), body.account)
        store.rebuild_holdings_from_orders()
        bus.publish("networth_updated", {})
        return o

    @app.post("/api/portfolio/import-orders")
    async def import_orders(body: CsvBody) -> dict:
        result = portfolio.import_orders_csv(store, body.csv)
        bus.publish("networth_updated", {})
        return result

    @app.post("/api/portfolio/refresh")
    async def refresh_portfolio() -> dict:
        result = await asyncio.to_thread(portfolio.refresh_prices, store)
        bus.publish("networth_updated", {})
        return {**result, "valuation": portfolio.valuation(store)}

    # ---- Watchlist (manual symbol tracking) ----
    @app.get("/api/watchlist")
    async def get_watchlist() -> dict:
        return {"items": store.get_setting("watchlist", []) or []}

    @app.post("/api/watchlist")
    async def add_watch(body: WatchBody) -> dict:
        items = store.get_setting("watchlist", []) or []
        sym = body.symbol.upper().strip()
        if sym and not any(i["symbol"] == sym for i in items):
            items.append({"symbol": sym, "note": (body.note or "").strip(), "price": None, "updated_at": None})
            store.set_setting("watchlist", items)
        return {"items": items}

    @app.delete("/api/watchlist/{symbol}")
    async def del_watch(symbol: str) -> dict:
        sym = symbol.upper().strip()
        items = [i for i in (store.get_setting("watchlist", []) or []) if i["symbol"] != sym]
        store.set_setting("watchlist", items)
        return {"items": items}

    @app.post("/api/watchlist/refresh")
    async def refresh_watch() -> dict:
        items = store.get_setting("watchlist", []) or []
        syms = [i["symbol"] for i in items]
        prices = await asyncio.to_thread(yahoo.latest_prices, syms) if syms else {}
        for i in items:
            if i["symbol"] in prices:
                i["price"] = prices[i["symbol"]]; i["updated_at"] = time.time()
        store.set_setting("watchlist", items)
        return {"items": items, "updated": len(prices)}

    # ---- Picks (fundamentals-driven, ranked by quality + mispricing via CommonSense) ----
    @app.get("/api/picks")
    async def get_picks() -> dict:
        return picks.list_picks(store)

    # Static sub-paths must precede /api/picks/{symbol} so they aren't captured as a symbol.
    @app.get("/api/picks/daily")
    async def get_daily_picks(offset: int = 0, count: int = 10) -> dict:
        return picks.daily_picks(store, count=count, offset=offset)

    @app.post("/api/picks/daily-run")
    async def run_daily_picks() -> dict:
        result = await picks.run_daily_job(store)
        bus.publish("picks_updated", {})
        return result

    @app.post("/api/picks/refresh")
    async def refresh_picks(body: PicksRefreshBody) -> dict:
        result = await picks.refresh_picks(store, limit=body.limit, ingest=body.ingest)
        bus.publish("picks_updated", {})
        return result

    # Static paths (watch, lookup) must register BEFORE the /{symbol} catch-all.
    @app.get("/api/picks/outlook")
    async def get_market_outlook(regenerate: bool = False) -> dict:
        return await market_outlook.get_outlook(store, claude, regenerate=regenerate)

    @app.get("/api/picks/watch")
    async def get_picks_watchlist() -> dict:
        return picks.watchlist_view(store)

    @app.post("/api/picks/watch/{symbol}")
    async def post_picks_watch(symbol: str) -> dict:
        return picks.watch(store, symbol)

    @app.delete("/api/picks/watch/{symbol}")
    async def delete_picks_watch(symbol: str) -> dict:
        return picks.unwatch(store, symbol)

    @app.get("/api/picks/lookup/{symbol}")
    async def get_lookup_status(symbol: str) -> dict:
        return picks.lookup_status(store, symbol)

    @app.post("/api/picks/lookup/{symbol}")
    async def post_lookup(symbol: str) -> dict:
        return await picks.run_lookup(store, symbol)

    @app.get("/api/picks/{symbol}")
    async def get_pick(symbol: str) -> dict:
        return await picks.pick_detail(store, symbol)

    @app.get("/api/picks/{symbol}/analysis")
    async def get_pick_analysis(symbol: str, regenerate: bool = False) -> dict:
        return await picks.pick_analysis(store, claude, symbol, regenerate=regenerate)

    @app.get("/api/picks/{symbol}/chart")
    async def get_pick_chart(symbol: str, range: str = "1y") -> dict:
        return await picks.chart_bundle(store, symbol, range)

    @app.get("/api/picks/{symbol}/mdna")
    async def get_pick_mdna(symbol: str) -> dict:
        return await picks.pick_mdna(symbol)

    @app.get("/api/picks/{symbol}/series")
    async def get_pick_series(symbol: str, range: str = "1y") -> dict:
        return {"symbol": symbol.upper(), "series": await picks.daily_series(store, symbol, range)}

    @app.post("/api/picks/{symbol}/close")
    async def close_pick_ep(symbol: str) -> dict:
        result = picks.close_pick(store, symbol)
        bus.publish("picks_updated", {})
        return result

    # ---- Soccer (WC 2026 model: probs + odds + lineups + parlays) ----
    @app.get("/api/soccer")
    async def get_soccer() -> dict:
        return soccer.overview(store)

    @app.post("/api/soccer/refresh")
    async def refresh_soccer(body: SoccerRefreshBody) -> dict:
        result = await soccer.refresh(store, demo=body.demo, full=body.full)
        if not result.get("error"):
            bus.publish("soccer_updated", {})
        return result

    @app.post("/api/soccer/lineups/refresh")
    async def refresh_soccer_lineups() -> dict:
        data = soccer.overview(store)
        result = await soccer_lineups.refresh(store, data.get("matches", []))
        bus.publish("soccer_updated", {})
        return result

    @app.get("/api/soccer/{match_id}")
    async def get_soccer_match(match_id: int) -> dict:
        d = soccer.match_detail(store, match_id)
        if d is None:
            raise HTTPException(404, "match not found")
        return d

    # ---- Net worth ----
    @app.get("/api/networth")
    async def get_networth() -> dict:
        return networth.compute(store)

    @app.post("/api/networth/snapshot")
    async def take_snapshot() -> dict:
        return {"total": networth.snapshot(store)}

    @app.post("/api/networth/ingest-image")
    async def networth_image(file: UploadFile) -> dict:
        """Bank-app screenshot → Claude reads balances → accounts updated/created."""
        path = await _save_upload(file)
        bus.publish("claude_busy", {"task": "finance"})
        try:
            data = await claude.extract_json(FINANCE_FROM_IMAGE.format(path=path))
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        finally:
            bus.publish("claude_idle", {})
        if not isinstance(data, dict):
            raise HTTPException(502, "Claude did not return an object")
        existing = store.list_accounts()
        updated, created = [], []
        for a in data.get("accounts") or []:
            if not isinstance(a, dict) or not a.get("name") or a.get("balance") is None:
                continue
            name, bal = str(a["name"]).strip(), float(a["balance"])
            typ = a.get("type") if a.get("type") in ("cash", "savings", "debt") else "cash"
            # Fuzzy match: exact name, else substring either way (case-insensitive).
            low = name.lower()
            match = next((x for x in existing if x["name"].lower() == low), None) \
                or next((x for x in existing if low in x["name"].lower() or x["name"].lower() in low), None)
            if match:
                store.update_account(match["id"], balance=bal)
                updated.append({"name": match["name"], "balance": bal})
            else:
                store.add_account(name, typ, bal)
                created.append({"name": name, "type": typ, "balance": bal})
        bus.publish("networth_updated", {})
        return {"updated": updated, "created": created, "note": data.get("note"),
                "networth": networth.compute(store)["total"]}

    @app.post("/api/accounts")
    async def add_account(body: AccountBody) -> dict:
        a = store.add_account(body.name, body.type, body.balance)
        bus.publish("networth_updated", {})
        return a

    @app.patch("/api/accounts/{account_id}")
    async def patch_account(account_id: int, body: AccountPatch) -> dict:
        a = store.update_account(account_id, **body.model_dump(exclude_none=True))
        if a is None:
            raise HTTPException(404, "account not found")
        bus.publish("networth_updated", {})
        return a

    @app.delete("/api/accounts/{account_id}")
    async def delete_account(account_id: int) -> dict:
        if not store.delete_account(account_id):
            raise HTTPException(404, "account not found")
        bus.publish("networth_updated", {})
        return {"deleted": account_id}

    # ---- Health ----
    @app.get("/api/health/food")
    async def get_food() -> dict:
        return health.today_summary(store)

    @app.post("/api/health/food")
    async def add_food(body: FoodBody) -> dict:
        f = store.add_food(body.description, body.calories, body.protein, body.carbs, body.fat)
        bus.publish("food_added", f)
        return f

    @app.post("/api/health/food/estimate")
    async def food_estimate(body: HealthChatBody) -> dict:
        """Free-text meal → Claude's component-decomposed calorie estimate → logged."""
        bus.publish("claude_busy", {"task": "food"})
        try:
            data = await claude.extract_json(FOOD_FROM_TEXT.format(text=body.message.replace('"', "'")))
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        finally:
            bus.publish("claude_idle", {})
        if not isinstance(data, dict):
            raise HTTPException(502, "Claude did not return an object")
        logged = []
        for it in data.get("items") or []:
            if isinstance(it, dict) and it.get("description"):
                f = store.add_food(it["description"], it.get("calories"), it.get("protein"),
                                   it.get("carbs"), it.get("fat"), source="chat")
                logged.append(f)
        bus.publish("food_added", {})
        return {"logged": logged, "note": data.get("note"),
                "total_calories": sum(x["calories"] or 0 for x in logged),
                "today": health.today_summary(store)}

    @app.post("/api/health/food/ingest-image")
    async def food_image(file: UploadFile) -> dict:
        path = await _save_upload(file)
        bus.publish("claude_busy", {"task": "food"})
        try:
            data = await claude.extract_json(FOOD_FROM_IMAGE.format(path=path))
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        finally:
            bus.publish("claude_idle", {})
        if not isinstance(data, dict):
            raise HTTPException(502, "Claude did not return an object")
        f = store.add_food(
            data.get("description", "Meal"), data.get("calories"), data.get("protein"),
            data.get("carbs"), data.get("fat"), source="image",
        )
        bus.publish("food_added", f)
        return f

    @app.delete("/api/health/food/{food_id}")
    async def delete_food(food_id: int) -> dict:
        if not store.delete_food(food_id):
            raise HTTPException(404, "food not found")
        bus.publish("food_added", {})
        return {"deleted": food_id}

    @app.get("/api/health/workouts")
    async def get_workouts() -> dict:
        return health.recent_workouts(store)

    @app.post("/api/health/workouts")
    async def add_workout(body: WorkoutBody) -> dict:
        w = store.add_workout(body.type, body.duration_min, body.notes, body.calories_burned)
        bus.publish("workout_added", w)
        return w

    @app.post("/api/health/workouts/parse")
    async def parse_workout(body: WorkoutTextBody) -> dict:
        bus.publish("claude_busy", {"task": "workout"})
        try:
            data = await claude.extract_json(WORKOUT_FROM_TEXT.format(text=body.text))
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        finally:
            bus.publish("claude_idle", {})
        if not isinstance(data, dict):
            raise HTTPException(502, "Claude did not return an object")
        w = store.add_workout(
            data.get("type", "Workout"), data.get("duration_min"),
            data.get("notes"), data.get("calories_burned"),
        )
        # Fan out: weights → lifts (strength progression), bodyweight, recovery → coach.
        lifts_logged = []
        for l in data.get("lifts") or []:
            if isinstance(l, dict) and l.get("exercise") and l.get("weight") is not None:
                store.add_lift(l["exercise"], float(l["weight"]), l.get("reps"), None)
                lifts_logged.append(f"{l['exercise']} @ {l['weight']:g}")
        if data.get("bodyweight"):
            store.add_bodyweight(float(data["bodyweight"]))
        rec = data.get("recovery")
        if isinstance(rec, dict) and any(rec.get(k) for k in ("soreness", "sleep", "energy", "note")):
            health.add_recovery_note(store, rec)
        bus.publish("workout_added", w)
        return {**w, "lifts_logged": lifts_logged,
                "bodyweight_logged": data.get("bodyweight"),
                "recovery_logged": bool(rec and isinstance(rec, dict) and any(rec.values()))}

    @app.delete("/api/health/workouts/{workout_id}")
    async def delete_workout(workout_id: int) -> dict:
        if not store.delete_workout(workout_id):
            raise HTTPException(404, "workout not found")
        bus.publish("workout_added", {})
        return {"deleted": workout_id}

    # ---- Bodyweight + strength ----
    @app.get("/api/health/strength")
    async def strength() -> dict:
        return {
            "bodyweight": health.bodyweight(store),
            "strength": health.strength_progress(store),
            "recovery": health.get_recovery(store),
        }

    @app.post("/api/health/bodyweight")
    async def add_bodyweight(body: BodyweightBody) -> dict:
        w = store.add_bodyweight(body.weight)
        bus.publish("workout_added", {})
        return w

    @app.post("/api/health/lifts")
    async def add_lift(body: LiftBody) -> dict:
        l = store.add_lift(body.exercise, body.weight, body.reps, body.notes)
        bus.publish("workout_added", {})
        return l

    @app.delete("/api/health/lifts/{lift_id}")
    async def delete_lift(lift_id: int) -> dict:
        if not store.delete_lift(lift_id):
            raise HTTPException(404, "lift not found")
        bus.publish("workout_added", {})
        return {"deleted": lift_id}

    # ---- Recovery insight + AI chat ----
    @app.post("/api/health/recovery")
    async def gen_recovery() -> dict:
        return await health.recovery(store, claude)

    @app.get("/api/health/chat")
    async def get_chat() -> dict:
        return {"history": health.chat_history(store)}

    @app.post("/api/health/chat")
    async def post_chat(body: HealthChatBody) -> dict:
        try:
            r = await health.chat(store, claude, body.message)
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        bus.publish("food_added", {})
        return r

    # ---- Settings ----
    @app.get("/api/settings/{key}")
    async def get_setting(key: str) -> dict:
        return {"key": key, "value": store.get_setting(key)}

    @app.put("/api/settings")
    async def put_setting(body: SettingBody) -> dict:
        store.set_setting(body.key, body.value)
        return {"key": body.key, "value": body.value}

    # ---- Google connection ----
    @app.get("/api/google/status")
    async def google_status() -> dict:
        return google_auth.status()

    @app.post("/api/google/connect")
    async def google_connect() -> dict:
        try:
            return await asyncio.to_thread(google_auth.connect)
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/api/google/disconnect")
    async def google_disconnect() -> dict:
        return google_auth.disconnect()

    # ---- Inbox (Gmail) ----
    @app.get("/api/inbox")
    async def inbox() -> dict:
        return {
            "google": google_auth.status(),
            "emails": store.list_emails_collapsed(limit=150),
            "recruiting": store.list_recruiting(),
            "drafts": store.list_drafts(status="pending"),
            "briefing": store.get_setting("latest_briefing"),
            "stats": {**store.email_stats(), "gmail_unread": store.get_setting("gmail_unread_cache")},
            "trash_count": len(store.list_trash()),
            "reports_unread": _unread_report_count(),
        }

    @app.get("/api/inbox/stats")
    async def inbox_stats(remote: bool = False) -> dict:
        """Reading-stats widgets. remote=true also asks Gmail for the server-side unread count."""
        stats = store.email_stats()
        if remote and google_auth.status().get("state") == "connected":
            stats["gmail_unread"] = await asyncio.to_thread(gmail.gmail_unread_estimate)
        else:
            stats["gmail_unread"] = store.get_setting("gmail_unread_cache")
        return stats

    @app.post("/api/inbox/sync")
    async def inbox_sync(unread_only: bool = False) -> dict:
        try:
            pulled = await asyncio.to_thread(gmail.sync, store, 100, unread_only)
            remote_unread = await asyncio.to_thread(gmail.gmail_unread_estimate)
            if remote_unread is not None:
                store.set_setting("gmail_unread_cache", remote_unread)
        except Exception as e:
            raise HTTPException(502, f"Gmail sync failed: {e}")
        analyzed = await gmail.analyze_unprocessed(store, claude)
        added = await _autoadd_events(analyzed.get("events", []))
        await gmail.draft_all_needing_reply(store, claude)
        brief = await gmail.briefing(store, claude)
        bus.publish("inbox_updated", {})
        return {"pulled": pulled["pulled"], "processed": analyzed["processed"], "events_added": added,
                "briefing": brief, "stats": store.email_stats()}

    @app.post("/api/inbox/analyze-next")
    async def analyze_next() -> dict:
        """Analyze the next batch of already-pulled (on-disk) emails — no new Gmail pull."""
        analyzed = await gmail.analyze_unprocessed(store, claude)
        added = await _autoadd_events(analyzed.get("events", []))
        await gmail.draft_all_needing_reply(store, claude)
        bus.publish("inbox_updated", {})
        return {"processed": analyzed["processed"], "events_added": added, "stats": store.email_stats()}

    async def _autoadd_events(events: list[dict]) -> int:
        """Auto-create detected meetings/appointments on the calendar (deduped)."""
        if not events or google_auth.status().get("state") != "connected":
            return 0
        added = 0
        for ev in events:
            try:
                r = await asyncio.to_thread(gcal.find_or_create_event, store, ev["title"], ev["start"], ev.get("end"))
                if r.get("created"):
                    added += 1
            except Exception as e:
                log.warning("auto-add event failed: %s", e)
        if added:
            bus.publish("calendar_updated", {})
        return added

    @app.post("/api/inbox/reindex")
    async def inbox_reindex() -> dict:
        """Clear the local email index and pull the next batch of unread mail."""
        store.clear_inbox()
        try:
            pulled = await asyncio.to_thread(gmail.sync, store, 25, True)
        except Exception as e:
            raise HTTPException(502, f"Gmail sync failed: {e}")
        analyzed = await gmail.analyze_unprocessed(store, claude)
        added = await _autoadd_events(analyzed.get("events", []))
        await gmail.draft_all_needing_reply(store, claude)
        brief = await gmail.briefing(store, claude)
        bus.publish("inbox_updated", {})
        return {"pulled": pulled["pulled"], "processed": analyzed["processed"], "events_added": added, "briefing": brief}

    @app.post("/api/inbox/emails/{email_id}/read")
    async def read_email(email_id: int) -> dict:
        e = await asyncio.to_thread(gmail.mark_read, store, email_id)
        if e is None:
            raise HTTPException(404, "email not found")
        bus.publish("inbox_updated", {})
        return e

    @app.patch("/api/inbox/emails/{email_id}/flags")
    async def flag_email(email_id: int, body: EmailFlags) -> dict:
        e = store.update_email(email_id, **body.model_dump(exclude_none=True))
        if e is None:
            raise HTTPException(404, "email not found")
        bus.publish("inbox_updated", {})
        return e

    # ---- Trash pile (delete = mark read + local trash; Gmail never deleted) ----
    @app.post("/api/inbox/emails/{email_id}/trash")
    async def trash_email(email_id: int, force: bool = False) -> dict:
        e = store.get_email(email_id)
        if e is None:
            raise HTTPException(404, "email not found")
        if not force and (e["starred"] or e["important"] or store.email_has_reminder(email_id)):
            raise HTTPException(409, "protected: starred/important/has a reminder — pass force=true to override")
        await asyncio.to_thread(gmail.mark_read, store, email_id)  # read on Gmail too
        store.set_trashed(email_id, True)
        bus.publish("inbox_updated", {})
        return {"trashed": email_id, "stats": store.email_stats()}

    @app.get("/api/inbox/trash")
    async def get_trash() -> list[dict]:
        return store.list_trash()

    @app.post("/api/inbox/trash/restore/{email_id}")
    async def restore_email(email_id: int) -> dict:
        e = store.set_trashed(email_id, False)
        if e is None:
            raise HTTPException(404, "email not found")
        bus.publish("inbox_updated", {})
        return e

    @app.post("/api/inbox/trash/review")
    async def trash_review() -> dict:
        """Claude scans the pile; flagged emails are restored + marked important."""
        bus.publish("claude_busy", {"task": "trash-review"})
        try:
            result = await gmail.review_trash(store, claude)
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        finally:
            bus.publish("claude_idle", {})
        bus.publish("inbox_updated", {})
        return result

    @app.post("/api/inbox/trash/finalize")
    async def trash_finalize() -> dict:
        purged = store.purge_trash()
        bus.publish("inbox_updated", {})
        return {"purged": purged, "stats": store.email_stats()}

    # ---- Reports (saved briefings) ----
    def _unread_report_count() -> int:
        read = set(store.get_setting("reports_read", []) or [])
        return sum(1 for r in gmail.list_reports() if r["name"] not in read)

    @app.get("/api/inbox/reports")
    async def inbox_reports() -> list[dict]:
        read = set(store.get_setting("reports_read", []) or [])
        return [{**r, "read": r["name"] in read} for r in gmail.list_reports()]

    @app.get("/api/inbox/reports/{name}")
    async def report_content(name: str) -> dict:
        from .config import APP_SUPPORT

        path = (APP_SUPPORT / "reports" / Path(name).name)  # basename only — no traversal
        if not path.exists() or not path.name.startswith("briefing_"):
            raise HTTPException(404, "report not found")
        return {"name": path.name, "text": path.read_text()}

    @app.post("/api/inbox/reports/{name}/read")
    async def mark_report_read(name: str) -> dict:
        read = set(store.get_setting("reports_read", []) or [])
        read.add(name)
        store.set_setting("reports_read", sorted(read))
        bus.publish("inbox_updated", {})
        return {"read": name}

    @app.post("/api/inbox/emails/{email_id}/draft")
    async def make_draft(email_id: int) -> dict:
        try:
            d = await gmail.draft_reply(store, claude, email_id)
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        bus.publish("inbox_updated", {})
        return d

    @app.patch("/api/inbox/drafts/{draft_id}")
    async def edit_draft(draft_id: int, body: DraftPatch) -> dict:
        d = store.update_draft(draft_id, **body.model_dump(exclude_none=True))
        if d is None:
            raise HTTPException(404, "draft not found")
        return d

    @app.post("/api/inbox/drafts/{draft_id}/approve")
    async def approve_draft(draft_id: int) -> dict:
        """Approve = send. This is the explicit user consent to send the email."""
        try:
            sent = await asyncio.to_thread(gmail.send_draft, store, draft_id)
        except Exception as e:
            raise HTTPException(502, f"Send failed: {e}")
        bus.publish("inbox_updated", {})
        return sent

    @app.post("/api/inbox/drafts/{draft_id}/discard")
    async def discard_draft(draft_id: int) -> dict:
        d = store.update_draft(draft_id, status="discarded")
        if d is None:
            raise HTTPException(404, "draft not found")
        bus.publish("inbox_updated", {})
        return d

    @app.post("/api/inbox/drafts/{draft_id}/revise")
    async def revise_draft(draft_id: int, body: DraftReviseBody) -> dict:
        """Claude rewrites the CURRENT draft (as passed in, incl. unsaved edits) per instruction."""
        d = store.get_draft(draft_id)
        if d is None:
            raise HTTPException(404, "draft not found")
        context = ""
        if d.get("email_id"):
            e = store.get_email(d["email_id"])
            if e:
                context = (e.get("body") or e.get("snippet") or "")[:2500]
        bus.publish("claude_busy", {"task": "draft-revise"})
        try:
            data = await claude.extract_json(DRAFT_REVISE.format(
                to=body.to_addr or d["to_addr"] or "", subject=body.subject or d["subject"] or "",
                body=(body.body if body.body is not None else d["body"]) or "",
                context=context or "(not available)",
                instruction=body.message.replace('"', "'"),
            ))
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        finally:
            bus.publish("claude_idle", {})
        if not isinstance(data, dict) or not data.get("body"):
            raise HTTPException(502, "Claude did not return a revised draft")
        updated = store.update_draft(draft_id, to_addr=data.get("to"), subject=data.get("subject"), body=data["body"])
        bus.publish("inbox_updated", {})
        return {**updated, "note": data.get("note", "")}

    @app.patch("/api/inbox/recruiting/{rec_id}")
    async def patch_recruiting(rec_id: int, body: RecruitingPatch) -> dict:
        r = store.update_recruiting(rec_id, **body.model_dump(exclude_none=True))
        if r is None:
            raise HTTPException(404, "not found")
        return r

    # ---- Calendar ----
    @app.get("/api/calendar")
    async def calendar() -> dict:
        return {
            "google": google_auth.status(),
            "events": store.list_events(),
            "upcoming": gcal.upcoming(store),
        }

    @app.post("/api/calendar/sync")
    async def calendar_sync() -> dict:
        try:
            r = await asyncio.to_thread(gcal.sync, store)
        except Exception as e:
            raise HTTPException(502, f"Calendar sync failed: {e}")
        bus.publish("calendar_updated", {})
        return r

    @app.post("/api/calendar/command")
    async def calendar_command(body: HealthChatBody) -> dict:
        """Natural-language calendar change → Claude plans actions → applied to Google Calendar."""
        events = store.list_events()
        digest = "\n".join(
            f"{e['gcal_id']} | {e['title']} | {e['start']} | {e['end']}" for e in events[:60]
        ) or "(no events)"
        now = dt.datetime.now()
        bus.publish("claude_busy", {"task": "calendar"})
        try:
            data = await claude.extract_json(CALENDAR_COMMAND.format(
                today=now.strftime("%Y-%m-%d %H:%M"), weekday=now.strftime("%A"),
                events=digest, text=body.message.replace('"', "'"),
            ))
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        finally:
            bus.publish("claude_idle", {})
        if not isinstance(data, dict):
            raise HTTPException(502, "Claude did not return an object")
        known_ids = {e["gcal_id"] for e in events}
        applied, errors = [], []
        for a in data.get("actions") or []:
            if not isinstance(a, dict):
                continue
            try:
                if a.get("action") == "create" and a.get("title") and a.get("start"):
                    end = a.get("end") or a["start"]
                    await asyncio.to_thread(gcal.create_event, store, a["title"], a["start"], end,
                                            a.get("location"), a.get("description"))
                    applied.append(f"created “{a['title']}”")
                elif a.get("action") == "update" and a.get("gcal_id") in known_ids:
                    fields = {k: v for k, v in a.items() if k in ("title", "start", "end", "location") and v}
                    await asyncio.to_thread(gcal.update_event, store, a["gcal_id"], **fields)
                    applied.append(f"updated “{a.get('title') or a['gcal_id'][:8]}”")
                elif a.get("action") == "delete" and a.get("gcal_id") in known_ids:
                    title = next((e["title"] for e in events if e["gcal_id"] == a["gcal_id"]), a["gcal_id"][:8])
                    await asyncio.to_thread(gcal.delete_event, store, a["gcal_id"])
                    applied.append(f"deleted “{title}”")
            except Exception as e:  # keep going; report per-action failures
                errors.append(str(e)[:120])
        bus.publish("calendar_updated", {})
        return {"applied": applied, "errors": errors, "reply": data.get("reply", ""),
                "events": store.list_events()}

    @app.post("/api/calendar/events")
    async def create_event(body: EventBody) -> dict:
        try:
            ev = await asyncio.to_thread(
                gcal.create_event, store, body.title, body.start, body.end, body.location, body.description
            )
        except Exception as e:
            raise HTTPException(502, f"Create failed: {e}")
        bus.publish("calendar_updated", {})
        return ev

    @app.patch("/api/calendar/events/{gcal_id}")
    async def update_event(gcal_id: str, body: EventPatch) -> dict:
        try:
            ev = await asyncio.to_thread(gcal.update_event, store, gcal_id, **body.model_dump(exclude_none=True))
        except Exception as e:
            raise HTTPException(502, f"Update failed: {e}")
        bus.publish("calendar_updated", {})
        return ev

    @app.delete("/api/calendar/events/{gcal_id}")
    async def delete_event(gcal_id: str) -> dict:
        try:
            await asyncio.to_thread(gcal.delete_event, store, gcal_id)
        except Exception as e:
            raise HTTPException(502, f"Delete failed: {e}")
        bus.publish("calendar_updated", {})
        return {"deleted": gcal_id}

    # ---- Meetings (Vein, read-only) ----
    @app.get("/api/meetings")
    async def list_meetings() -> dict:
        return {"source": meetings.available(), "meetings": meetings.list_meetings()}

    @app.get("/api/meetings/{meeting_id}")
    async def meeting_detail(meeting_id: int) -> dict:
        d = meetings.meeting_detail(meeting_id)
        if d is None:
            raise HTTPException(404, "meeting not found")
        return d

    # ---- Resume (LaTeX drafting + tailoring + visual-fit via Tectonic) ----
    @app.get("/api/resume")
    async def get_resume() -> dict:
        return resume.overview(store)

    # Static sub-paths must precede /api/resume/{doc_id:int} — int converter also
    # guards them, but keep the ordering explicit.
    @app.get("/api/resume/corpus")
    async def get_resume_corpus() -> dict:
        return resume_corpus.get(store)

    @app.post("/api/resume/corpus/refresh")
    async def refresh_resume_corpus(body: ResumeCorpusBody) -> dict:
        bus.publish("resume_busy", {"task": "corpus"})
        try:
            data = await asyncio.to_thread(resume_corpus.refresh, store, github=body.github, local=body.local)
        finally:
            bus.publish("resume_idle", {})
        bus.publish("resume_updated", {})
        return resume.overview(store) | {"corpus": data}

    @app.post("/api/resume/compile")
    async def compile_resume(body: ResumeCompileBody) -> dict:
        diag = await asyncio.to_thread(resume.compile_preview, body.latex)
        # Strip the absolute pdf path; the client fetches it via the preview route.
        return {**diag, "pdf_path": None, "pdf_url": "/api/resume/preview/pdf" if diag.get("pdf_path") else None}

    @app.get("/api/resume/preview/pdf")
    async def get_resume_preview_pdf() -> FileResponse:
        pdf = resume.RESUME_DIR / "_preview" / "main.pdf"
        if not pdf.exists():
            raise HTTPException(404, "no preview compiled yet")
        return FileResponse(pdf, media_type="application/pdf", headers={"Cache-Control": "no-store"})

    @app.post("/api/resume/entry")
    async def resume_entry(body: ResumeEntryBody) -> dict:
        bus.publish("claude_busy", {"task": "resume"})
        try:
            r = await resume.write_entry(
                store, claude, company=body.company, role=body.role, dates=body.dates,
                contributions=body.contributions, category=body.category, location=body.location,
                n_bullets=body.n_bullets, save=body.save,
            )
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        finally:
            bus.publish("claude_idle", {})
        if r.get("error"):
            raise HTTPException(502, r["error"])
        return r

    @app.post("/api/resume/tailor")
    async def resume_tailor(body: ResumeTailorBody) -> dict:
        base_doc = store.get_resume_doc(body.base_id)
        if not base_doc:
            raise HTTPException(404, "base resume not found")
        bus.publish("claude_busy", {"task": "resume"})
        try:
            r = await resume.tailor(
                store, claude, base_doc=base_doc, job_description=body.job_description,
                label=body.label, variant=body.variant,
            )
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        finally:
            bus.publish("claude_idle", {})
        bus.publish("resume_updated", {})
        return r

    @app.get("/api/resume/{doc_id}")
    async def get_resume_doc(doc_id: int) -> dict:
        doc = resume.get_doc(store, doc_id)
        if not doc:
            raise HTTPException(404, "resume not found")
        return doc

    @app.get("/api/resume/{doc_id}/pdf")
    async def get_resume_pdf(doc_id: int) -> FileResponse:
        pdf = await asyncio.to_thread(resume.ensure_pdf, store, doc_id)
        if not pdf or not Path(pdf).exists():
            raise HTTPException(422, "resume did not compile (is Tectonic installed?)")
        return FileResponse(pdf, media_type="application/pdf", headers={"Cache-Control": "no-store"})

    @app.patch("/api/resume/{doc_id}")
    async def patch_resume_doc(doc_id: int, body: ResumeDocPatch) -> dict:
        doc = resume.save_doc(store, doc_id, **body.model_dump(exclude_none=True))
        if not doc:
            raise HTTPException(404, "resume not found")
        bus.publish("resume_updated", {})
        return doc

    @app.post("/api/resume/{doc_id}/compile")
    async def recompile_resume_doc(doc_id: int) -> dict:
        if not store.get_resume_doc(doc_id):
            raise HTTPException(404, "resume not found")
        diag = await asyncio.to_thread(resume.compile_doc, store, doc_id)
        bus.publish("resume_updated", {})
        return {**diag, "pdf_url": f"/api/resume/{doc_id}/pdf" if diag.get("pdf_path") else None}

    @app.post("/api/resume/{doc_id}/refine")
    async def refine_resume_doc(doc_id: int) -> dict:
        if not store.get_resume_doc(doc_id):
            raise HTTPException(404, "resume not found")
        bus.publish("claude_busy", {"task": "resume"})
        try:
            r = await resume.refine_doc(store, claude, doc_id)
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        finally:
            bus.publish("claude_idle", {})
        bus.publish("resume_updated", {})
        return r

    @app.get("/api/resume/{doc_id}/chat")
    async def get_resume_chat(doc_id: int) -> dict:
        return {"history": resume.doc_chat_history(store, doc_id)}

    @app.post("/api/resume/{doc_id}/chat")
    async def post_resume_chat(doc_id: int, body: ResumeChatBody) -> dict:
        if not store.get_resume_doc(doc_id):
            raise HTTPException(404, "resume not found")
        # Note: no claude_busy/idle bus events here — those trigger a global view
        # refresh that would wipe the in-chat "Atlas is making changes…" indicator.
        try:
            r = await resume.refine_chat(store, claude, doc_id, body.message)
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        if r.get("error"):
            raise HTTPException(404, r["error"])
        return r

    @app.post("/api/resume/{doc_id}/base")
    async def set_resume_base(doc_id: int) -> dict:
        doc = resume.set_base(store, doc_id)
        if not doc:
            raise HTTPException(404, "resume not found")
        bus.publish("resume_updated", {})
        return doc

    @app.delete("/api/resume/{doc_id}")
    async def delete_resume_doc(doc_id: int) -> dict:
        if not resume.delete_doc(store, doc_id):
            raise HTTPException(404, "resume not found")
        bus.publish("resume_updated", {})
        return {"deleted": doc_id}

    # ---- Profile (conversational master profile that résumés are built from) ----
    @app.get("/api/profile")
    async def get_profile() -> dict:
        return {"profile": profile.get(store), "history": profile.chat_history(store),
                "claude_available": claude.available}

    @app.post("/api/profile/chat")
    async def post_profile_chat(body: ProfileChatBody) -> dict:
        # No claude_busy/idle here — see the resume chat note; the in-view spinner owns feedback.
        try:
            r = await profile.chat(store, claude, body.message)
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        return r

    @app.delete("/api/profile/role/{idx}")
    async def delete_profile_role(idx: int) -> dict:
        p = profile.delete_role(store, idx)
        bus.publish("profile_updated", {})
        return {"profile": p}

    @app.delete("/api/profile/education/{idx}")
    async def delete_profile_education(idx: int) -> dict:
        p = profile.delete_education(store, idx)
        bus.publish("profile_updated", {})
        return {"profile": p}

    @app.delete("/api/profile/skill/{skill}")
    async def delete_profile_skill(skill: str) -> dict:
        p = profile.delete_skill(store, skill)
        bus.publish("profile_updated", {})
        return {"profile": p}

    @app.post("/api/profile/chat/clear")
    async def clear_profile_chat() -> dict:
        profile.clear_chat(store)
        return {"cleared": True}

    # ---- Reminders ----
    @app.get("/api/reminders")
    async def list_reminders() -> list[dict]:
        return store.list_reminders("pending")

    @app.post("/api/reminders")
    async def add_reminder(body: ReminderBody) -> dict:
        r = store.add_reminder(body.text, body.remind_at, body.method, body.target, body.email_id)
        return r

    @app.delete("/api/reminders/{rid}")
    async def cancel_reminder(rid: int) -> dict:
        if not store.cancel_reminder(rid):
            raise HTTPException(404, "reminder not found")
        return {"cancelled": rid}

    # ---- Weekly hours (payroll overrides) ----
    @app.get("/api/hours-weeks")
    async def hours_weeks() -> dict:
        return store.hours_weeks()

    @app.put("/api/hours-weeks")
    async def set_hours_week(body: HoursWeekBody) -> dict:
        w = store.upsert_hours_week(body.week_start, body.regular, body.overtime)
        bus.publish("pay_updated", {})
        return w

    @app.delete("/api/hours-weeks/{week_start}")
    async def delete_hours_week(week_start: str) -> dict:
        store.delete_hours_week(week_start)
        bus.publish("pay_updated", {})
        return {"deleted": week_start}

    # ---- Unpaid checks (per pay period) ----
    @app.get("/api/pay/checks")
    async def get_pay_checks() -> dict:
        return paycheck.pay_checks(store)

    @app.post("/api/pay/checks/deposit")
    async def deposit_pay_check(body: CheckDepositBody) -> dict:
        try:
            r = paycheck.deposit_check(store, body.period_start, body.account_id, body.amount, body.gross)
        except ValueError as e:
            raise HTTPException(400, str(e))
        bus.publish("networth_updated", {})
        return r

    @app.post("/api/pay/checks/undo")
    async def undo_pay_check(body: CheckDepositBody) -> dict:
        try:
            r = paycheck.undo_deposit(store, body.period_start)
        except ValueError as e:
            raise HTTPException(400, str(e))
        bus.publish("networth_updated", {})
        return r

    @app.post("/api/pay/ingest-timecard")
    async def ingest_timecard(file: UploadFile) -> dict:
        """Timecard screenshot → Claude reads weekly reg/OT → hours_week overrides."""
        path = await _save_upload(file)
        bus.publish("claude_busy", {"task": "timecard"})
        try:
            data = await claude.extract_json(TIMECARD_FROM_IMAGE.format(path=path, today=dt.date.today().isoformat()))
        except ClaudeError as e:
            raise HTTPException(502, f"Claude: {e}")
        finally:
            bus.publish("claude_idle", {})
        if not isinstance(data, dict):
            raise HTTPException(502, "Claude did not return an object")
        saved = []
        for w in data.get("weeks") or []:
            if not isinstance(w, dict) or not w.get("week_start"):
                continue
            try:
                dt.date.fromisoformat(w["week_start"])
            except ValueError:
                continue
            store.upsert_hours_week(w["week_start"], float(w.get("regular") or 0), float(w.get("overtime") or 0))
            saved.append(w)
        bus.publish("pay_updated", {})
        return {"weeks": saved, "note": data.get("note"), "pay": paycheck.compute_status(store)}

    @app.get("/api/meta")
    async def meta() -> dict:
        return {"claude_available": claude.available, "today": dt.date.today().isoformat()}

    # ---- WebSocket ----
    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        q = bus.subscribe()
        try:
            while True:
                event = await q.get()
                await socket.send_json(event.to_dict())
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("ws error")
        finally:
            bus.unsubscribe(q)

    # ---- Static SPA ----
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(_WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")

    @app.on_event("startup")
    async def _startup() -> None:
        try:
            seeded = resume_templates.seed(store)
            if seeded:
                log.info("seeded %d base resume template(s)", len(seeded))
        except Exception:
            log.exception("resume template seed failed")
        try:
            if profile_seed.seed(store):
                log.info("seeded master CV profile")
        except Exception:
            log.exception("profile seed failed")
        asyncio.create_task(reminders.scheduler(store, bus))

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        store.close()

    return app


async def _save_upload(file: UploadFile) -> Path:
    ensure_dirs()
    suffix = Path(file.filename or "upload.png").suffix or ".png"
    dest = UPLOAD_DIR / f"upload_{int(time.time() * 1000)}{suffix}"
    dest.write_bytes(await file.read())
    return dest


def main() -> None:
    import uvicorn

    ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_DIR / "engine.log")],
    )
    uvicorn.run(create_app(), host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
