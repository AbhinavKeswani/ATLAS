# Atlas — a localhosted dashboard for life

A fully local life dashboard: organized checkable todos (with image→todo extraction
by a locally-launched Claude Code session), a paycheck running-total + next-payday
engine, portfolio valuation priced from the local HYDRA equity feed, net-worth
tracking, and workout + food/calorie logging. Keynote-grade frosted-glass UI over an
animated aurora backdrop. Everything stays on the machine — no SDK, no API calls.

Built on the proven stack from Vein (FastAPI + SQLite + vanilla glass UI).

## Run

```bash
cd engine
uv sync                 # core deps (FastAPI, uvicorn)
uv sync --extra hydra   # optional: live equity pricing (pandas, pyarrow, databento)
uv run atlas-server     # serves http://127.0.0.1:8770
```

Open <http://127.0.0.1:8770>. Data lives in `~/Library/Application Support/Atlas/atlas.db`.

## Modules

| Tab        | What it does |
|------------|--------------|
| Home       | At-a-glance: running pay, next payday, net worth, calories ring, open todos, upcoming-event reminders |
| Todos      | CRUD + check-off, grouped by category; drop an image → Claude extracts tasks |
| Money      | Paycheck (running net, withholding, next payday) + portfolio positions |
| Net Worth  | Accounts (assets/debts) + live brokerage value + snapshot sparkline |
| Soccer     | WC 2026 betting model: lineup-checked picks vs live sportsbook lines (Fanatics/FanDuel/bet365/…), 3 recommended parlays, per-pick math pop-up |
| Health     | Food log vs. calorie target (+ meal-photo estimation) and workout logging |
| Inbox      | Gmail + Calendar: sync/summarize/brief email, track recruiting, draft replies (approve-to-send), month-grid calendar + reminders |
| Meetings   | Read-only view of Vein meeting notes (summary, action items, transcript) |
| Profile    | Conversational master career record — talk to Atlas to add roles/skills/summary; every résumé is generated from it |
| Resume     | LaTeX résumé studio: chat to refine (raw LaTeX hidden), Tectonic compiles + visually verifies one-page fit, grounded in the Profile + a GitHub/local-project corpus; saves named iterations by category |
| Settings   | Pay config (rate, OT, anchor payday, NYC tax, schedule), calorie target |

## Gmail + Calendar setup (Inbox tab)

Requires Google's official API via OAuth. One-time setup:

```bash
cd engine && uv sync --extra google       # install Google client libraries
```

1. In **Google Cloud Console** → APIs & Services → Credentials, create an **OAuth client ID** of type **Desktop app**.
2. Enable the **Gmail API** and **Google Calendar API** for the project.
3. Download the client JSON → save as `~/Library/Application Support/Atlas/google_credentials.json` (or set `ATLAS_GOOGLE_CREDENTIALS`).
4. Add yourself as a test user on the OAuth consent screen.
5. In Atlas → Inbox → **Connect Google** (a browser opens for consent; the token is cached locally).

Scopes: `gmail.modify`, `gmail.send`, `calendar`. **Nothing is ever sent or changed without an explicit action** — drafted replies sit as "pending" cards and only send when you click **Approve & send** in the pop-out.

## Meetings tab (Vein)

Atlas reads Vein's notes **read-only** from `~/Library/Application Support/Vein/vein.db`
(works even when Vein isn't running), falling back to a live Vein engine's HTTP API at
`http://127.0.0.1:8765` if the DB isn't present. It does **not** absorb Vein's audio/STT
pipeline. Override the DB path with `ATLAS_VEIN_DB`.

## A note on Grok

Atlas's AI features run on the **local Claude bridge**. The unofficial `realasfngl/Grok-Api`
wrapper was evaluated and rejected: it's a reverse-engineered scraper that bypasses auth,
violates xAI ToS, and breaks on any web change. For a legitimate Grok path, use the official
xAI API (`api.x.ai`) with a real key.

## The Claude bridge (`atlas/claude_bridge.py`)

Shells out to the local `claude` CLI in print mode:

```
claude -p "<prompt>" --output-format json --allowedTools Read --add-dir <uploads>
```

The first call's `session_id` is persisted and passed via `--resume` on every later
call, so it's one long-lived session. Used for: image→todos, meal-photo→calories,
free-text→structured workout. No API key — piggybacks on your existing Claude login.
Override the binary with `ATLAS_CLAUDE_BIN`.

## Soccer tab (`atlas/soccer.py` + WC 2026 model)

Same bridge pattern as CommonSense: Atlas shells into the sibling **WC 2026**
project's venv (`ATLAS_WC_ROOT`, default `~/Desktop/WC 2026`; `ATLAS_WC_PYTHON`
to override the interpreter), runs the pipeline (`ingest → features → model →
odds`) plus `scripts/atlas_export.py`, and reads `data/built/soccer.json`.

- **Model**: Dixon-Coles Poisson over blended international Elo + player club-xG,
  RPS-calibrated on 2021–24 internationals. Markets: 1X2, double chance, totals,
  BTTS, Asian handicap.
- **Lines**: The Odds API (`ODDS_API_KEY` in the WC project's `.env`; regions
  us+uk → Fanatics, FanDuel, DraftKings, BetMGM, bet365…). Without a key the
  odds stage synthesizes demo lines and the tab shows a "demo lines" chip.
  A sportsbook dropdown re-prices every card client-side.
- **Lineups**: checked against ESPN's public scoreboard before anything is
  recommended; matches without a team sheet are shown dimmed and excluded.
- **Parlays**: three recommended 4-leg tickets (safe ~75% / medium ~65% /
  higher ~55% to hit), one leg per match so probabilities multiply cleanly.
- Clicking any pick or parlay opens a pop-up with the full math: λ decomposition,
  the scoreline heatmap, goal-timing bins, and model-vs-book probability bars.

## Profile + Resume tabs (`atlas/profile.py`, `atlas/resume.py`, `atlas/latex.py`, `atlas/resume_corpus.py`)

A résumé studio that writes LaTeX and **verifies it visually**, driven by a conversational
career profile. The persistent Claude bridge is the writer; **Tectonic** is the eyes.

```bash
brew install tectonic     # one-time: self-contained XeLaTeX-compatible engine
```

**Profile tab** (`profile.py`) — your master career record, built by *talking* to Atlas:
"hey Atlas, I want to add my new role at Seaport…". The agent draws out details (asking
follow-ups when they're thin), then records structured **roles** (with concrete
accomplishment facts), **skills**, and a **summary** — stored as a settings blob. This is
the single source every résumé is generated from, so drafting stays grounded in real,
user-confirmed work. (This replaced the old "Add experience" modal.)

**Resume tab** (`resume.py`) — chat to build and refine; **raw LaTeX stays hidden**:
- **Base templates** (`resume_templates.py`): one per category — **SWE / Quant / Finance** —
  on the self-contained `ebgaramond` package (no system font install). Seeded on first run.
- **Refine by chat**: "make the Vincere role focus more on latency", "lengthen the last
  bullet", "pull in my Seaport role from my profile". Claude rewrites the source, replies
  with what it changed, and the doc **auto-iterates to fit** — compile → read page count +
  `Overfull \hbox` → read the compiled **PDF back** (via the bridge's `Read` tool) → revise
  until it's one clean page (≤4 passes). A big inline PDF preview updates each turn.
- **Tailor to a job**: paste a JD → a new iteration rewritten to foreground the relevant
  work, **labeled for the company**.
- **Per-category emphasis**: each category pitches technicality to its audience — Finance
  de-emphasizes deep systems/ML internals; Quant foregrounds methods/data; SWE foregrounds
  engineering depth (`CATEGORY_EMPHASIS` in `resume.py`).
- **Grounding** (`resume_corpus.py`): the Profile plus your **GitHub** (authed `gh` CLI) and
  **local Desktop projects** (`ATLAS_RESUME_ROOTS`, default HYDRA/CommonSense/WC 2026/Atlas/
  Vein — stacks, dependency manifests, READMEs).
- **History**: every iteration in SQLite (`resume_docs`), categorized, named, sortable, with
  a protected `base` per category. Compiled PDFs live under
  `~/Library/Application Support/Atlas/resumes/`. Refinement chats persist per doc.

Without Tectonic the tab still saves résumés; it just can't compile or auto-verify (a
"no compiler" chip shows). Override the engine with `ATLAS_TECTONIC_BIN`.

## HYDRA pricing (`atlas/hydra_prices.py`)

Read-only. Reads the latest cached 1-min OHLCV close per held symbol from
`~/Desktop/HYDRA/data/databento/XNAS.ITCH/ohlcv-1m/` (requires the `hydra` extra).
Held symbols HYDRA doesn't cover can be appended to its `universe.json` from the UI
("Subscribe in HYDRA"). Without the extra / without HYDRA, positions fall back to
cost basis. Override the root with `ATLAS_HYDRA_ROOT`.

## Notes

- Withholding is an **estimate** (annualized percentage method: federal + FICA + NY
  State + optional NYC), not payroll-exact. 2025 tax constants live in `atlas/paycheck.py`.
- Default schedule (Mon–Thu 08:00–20:00, Fri 08:00–16:00) auto-fills any day without a
  manual time entry, so pay accrues without daily data entry.
- Pay cadence is biweekly (14-day cycle) anchored to a known payday set in Settings.
