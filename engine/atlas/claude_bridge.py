"""Drive a local Claude Code session headlessly — no SDK, no API keys.

We shell out to the installed `claude` CLI in print mode:

    claude -p "<prompt>" --output-format json --allowedTools Read --add-dir <uploads>

The first call returns a `session_id` in its JSON envelope; we persist it and pass
`--resume <session_id>` on every later call, so it behaves like one long-lived open
session (extracting todos from one image, then estimating calories from a meal photo,
all sharing context). Auth piggybacks on the user's existing `claude` login.

All requests are serialized through a single lock — one in-flight prompt per session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path

from .config import CLAUDE_BIN, UPLOAD_DIR
from .store import Store

log = logging.getLogger("atlas.claude")

_SESSION_KEY = "claude_session_id"
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class ClaudeError(RuntimeError):
    pass


class ClaudeBridge:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return shutil.which(CLAUDE_BIN) is not None or Path(CLAUDE_BIN).exists()

    async def ask(self, prompt: str, extra_dirs: list[Path] | None = None, timeout: float = 180.0) -> str:
        """Send a prompt to the persistent Claude session; return the result text."""
        if not self.available:
            raise ClaudeError(
                f"Claude CLI not found (looked for '{CLAUDE_BIN}'). "
                "Install Claude Code or set ATLAS_CLAUDE_BIN."
            )
        async with self._lock:
            return await self._run(prompt, extra_dirs or [], timeout)

    async def _run(self, prompt: str, extra_dirs: list[Path], timeout: float) -> str:
        cmd = [
            CLAUDE_BIN,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--allowedTools",
            "Read",
            "--add-dir",
            str(UPLOAD_DIR),
        ]
        for d in extra_dirs:
            cmd += ["--add-dir", str(d)]
        session_id = self._store.get_setting(_SESSION_KEY)
        if session_id:
            cmd += ["--resume", str(session_id)]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(UPLOAD_DIR),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise ClaudeError("Claude session timed out.")

        if proc.returncode != 0:
            err = stderr.decode("utf-8", "replace").strip()
            # A stale/invalid resumed session id: drop it and retry once fresh.
            if session_id and "session" in err.lower():
                log.warning("stale claude session %s; starting fresh", session_id)
                self._store.set_setting(_SESSION_KEY, None)
                return await self._run(prompt, extra_dirs, timeout)
            raise ClaudeError(err or f"claude exited {proc.returncode}")

        envelope = json.loads(stdout.decode("utf-8", "replace"))
        new_sid = envelope.get("session_id")
        if new_sid:
            self._store.set_setting(_SESSION_KEY, new_sid)
        result = envelope.get("result", "")
        if envelope.get("is_error"):
            raise ClaudeError(result or "claude reported an error")
        return result

    async def extract_json(self, prompt: str, extra_dirs: list[Path] | None = None) -> object:
        """Ask Claude for strict JSON and parse it (tolerating ```json fences)."""
        raw = await self.ask(prompt, extra_dirs=extra_dirs)
        cleaned = _FENCE_RE.sub("", raw).strip()
        # Grab the first {...} or [...] block if Claude added prose around it.
        m = re.search(r"(\[.*\]|\{.*\})", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(1)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise ClaudeError(f"Claude returned non-JSON: {raw[:200]}") from e


# --- Prompt builders ---------------------------------------------------------

TODO_FROM_IMAGE = """Read the image file at: {path}

It contains a to-do list, notes, a whiteboard, or tasks. Extract every actionable
task. Respond with ONLY a JSON array (no prose, no markdown), each item shaped:
{{"title": str, "category": str, "priority": "low"|"normal"|"high", "due": "YYYY-MM-DD"|null}}
Infer a short category per task (e.g. "Work", "Home", "Errands"). If no due date is
present, use null. If the image has no tasks, return []."""

FOOD_FROM_IMAGE = """Read the image file at: {path}

It is a photo of a meal or food. Estimate its nutrition. Respond with ONLY a JSON
object (no prose, no markdown), shaped:
{{"description": str, "calories": int, "protein": number, "carbs": number, "fat": number}}
Give your best single estimate for the whole plate. Grams for protein/carbs/fat."""

FOOD_FROM_TEXT = """You are a meticulous nutritionist estimating calories from a free-text food log.
Entry: "{text}"

METHOD — follow exactly:
1. Split the entry into distinct items/meals (e.g. "chipotle bowl and a coke" → 2 items).
2. For each item, decompose into components (protein, carb base, fats/oils, sauces, sides, drink).
3. Size each component from the stated quantity; if unstated, assume TYPICAL AMERICAN PORTIONS
   for how the food is usually served (restaurant portions for restaurant/chain food, standard
   packages for packaged food, moderate home portions otherwise).
4. Use USDA-style reference values per component and SUM them. Include the hidden calories people
   forget: cooking oil (~100-120 kcal/tbsp), butter, dressings, sugary sauces, breading, and
   sweetened drinks.
5. For chain restaurants (Chipotle, Chick-fil-A, McDonald's...), anchor on their published
   nutrition for the named item as typically configured.
6. Give ONE point estimate per item — the median of your plausible range, not the low end.
   People systematically under-log; do not lowball. Round calories to the nearest 10.

Respond with ONLY a JSON object (no prose, no markdown):
{{"items": [{{"description": str (short, e.g. "Chipotle chicken bowl w/ rice, beans, cheese, guac"),
             "calories": int, "protein": number, "carbs": number, "fat": number}}],
  "note": str|null (one short caveat ONLY if the entry was too vague to size confidently)}}
Macros in grams, consistent with calories (protein*4 + carbs*4 + fat*9 ≈ calories, ±10%).
If the text contains no food at all, return {{"items": [], "note": "no food found"}}."""

FINANCE_FROM_IMAGE = """Read the image file at: {path}

It is a screenshot of a bank/financial app (e.g. Bank of America) showing account balances.
Extract every account visible. Respond with ONLY a JSON object (no prose, no markdown):
{{"accounts": [{{"name": str (as displayed, e.g. "Adv Plus Banking", "BofA Checking ...1234"),
               "type": "cash"|"savings"|"debt",
               "balance": number}}],
  "note": str|null}}

Rules:
- Checking/spending accounts → "cash". Savings/money market → "savings". Credit cards and
  loans → "debt" with balance = the amount OWED as a positive number.
- balance is the CURRENT/available balance as a plain number (strip $ and commas).
- Ignore transactions, offers, rewards points, and anything that isn't an account balance.
- If the image shows no account balances, return {{"accounts": [], "note": "no balances found"}}."""

TIMECARD_FROM_IMAGE = """Read the image file at: {path}

It is a screenshot of a payroll timecard / hours report. Today is {today}.
Extract the hours per payroll week. Respond with ONLY a JSON object (no prose, no markdown):
{{"weeks": [{{"week_start": "YYYY-MM-DD" (the SUNDAY the week begins; infer the year from today),
             "regular": number, "overtime": number}}],
  "note": str|null}}

Rules:
- Week rows usually look like "Week 1, 06/21 - 06/27" with Regular and Overtime columns —
  week_start is the first date of that range.
- Use the WEEK subtotal rows, not per-day rows and not the pay-period grand total.
- Hours are decimal numbers (e.g. 22.13). Missing/dash overtime = 0.
- If no weekly hours are visible, return {{"weeks": [], "note": "no hours found"}}."""

CALENDAR_COMMAND = """You are a scheduling agent translating a spoken calendar request into concrete
calendar actions. Requests may contain SEVERAL independent tasks — handle all of them.
Today is {today} ({weekday}). The user's current events (id | title | start | end):
{events}

User request: "{text}"

Respond with ONLY a JSON object (no prose, no markdown):
{{"actions": [
   {{"action": "create", "title": str, "start": "YYYY-MM-DDTHH:MM:SS", "end": "YYYY-MM-DDTHH:MM:SS", "location": str|null, "description": str|null}}
   | {{"action": "update", "gcal_id": str, "title": str|null, "start": str|null, "end": str|null, "location": str|null}}
   | {{"action": "delete", "gcal_id": str}}
 ],
 "reply": str (one short sentence per task, confirming what was done or explaining what wasn't)}}

SEMANTIC FILLING — infer what a competent assistant would:
- Split compound requests into every task ("dentist tue 2, gym MWF at 7, and cancel lunch friday" = 3 tasks).
- Recurrence: expand explicit patterns into individual events within the NEXT 14 DAYS only
  ("gym every Mon/Wed/Fri at 7am" → up to 6 create actions). Cap total actions at 12; if a
  pattern would exceed that, create the nearest ones and say so in reply.
- Durations by event type when unstated: meals/coffee 45–60m, gym/workout 75m, haircut 45m,
  doctor/dentist 60m, meeting/call 30–60m, flight/travel as stated, parties 3h.
- Times by type when only a day is given: breakfast 9am, lunch 12:30pm, dinner 7pm,
  drinks 8pm, errands/appointments 10am, workouts 7am. "morning"≈9am, "afternoon"≈2pm, "evening"≈6:30pm.
- Title events like a human would write them ("Dentist", "Gym — push day", "Dinner w/ Sarah").
  Put stated venues/addresses in location; extra details in description.
- Conflict awareness: if a new event would overlap an existing one, still create it but flag
  the overlap in reply.
- Resolve relative dates against today ("tomorrow", "next Friday", "in 2 hours", "this weekend").

RULES:
- For update/delete, match events by title from the list above and use the exact gcal_id.
  Never invent gcal_ids. If a referenced event isn't found, skip that task and say so in reply.
- If a task is genuinely too ambiguous to schedule (no inferable day), skip it and ask a
  concise clarifying question in reply. Do the unambiguous tasks anyway."""

DRAFT_REVISE = """You are revising an email reply draft per the user's instruction.

CURRENT DRAFT
To: {to}
Subject: {subject}
Body:
{body}

ORIGINAL EMAIL (context, for grounding — do not contradict it):
{context}

USER INSTRUCTION: "{instruction}"

Rewrite the draft applying the instruction. Keep it professional and concise; preserve anything
the instruction doesn't ask to change (including the recipient and subject unless asked).
Respond with ONLY a JSON object (no prose, no markdown):
{{"to": str, "subject": str, "body": str, "note": str (one short sentence on what you changed)}}"""

WORKOUT_FROM_TEXT = """Turn this free-text workout note into structured data:
"{text}"

Respond with ONLY a JSON object (no prose, no markdown), shaped:
{{"type": str, "duration_min": int|null, "calories_burned": int|null, "notes": str|null,
  "lifts": [{{"exercise": str, "weight": number, "reps": int|null}}],
  "bodyweight": number|null,
  "recovery": {{"soreness": str|null, "sleep": str|null, "energy": str|null, "note": str|null}}|null}}

Rules:
- "lifts": one entry per exercise where a working WEIGHT is stated or clearly implied
  (e.g. "bench 185x5" → {{"exercise":"Bench Press","weight":185,"reps":5}}). Normalize
  exercise names (Bench Press, Squat, Deadlift, Overhead Press...). Empty list if none.
- "bodyweight": ONLY if the note states the user's own body weight (e.g. "weighed in at 172").
- "recovery": capture any recovery signals — soreness/pain, sleep quality, fatigue or energy,
  general feeling ("felt strong", "gassed", "tweaked shoulder" → note). null if none mentioned.
- Estimate calories_burned if you reasonably can."""

MARKET_OUTLOOK_FROM_DATA = """You are the market-desk layer of a systematic stock-picking tool.
Write today's market outlook using ONLY the material below: our computed market data (real numbers
from our own price database) and today's market headlines. You are a summarizer of evidence, not a
forecaster — do not introduce any fact, number, or event that is not in the supplied material.

DATE: {date}

COMPUTED MARKET DATA (from our own price store — the only numbers you may cite):
{stats}

MARKET HEADLINES (index, date, source, headline, summary):
{news}

Respond with ONLY a JSON object (no prose, no markdown), shaped:
{{
  "headline": str,             // one line: today's market in a sentence, grounded in the data/news above
  "summary": str,              // 2-3 short paragraphs: what's driving the market per the headlines, reconciled with the computed data (breadth, sector moves). Cite article indices like [3] for every claim taken from news.
  "themes": [{{"name": str, "sentiment": "positive"|"neutral"|"negative", "detail": str}}],  // 2-4 dominant themes across the headlines, each detail citing article indices
  "watch": [str],              // 2-3 things the headlines say are upcoming/unresolved (earnings, data releases, policy) — only if actually mentioned, with indices
  "news_analysis": [{{"index": int, "sentiment": "positive"|"neutral"|"negative", "impact": "high"|"medium"|"low", "why": str}}]  // one entry per supplied article: sentiment for the MARKET overall (not one company), impact = how much this news moves the broad market picture
}}
Rules: every quantitative claim must come from COMPUTED MARKET DATA verbatim; every event claim must
cite a supplied article index. If the headlines don't explain the computed moves, say the driver is
unclear rather than inventing one. Judge sentiment/impact from the market's perspective (a single
company's bad day is low impact unless the headlines tie it to the broader market)."""


PICK_ANALYSIS_FROM_DATA = """You are the analyst layer of a systematic stock-picking tool. The BUY
DECISION IS NOT YOURS — it is made mathematically by our scoring method (below). Your job is to
explain how our method scores THIS company given its fundamentals and what the news/business show,
and to semantically process the supplied news. Do not override, second-guess, or invent a rating.

TICKER: {symbol}

OUR SCORING METHOD + THIS COMPANY'S SCORE (the picking method + computed result — the source of truth):
{scores}

COMPANY BACKGROUND (sector, industry, business summary):
{profile}

C-SUITE ROSTER (factual, from filings/market data — name, title, age, pay):
{officers}

MD&A EXCERPT (management's own discussion from the latest filing; may be empty):
{mdna}

RECENT NEWS ARTICLES (index, date, source, headline, summary) — process each with the company + industry context above:
{news}

Respond with ONLY a JSON object (no prose, no markdown), shaped:
{{
  "reasons": [str],            // 3-5 bullets grounded ONLY in the score data: why our method scores it as it does, each citing a specific metric/subscore
  "mispricing_note": str,      // reconcile the computed quality score/verdict with the valuation multiples — is the price unjustified vs. the quality our method sees?
  "problem_solved": str,       // one short paragraph from the company background: the problem it solves and its industry
  "industry_health": str,      // one short paragraph: how the industry is doing, informed by the news + background
  "leadership": {{             // C-suite analysis. Career histories come from your own knowledge — flag anything you're unsure of.
    "ceo": {{"name": str, "tenure": str, "history": [{{"company": str, "role": str, "years": str}}], "track_record": str}},
    "assessment": str,         // 2-3 sentences: management quality/stability read, connected to the score data where possible (e.g. capital allocation vs ROE/FCF)
    "notes": [str]             // notable facts about other key officers (succession, founder status, recent departures) — [] if none known
  }},
  "mdna_read": {{              // ONLY if an MD&A excerpt is present, else null. Management narrative vs our computed numbers.
    "management_tone": str,    // one line: what management emphasizes/downplays
    "checks": [{{"claim": str, "our_data": str, "verdict": "match"|"partial"|"mismatch"}}]  // 2-4 claims from the MD&A checked against the score metrics above
  }},
  "risks": [str],              // 2-3 bullets: what in the data/news would break the thesis
  "competitors": [{{"name": str, "ticker": str, "compare": str}}],  // 3-5 real peers you identify: same GICS sector / similar sub-industry / closest comparables
  "news_analysis": [{{"index": int, "sentiment": "positive"|"neutral"|"negative", "relevance": "high"|"medium"|"low", "why_it_matters": str}}]  // one entry per supplied article, judged in the company+industry context
}}
Rules: for reasons and mispricing_note, cite a specific metric/subscore from the score data and NEVER
invent numbers not present there. Competitors, news judgement, and executive career histories may use
your own knowledge — but keep leadership history to what you're confident about (well-documented
careers), and say "uncertain" rather than fabricating dates or companies. For mdna_read, quote claims
only from the supplied MD&A excerpt and check them only against the supplied score metrics. If the
score data is missing a field, say so plainly rather than guessing."""


# --- Resume prompt builders --------------------------------------------------

RESUME_STYLE_RULES = r"""STYLE RULES (match the existing resume exactly):
- One page. EB Garamond serif via \usepackage{ebgaramond}. Dense but readable.
- Bullets are \item lines inside an itemize. Each bullet is ONE strong action-verb
  sentence (Architected, Engineered, Designed, Automated, Developed, Built…), present
  or past tense consistent with the section, NO leading pronoun, NO period-less run-ons.
- Bold the 2-4 highest-signal keywords per bullet with \textbf{...} (tools, metrics,
  named systems) — never bold a whole bullet.
- Quantify with real numbers when the source gives them ($, %, LOC, latency, counts).
  NEVER invent a metric that isn't in the supplied material.
- Escape LaTeX specials: & -> \&, % -> \%, $ -> \$, # -> \#, _ -> \_ (except inside
  math), and use -- for date ranges.
- Keep each bullet to at most ~2 typeset lines so the page stays balanced."""

RESUME_ENTRY_FROM_CONTRIB = r"""You are drafting one work-experience block for Abhinav Keswani's resume.

TARGET RESUME CATEGORY: {category}   (tune emphasis for this audience)

ROLE:
  Company:  {company}
  Title:    {role}
  Dates:    {dates}

RAW CONTRIBUTIONS (messy notes from the user — distill, don't copy verbatim):
{contributions}

PORTFOLIO CONTEXT (things Abhinav has actually built — use ONLY to enrich accuracy,
never to import unrelated work into this role):
{corpus}

{style}

Produce {n_bullets} bullets that best represent this role for the {category} audience.
Respond with ONLY a JSON object (no prose, no markdown fences), shaped:
{{"bullets": [str, ...]}}
Each string is the LaTeX body of one \item WITHOUT the leading "\item " token
(e.g. "Architected \\textbf{{Hydra}}, a 15{{,}}000+ LOC trading framework...")."""

RESUME_TAILOR_TO_JD = r"""You are tailoring Abhinav Keswani's resume to a specific job.

Rewrite the FULL LaTeX document below so it foregrounds the experience, skills, and
keywords most relevant to the target job — reordering bullets, re-weighting emphasis,
and lightly rewording for the job's language. Keep it truthful: reuse only facts already
present in the resume or the portfolio context; do NOT fabricate roles, metrics, or tools.
Preserve the exact preamble, fonts, section structure, and one-page layout. It MUST remain
a single page.

TARGET JOB / COMPANY: {label}

JOB DESCRIPTION:
{job_description}

PORTFOLIO CONTEXT (real projects/stacks — may surface a relevant, already-true detail):
{corpus}

CURRENT RESUME (full LaTeX source):
{latex}

{style}

Respond with ONLY the complete, compilable LaTeX source of the tailored resume — starting
at \documentclass and ending at \end{{document}}. No prose, no markdown fences, no commentary."""

RESUME_VISUAL_CRITIQUE = r"""You are the visual QA step for a LaTeX resume. Read the compiled PDF and judge it
as a recruiter would — then, if needed, return a corrected full LaTeX source.

COMPILED PDF: {pdf_path}   (open and look at it)

DETERMINISTIC COMPILE DIAGNOSTICS:
  page_count: {page_count}   (target: exactly 1)
  overfull_hboxes: {overfull}   (target: none — these are lines spilling past the margin)

Check: Does it fit on ONE page with comfortable margins (not cramped, not half-empty)?
Any line running into the right margin? Awkward one-word last lines / widows? Consistent
spacing between sections? Professional overall?

If it is already clean AND page_count is 1 AND there are no overfull hboxes, return
{{"ok": true, "issues": [], "latex": null}}.

Otherwise return {{"ok": false, "issues": [short strings], "latex": "<full corrected LaTeX>"}}
where `latex` is the COMPLETE compilable source (\documentclass … \end{{document}}) with the
MINIMAL edits that fix the problems — tighten wording, trim/merge a bullet, or nudge spacing
to reach one clean page. Never invent new facts; only condense existing content. Preserve the
preamble, fonts, and section structure.

CURRENT LaTeX SOURCE:
{latex}

Respond with ONLY the JSON object (no prose, no markdown fences)."""


PROFILE_CHAT = r"""You are Atlas, a warm, concise conversational assistant that helps Abhinav Keswani
build and maintain his master career PROFILE — the single source of truth every résumé is
generated from. It holds his roles (with concrete accomplishments), skills, and a short summary.

Behave like a thoughtful collaborator, not a form:
- When he mentions a new role, project, or accomplishment, draw it out. If key details are
  missing (company, title, dates, what he actually did, real metrics/scale), ask ONE or TWO
  focused follow-up questions — don't interrogate.
- Once you have enough for a role, record it and confirm in a sentence what you captured.
- He can also just talk; reply naturally and only record when there's something concrete.
- NEVER invent facts. Record only what he tells you (the portfolio context may confirm details).

CURRENT PROFILE (JSON):
{profile}

PORTFOLIO CONTEXT (his real GitHub repos + local projects — to confirm/enrich, never to invent):
{corpus}

CONVERSATION SO FAR:
{history}

USER: {message}

Respond with ONLY a JSON object (no prose, no markdown fences), shaped:
{{
  "reply": str,
  "updates": {{
    "roles": [{{"company": str, "role": str, "dates": str, "location": str|null,
                "category_hint": "swe"|"quant"|"finance"|null, "facts": [str]}}],
    "education": [{{"school": str, "degree": str, "dates": str, "location": str|null,
                    "coursework": str, "honors": str}}],
    "skills": [str],
    "summary": str|null
  }}
}}
Merge semantics: a role matching an existing company+role UPDATES it (its facts are appended,
deduped); otherwise it's added. "facts" are concrete, résumé-worthy accomplishment statements
in his own reality (tools, metrics, scale, outcomes) — not yet LaTeX. Use an empty object
{{"reply": "...", "updates": {{}}}} when the message is just conversation with nothing to record."""

# Refine chat uses a delimiter (not JSON) so the full LaTeX body — which is large and full of
# braces/backslashes — never has to survive JSON escaping.
RESUME_REFINE_CHAT = r"""You are refining one of Abhinav Keswani's résumés from a natural-language
instruction. Apply his change and return the full revised LaTeX.

Rules: stay truthful — use only facts already in the résumé, the PROFILE, or the portfolio
context; never invent. Keep the exact preamble, fonts, and section structure. It MUST remain a
single page. Honor the category's emphasis so the résumé speaks to the right audience.

TARGET CATEGORY: {category} — {category_note}

HIS INSTRUCTION: {message}

RECENT REFINEMENT CONVERSATION:
{history}

PROFILE + PORTFOLIO CONTEXT (pull real facts from here when he asks to add or expand something):
{context}

CURRENT RÉSUMÉ (full LaTeX):
{latex}

{style}

Respond in EXACTLY this format and nothing else:
<one or two sentences describing what you changed and why — this is shown to him as chat>
%%%LATEX%%%
<the COMPLETE revised LaTeX source, from \documentclass to \end{{document}}>"""
