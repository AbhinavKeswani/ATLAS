"use strict";

// ---------- helpers ----------
const $ = (sel, root = document) => root.querySelector(sel);
const el = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, opts = {}) {
  const res = await fetch(`/api${path}`, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body && typeof opts.body !== "string" && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

const money = (n, signed = false) => {
  if (n === null || n === undefined) return "—";
  const s = n < 0 ? "-" : signed && n > 0 ? "+" : "";
  return `${s}$${Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};
const money0 = (n) => (n == null ? "—" : `$${Math.round(n).toLocaleString("en-US")}`);

function toast(msg, kind = "ok") {
  const t = el("toast");
  t.textContent = msg; t.className = `toast glass show ${kind}`;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.className = `toast glass ${kind}`), 3200);
}

const ICONS = {
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2m-9 0v14a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V6"/></svg>',
  image: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M21 16l-5-5L5 20"/></svg>',
  dumbbell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6v12M3 9v6M18 6v12M21 9v6M6 12h12"/></svg>',
  food: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 3v7a2 2 0 0 0 2 2 2 2 0 0 0 2-2V3M6 12v9M16 3c-1.5 0-3 2-3 5s1.5 4 3 4v9"/></svg>',
};

// ---------- router ----------
const VIEWS = {};
let active = "home";

// --- UI-state preservation: re-renders must never lose the user's place -------
// Snapshot every scrolled container, non-empty input, and the focused element
// (keyed by id, or by DOM index path as a fallback) before a view re-renders;
// restore afterwards. This is what makes background refreshes invisible.
function elKey(el, root) {
  if (el.id) return "#" + el.id;
  const path = [];
  let n = el;
  while (n && n !== root && n.parentElement) { path.unshift([...n.parentElement.children].indexOf(n)); n = n.parentElement; }
  return "p:" + path.join(">");
}
function elByKey(key, root) {
  if (key.startsWith("#")) return document.getElementById(key.slice(1));
  let n = root;
  for (const i of key.slice(2).split(">")) { n = n && n.children[+i]; if (!n) return null; }
  return n;
}
function uiSnapshot(root) {
  const scrolls = [];
  root.querySelectorAll("*").forEach((n) => { if (n.scrollTop > 0) scrolls.push({ key: elKey(n, root), top: n.scrollTop }); });
  const inputs = [...root.querySelectorAll("input,textarea,select")]
    .filter((i) => i.type !== "checkbox" && i.type !== "radio" && i.value !== "" && !i.readOnly)
    .map((i) => ({ key: elKey(i, root), value: i.value }));
  const ae = document.activeElement;
  const focused = ae && root.contains(ae) && (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA")
    ? { key: elKey(ae, root), selStart: ae.selectionStart, selEnd: ae.selectionEnd } : null;
  return { scrolls, inputs, focused };
}
function uiRestore(root, snap) {
  for (const s of snap.scrolls) { const n = elByKey(s.key, root); if (n) n.scrollTop = s.top; }
  for (const i of snap.inputs) { const n = elByKey(i.key, root); if (n && n.value === "") n.value = i.value; }
  if (snap.focused) {
    const n = elByKey(snap.focused.key, root);
    if (n) { n.focus({ preventScroll: true }); try { n.setSelectionRange(snap.focused.selStart, snap.focused.selEnd); } catch {} }
  }
}
let renderSeq = 0;
async function render(view) {
  view = view || active;
  if (!VIEWS[view]) return;
  const seq = ++renderSeq;
  const root = document.querySelector(`.view[data-view="${view}"]`);
  const snap = root && root.innerHTML.trim() ? uiSnapshot(root) : null;
  await VIEWS[view]();
  if (seq !== renderSeq) return;      // a newer render superseded this one
  if (snap && root) uiRestore(root, snap);
  restoreOps();
}

function go(view) {
  active = view;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.dataset.view === view));
  render(view);
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => go(t.dataset.view)));

// ---------- in-flight op registry (keeps loading state alive across tab switches) ----------
// Views re-render from scratch on every entry, which wipes transient DOM (spinners,
// optimistic chat bubbles). We track long-running ops in a registry and restore their
// loading UI after every render. Any button with data-op="<key>" auto-restores its spinner.
const PENDING = new Map();       // opKey -> spinner label
const chatPending = {};          // chat key -> the in-flight user message (working bubble)
const inputDraft = {};           // input key -> unsent text, so drafts survive navigation
function opStart(key, label) { if (key) { PENDING.set(key, label || ""); restoreOps(); } }
function opEnd(key) { if (key) PENDING.delete(key); }
function opPending(key) { return PENDING.has(key); }
function restoreOps() {
  document.querySelectorAll("[data-op]").forEach((b) => {
    if (opPending(b.dataset.op)) {
      b.disabled = true;
      if (!b.querySelector(".spinner")) b.innerHTML = `<span class="spinner"></span> ${b.dataset.opLabel || PENDING.get(b.dataset.op) || ""}`.trimEnd();
    }
  });
}
// Run an async op under a registry key so its button spinner survives re-renders.
async function runOp(key, label, fn) { opStart(key, label); try { return await fn(); } finally { opEnd(key); } }
function workingBubble(text) { return `<div class="msgbub ai atlas-working"><span class="spinner"></span> ${esc(text)}</div>`; }

// Draggable divider: resize the right pane by driving a CSS var on the grid layout.
// Width persists in localStorage; double-click resets to the default.
function mountResizer(handle, layout, cssVar, storeKey, opts) {
  if (!handle || !layout) return;
  const { min = 300, minLeft = 340 } = opts || {};
  const saved = parseInt(localStorage.getItem(storeKey) || "", 10);
  if (saved) layout.style.setProperty(cssVar, saved + "px");
  handle.onmousedown = (e) => {
    e.preventDefault(); handle.classList.add("drag"); document.body.style.userSelect = "none";
    const rect = layout.getBoundingClientRect();
    const move = (ev) => {
      let w = rect.right - ev.clientX;
      w = Math.max(min, Math.min(w, rect.width - minLeft));
      layout.style.setProperty(cssVar, w + "px");
    };
    const up = () => {
      document.removeEventListener("mousemove", move); document.removeEventListener("mouseup", up);
      handle.classList.remove("drag"); document.body.style.userSelect = "";
      const w = parseInt(layout.style.getPropertyValue(cssVar), 10); if (w) localStorage.setItem(storeKey, w);
    };
    document.addEventListener("mousemove", move); document.addEventListener("mouseup", up);
  };
  handle.ondblclick = () => { layout.style.removeProperty(cssVar); localStorage.removeItem(storeKey); };
}

// ---------- websocket ----------
let claudeBusy = false;
function connectWS() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => el("conn").classList.add("ok");
  ws.onclose = () => { el("conn").classList.remove("ok"); setTimeout(connectWS, 1500); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "claude_busy") { claudeBusy = true; refresh(); }
    else if (m.type === "claude_idle") { claudeBusy = false; refresh(); }
    else if (Date.now() < suppressRefreshUntil) { /* self-originated in-place update — skip re-render */ }
    else refresh();
  };
}
let refreshTimer, suppressRefreshUntil = 0;
function suppressRefresh(ms = 1200) { suppressRefreshUntil = Date.now() + ms; }
function refresh() { clearTimeout(refreshTimer); refreshTimer = setTimeout(() => render(active), 120); }

// ============================================================ HOME
VIEWS.home = async () => {
  const [d, cal, mailStats] = await Promise.all([api("/overview"), api("/calendar").catch(() => null), api("/inbox/stats").catch(() => null)]);
  const pay = d.pay, nw = d.networth, food = d.food;
  const calPct = Math.min(100, Math.round((food.calories / Math.max(1, food.target)) * 100));
  const up = d.upcoming || [], ib = d.inbox || {};
  el("view-home").innerHTML = `
    <div class="viewhead"><h1>Good ${greeting()}</h1><div class="sub">${new Date().toLocaleDateString("en-US",{weekday:"long",month:"long",day:"numeric"})}</div><div class="spacer"></div>
      ${d.claude_available ? "" : `<span class="chip src">Claude CLI not detected</span>`}</div>
    ${up.length?`<div style="margin-top:16px">${up.map(r=>`<div class="reminder"><div class="rt">${esc(r.title)}</div><div class="rs">in ${r.minutes_until} min</div></div>`).join("")}</div>`:""}
    <div class="grid cols-3" style="margin-top:16px">
      <div class="card glass"><div class="ch">Open todos</div>
        <div class="bignum">${d.todos_open}</div>
        <div class="sublabel">${ib.drafts_pending?`${ib.drafts_pending} reply draft${ib.drafts_pending===1?"":"s"} to approve`:`${ib.needs_reply||0} emails need a reply`}</div></div>
      <div class="card glass"><div class="ch">${ICONS.food} Today's calories</div>
        <div class="ring-wrap">${ring(calPct, food.calories, food.target)}
          <div><div class="kpi"><div class="v">${food.remaining} left</div><div class="l">of ${food.target} kcal</div></div>
          <div class="sublabel">P ${food.protein}g · C ${food.carbs}g · F ${food.fat}g</div></div></div>
        <div class="row" style="margin-top:auto;padding-top:14px">
          <input id="home-meal" placeholder="Log a meal — what did you eat?" style="flex:1;min-width:0"/>
          <button class="btn-flow btn-sm" id="home-meal-log">Log</button></div>
        <div class="sublabel" style="color:var(--dim);margin-top:6px">Claude estimates calories & macros from a plain description.</div></div>
      ${mailStats?`<div class="card glass"><div class="ch">Email reading</div><div class="statlines">${statLines(mailStats)}</div></div>`:""}
    </div>
    <div class="card glass" style="margin-top:16px"><div class="ch">Net worth trend</div>${sparkline(nw.series)}</div>
    ${cal && cal.google && cal.google.state === "connected" ? `<div class="card glass" style="margin-top:16px"><div class="ch">This week</div>${weekView(cal.events || [], 34)}</div>` : ""}`;
  const logMeal = async () => {
    const txt = $("#home-meal").value.trim(); if (!txt) return;
    const btn = $("#home-meal-log"); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>';
    try {
      const r = await api("/health/food/estimate", { method: "POST", body: { message: txt } });
      if (!r.logged.length) toast(r.note || "No food found in that", "err");
      else toast(`Logged ${r.logged.map(f=>`${f.description} (${f.calories} kcal)`).join(" + ")}${r.note?` — ${r.note}`:""}`);
      render("home");
    } catch (e) { toast(e.message, "err"); btn.disabled = false; btn.textContent = "Log"; }
  };
  $("#home-meal-log").onclick = logMeal;
  $("#home-meal").onkeydown = (e) => { if (e.key === "Enter") logMeal(); };
};
const STAT_DEFS = [
  ["indexed","Indexed","--text"], ["gmail_unread","Available unread","--amber"], ["unread","Unread here","--flow"],
  ["important","Important","--blood"], ["awaiting_analysis","Awaiting analysis","--violet"], ["needs_reply","Need a reply","--gain"],
];
function statLines(st){
  return STAT_DEFS.map(([k,l,c])=>{ const v = k==="gmail_unread" ? (st.gmail_unread ?? "—") : (st[k] ?? "—");
    return `<div class="statline"><span class="sl-dot" style="background:var(${c})"></span><span class="sl-l">${l}</span><span class="sl-v" style="color:var(${c})">${v}</span></div>`; }).join("");
}
const greeting = () => { const h = new Date().getHours(); return h < 12 ? "morning" : h < 18 ? "afternoon" : "evening"; };
const fmtDate = (iso) => new Date(iso + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" });

// ============================================================ TODOS
VIEWS.todos = async () => {
  const todos = await api("/todos");
  const open = todos.filter((t) => !t.done), done = todos.filter((t) => t.done);
  const byCat = {};
  open.forEach((t) => (byCat[t.category] = byCat[t.category] || []).push(t));
  const cats = Object.keys(byCat).sort();
  el("view-todos").innerHTML = `
    <div class="viewhead"><h1>Todos</h1><div class="sub">${open.length} open · ${done.length} done</div><div class="spacer"></div>
      ${claudeBusy ? busy("Reading image") : ""}</div>
    <div class="grid cols-2" style="margin-top:16px">
      <div class="card glass" style="min-height:0">
        ${cats.length ? cats.map((c) => `
          <div class="todocat"><div class="cat-h">${esc(c)} <span class="chip p-low">${byCat[c].length}</span></div>
          ${byCat[c].map(todoRow).join("")}</div>`).join("") : `<div class="empty">${ICONS.check}<div>Nothing yet — add a task or drop an image.</div></div>`}
        ${done.length ? `<div class="todocat"><div class="cat-h">Completed</div>${done.slice(0,8).map(todoRow).join("")}</div>` : ""}
      </div>
      <div style="display:flex; flex-direction:column; gap:16px">
        <div class="card glass"><div class="ch">Add a task</div>
          <div class="row"><input id="t-title" placeholder="What needs doing?" style="flex:1"/></div>
          <div class="row" style="margin-top:10px">
            <input id="t-cat" placeholder="Category" value="Inbox" style="flex:1"/>
            <select id="t-pri"><option value="normal">Normal</option><option value="high">High</option><option value="low">Low</option></select>
            <input id="t-due" type="date" />
            <button class="btn-flow" id="t-add">Add</button></div></div>
        <div class="card glass"><div class="ch">${ICONS.image} From an image</div>
          <div class="dropzone" id="dz"><div>${ICONS.image}</div><div><b>Drop a photo</b> or click to choose</div>
            <div style="font-size:12px;color:var(--dim);margin-top:4px">A local Claude session reads it and extracts your todos.</div></div>
          <input type="file" id="dz-file" accept="image/*" hidden /></div>
      </div>
    </div>`;
  $("#t-add").onclick = addTodo;
  $("#t-title").onkeydown = (e) => { if (e.key === "Enter") addTodo(); };
  setupDrop("#dz", "#dz-file", "/todos/ingest-image", (r) => toast(`Added ${r.created.length} todo${r.created.length===1?"":"s"}`));
};
function todoRow(t) {
  return `<div class="todo ${t.done ? "done" : ""}" data-id="${t.id}">
    <div class="cb" data-act="toggle">${ICONS.check}</div>
    <div class="body"><div class="task">${esc(t.title)}</div>
      <div class="meta">
        ${t.priority === "high" ? '<span class="chip p-high">High</span>' : ""}
        ${t.due ? `<span class="chip due">${esc(t.due)}</span>` : ""}
        ${t.source !== "manual" ? `<span class="chip src">${esc(t.source)}</span>` : ""}</div></div>
    <button class="del" data-act="del" title="Delete">${ICONS.trash}</button></div>`;
}
async function addTodo() {
  const title = $("#t-title").value.trim(); if (!title) return;
  try { await api("/todos", { method: "POST", body: { title, category: $("#t-cat").value.trim() || "Inbox", priority: $("#t-pri").value, due: $("#t-due").value || null } }); toast("Todo added"); }
  catch (e) { toast(e.message, "err"); }
  $("#t-title").value = "";
}
document.addEventListener("click", async (e) => {
  const t = e.target.closest(".todo[data-id]"); if (!t) return;
  const act = e.target.closest("[data-act]")?.dataset.act; const id = t.dataset.id;
  if (act === "toggle") {
    const willDone = !t.classList.contains("done");
    if (willDone) { const cb = t.querySelector(".cb"); const r = cb.getBoundingClientRect(); celebrate(r.left + r.width / 2, r.top + r.height / 2); t.classList.add("just-done", "done"); }
    await api(`/todos/${id}`, { method: "PATCH", body: { done: willDone } });
  } else if (act === "del") await del(`/todos/${id}`, "Todo deleted");
});
// Delegated click for calendar events (week view + all-day chips) → offer delete.
document.addEventListener("click", async (e) => {
  const ev = e.target.closest("[data-ev]"); if (!ev) return;
  const id = ev.dataset.ev, title = ev.dataset.title || "this event";
  if (confirm(`Delete “${title}” from your Google Calendar?`)) {
    try { await api(`/calendar/events/${encodeURIComponent(id)}`, { method: "DELETE" }); toast("Event deleted"); }
    catch (err) { toast(err.message, "err"); }
  }
});
function celebrate(x, y) {
  const wrap = document.createElement("div"); wrap.className = "confetti-wrap"; document.body.appendChild(wrap);
  const colors = ["#2bff9a", "#7dffc0", "#43e0ff", "#ffffff", "#ffcf5e"];
  for (let i = 0; i < 36; i++) {
    const p = document.createElement("div"); p.className = "confetti";
    const ang = Math.random() * Math.PI * 2, dist = 60 + Math.random() * 150;
    p.style.setProperty("--dx", (Math.cos(ang) * dist).toFixed(0) + "px");
    p.style.setProperty("--dy", (Math.sin(ang) * dist - 50).toFixed(0) + "px");
    p.style.setProperty("--dr", (Math.random() * 720 - 360) + "deg");
    p.style.left = x + "px"; p.style.top = y + "px"; p.style.background = colors[i % colors.length];
    if (i % 3 === 0) p.style.borderRadius = "50%";
    wrap.appendChild(p);
  }
  setTimeout(() => wrap.remove(), 1000);
}

// ============================================================ MONEY (paycheck + portfolio)
VIEWS.money = async () => {
  const [pay, pf, nw, hw, wl, chk] = await Promise.all([api("/pay"), api("/portfolio"), api("/networth"), api("/hours-weeks"), api("/watchlist"), api("/pay/checks")]);
  const r = pay.running, p = pay.projected, v = pf.valuation;
  const cash = (nw.accounts || []).filter(a => a.type === "cash").reduce((s, a) => s + a.balance, 0);
  const cashAccts = (nw.accounts || []).filter(a => a.type === "cash");
  el("view-money").innerHTML = `
    <div class="viewhead"><h1>Money</h1><div class="sub">period ${fmtDate(pay.period_start)} – ${fmtDate(pay.period_end)}</div></div>
    <div class="grid cols-3" style="margin-top:16px">
      <div class="card glass"><div class="ch">Running net pay</div>
        <div class="bignum">${money(r.net)}</div>
        <div class="sublabel">${r.reg_hours} reg + ${r.ot_hours} OT h · gross ${money(r.gross)}</div></div>
      <div class="card glass"><div class="ch">Next payday</div>
        <div class="bignum sm">${new Date(pay.next_payday+"T00:00:00").toLocaleDateString("en-US",{weekday:"short",month:"short",day:"numeric"})}</div>
        <div class="sublabel">in ${pay.days_until_payday} days</div></div>
      <div class="card glass"><div class="ch">Projected check</div>
        <div class="bignum sm">${money(p.net)}</div>
        <div class="sublabel">full period · ${(p.reg_hours + p.ot_hours).toFixed(2)} h</div></div>
    </div>
    <div class="grid cols-2" style="margin-top:16px">
      <div class="card glass"><div class="ch">Take-home (this period)</div>
        ${taxRow("Gross", p.gross)}
        ${taxRow(`Tax · ${(( p.effective_rate||0)*100).toFixed(2)}% effective`, -p.total_tax)}
        <div style="border-top:1px solid var(--hair);margin:8px 0"></div>${taxRow("Net", p.net, true)}
        <div class="sublabel" style="margin-top:8px;color:var(--dim)">${p.mode==="effective"?"Flat effective NYC rate from your actual paystub.":"Bracket estimate — set an effective rate in Settings for paystub accuracy."} Adjust in Settings.</div></div>
      <div class="card glass"><div class="ch">Hours by week — this period</div>
        ${weeksInPeriod(pay.period_start, pay.period_end).map(w => { const ov = hw[w.start] || {}; return `
          <div class="row" style="margin-bottom:10px"><div style="flex:1"><b>${w.label}</b></div>
            <label class="fld" style="flex-direction:row;align-items:center;gap:6px">Reg<input id="hw-${w.start}-r" type="number" step="0.01" value="${ov.regular ?? ""}" placeholder="0" style="width:80px"/></label>
            <label class="fld" style="flex-direction:row;align-items:center;gap:6px">OT<input id="hw-${w.start}-o" type="number" step="0.01" value="${ov.overtime ?? ""}" placeholder="0" style="width:80px"/></label>
            <button class="btn-flow btn-sm" onclick="saveWeek('${w.start}')">Save</button></div>`; }).join("")}
        <div class="sublabel" style="color:var(--dim)">Enter your actual regular + overtime hours per payroll week (Sun–Sat).</div></div>
    </div>
    <div class="grid cols-2" style="margin-top:16px;align-items:start">
      <div class="card glass"><div class="ch">Unpaid checks
          ${chk.unpaid_total?`<span class="count" style="background:var(--amber-soft);color:var(--amber)">${money(chk.unpaid_total)} owed</span>`:""}
          ${cashAccts.length>1?`<select id="chk-acct" class="btn-sm" style="margin-left:auto" title="Deposits go to">${cashAccts.map(a=>`<option value="${a.id}">${esc(a.name)}</option>`).join("")}</select>`:""}</div>
        ${chk.checks.length?chk.checks.map(c=>`
          <div class="check-row ${c.state}">
            <div class="ck-main"><b>${fmtDate(c.period_start)} – ${fmtDate(c.period_end)}</b>
              <span class="sublabel">payday ${fmtDate(c.payday)} · ${(c.reg_hours+c.ot_hours).toFixed(2)} h${c.ot_hours?` (${c.ot_hours} OT)`:""}</span></div>
            <div class="ck-amt">${money(c.state==="deposited"&&c.deposit?c.deposit.amount:c.net)}${c.state==="deposited"&&c.deposit&&!c.deposit.actual?"":c.state==="deposited"?'<span class="sublabel" style="display:block">actual</span>':'<span class="sublabel" style="display:block">est.</span>'}</div>
            ${c.state==="unpaid"
              ?`<label class="toggle" title="Money hit the bank?"><input type="checkbox" data-deposit="${c.period_start}" data-net="${c.net}"/><span class="sw"></span></label>`
              :`<span class="chip src" title="Deposited to ${esc(c.deposit?.account||"")}">in bank ✓</span>
                <button class="del" style="opacity:.6" data-undo="${c.period_start}" title="Undo deposit">${ICONS.trash}</button>`}
          </div>`).join(""):`<div class="sublabel" style="color:var(--dim)">No completed pay periods with hours yet.</div>`}
        <div class="check-row accruing">
          <div class="ck-main"><b>${fmtDate(chk.current.period_start)} – ${fmtDate(chk.current.period_end)}</b>
            <span class="sublabel">accruing now · ${(chk.current.reg_hours+chk.current.ot_hours).toFixed(2)} h so far · pays ${fmtDate(chk.current.payday)}</span></div>
          <div class="ck-amt" style="color:var(--flow)">${money(chk.current.net)}</div>
          <span class="chip due">accruing</span></div>
        <div class="sublabel" style="color:var(--dim);margin-top:8px">Computed from your clocked hours per pay period. Toggle when a check lands — it converts into your cash balance.</div></div>
      <div class="card glass"><div class="ch">${ICONS.image} Update from screenshots</div>
        <div class="dropzone" id="bank-dz"><div>${ICONS.image}</div><div><b>Bank screenshot</b> (BofA etc.) — Claude updates account balances</div></div>
        <input type="file" id="bank-dz-file" accept="image/*" hidden/>
        <div class="dropzone" id="tc-dz" style="margin-top:12px"><div>${ICONS.image}</div><div><b>Timecard screenshot</b> — Claude fills the weekly hours above</div></div>
        <input type="file" id="tc-dz-file" accept="image/*" hidden/></div>
    </div>
    <div class="card glass" style="margin-top:16px"><div class="ch">Portfolio
        ${v.total_unrealized!=null?`<span class="count" style="background:${v.total_unrealized>=0?"var(--gain-soft)":"var(--loss-soft)"};color:${v.total_unrealized>=0?"var(--gain)":"var(--loss)"}">${money(v.total_unrealized,true)}</span>`:""}
        <button class="btn-ghost btn-sm" id="pf-refresh" style="margin-left:auto">Refresh prices</button></div>
      <div class="bignum sm" style="margin-bottom:8px">${money(v.total_value)}${cash>0?` <span style="font-size:16px;color:var(--flow);font-weight:600">+ ${money(cash)} cash</span>`:""}</div>
      ${v.unpriced_symbols.length?`<div class="sublabel" style="color:var(--amber)">Showing cost basis for ${v.unpriced_symbols.join(", ")} — hit “Refresh prices” to pull live from Yahoo.</div>`:`<div class="sublabel" style="color:var(--dim)">Live prices from Yahoo Finance.</div>`}
      ${v.positions.length?`<table style="margin-top:12px"><thead><tr><th>Symbol</th><th class="num">Qty</th><th class="num">Price</th><th class="num">Value</th><th class="num">Unrealized</th><th></th></tr></thead><tbody>
        ${v.positions.map(posRow).join("")}</tbody></table>`:`<div class="empty">${ICONS.check}<div>No holdings. Add one or import orders below.</div></div>`}
      <div class="row" style="margin-top:14px">
        <input id="pf-sym" placeholder="AAPL" style="width:90px;text-transform:uppercase"/>
        <input id="pf-qty" type="number" placeholder="shares" style="width:100px"/>
        <input id="pf-cost" type="number" placeholder="avg cost" style="width:110px"/>
        <input id="pf-price" type="number" placeholder="cur. price" style="width:110px"/>
        <button class="btn-flow btn-sm" id="pf-add">Add holding</button>
        <span style="flex:1"></span>
        <button class="btn-ghost btn-sm" id="pf-import">Import orders CSV…</button></div>
    </div>
    <div class="card glass" style="margin-top:16px"><div class="ch">Next Stock Picks <span class="count">${wl.items.length}</span>
        <button class="btn-ghost btn-sm" id="wl-refresh" style="margin-left:auto">Refresh prices</button></div>
      <div class="sublabel" style="color:var(--dim);margin-bottom:12px">Manual watchlist. For fundamentals-ranked candidates, see the <b>Picks</b> tab.</div>
      ${wl.items.length?`<table><thead><tr><th>Symbol</th><th class="num">Price</th><th>Thesis</th><th></th></tr></thead><tbody>
        ${wl.items.map(w=>`<tr><td><span class="sym">${esc(w.symbol)}</span></td><td class="num">${w.price!=null?money(w.price):"—"}</td><td style="color:var(--muted)">${esc(w.note||"")}</td>
          <td class="num"><button class="del" style="opacity:.6" onclick="delWatch('${esc(w.symbol)}')">${ICONS.trash}</button></td></tr>`).join("")}</tbody></table>`
        :`<div class="empty">${ICONS.check}<div>No candidates yet. Add tickers you're eyeing.</div></div>`}
      <div class="row" style="margin-top:14px">
        <input id="wl-sym" placeholder="Ticker" style="width:100px;text-transform:uppercase"/>
        <input id="wl-note" placeholder="why you're watching it" style="flex:1"/>
        <button class="btn-flow btn-sm" id="wl-add">Add pick</button></div>
    </div>`;
  // portfolio
  $("#pf-add").onclick = async () => { const s = $("#pf-sym").value.trim(); if (!s) return; try { await api("/portfolio/holdings", { method: "POST", body: { symbol: s, qty: +$("#pf-qty").value || 0, cost_basis: +$("#pf-cost").value || null, last_price: +$("#pf-price").value || null } }); toast(`Added ${s.toUpperCase()}`); } catch (e) { toast(e.message, "err"); } };
  $("#pf-import").onclick = importOrders;
  $("#pf-refresh").onclick = async (e) => { e.target.disabled=true; e.target.innerHTML='<span class="spinner"></span> Fetching…'; try{ const r = await api("/portfolio/refresh", {method:"POST"}); toast(r.updated?`Updated ${r.updated} price${r.updated===1?"":"s"} from Yahoo${r.failed?.length?` · failed: ${r.failed.join(", ")}`:""}`:"No prices updated", r.updated?"ok":"err"); }catch(err){ toast(err.message,"err"); } render("money"); };
  $("#wl-add").onclick = async () => { const s=$("#wl-sym").value.trim(); if(!s) return; try{ await api("/watchlist",{method:"POST",body:{symbol:s, note:$("#wl-note").value.trim()}}); toast(`Watching ${s.toUpperCase()}`); }catch(e){ toast(e.message,"err"); } render("money"); };
  $("#wl-refresh").onclick = async (e) => { e.target.disabled=true; e.target.innerHTML='<span class="spinner"></span>'; try{ const rr=await api("/watchlist/refresh",{method:"POST"}); toast(`Priced ${rr.updated} pick${rr.updated===1?"":"s"}`); }catch(err){ toast(err.message,"err"); } render("money"); };
  // unpaid checks: toggle → deposit into cash; trash on a deposited row undoes it
  el("view-money").querySelectorAll("[data-deposit]").forEach(t => t.onchange = async () => {
    const est = t.dataset.net;
    const val = prompt("Actual net that hit the bank?\nFrom your paystub — Atlas's withholding is only an estimate.\nLeave as-is to use the estimate.", est);
    if (val === null) { t.checked = false; return; }          // cancelled
    const amount = parseFloat(val);
    const acct = $("#chk-acct");
    try {
      const r = await api("/pay/checks/deposit", { method: "POST", body: { period_start: t.dataset.deposit, account_id: acct ? +acct.value : null, amount: isFinite(amount) ? amount : null } });
      toast(`${money(r.deposited)} → ${r.account}${r.actual ? " (actual)" : ""}`);
    } catch (e) { toast(e.message, "err"); }
    render("money");
  });
  el("view-money").querySelectorAll("[data-undo]").forEach(b => b.onclick = async () => {
    if (!confirm("Undo this deposit? The amount will be subtracted from the account.")) return;
    try { const r = await api("/pay/checks/undo", { method: "POST", body: { period_start: b.dataset.undo } }); toast(`Reversed ${money(r.reversed)}`); }
    catch (e) { toast(e.message, "err"); }
    render("money");
  });
  // screenshot ingestion
  setupDrop("#bank-dz", "#bank-dz-file", "/networth/ingest-image", (r) => {
    const bits = [...r.updated.map(u=>`${u.name} → ${money(u.balance)}`), ...r.created.map(c=>`+ ${c.name} (${money(c.balance)})`)];
    toast(bits.length ? `Updated: ${bits.join(" · ")}` : (r.note || "No balances found"), bits.length ? "ok" : "err");
    render("money");
  });
  setupDrop("#tc-dz", "#tc-dz-file", "/pay/ingest-timecard", (r) => {
    toast(r.weeks.length ? `Hours saved: ${r.weeks.map(w=>`wk ${fmtDate(w.week_start)}: ${w.regular}+${w.overtime}OT`).join(" · ")}` : (r.note || "No hours found"), r.weeks.length ? "ok" : "err");
    render("money");
  });
};
const taxRow = (label, n, strong) => `<div class="row" style="justify-content:space-between;padding:5px 0"><span style="${strong?"font-weight:700":"color:var(--muted)"}">${label}</span><span class="${strong?"":""}" style="font-variant-numeric:tabular-nums;${strong?"font-weight:700":""}">${money(n, false)}</span></div>`;
function posRow(p) {
  const u = p.unrealized;
  return `<tr><td><span class="sym">${esc(p.symbol)}</span>${p.source==="cost"?' <span class="tag">cost</span>':""}</td>
    <td class="num">${p.qty}</td><td class="num">${money(p.price)}</td><td class="num">${money(p.value)}</td>
    <td class="num ${u==null?"":u>=0?"pos":"neg"}">${u==null?"—":money(u,true)}</td>
    <td class="num"><button class="del" style="opacity:.7" onclick="delHolding(${p.id})">${ICONS.trash}</button></td></tr>`;
}
window.delHolding = (id) => del(`/portfolio/holdings/${id}`, "Holding removed");
window.delWatch = async (s) => { try { await api(`/watchlist/${encodeURIComponent(s)}`, { method: "DELETE" }); toast("Removed from watchlist"); } catch (e) { toast(e.message, "err"); } render("money"); };
function weeksInPeriod(startIso, endIso){
  const s=new Date(startIso+"T00:00:00"), e=new Date(endIso+"T00:00:00"), out=[]; let d=new Date(s);
  while(d<=e){ const ws=d.toISOString().slice(0,10); const we=new Date(d); we.setDate(we.getDate()+6);
    out.push({start:ws, label:`${fmtDate(ws)} – ${fmtDate(we.toISOString().slice(0,10))}`}); d.setDate(d.getDate()+7); }
  return out;
}
window.saveWeek = async (ws) => {
  const r=+$(`#hw-${ws}-r`).value||0, o=+$(`#hw-${ws}-o`).value||0;
  try{ await api("/hours-weeks",{method:"PUT",body:{week_start:ws, regular:r, overtime:o}}); toast(`Saved ${r}h reg + ${o}h OT`); }catch(e){ toast(e.message,"err"); }
};
async function importOrders() {
  const csv = prompt("Paste order CSV (with symbol, side, quantity, price[, date] columns):");
  if (!csv) return;
  try { const r = await api("/portfolio/import-orders", { method: "POST", body: { csv } }); toast(r.error ? r.error : `Imported ${r.imported} orders`, r.error ? "err" : "ok"); }
  catch (e) { toast(e.message, "err"); }
}

// ============================================================ NET WORTH
VIEWS.networth = async () => {
  const d = await api("/networth");
  el("view-networth").innerHTML = `
    <div class="viewhead"><h1>Net Worth</h1><div class="spacer"></div><button class="btn-ghost btn-sm" id="snap">Snapshot now</button></div>
    <div class="grid cols-3" style="margin-top:16px">
      <div class="card glass span-2"><div class="ch">Total net worth</div>
        <div class="bignum">${money(d.total)}</div>
        <div class="sublabel">assets ${money0(d.assets)} · brokerage ${money0(d.brokerage)} · debts ${money0(d.debts)}</div>
        ${sparkline(d.series)}</div>
      <div class="card glass"><div class="ch">Add account</div>
        <label class="fld">Name<input id="a-name" placeholder="Checking"/></label>
        <label class="fld" style="margin-top:10px">Type<select id="a-type"><option value="cash">Cash</option><option value="savings">Savings</option><option value="debt">Debt</option><option value="other">Other</option></select></label>
        <label class="fld" style="margin-top:10px">Balance<input id="a-bal" type="number" placeholder="0"/></label>
        <button class="btn-flow" id="a-add" style="margin-top:14px">Add account</button></div>
    </div>
    <div class="card glass" style="margin-top:16px"><div class="ch">Accounts</div>
      ${d.accounts.length?`<table><thead><tr><th>Account</th><th>Type</th><th class="num">Balance (click to edit)</th><th></th></tr></thead><tbody>
        ${d.accounts.map((a)=>`<tr><td>${esc(a.name)}</td><td><span class="tag">${a.type}</span></td>
          <td class="num"><input class="bal-edit ${a.type==="debt"?"neg":""}" data-acct="${a.id}" type="number" step="0.01" value="${a.balance}"/></td>
          <td class="num"><button class="del" style="opacity:.7" onclick="delAccount(${a.id})">${ICONS.trash}</button></td></tr>`).join("")}
        <tr><td><b>Brokerage</b> <span class="tag">live</span></td><td></td><td class="num"><b title="Priced live from holdings — not editable">${money(d.brokerage)}</b></td><td></td></tr>
        </tbody></table>`:`<div class="empty">${ICONS.check}<div>No accounts yet.</div></div>`}</div>`;
  $("#a-add").onclick = async () => { const n = $("#a-name").value.trim(); if (!n) return; try { await api("/accounts", { method: "POST", body: { name: n, type: $("#a-type").value, balance: +$("#a-bal").value || 0 } }); toast("Account added"); } catch (e) { toast(e.message, "err"); } };
  $("#snap").onclick = async () => { await api("/networth/snapshot", { method: "POST" }); toast("Snapshot saved"); };
  // Inline balance editing (brokerage excluded — it's computed live from holdings).
  el("view-networth").querySelectorAll(".bal-edit").forEach(inp => inp.onchange = async () => {
    try { await api(`/accounts/${inp.dataset.acct}`, { method: "PATCH", body: { balance: +inp.value || 0 } }); toast("Balance updated"); }
    catch (e) { toast(e.message, "err"); }
    render("networth");
  });
};
window.delAccount = (id) => del(`/accounts/${id}`, "Account removed");

// ============================================================ HEALTH
VIEWS.health = async () => {
  const [food, wk, st, chat] = await Promise.all([api("/health/food"), api("/health/workouts"), api("/health/strength"), api("/health/chat")]);
  const calPct = Math.min(100, Math.round((food.calories / Math.max(1, food.target)) * 100));
  const bw = st.bodyweight || {}, strength = st.strength || [], rec = st.recovery;
  const recStage = rec ? (rec.recovery === "good" ? "offer" : rec.recovery === "low" ? "rejected" : "screen") : "";
  el("view-health").innerHTML = `
    <div class="viewhead"><h1>Health</h1><div class="sub">${food.calories} / ${food.target} kcal today</div><div class="spacer"></div>${claudeBusy?busy("Thinking"):""}</div>
    <div class="grid cols-2" style="margin-top:16px">
      <div class="card glass"><div class="ch">${ICONS.food} Food log</div>
        <div class="ring-wrap" style="margin-bottom:14px">${ring(calPct, food.calories, food.target)}
          <div class="kpi"><div class="v">${food.remaining} left</div><div class="l">P ${food.protein} · C ${food.carbs} · F ${food.fat} g</div></div></div>
        <div class="row"><input id="f-desc" placeholder="e.g. chicken bowl" style="flex:1"/><input id="f-cal" type="number" placeholder="kcal" style="width:90px"/><button class="btn-flow btn-sm" id="f-add">Add</button></div>
        <div class="dropzone" id="fdz" style="margin-top:12px"><div>${ICONS.image}</div><div><b>Snap a meal</b> — Claude estimates calories</div></div>
        <input type="file" id="fdz-file" accept="image/*" hidden/>
        <div style="margin-top:8px">${food.items.map((i)=>`<div class="list-item"><div class="ic">${ICONS.food}</div><div class="main"><div class="t">${esc(i.description)}</div><div class="s">${i.calories??"—"} kcal${i.source!=="manual"?" · "+i.source:""}</div></div><button class="del" style="opacity:.7" onclick="delFood(${i.id})">${ICONS.trash}</button></div>`).join("")}</div></div>
      <div class="card glass"><div class="ch">${ICONS.dumbbell} Workouts <span class="count">${wk.week_count} this wk · ${wk.week_minutes}m</span></div>
        <div class="row"><input id="w-type" placeholder="Push day" style="flex:1"/><input id="w-dur" type="number" placeholder="min" style="width:80px"/><button class="btn-flow btn-sm" id="w-add">Log</button></div>
        <div class="row" style="margin-top:10px"><input id="w-text" placeholder="…or describe it: '45 min upper body, felt strong'" style="flex:1"/><button class="btn-ghost btn-sm" id="w-parse">Parse</button></div>
        <div style="margin-top:12px">${wk.workouts.map((w)=>`<div class="list-item"><div class="ic" style="background:var(--blood-soft);color:var(--blood-2)">${ICONS.dumbbell}</div><div class="main"><div class="t">${esc(w.type)}</div><div class="s">${w.duration_min?w.duration_min+" min":""}${w.calories_burned?` · ${w.calories_burned} kcal`:""} · ${new Date(w.ts*1000).toLocaleDateString()}</div></div><button class="del" style="opacity:.7" onclick="delWorkout(${w.id})">${ICONS.trash}</button></div>`).join("")||`<div class="sublabel" style="color:var(--dim)">No workouts logged yet.</div>`}</div></div>
    </div>
    <div class="grid cols-2" style="margin-top:16px">
      <div class="card glass"><div class="ch">Bodyweight ${bw.latest?`<span class="count">${bw.latest} lb${bw.change!=null?` · Δ${bw.change>0?"+":""}${bw.change}`:""}</span>`:""}</div>
        ${sparkVals((bw.series||[]).map(s=>s.weight))}
        <div class="row" style="margin-top:12px"><input id="bw-val" type="number" step="0.1" placeholder="weight (lb)" style="flex:1"/><button class="btn-flow btn-sm" id="bw-add">Log</button></div></div>
      <div class="card glass"><div class="ch">Strength — weekly top sets <span class="count">${strength.length}</span></div>
        <div style="max-height:300px;overflow-y:auto">${strength.length?strength.map(s=>`
          <div style="margin-bottom:14px">
            <div class="row" style="justify-content:space-between"><b>${esc(s.exercise)}</b><span class="sublabel" style="margin:0">${s.current?`${s.current} lb top`:""}</span></div>
            ${sparkVals(s.series.map(w=>w.max_weight), "var(--flow)", 60)}
            ${s.suggestion?`<div class="sublabel" style="color:var(--neon)">Next: <b>${s.suggestion.weight} lb</b> — ${esc(s.suggestion.note)}</div>`:""}</div>`).join(""):`<div class="sublabel" style="color:var(--dim)">Log lifts to chart progression + get next-week targets.</div>`}</div>
        <div class="row" style="margin-top:8px"><input id="lf-ex" placeholder="Exercise" style="flex:1"/><input id="lf-wt" type="number" placeholder="lb" style="width:80px"/><input id="lf-reps" type="number" placeholder="reps" style="width:80px"/><button class="btn-flow btn-sm" id="lf-add">Log lift</button></div></div>
    </div>
    <div class="grid cols-2" style="margin-top:16px">
      <div class="card glass"><div class="ch">Recovery insights <button class="btn-ghost btn-sm" id="rec-refresh" style="margin-left:auto">${rec?"Refresh":"Generate"}</button></div>
        ${rec?`<div style="margin-bottom:8px"><span class="stage ${recStage}">${rec.recovery} recovery</span></div>
          <div class="brief">${esc(rec.insight)}</div>
          ${rec.workout_suggestion?`<div class="sublabel" style="color:var(--neon);margin-top:12px"><b>Suggested next workout:</b> ${esc(rec.workout_suggestion)}</div>`:""}`
         :`<div class="sublabel" style="color:var(--dim)">Generate recovery insights from your food, workouts & bodyweight — includes a workout suggestion.</div>`}</div>
      <div class="card glass" style="display:flex;flex-direction:column"><div class="ch">Coach chat</div>
        <div id="hc-log" style="flex:1;max-height:280px;min-height:120px;overflow-y:auto;display:flex;flex-direction:column;gap:8px;margin-bottom:10px">
          ${(chat.history||[]).map(m=>`<div class="msgbub ${m.role}">${esc(m.text)}</div>`).join("")||`<div class="sublabel" style="color:var(--dim)">Text me what you ate (I'll log it) or ask about recovery & training.</div>`}</div>
        <div class="row"><input id="hc-in" placeholder="e.g. had chicken & rice for lunch" style="flex:1"/><button class="btn-flow btn-sm" id="hc-send">Send</button></div></div>
    </div>`;
  $("#f-add").onclick = async () => { const dsc = $("#f-desc").value.trim(); if (!dsc) return; try { await api("/health/food", { method: "POST", body: { description: dsc, calories: +$("#f-cal").value || null } }); toast("Food logged"); } catch (e) { toast(e.message, "err"); } };
  $("#w-add").onclick = async () => { const ty = $("#w-type").value.trim(); if (!ty) return; try { await api("/health/workouts", { method: "POST", body: { type: ty, duration_min: +$("#w-dur").value || null } }); toast("Workout logged"); } catch (e) { toast(e.message, "err"); } };
  $("#w-parse").onclick = async () => {
    const txt = $("#w-text").value.trim(); if (!txt) return;
    const b = $("#w-parse"); b.disabled = true; b.innerHTML = '<span class="spinner"></span>';
    try {
      const r = await api("/health/workouts/parse", { method: "POST", body: { text: txt } });
      const bits = [`Logged ${r.type}`];
      if (r.lifts_logged?.length) bits.push(`${r.lifts_logged.join(", ")} → strength`);
      if (r.bodyweight_logged) bits.push(`bodyweight ${r.bodyweight_logged} lb`);
      if (r.recovery_logged) bits.push("recovery noted → coach");
      toast(bits.join(" · "));
      $("#w-text").value = "";
    } catch (e) { toast(e.message, "err"); }
    finally { b.disabled = false; b.textContent = "Parse"; }
  };
  $("#bw-add").onclick = async () => { const w=+$("#bw-val").value; if(!w) return; try{ await api("/health/bodyweight",{method:"POST",body:{weight:w}}); toast("Bodyweight logged"); }catch(e){ toast(e.message,"err"); } };
  $("#lf-add").onclick = async () => { const ex=$("#lf-ex").value.trim(), wt=+$("#lf-wt").value; if(!ex||!wt){ toast("Exercise + weight needed","err"); return; } try{ await api("/health/lifts",{method:"POST",body:{exercise:ex, weight:wt, reps:+$("#lf-reps").value||null}}); toast(`Logged ${ex} @ ${wt}`); }catch(e){ toast(e.message,"err"); } };
  $("#rec-refresh").onclick = async (e) => { e.target.disabled=true; e.target.innerHTML='<span class="spinner"></span>'; try{ await api("/health/recovery",{method:"POST"}); toast("Recovery insights updated"); }catch(err){ toast(err.message,"err"); } render("health"); };
  const send = async () => { const m=$("#hc-in").value.trim(); if(!m) return; $("#hc-in").value=""; $("#hc-log").insertAdjacentHTML("beforeend",`<div class="msgbub user">${esc(m)}</div><div class="msgbub ai" id="hc-pending"><span class="spinner"></span></div>`); const log=$("#hc-log"); log.scrollTop=log.scrollHeight; try{ const r=await api("/health/chat",{method:"POST",body:{message:m}}); if(r.logged?.length) toast(`Logged: ${r.logged.join(", ")}`); }catch(e){ toast(e.message,"err"); } render("health"); };
  $("#hc-send").onclick = send;
  $("#hc-in").onkeydown = (e) => { if(e.key==="Enter") send(); };
  const log=$("#hc-log"); if(log) log.scrollTop=log.scrollHeight;
  setupDrop("#fdz", "#fdz-file", "/health/food/ingest-image", (r) => toast(`Logged ${r.description||"meal"} · ${r.calories??"?"} kcal`));
};
window.delFood = (id) => del(`/health/food/${id}`, "Removed");
window.delWorkout = (id) => del(`/health/workouts/${id}`, "Removed");

// ============================================================ SETTINGS
VIEWS.settings = async () => {
  const pay = await api("/pay"); const c = pay.config;
  const target = (await api("/settings/calorie_target")).value ?? 2200;
  const imsg = (await api("/settings/imessage_handle")).value ?? "";
  el("view-settings").innerHTML = `
    <div class="viewhead"><h1>Settings</h1></div>
    <div class="grid cols-2" style="margin-top:16px">
      <div class="card glass"><div class="ch">Pay</div>
        <label class="fld">Hourly rate ($)<input id="s-rate" type="number" step="0.01" value="${c.rate}"/></label>
        <label class="fld" style="margin-top:10px">OT multiplier<input id="s-ot" type="number" step="0.1" value="${c.ot_multiplier}"/></label>
        <label class="fld" style="margin-top:10px">Pay schedule<select id="s-schedule">
          <option value="semimonthly" ${c.pay_schedule==="semimonthly"?"selected":""}>Semi-monthly (15th & 30th)</option>
          <option value="biweekly" ${c.pay_schedule!=="semimonthly"?"selected":""}>Biweekly (every 14 days)</option></select></label>
        <label class="fld" style="margin-top:10px">Anchor payday (biweekly only)<input id="s-anchor" type="date" value="${c.anchor_payday||""}"/></label>
        <label class="fld" style="margin-top:10px">Unpaid break (min/day)<input id="s-break" type="number" value="${c.default_break_min}"/></label>
        <label class="fld" style="margin-top:10px">Effective tax rate (%) — all-in from your paystub<input id="s-efftax" type="number" step="0.01" value="${c.effective_tax_rate!=null?(c.effective_tax_rate*100).toFixed(2):""}" placeholder="e.g. 18.76"/></label>
        <div class="sublabel" style="color:var(--dim);margin-top:4px">Replaces the bracket estimate (fed + FICA + NY + NYC). Leave blank to estimate.</div>
        <label class="toggle" style="margin-top:12px"><input type="checkbox" id="s-sched" ${c.use_default_schedule?"checked":""}/><span class="sw"></span> Auto-fill default schedule (Mon–Thu 8–8, Fri 8–4)</label>
        <button class="btn-flow" id="s-save" style="margin-top:16px">Save pay settings</button></div>
      <div class="card glass"><div class="ch">Health, reminders & data</div>
        <label class="fld">Daily calorie target<input id="s-cal" type="number" value="${target}"/></label>
        <button class="btn-flow btn-sm" id="s-calsave" style="margin-top:12px">Save target</button>
        <label class="fld" style="margin-top:18px">iMessage handle for reminders (phone/email — leave blank for notifications only)<input id="s-imsg" value="${esc(imsg)}" placeholder="e.g. +15551234567"/></label>
        <button class="btn-flow btn-sm" id="s-imsgsave" style="margin-top:12px">Save handle</button>
        <div class="sublabel" style="margin-top:18px;color:var(--dim)">Claude bridge: <span id="claude-stat"></span></div>
        <div class="sublabel" style="color:var(--dim)">Data lives in ~/Library/Application Support/Atlas/atlas.db · all local.</div></div>
      <div class="card glass"><div class="ch">Layout presets</div>
        <div class="sublabel" style="color:var(--dim);margin-bottom:12px">Drag any widget by its header to rearrange; use ⇔ to toggle width. Save the arrangement as a named preset.</div>
        <div class="row"><select id="s-preset" style="flex:1">${Object.keys(LAYOUT.presets).map(n=>`<option ${n===LAYOUT.activeName?"selected":""}>${esc(n)}</option>`).join("")}</select>
          <button class="btn-ghost btn-sm" id="s-preset-apply">Apply</button>
          <button class="del" id="s-preset-del" title="Delete preset">${ICONS.trash}</button></div>
        <div class="row" style="margin-top:12px"><input id="s-preset-name" placeholder="Preset name" style="flex:1"/>
          <button class="btn-flow btn-sm" id="s-preset-save">Save current as preset</button></div>
        <div class="row" style="margin-top:12px"><button class="btn-ghost btn-sm" id="s-layout-reset">Reset layout to code default</button></div>
        <div class="sublabel" style="color:var(--dim);margin-top:8px">Active preset: <b>${esc(LAYOUT.activeName)}</b></div></div>
      <div class="card glass"><div class="ch">Tab icons</div>
        <div class="sublabel" style="color:var(--dim);margin-bottom:12px">Pick the icon for each dashboard tab.</div>
        ${[...document.querySelectorAll(".tab[data-view]")].map(t=>{
          const v=t.dataset.view, cur=LAYOUT.icons[v]||"";
          return `<div class="row" style="margin-bottom:8px"><span style="width:90px;text-transform:capitalize;color:var(--muted)">${esc(t.dataset.label||v)}</span>
            <div class="iconpick" data-tabicons="${v}">${Object.keys(ICON_LIB).map(k=>`<button class="iconopt ${cur===k?"on":""}" data-ik="${k}" title="${k}">${iconSvg(k)}</button>`).join("")}</div></div>`;
        }).join("")}</div>
    </div>`;
  $("#s-save").onclick = async () => {
    const effPct = parseFloat($("#s-efftax").value);
    await api("/pay/config", { method: "PUT", body: { rate: +$("#s-rate").value, ot_multiplier: +$("#s-ot").value, anchor_payday: $("#s-anchor").value || null, pay_schedule: $("#s-schedule").value, default_break_min: +$("#s-break").value || 0, effective_tax_rate: isFinite(effPct) ? effPct / 100 : null, use_default_schedule: $("#s-sched").checked } });
    toast("Pay settings saved");
  };
  $("#s-calsave").onclick = async () => { await api("/settings", { method: "PUT", body: { key: "calorie_target", value: +$("#s-cal").value } }); toast("Target saved"); };
  $("#s-imsgsave").onclick = async () => { await api("/settings", { method: "PUT", body: { key: "imessage_handle", value: $("#s-imsg").value.trim() } }); toast("iMessage handle saved"); };
  // Layout presets
  $("#s-preset-save").onclick = () => { const n=$("#s-preset-name").value.trim(); if(!n){ toast("Name the preset","err"); return; } layoutSavePreset(n); toast(`Preset “${n}” saved`); render("settings"); };
  $("#s-preset-apply").onclick = () => { const n=$("#s-preset").value; layoutApplyPreset(n); toast(`Applied “${n}”`); };
  $("#s-preset-del").onclick = () => { const n=$("#s-preset").value; if(n==="Default"){ toast("Can't delete Default","err"); return; } if(confirm(`Delete preset “${n}”?`)){ delete LAYOUT.presets[n]; if(LAYOUT.activeName===n) LAYOUT.activeName="Default"; layoutSave(); render("settings"); } };
  $("#s-layout-reset").onclick = () => { layoutReset(); toast("Layout reset"); };
  // Tab icon picker
  el("view-settings").querySelectorAll("[data-tabicons]").forEach(w => w.querySelectorAll(".iconopt").forEach(b => b.onclick = () => {
    LAYOUT.icons[w.dataset.tabicons] = b.dataset.ik; layoutSave(); applyTabIcons();
    w.querySelectorAll(".iconopt").forEach(x=>x.classList.toggle("on", x===b));
  }));
  const meta = await api("/meta"); $("#claude-stat").textContent = meta.claude_available ? "available ✓" : "not detected";
};

// ============================================================ INBOX (Gmail + Calendar)
const CAT_LABELS = [["action","Action Items"],["recruiting","Recruiting Updates"],["job_listing","Job Listings"],["security","Security"],["finance","Finances"],["news","News"],["general","General"]];
VIEWS.inbox = async () => {
  const [d, cal, reports] = await Promise.all([api("/inbox"), api("/calendar"), api("/inbox/reports")]);
  const g = d.google;
  if (g.state !== "connected") { el("view-inbox").innerHTML = inboxHead(g) + connectPanel(g); wireConnect(); return; }
  const brief = d.briefing?.text;
  const groups = {}; d.emails.forEach(e => (groups[e.category] = groups[e.category] || []).push(e));
  const sections = CAT_LABELS.filter(([k]) => groups[k]?.length).map(([k, label]) => `
    <div class="cat-h">${label} <span class="chip p-low">${groups[k].length}</span></div>
    ${groups[k].map(mailRow).join("")}`).join("");
  const st = d.stats || {};
  const unreadReports = reports.filter(r=>!r.read).length;
  el("view-inbox").innerHTML = inboxHead(g) + `
    <div class="statusbar glass" id="inbox-stats">${statBar(st)}</div>
    <div class="grid cols-2" style="margin-top:16px;align-items:start">
      <div class="card glass"><div class="ch">Inbox <span class="count" id="inbox-count">${d.emails.length}</span>
          ${st.awaiting_analysis?`<span class="chip due" style="margin-left:8px">${st.awaiting_analysis} awaiting analysis</span>
            <button class="btn-flow btn-sm" id="analyze-next" data-op="inbox:analyze" data-op-label="Analyzing…" style="margin-left:auto">Analyze next</button>`:""}</div>
        <div class="scroll mail-scroll" id="mail-list">${sections || `<div class="empty">${ICONS.check}<div>No emails. Hit Sync & analyze.</div></div>`}</div></div>
      ${calendarCard(cal)}
    </div>
    <div class="grid cols-2" style="margin-top:16px">
      <div class="card glass"><div class="ch">Morning briefing ${d.briefing?`<span class="count">${d.briefing.count} emails</span>`:""}
          ${reports.length?`<button class="btn-ghost btn-sm" id="reports-btn" style="margin-left:auto">${reports.length} reports${unreadReports?` · ${unreadReports} unread`:""}</button>`:""}</div>
        ${brief?`<div class="brief">${mdToHtml(brief)}</div>`:`<div class="sublabel" style="color:var(--dim)">Hit “Sync & analyze” to pull your inbox and generate a briefing.</div>`}</div>
      <div class="card glass"><div class="ch">Drafts awaiting approval <span class="count">${d.drafts.length}</span></div>
        ${d.drafts.length?`<div class="grid" style="gap:10px">${d.drafts.map(draftCard).join("")}</div>`:`<div class="sublabel" style="color:var(--dim)">No replies drafted. Open an email → Draft reply, or sync to auto-draft ones that need a response.</div>`}</div>
    </div>
    ${d.recruiting.length?`<div class="card glass" style="margin-top:16px"><div class="ch">Recruiting pipeline <span class="count">${d.recruiting.length}</span></div>
      <table><thead><tr><th>Company</th><th>Role</th><th>Recruiter</th><th>Stage</th></tr></thead><tbody>
        ${d.recruiting.map(r=>`<tr><td>${esc(r.company||"—")}</td><td>${esc(r.role||"—")}</td><td>${esc(r.recruiter||"—")}</td><td><span class="stage ${r.stage}">${esc(r.stage)}</span></td></tr>`).join("")}</tbody></table></div>`:""}
    ${st.trash?`<div class="card glass" style="margin-top:16px"><div class="ch">Delete pile <span class="count">${st.trash}</span>
        <span style="margin-left:auto;display:flex;gap:8px">
          <button class="btn-flow btn-sm" id="trash-review" data-op="inbox:trashreview" data-op-label="Analyzing…">Analyze with Claude</button>
          <button class="btn-ghost btn-sm" id="trash-finalize" style="color:var(--loss)" title="Purge everything without review">Delete all</button></span></div>
      <div class="sublabel" style="color:var(--dim)">Claude rules on every queued email: keeps are restored to the inbox (marked important), the genuinely disposable are deleted on the spot. Gmail itself is never touched — only Atlas's index. “Delete all” skips the review.</div></div>`:""}`;
  $("#inbox-sync").onclick = ()=>runSync("inbox:sync", "/inbox/sync", "Inbox synced & analyzed");
  $("#inbox-reindex").onclick = ()=>{ if(confirm("Clear the indexed emails and pull the next batch of unread mail?")) runSync("inbox:reindex", "/inbox/reindex", "Reindexed — next unread pulled"); };
  if($("#analyze-next")) $("#analyze-next").onclick = ()=>runSync("inbox:analyze", "/inbox/analyze-next", "Next batch analyzed");
  if($("#reports-btn")) $("#reports-btn").onclick = ()=>openReports(reports);
  if($("#trash-review")) $("#trash-review").onclick = async()=>{ await runOp("inbox:trashreview","Analyzing…", async()=>{
    try{
      const r=await api("/inbox/trash/review",{method:"POST"});
      if(r.kept.length){
        openModal(`<div class="modal-h"><h2>Claude's verdicts</h2><button class="btn-ghost btn-sm x" onclick="closeModal()">Close</button></div>
          <div class="modal-b">
            <div class="sublabel" style="margin-bottom:12px">Deleted <b>${r.deleted}</b> disposable email${r.deleted===1?"":"s"} · kept <b>${r.kept.length}</b> (restored & marked important)${r.unruled?` · ${r.unruled} left in pile`:""}</div>
            ${r.kept.map(k=>`<div class="list-item"><div class="ic" style="background:var(--blood-soft);color:var(--blood-2)">${ICONS.check}</div>
              <div class="main"><div class="t">${esc(k.subject||"(no subject)")}</div><div class="s">${esc(k.reason||"")}</div></div></div>`).join("")}
          </div>`);
      } else {
        toast(r.deleted?`Deleted all ${r.deleted} — nothing worth keeping`:`Nothing in the pile`);
      }
    }catch(err){ toast(err.message,"err"); } }); render("inbox"); };
  if($("#trash-finalize")) $("#trash-finalize").onclick = async()=>{ if(!confirm("Purge the whole pile WITHOUT Claude review?")) return;
    try{ const r=await api("/inbox/trash/finalize",{method:"POST"}); toast(`Purged ${r.purged} emails`); }catch(err){ toast(err.message,"err"); } render("inbox"); };
  wireCalendar(cal);
  el("view-inbox").querySelectorAll("[data-mail]").forEach(m=>m.onclick=(ev)=>{ if(ev.target.closest("[data-eact]")) return; openEmailAndRead(d.emails.find(x=>x.id==m.dataset.mail)); });
  el("view-inbox").querySelectorAll("[data-eact]").forEach(b=>b.onclick=(ev)=>{ ev.stopPropagation(); emailAction(b.dataset.eact, +b.dataset.eid, b.closest(".mail")); });
  el("view-inbox").querySelectorAll("[data-draft]").forEach(c=>c.onclick=()=>openDraft(d.drafts.find(x=>x.id==c.dataset.draft)));
};
function statBar(st){
  return STAT_DEFS.map(([k,l,c])=>{ const v = k==="gmail_unread" ? (st.gmail_unread ?? "—") : (st[k] ?? "—");
    return `<div class="statseg"><span class="sl-dot" style="background:var(${c});box-shadow:0 0 8px var(${c})"></span>
      <span class="ss-v" style="color:var(${c})">${v}</span><span class="ss-l">${l}</span></div>`; }).join('<div class="ss-div"></div>');
}
async function runSync(opKey, path, msg){ await runOp(opKey, "Working…", async () => { try{ const r=await api(path,{method:"POST"}); toast(r.events_added?`${msg} · ${r.events_added} event(s) added to calendar`:msg); }catch(err){ toast(err.message,"err"); } }); render("inbox"); }
function inboxHead(g){ return `<div class="viewhead"><h1>Inbox</h1><div class="sub">${g.state==="connected"?esc(g.email||"connected"):"Gmail + Calendar"}</div><div class="spacer"></div>
  ${g.state==="connected"?`<button class="btn-ghost" id="inbox-reindex" data-op="inbox:reindex" data-op-label="Reindexing…">Clear & reindex</button> <button class="btn-flow" id="inbox-sync" data-op="inbox:sync" data-op-label="Syncing…">Sync & analyze</button>`:""}</div>`; }
async function emailAction(act, id, row){
  if(act==="star"){ await api(`/inbox/emails/${id}/flags`,{method:"PATCH",body:{starred:true}}); toast("Starred"); render("inbox"); }
  else if(act==="unstar"){ await api(`/inbox/emails/${id}/flags`,{method:"PATCH",body:{starred:false}}); render("inbox"); }
  else if(act==="important"){ await api(`/inbox/emails/${id}/flags`,{method:"PATCH",body:{important:true}}); toast("Marked important"); render("inbox"); }
  else if(act==="archive"){ suppressRefresh(); await api(`/inbox/emails/${id}/flags`,{method:"PATCH",body:{archived:true}}); removeMailRow(row); toast("Dismissed"); }
  else if(act==="trash"){
    try{
      suppressRefresh();
      await api(`/inbox/emails/${id}/trash`,{method:"POST"});
      removeMailRow(row);
      updateStatBar();
      toast("Deleted → pile (marked read on Gmail)");
    }catch(err){
      if(String(err.message).startsWith("protected")){
        if(confirm("This email is starred/important/has a reminder. Delete anyway?")){
          await api(`/inbox/emails/${id}/trash?force=true`,{method:"POST"}); removeMailRow(row); toast("Deleted → pile");
        }
      } else toast(err.message,"err");
    }
  }
  else if(act==="remind"){ scheduleReminder(id); }
}
async function updateStatBar(){
  const bar = el("inbox-stats"); if(!bar) return;
  const st = await api("/inbox/stats").catch(()=>null);
  if(st) bar.innerHTML = statBar(st);
}
function removeMailRow(row){
  if(!row) return;
  const cnt = el("inbox-count"); if(cnt) cnt.textContent = Math.max(0, (+cnt.textContent||1)-1);
  row.style.transition="all .3s var(--ease)"; row.style.opacity="0"; row.style.transform="translateX(24px)";
  row.style.maxHeight=row.offsetHeight+"px";
  setTimeout(()=>{ row.style.maxHeight="0"; row.style.marginBottom="0"; row.style.paddingTop="0"; row.style.paddingBottom="0"; },150);
  setTimeout(()=>{ const cat=row.previousElementSibling; row.remove();
    if(cat && cat.classList.contains("cat-h") && (!cat.nextElementSibling || cat.nextElementSibling.classList.contains("cat-h"))) cat.remove(); },420);
}
async function openEmailAndRead(e){
  if(!e) return; openEmail(e);
  if(e.is_unread){
    try{
      suppressRefresh();
      await api(`/inbox/emails/${e.id}/read`,{method:"POST"});
      e.is_unread = 0;
      const row = document.querySelector(`.mail[data-mail="${e.id}"]`); if(row) row.classList.remove("unread");
      updateStatBar();
    }catch{}
  }
}
async function scheduleReminder(emailId){
  const hrs = prompt("Remind me in how many hours? (e.g. 3, or 24)", "3");
  if(hrs===null) return; const h=parseFloat(hrs); if(!(h>0)){ toast("Enter a positive number","err"); return; }
  const handle = (await api("/settings/imessage_handle")).value;
  const email = emailId ? null : null;
  const remind_at = Date.now()/1000 + h*3600;
  const text = "Follow up on an email in Atlas";
  const body = { text, remind_at, email_id: emailId, method: handle?"imessage":"notification", target: handle||null };
  try{ await api("/reminders",{method:"POST",body}); toast(`Reminder set for ${h}h from now${handle?" (iMessage)":""}`); }catch(e){ toast(e.message,"err"); }
}
function openReports(reports){
  openModal(`<div class="modal-h"><h2>Past briefings</h2><button class="btn-ghost btn-sm x" onclick="closeModal()">Close</button></div>
    <div class="modal-b">${reports.map(r=>`<div class="list-item" data-report="${esc(r.name)}" style="cursor:pointer;${r.read?"opacity:.55":""}">
      <div class="ic" style="${r.read?"":"background:var(--blood-soft);color:var(--blood-2)"}">${ICONS.check}</div>
      <div class="main"><div class="t">${new Date(r.when).toLocaleString()} ${r.read?"":'<span class="chip due" style="margin-left:6px">unread</span>'}</div><div class="s">${esc(r.name)}</div></div>
      ${r.read?"":`<button class="btn-ghost btn-sm" data-mark="${esc(r.name)}">Mark read</button>`}</div>`).join("")}</div>`);
  el("modal").querySelectorAll("[data-mark]").forEach(b=>b.onclick=async(ev)=>{ ev.stopPropagation();
    await api(`/inbox/reports/${encodeURIComponent(b.dataset.mark)}/read`,{method:"POST"});
    const reps=await api("/inbox/reports"); openReports(reps); });
  el("modal").querySelectorAll("[data-report]").forEach(x=>x.onclick=async(ev)=>{ if(ev.target.closest("[data-mark]")) return;
    const name=x.dataset.report;
    const c=await api(`/inbox/reports/${encodeURIComponent(name)}`);
    api(`/inbox/reports/${encodeURIComponent(name)}/read`,{method:"POST"}).catch(()=>{});  // opening = reading
    openModal(`<div class="modal-h"><h2>Briefing</h2><button class="btn-ghost btn-sm x" onclick="closeModal()">Close</button></div><div class="modal-b"><div class="brief">${mdToHtml(c.text)}</div></div>`); });
}
function connectPanel(g){
  const steps = g.state==="libs_missing"
    ? `Install the Google libraries once:<br><code>cd engine && uv sync --extra google</code><br>then restart Atlas.`
    : g.state==="no_credentials"
    ? `1. In <b>Google Cloud Console</b> → APIs & Services → Credentials, create an <b>OAuth client ID</b> of type <b>Desktop app</b>.<br>2. Enable the <b>Gmail API</b> and <b>Google Calendar API</b>.<br>3. Download the client JSON and save it here:<br><code>~/Library/Application Support/Atlas/google_credentials.json</code><br>4. Add yourself as a test user on the OAuth consent screen, then click Connect.`
    : `Click Connect — a browser window opens for Google consent. Nothing sends without your approval.`;
  return `<div class="card glass connect" style="margin-top:16px">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7l9 6 9-6"/><rect x="3" y="5" width="18" height="14" rx="2"/></svg>
    <div style="font-size:16px;font-weight:650">Connect Google to pull Gmail & Calendar</div>
    <div class="steps">${steps}</div>
    ${g.state==="not_connected"?`<button class="btn-flow" id="g-connect">Connect Google</button>`:`<button class="btn-ghost" id="g-recheck">Re-check</button>`}
    <div style="font-size:12px;color:var(--dim)">${esc(g.detail||"")}</div></div>`;
}
function wireConnect(){
  const c=$("#g-connect"); if(c) c.onclick=async(e)=>{ e.target.disabled=true; e.target.innerHTML='<span class="spinner"></span> Opening browser…'; try{ await api("/google/connect",{method:"POST"}); toast("Google connected"); }catch(err){ toast(err.message,"err"); } render("inbox"); };
  const r=$("#g-recheck"); if(r) r.onclick=()=>render("inbox");
}
function mailRow(e){
  const acts = `<div class="m-acts">
    <button class="eact ${e.starred?"on":""}" data-eact="${e.starred?"unstar":"star"}" data-eid="${e.id}" title="Star">★</button>
    <button class="eact ${e.important?"on":""}" data-eact="important" data-eid="${e.id}" title="Mark important">!</button>
    <button class="eact" data-eact="remind" data-eid="${e.id}" title="Remind me">⏰</button>
    <button class="eact danger" data-eact="trash" data-eid="${e.id}" title="Delete (marks read, goes to pile)">🗑</button></div>`;
  return `<div class="mail ${e.is_unread?"unread":""} ${e.important?"important":""}" data-mail="${e.id}"><div class="dot"></div><div class="m-main">
    <div class="m-top"><span class="m-from">${esc(e.sender||e.sender_email)}</span>
      ${e.thread_count>1?`<span class="chip p-low">⛓ ${e.thread_count}</span>`:""}
      ${e.needs_reply?'<span class="chip due">reply</span>':""}
      <span class="m-time">${e.received_at?new Date(e.received_at*1000).toLocaleDateString():""}</span></div>
    <div class="m-subj">${esc(e.subject||"(no subject)")}</div>
    <div class="m-sum">${esc(e.summary||e.snippet||"")}</div></div>${acts}</div>`;
}
function draftCard(d){
  return `<div class="draftcard" data-draft="${d.id}"><div class="dc-to">To: ${esc(d.to_addr||"—")}</div>
    <div class="dc-sub">${esc(d.subject||"")}</div><div class="dc-body">${esc(d.body)}</div>
    <div class="dc-foot">Click to review & approve →</div></div>`;
}
function openEmail(e){
  if(!e) return;
  closeEmailPop();
  const when = e.received_at ? new Date(e.received_at*1000).toLocaleString() : "";
  const pop = document.createElement("div");
  pop.id = "email-pop"; pop.className = "email-pop";
  pop.innerHTML = `
    <div class="cp-h">
      <div style="min-width:0"><h2 style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(e.subject||"(no subject)")}</h2>
        <div class="sublabel">${esc(e.sender||"")} &lt;${esc(e.sender_email||"")}&gt; · ${esc(when)}${e.thread_count>1?` · <b>${e.thread_count} messages in chain</b>`:""}</div></div>
      <span style="flex:1"></span>
      <button class="btn-flow btn-sm" id="ep-draft">Draft reply</button>
      <button class="btn-ghost btn-sm" id="ep-close">✕ Close</button></div>
    <div class="ep-grid">
      <div class="card glass ep-card"><div class="ch">Original view</div>
        <div class="ep-original" id="ep-original"></div></div>
      <div class="card glass ep-card"><div class="ch">Summarized view</div>
        <div class="ep-summary scroll">
          <div class="row" style="margin-bottom:12px">
            ${e.category?`<span class="chip src">${esc(e.category)}</span>`:""}
            ${e.important?'<span class="chip p-high">important</span>':""}
            ${e.needs_reply?'<span class="chip due">needs reply</span>':""}
            ${e.starred?'<span class="chip due">★ starred</span>':""}</div>
          ${e.summary?`<div class="ctx"><div class="lbl">Claude's summary</div><div style="line-height:1.6">${esc(e.summary)}</div></div>`
            :`<div class="sublabel" style="color:var(--dim);margin-bottom:12px">Not analyzed yet — run Sync & analyze.</div>`}
          <div class="ctx"><div class="lbl">Extracted text</div><div class="ep-plain">${esc(e.body||e.snippet||"(no text)")}</div></div>
        </div></div>
    </div>`;
  document.body.appendChild(pop);
  requestAnimationFrame(()=>pop.classList.add("show"));
  // Original view: the email as it was sent — real HTML in a sandboxed frame.
  const orig = pop.querySelector("#ep-original");
  const asSent = e.body_html || (looksHtml(e.body) ? e.body : null);
  if (asSent) fillEmailBody(orig, asSent);
  else { orig.classList.add("ep-plainwrap"); orig.textContent = e.body || e.snippet || "(no content)"; }
  pop.querySelector("#ep-close").onclick = closeEmailPop;
  document.addEventListener("keydown", _emailPopEsc);
  pop.querySelector("#ep-draft").onclick = async(ev)=>{
    ev.target.disabled=true; ev.target.innerHTML='<span class="spinner"></span> Drafting…';
    try{ const dr=await api(`/inbox/emails/${e.id}/draft`,{method:"POST"}); closeEmailPop(); openDraft(dr); }
    catch(err){ toast(err.message,"err"); ev.target.disabled=false; ev.target.textContent="Draft reply"; }
  };
}
function closeEmailPop(){ const p=document.getElementById("email-pop"); if(p) p.remove(); document.removeEventListener("keydown", _emailPopEsc); }
function _emailPopEsc(ev){ if(ev.key==="Escape") closeEmailPop(); }
// Render email bodies: HTML mail goes in a sandboxed iframe (no scripts run); plain text stays text.
function looksHtml(s){ return /<(html|body|div|table|tbody|tr|td|p|br|a|img|span|h[1-6]|ul|ol|li|style)[\s>\/]/i.test(s||""); }
function fillEmailBody(container, body){
  if(!container) return;
  if(body && looksHtml(body)){
    const clean = body.replace(/<script[\s\S]*?<\/script>/gi, "").replace(/ on[a-z]+="[^"]*"/gi, "");
    const frame = document.createElement("iframe");
    frame.className = "email-frame"; frame.setAttribute("sandbox", "");
    container.classList.add("has-frame"); container.innerHTML = ""; container.appendChild(frame);
    frame.srcdoc = `<base target="_blank"><style>body{font:14px/1.5 -apple-system,system-ui,sans-serif;color:#1a1a1a;margin:12px}img{max-width:100%;height:auto}a{color:#0b57d0}</style>${clean}`;
  } else {
    container.textContent = body || "(no content)"; container.style.whiteSpace = "pre-wrap";
  }
}
function openDraft(d){
  if(!d) return;
  const ctx = d.email_body||d.email_summary||d.email_snippet;
  openModal(`<div class="modal-h"><h2>Review reply</h2><button class="btn-ghost btn-sm x" onclick="closeModal()">Close</button></div>
    <div class="modal-b">
      ${ctx?`<div class="ctx"><div class="lbl">In reply to ${esc(d.email_sender||"")}: ${esc(d.email_subject||"")}</div><div class="ctx-body" id="draft-ctx"></div></div>`:""}
      <label class="fld">To<input id="d-to" value="${esc(d.to_addr||"")}"/></label>
      <label class="fld" style="margin-top:10px">Subject<input id="d-sub" value="${esc(d.subject||"")}"/></label>
      <label class="fld" style="margin-top:10px">Reply<textarea id="d-body">${esc(d.body)}</textarea></label>
      <div class="row" style="margin-top:12px">
        <input id="d-revise" placeholder="Tell Claude how to change it: 'shorter', 'offer Friday 2pm'…" style="flex:1;min-width:0"/>
        <button class="btn-ghost btn-sm" id="d-revise-go">Revise</button></div>
      <div class="sublabel" id="d-revise-note" style="color:var(--dim);margin-top:6px"></div></div>
    <div class="modal-f"><button class="btn-approve" id="d-approve"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" style="width:15px;height:15px"><path d="M5 13l4 4L19 7"/></svg> Approve & send</button>
      <button class="btn-ghost" id="d-save">Save draft</button><span style="flex:1"></span><button class="btn-ghost" id="d-discard">Discard</button></div>`);
  if(ctx) fillEmailBody($("#draft-ctx"), ctx);
  const patch=()=>({to_addr:$("#d-to").value,subject:$("#d-sub").value,body:$("#d-body").value});
  const reviseGo=$("#d-revise-go");
  const revise=async()=>{
    const instr=$("#d-revise").value.trim(); if(!instr) return;
    reviseGo.disabled=true; reviseGo.innerHTML='<span class="spinner"></span>';
    try{
      // Pass the CURRENT (possibly edited, unsaved) fields so Claude revises what you see.
      const r=await api(`/inbox/drafts/${d.id}/revise`,{method:"POST",body:{message:instr, ...patch()}});
      $("#d-to").value=r.to_addr||""; $("#d-sub").value=r.subject||""; $("#d-body").value=r.body||"";
      $("#d-revise").value=""; $("#d-revise-note").textContent=r.note?`Claude: ${r.note}`:"Revised.";
    }catch(err){ toast(err.message,"err"); }
    reviseGo.disabled=false; reviseGo.textContent="Revise";
  };
  reviseGo.onclick=revise;
  $("#d-revise").onkeydown=(e)=>{ if(e.key==="Enter") revise(); };
  $("#d-save").onclick=async()=>{ await api(`/inbox/drafts/${d.id}`,{method:"PATCH",body:patch()}); toast("Draft saved"); closeModal(); render("inbox"); };
  $("#d-discard").onclick=async()=>{ await api(`/inbox/drafts/${d.id}/discard`,{method:"POST"}); toast("Discarded"); closeModal(); render("inbox"); };
  $("#d-approve").onclick=async(ev)=>{ await api(`/inbox/drafts/${d.id}`,{method:"PATCH",body:patch()}); ev.target.disabled=true; ev.target.innerHTML='<span class="spinner"></span> Sending…'; try{ await api(`/inbox/drafts/${d.id}/approve`,{method:"POST"}); toast("Sent ✓"); }catch(err){ toast(err.message,"err"); } closeModal(); render("inbox"); };
}

// ---- Calendar (within Inbox) ----
function calendarCard(cal){
  if(cal.google.state!=="connected") return "";
  const rem = cal.upcoming||[];
  return `<div class="card glass"><div class="ch">This week
      <span class="spacer" style="flex:1"></span><button class="btn-ghost btn-sm" id="cal-sync">Sync</button></div>
    ${rem.length?`<div style="margin-bottom:12px">${rem.map(r=>`<div class="reminder"><div class="rt">${esc(r.title)}</div><div class="rs">in ${r.minutes_until} min</div></div>`).join("")}</div>`:""}
    ${weekView(cal.events||[], 30)}
    <div class="row" style="margin-top:14px">
      <input id="cal-cmd" placeholder="Tell Claude: 'dentist Tue 2pm, gym MWF 7am, cancel Friday lunch'…" style="flex:1;min-width:0" data-draft="cal-cmd"/>
      <button class="btn-flow btn-sm" id="cal-cmd-go" data-op="cal:cmd" data-op-label="">Do it</button></div>
    <div id="cal-cmd-log">${calCmdLog.map(m=>`<div class="cclog ${m.role}">${m.role==="user"?"›":"✓"} ${esc(m.text)}</div>`).join("")}
      ${opPending("cal:cmd")?`<div class="cclog working"><span class="spinner"></span> Scheduling…</div>`:""}</div>
    <div class="sublabel" style="color:var(--dim);margin-top:6px">Handles multiple tasks at once — create, move, or delete; recurring patterns expand automatically.</div></div>`;
}
const calCmdLog = [];
function wireCalendar(cal){
  if(cal.google.state!=="connected") return;
  const s=$("#cal-sync"); if(s) s.onclick=async(e)=>{ e.target.disabled=true; e.target.innerHTML='<span class="spinner"></span>'; try{ await api("/calendar/sync",{method:"POST"}); toast("Calendar synced"); }catch(err){ toast(err.message,"err"); } render("inbox"); };
  const go=$("#cal-cmd-go");
  const run=async()=>{
    const inp=$("#cal-cmd"); const txt=inp.value.trim(); if(!txt) return;
    inp.value="";
    calCmdLog.push({role:"user", text:txt}); if(calCmdLog.length>8) calCmdLog.shift();
    runOp("cal:cmd", "Scheduling…", async()=>{
      await render("inbox");   // shows the user line + working row immediately, keeps place
      try{
        const r=await api("/calendar/command",{method:"POST",body:{message:txt}});
        const done = r.applied.length ? r.applied.join(" · ") : "";
        const line = [done, r.reply].filter(Boolean).join(" — ") || "Nothing to change";
        calCmdLog.push({role:"ai", text: r.errors.length ? `${line} (${r.errors.length} failed)` : line});
      }catch(err){ calCmdLog.push({role:"ai", text:"Error: "+err.message}); }
      if(calCmdLog.length>8) calCmdLog.shift();
    }).then(()=>render("inbox"));
  };
  if(go){ go.onclick=run; $("#cal-cmd").onkeydown=(e)=>{ if(e.key==="Enter") run(); }; }
}
function weekView(events, hourHeight){
  const HH = hourHeight || 40, SH = 7, EH = 22; // 7am–10pm window
  const now = new Date();
  const ws = new Date(now); ws.setDate(now.getDate() - now.getDay()); ws.setHours(0, 0, 0, 0);
  const days = [...Array(7)].map((_, i) => { const d = new Date(ws); d.setDate(ws.getDate() + i); return d; });
  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  const timed = days.map(() => []), allday = days.map(() => []);
  (events || []).forEach(e => {
    if (!e.start) return;
    if (e.all_day) { const di = days.findIndex(d => d.toISOString().slice(0, 10) === (e.start || "").slice(0, 10)); if (di >= 0) allday[di].push(e); return; }
    const s = new Date(e.start); if (isNaN(s)) return;
    const di = days.findIndex(d => sameDay(d, s)); if (di < 0) return;
    timed[di].push({ e, s, en: e.end ? new Date(e.end) : new Date(s.getTime() + 3600000) });
  });
  const DN = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const head = days.map((d, i) => `<div class="wk-dh ${sameDay(d, now) ? "today" : ""}"><div class="dn">${DN[i]}</div><div class="dd">${d.getDate()}</div></div>`).join("");
  const anyAllday = allday.some(a => a.length);
  const alldayRow = anyAllday ? `<div class="wk-allday"><div class="wk-gut" style="font-size:9px;color:var(--dim);display:flex;align-items:center;justify-content:center">all-day</div>${allday.map(a => `<div class="adc">${a.map(ev => `<div class="adchip" data-ev="${esc(ev.gcal_id)}" data-title="${esc(ev.title)}">${esc(ev.title)}</div>`).join("")}</div>`).join("")}</div>` : "";
  const colH = (EH - SH) * HH;
  const gutter = `<div class="wk-gut" style="height:${colH}px">${[...Array(EH - SH)].map((_, i) => `<div class="hl" style="top:${i * HH}px">${fmtHour(SH + i)}</div>`).join("")}</div>`;
  const cols = timed.map(list => {
    const blocks = list.map(({ e, s, en }) => {
      const top = Math.max(0, (s.getHours() * 60 + s.getMinutes() - SH * 60) / 60 * HH);
      const h = Math.min(colH - top, Math.max(20, (en - s) / 60000 / 60 * HH));
      return `<div class="wk-ev" data-ev="${esc(e.gcal_id)}" data-title="${esc(e.title)}" style="top:${top}px;height:${h - 2}px"><div class="evt">${esc(e.title)}</div><div class="evtm">${fmtTime(s)}</div></div>`;
    }).join("");
    return `<div class="wk-col" style="height:${colH}px;background-image:repeating-linear-gradient(rgba(255,255,255,.045) 0 1px,transparent 1px ${HH}px)">${blocks}</div>`;
  }).join("");
  return `<div class="wk"><div class="wk-head"><div></div>${head}</div>${alldayRow}<div class="wk-grid">${gutter}${cols}</div></div>`;
}
function fmtHour(h){ return (h % 12 || 12) + (h < 12 ? "a" : "p"); }
function fmtTime(d){ let h = d.getHours(), m = d.getMinutes(); const ap = h < 12 ? "AM" : "PM"; h = h % 12 || 12; return `${h}:${m < 10 ? "0" + m : m} ${ap}`; }
function monthGrid(events){
  const now=new Date(); const y=now.getFullYear(), m=now.getMonth();
  const first=new Date(y,m,1); const startDow=first.getDay(); const days=new Date(y,m+1,0).getDate();
  const byDay={}; events.forEach(e=>{ if(!e.start) return; const dd=new Date(e.start); if(dd.getFullYear()===y&&dd.getMonth()===m) (byDay[dd.getDate()]=byDay[dd.getDate()]||[]).push(e); });
  const dow=["S","M","T","W","T","F","S"].map(d=>`<div class="dow">${d}</div>`).join("");
  let cells="";
  for(let i=0;i<startDow;i++) cells+=`<div class="calcell out"></div>`;
  for(let d=1;d<=days;d++){ const evs=byDay[d]||[]; const today=d===now.getDate();
    cells+=`<div class="calcell ${today?"today":""}"><div class="cnum">${d}</div>${evs.slice(0,3).map(e=>`<div class="cev" title="${esc(e.title)}">${esc(e.title)}</div>`).join("")}</div>`; }
  return `<div class="calgrid">${dow}${cells}</div>`;
}

// ============================================================ MEETINGS (Vein, read-only)
VIEWS.meetings = async () => {
  const d = await api("/meetings");
  const src = d.source.source;
  el("view-meetings").innerHTML = `
    <div class="viewhead"><h1>Meetings</h1><div class="sub">${src==="db"?"from Vein · local db":src==="http"?"from Vein · live":"Vein not found"}</div></div>
    ${src==="none"?`<div class="card glass connect" style="margin-top:16px">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M8 2v4M16 2v4M3 10h18"/></svg>
      <div style="font-size:16px;font-weight:650">No Vein data found</div>
      <div class="steps">Atlas reads Vein's notes read-only from <code>~/Library/Application Support/Vein/vein.db</code>. Record a meeting in Vein, or set <code>ATLAS_VEIN_DB</code>.</div></div>`
    : d.meetings.length?`<div class="card glass" style="margin-top:16px"><div class="ch">Past meetings <span class="count">${d.meetings.length}</span></div>
        ${d.meetings.map(mtgRow).join("")}</div>`
    : `<div class="card glass" style="margin-top:16px"><div class="empty">${ICONS.check}<div>Vein is connected but has no meetings yet.</div></div></div>`}`;
  el("view-meetings").querySelectorAll("[data-mtg]").forEach(r=>r.onclick=()=>openMeeting(r.dataset.mtg));
};
function mtgRow(m){
  const gist = m.summary?.gist;
  const when = m.started_at?new Date(m.started_at*1000).toLocaleString([], {month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}):"";
  return `<div class="list-item" data-mtg="${m.id}" style="cursor:pointer"><div class="ic" style="background:var(--violet-soft);color:var(--violet)">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M8 2v4M16 2v4M3 10h18"/></svg></div>
    <div class="main"><div class="t">${esc(m.title||"Untitled meeting")}</div><div class="s">${when}${m.people?.length?` · ${m.people.join(", ")}`:""}${gist?` · ${esc(gist)}`:""}</div></div>
    <span class="chip p-low">${m.n_todos||0} todos</span></div>`;
}
async function openMeeting(id){
  const m = await api(`/meetings/${id}`);
  const s = m.summary||{};
  openModal(`<div class="modal-h"><h2>${esc(m.title||"Meeting")}</h2><button class="btn-ghost btn-sm x" onclick="closeModal()">Close</button></div>
    <div class="modal-b">
      ${s.gist?`<div class="ctx"><div class="lbl">Summary</div><div class="ctx-body">${esc(s.gist)}${(s.key_points||[]).length?"\n\n• "+s.key_points.map(esc).join("\n• "):""}</div></div>`:""}
      ${(m.todos||[]).length?`<div class="ch" style="margin:6px 0 8px">Action items</div>${m.todos.map(t=>`<div class="list-item"><div class="main"><div class="t">${esc(t.task||"")}</div><div class="s">${t.owner?esc(t.owner):""}${t.due?` · due ${esc(t.due)}`:""}</div></div></div>`).join("")}`:""}
      ${(m.transcript||[]).length?`<div class="ch" style="margin:14px 0 8px">Transcript</div><div class="ctx"><div class="ctx-body">${m.transcript.map(esc).join("\n")}</div></div>`:""}
    </div>`);
}

// ---------- shared widgets ----------
// Minimal, safe markdown → HTML (escape first, then apply headings/bold/lists).
function mdToHtml(md){
  if(!md) return "";
  const lines = esc(md).split("\n");
  let html = "", inList = false;
  const close = () => { if(inList){ html += "</ul>"; inList = false; } };
  for(let raw of lines){
    const line = raw.trimEnd();
    let m;
    if((m = line.match(/^###\s+(.*)/))){ close(); html += `<h4 class="md-h">${m[1]}</h4>`; }
    else if((m = line.match(/^##\s+(.*)/))){ close(); html += `<h3 class="md-h">${m[1]}</h3>`; }
    else if((m = line.match(/^#\s+(.*)/))){ close(); html += `<h3 class="md-h">${m[1]}</h3>`; }
    else if((m = line.match(/^[-*]\s+(.*)/))){ if(!inList){ html += "<ul class='md-ul'>"; inList = true; } html += `<li>${m[1]}</li>`; }
    else if(line.trim() === ""){ close(); }
    else { close(); html += `<p class="md-p">${line}</p>`; }
  }
  close();
  return html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*(?!\s)(.+?)\*/g, "$1<em>$2</em>");
}

async function del(path, msg){ try{ await api(path, {method:"DELETE"}); toast(msg||"Deleted"); }catch(e){ toast(e.message,"err"); } }
window.del = del;
function openModal(html){ const m=el("modal"); m.innerHTML=`<div class="modal glass">${html}</div>`; m.classList.add("show"); m.onclick=(e)=>{ if(e.target===m) closeModal(); }; }
function closeModal(){ el("modal").classList.remove("show"); }
window.closeModal = closeModal;
function busy(label) { return `<span class="busy">${label}<span class="dots"><span></span><span></span><span></span></span></span>`; }
function ring(pct, val, target) {
  const r = 46, circ = 2 * Math.PI * r, off = circ * (1 - pct / 100);
  return `<svg class="ring" viewBox="0 0 104 104"><circle class="bg" cx="52" cy="52" r="${r}"/>
    <circle class="fg" cx="52" cy="52" r="${r}" stroke-dasharray="${circ}" stroke-dashoffset="${off}" transform="rotate(-90 52 52)"/>
    <text x="52" y="50" text-anchor="middle" class="ring-center" fill="var(--text)" style="font-size:20px;font-weight:740">${pct}%</text>
    <text x="52" y="68" text-anchor="middle" fill="var(--muted)" style="font-size:11px">${val} kcal</text></svg>`;
}
function sparkline(series) {
  if (!series || series.length < 2) return `<div class="sublabel" style="color:var(--dim);margin-top:10px">Not enough snapshots yet — add accounts and snapshot to build a trend.</div>`;
  const vals = series.map((s) => s.total), min = Math.min(...vals), max = Math.max(...vals), pad = (max - min) * 0.1 || 1;
  const lo = min - pad, hi = max + pad, W = 600, H = 120;
  const x = (i) => (i / (series.length - 1)) * W, y = (v) => H - ((v - lo) / (hi - lo)) * H;
  const line = vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${line} L${W},${H} L0,${H} Z`;
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <defs><linearGradient id="sparkgrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--flow)" stop-opacity=".4"/><stop offset="1" stop-color="var(--flow)" stop-opacity="0"/></linearGradient></defs>
    <path class="area" d="${area}"/><path class="line" d="${line}"/></svg>`;
}
function sparkVals(values, color, height) {
  values = (values || []).filter(v => v != null);
  if (values.length < 2) return `<div class="sublabel" style="color:var(--dim)">Not enough data yet — log a few to see the trend.</div>`;
  const min = Math.min(...values), max = Math.max(...values), pad = (max - min) * 0.12 || 1, lo = min - pad, hi = max + pad, W = 560, H = height || 90;
  const x = i => (i / (values.length - 1)) * W, y = v => H - ((v - lo) / (hi - lo)) * H;
  const line = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="height:${H}px"><path d="${line}" fill="none" stroke="${color || "var(--neon)"}" stroke-width="2.5"/></svg>`;
}
function setupDrop(zoneSel, fileSel, endpoint, onOk) {
  const zone = $(zoneSel), file = $(fileSel); if (!zone) return;
  const upload = async (f) => {
    if (!f) return; const fd = new FormData(); fd.append("file", f);
    zone.innerHTML = `<span class="spinner"></span> Claude is reading…`;
    try { const r = await api(endpoint, { method: "POST", body: fd }); onOk(r); }
    catch (e) { toast(e.message, "err"); render(active); }
  };
  zone.onclick = () => file.click();
  file.onchange = () => upload(file.files[0]);
  zone.ondragover = (e) => { e.preventDefault(); zone.classList.add("over"); };
  zone.ondragleave = () => zone.classList.remove("over");
  zone.ondrop = (e) => { e.preventDefault(); zone.classList.remove("over"); upload(e.dataTransfer.files[0]); };
}

// ============================================================ PICKS
const VERDICT_CLASS = { strong: "v-strong", solid: "v-solid", watch: "v-watch", weak: "v-weak" };
const num1 = (n) => (n == null ? "—" : (+n).toFixed(1));

let _pkOffset = 0;
let _watched = new Set(); // symbols on the watchlist (shared with Money tab)

VIEWS.picks = async () => {
  const root = el("view-picks");
  const [d, w] = await Promise.all([
    api(`/picks/daily?offset=${_pkOffset}&count=10`).catch(() => ({ picks: [], owned: [] })),
    api("/picks/watch").catch(() => ({ items: [] })),
  ]);
  const picks = d.picks || [], owned = d.owned || [], watchItems = w.items || [];
  _watched = new Set(watchItems.map((i) => i.symbol));
  const csMissing = d.commonsense_available === false;
  const header = `<div class="vhead"><div><h1>Today's Picks</h1>
      <div class="sublabel">10 fresh fundamentals-first names you don't own yet — ranked by quality &amp; unjustified price.</div></div>
    <div class="row" style="gap:8px">
      ${d.has_more ? '<button class="btn-ghost btn-sm" id="pk-diff">Show different</button>' : ""}
      <button class="btn-flow btn-sm" id="pk-run" data-op="picks:screen" data-op-label="Screening…">Re-screen</button></div></div>`;

  if (csMissing) {
    root.innerHTML = header + `<div class="card glass"><div class="empty">${ICONS.check}
      <div>CommonSense engine not found. Set <code>ATLAS_COMMONSENSE_ROOT</code> so Atlas can screen fundamentals.</div></div></div>`;
    wirePicksRun(); return;
  }
  const list = picks.length
    ? `<div class="picklist">${picks.map((p) => pickRow(p, false)).join("")}</div>`
    : `<div class="card glass"><div class="empty">${ICONS.check}<div>No fresh picks yet. Click <b>Re-screen</b> to score the S&amp;P 500 (first run pulls SEC data, so it takes a bit).</div></div></div>`;
  const lookupBar = `<div class="lookup-bar">
      <input id="lk-in" type="text" placeholder="Ticker lookup — e.g. SHOP, PLTR, TSM" maxlength="8" autocomplete="off" spellcheck="false"/>
      <button class="btn-flow btn-sm" id="lk-btn">Analyze</button>
      <div id="lk-status" class="lk-status"></div></div>`;
  const watchBlock = watchItems.length ? `<h3 class="pk-h3">Watchlist</h3>
      <div class="picklist">${watchItems.map(watchRow).join("")}</div>` : "";
  const ownedBlock = owned.length ? `<details class="owned"><summary>Suggestions already owned (${owned.length})</summary>
      <div class="picklist">${owned.map((p) => pickRow(p, true)).join("")}</div></details>` : "";
  const prevLk = $("#lk-in") ? $("#lk-in").value : "";  // survive WS-triggered re-renders
  const outlookBox = `<div id="mkt-outlook" class="card glass outlook">
      <div class="ch">Market Outlook <button class="info-btn" id="ol-info">i</button><span class="count" id="ol-date"></span></div>
      <div class="expl-body" id="ol-expl">Today's read of the business &amp; finance world, regenerated once per day. The narrative is grounded: Claude may only cite our computed market data (SPY/sector day moves and universe breadth from our own price store) and the day's market headlines — every claim carries an article index. The score is not Claude's opinion: each article gets a sentiment × market-impact read, and the score is the impact-weighted proportion of positive vs negative articles, −100…+100. The graph tracks that score day over day.</div>
      <div id="ol-body"><div class="analyzing"><span class="spinner"></span> Reading today's market…</div></div></div>`;
  root.innerHTML = header + outlookBox + list + lookupBar + watchBlock + ownedBlock;
  if (prevLk && $("#lk-in")) $("#lk-in").value = prevLk;
  const olInfo = $("#ol-info"); if (olInfo) olInfo.onclick = () => $("#ol-expl").classList.toggle("show");
  loadOutlook();
  root.querySelectorAll("[data-pick]").forEach((r) => (r.onclick = (ev) => {
    if (ev.target.closest("[data-close],[data-star],[data-unwatch],[data-lookup]")) return;
    openPick(r.dataset.pick);
  }));
  root.querySelectorAll("[data-close]").forEach((b) => (b.onclick = async () => {
    await api(`/picks/${encodeURIComponent(b.dataset.close)}/close`, { method: "POST" });
    toast(`Dismissed ${b.dataset.close}`); render("picks");
  }));
  root.querySelectorAll("[data-star]").forEach((b) => (b.onclick = () => toggleWatch(b.dataset.star)));
  root.querySelectorAll("[data-unwatch]").forEach((b) => (b.onclick = async () => {
    await api(`/picks/watch/${encodeURIComponent(b.dataset.unwatch)}`, { method: "DELETE" });
    toast(`Unwatched ${b.dataset.unwatch}`); render("picks");
  }));
  root.querySelectorAll("[data-lookup]").forEach((b) => (b.onclick = () => runLookup(b.dataset.lookup)));
  wirePicksRun();
  wireLookup();
};

async function toggleWatch(sym) {
  const method = _watched.has(sym) ? "DELETE" : "POST";
  try {
    await api(`/picks/watch/${encodeURIComponent(sym)}`, { method });
    if (method === "POST") { _watched.add(sym); toast(`Watching ${sym}`); }
    else { _watched.delete(sym); toast(`Unwatched ${sym}`); }
    if (active === "picks") render("picks");
    const ds = document.querySelector(`#drawer [data-star="${sym}"]`);
    if (ds) { const on = _watched.has(sym); ds.classList.toggle("on", on); ds.textContent = on ? "★" : "☆"; }
  } catch (e) { toast(e.message, "err"); }
}
window.toggleWatch = toggleWatch;

// Ticker lookup: reference our system first; on a miss, launch a fresh pull.
function wireLookup() {
  const input = $("#lk-in"), btn = $("#lk-btn");
  if (!btn) return;
  const go2 = () => { const s = (input.value || "").trim().toUpperCase(); if (s) runLookup(s); };
  btn.onclick = go2;
  input.onkeydown = (e) => { if (e.key === "Enter") go2(); };
}

async function runLookup(sym) {
  sym = sym.toUpperCase().trim();
  const status = $("#lk-status"), btn = $("#lk-btn");
  const say = (html) => { if (status) status.innerHTML = html; };
  try {
    say(`<span class="spinner"></span> Checking our system for ${esc(sym)}…`);
    const st = await api(`/picks/lookup/${encodeURIComponent(sym)}`);
    if (st.in_system) {
      say(`${esc(sym)} is in our system${st.ranked ? ` (ranked #${st.rank})` : ""} — opening breakout.`);
      openPick(sym);
      return;
    }
    // Not in the system: launch a fresh pull (SEC facts → analyze → score).
    if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Pulling…`; }
    say(`${esc(sym)} isn't in our system — pulling SEC filings &amp; scoring with our method (~1 min)…`);
    const r = await api(`/picks/lookup/${encodeURIComponent(sym)}`, { method: "POST" });
    if (r.error) { say(`<span class="lk-err">${esc(r.error)}</span>`); }
    else {
      say(`${esc(sym)} analyzed — quality ${num1(r.quality_score)} (${esc(r.verdict || "")}). Opening breakout.`);
      openPick(sym);
    }
  } catch (e) {
    say(`<span class="lk-err">${esc(e.message)}</span>`);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Analyze"; }
  }
}

function watchRow(wi) {
  const vClass = VERDICT_CLASS[wi.verdict] || "v-watch";
  const badge = wi.in_system
    ? `<div class="qbadge ${vClass}">${num1(wi.quality_score)}</div><div class="qlabel">${esc(wi.verdict || "")}</div>`
    : `<button class="btn-ghost btn-sm" data-lookup="${esc(wi.symbol)}">Analyze</button>`;
  return `<div class="pick" data-pick="${esc(wi.symbol)}">
    <div class="pk-rank">★</div>
    <div class="pk-main">
      <div class="pk-top"><span class="sym">${esc(wi.symbol)}</span>
        ${wi.rank ? `<span class="tag">#${wi.rank}</span>` : ""}
        ${wi.mispricing ? '<span class="chip mispriced">mispriced</span>' : ""}
        ${!wi.in_system ? '<span class="chip notsys">not analyzed</span>' : ""}
        <span class="pk-price">${wi.price != null ? money(wi.price) : ""}</span></div>
      <div class="pk-sub">${esc(wi.note || "")}</div></div>
    <div class="pk-score">${badge}</div>
    <button class="pk-x" data-unwatch="${esc(wi.symbol)}" title="Remove from watchlist">✕</button></div>`;
}

// ---- Market Outlook: daily grounded market summary + sentiment trend ----

async function loadOutlook() {
  const body = document.getElementById("ol-body");
  if (!body) return;
  try {
    const d = await api("/picks/outlook");        // cached per day; slow only on first generation
    if (document.getElementById("ol-body")) renderOutlook(d.outlook || {}, d.history || []);
  } catch (e) {
    const b = document.getElementById("ol-body");
    if (b) b.innerHTML = `<div class="sublabel" style="color:var(--dim)">Outlook unavailable: ${esc(e.message)}</div>`;
  }
}

function renderOutlook(o, hist) {
  const body = document.getElementById("ol-body");
  if (!body) return;
  if (o.error) { body.innerHTML = `<div class="sublabel" style="color:var(--dim)">${esc(o.error)}</div>`; return; }
  const st = o.stats || {}, sent = o.sentiment || {};
  const dateEl = document.getElementById("ol-date");
  if (dateEl) dateEl.textContent = o.date || "";
  const score = sent.score;
  const sCls = score == null ? "flat" : score > 15 ? "up" : score < -15 ? "down" : "flat";
  const fmtPct = (v) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`);
  const statChips = [
    ["SPY day", fmtPct(st.spy_day_pct), st.spy_day_pct],
    st.best_sector ? [`Best · ${st.best_sector.name}`, fmtPct(st.best_sector.pct), st.best_sector.pct] : null,
    st.worst_sector ? [`Worst · ${st.worst_sector.name}`, fmtPct(st.worst_sector.pct), st.worst_sector.pct] : null,
    ["Breadth (adv.)", st.breadth_advancing_pct != null ? `${st.breadth_advancing_pct}%` : "—", (st.breadth_advancing_pct || 50) - 50],
    ["Mispriced flags", st.mispriced_count != null ? String(st.mispriced_count) : "—", 0],
  ].filter(Boolean).map(([l, v, sign]) => `<div class="mchip"><span>${esc(l)}</span><b class="${sign > 0 ? "up" : sign < 0 ? "dn" : ""}">${esc(v)}</b></div>`).join("");
  const themes = (o.themes || []).map((t) => `<div class="ol-theme">
      <span class="ns ns-${esc((t.sentiment || "neutral").toLowerCase())}">${esc(t.sentiment || "neutral")}</span>
      <div><b>${esc(t.name || "")}</b><div class="ol-td">${esc(t.detail || "")}</div></div></div>`).join("");
  const watch = (o.watch || []).length ? `<ul class="reasons ol-watch">${o.watch.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "";
  const summary = (o.summary || "").split(/\n+/).map((p) => `<p class="para">${esc(p)}</p>`).join("");
  body.innerHTML = `<div class="ol-grid">
      <div class="ol-main">
        <div class="ol-head"><div class="sent-chip sc-${sCls} ol-score"><span class="sc-l">Sentiment</span>
            <b class="sc-v">${score == null ? "—" : (score > 0 ? "+" : "") + score.toFixed(0)}</b>
            <span class="sc-n">${sent.n_positive || 0}▲ ${sent.n_negative || 0}▼ ${sent.n_neutral || 0}·</span></div>
          <div class="ol-headline">${esc(o.headline || "")}</div></div>
        ${summary}
        ${themes ? `<div class="ol-themes">${themes}</div>` : ""}
        ${watch ? `<div class="ol-wl"><span class="lbl">Watching</span>${watch}</div>` : ""}
      </div>
      <div class="ol-side">
        <div class="lbl">Sentiment trend</div>
        ${outlookTrend(hist)}
        <div class="mrow ol-stats">${statChips}</div>
      </div></div>`;
}

// Daily sentiment score history as a small zero-anchored line (HYDRA style).
function outlookTrend(hist) {
  const pts = (hist || []).filter((h) => h.score != null);
  if (!pts.length) return `<div class="sublabel" style="color:var(--dim)">First data point today — trend builds daily.</div>`;
  const W = 300, H = 90, padY = 8;
  const lo = Math.min(-20, ...pts.map((p) => p.score)), hi = Math.max(20, ...pts.map((p) => p.score));
  const x = (i) => (pts.length === 1 ? W / 2 : (i / (pts.length - 1)) * W);
  const y = (v) => padY + (1 - (v - lo) / (hi - lo)) * (H - 2 * padY);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.score).toFixed(1)}`).join(" ");
  const dots = pts.map((p, i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(p.score).toFixed(1)}" r="3" fill="${p.score >= 0 ? "var(--up)" : "var(--down)"}"><title>${esc(p.date)}: ${p.score}</title></circle>`).join("");
  const last = pts[pts.length - 1];
  return `<svg class="ol-trend" viewBox="0 0 ${W} ${H}">
      <line x1="0" x2="${W}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}" class="pc-zero"/>
      ${pts.length > 1 ? `<path d="${line}" class="pc-line" stroke="${last.score >= 0 ? "var(--up)" : "var(--down)"}"/>` : ""}${dots}</svg>
    <div class="pc-ax"><span>${esc(pts[0].date)}</span><span>${esc(last.date)}</span></div>`;
}

function wirePicksRun() {
  const diff = $("#pk-diff");
  if (diff) diff.onclick = () => { _pkOffset += 10; render("picks"); };
  const b = $("#pk-run"); if (!b) return;
  b.onclick = async () => {
    await runOp("picks:screen", "Screening…", async () => {
      try {
        const r = await api("/picks/refresh", { method: "POST", body: { ingest: false } });
        if (r.error) toast(r.error, "err"); else toast(`Re-scored — ${r.count} ranked`);
      } catch (err) { toast(err.message, "err"); }
    });
    _pkOffset = 0; render("picks");
  };
}

function pickRow(p, isOwned) {
  const vClass = VERDICT_CLASS[p.verdict] || "v-watch";
  const m = p.multiples || {};
  const mult = [m.pe && `P/E ${num1(m.pe)}`, m.ev_ebitda && `EV/EBITDA ${num1(m.ev_ebitda)}`, m.pb && `P/B ${num1(m.pb)}`]
    .filter(Boolean).join(" · ");
  return `<div class="pick" data-pick="${esc(p.symbol)}">
    <div class="pk-rank">#${p.rank}</div>
    <div class="pk-main">
      <div class="pk-top"><span class="sym">${esc(p.symbol)}</span>
        <span class="tag">${esc(p.sector || "")}</span>
        ${p.mispricing ? '<span class="chip mispriced">mispriced</span>' : ""}
        ${isOwned ? '<span class="chip owned-chip">owned</span>' : ""}
        <span class="pk-price">${p.price != null ? money(p.price) : ""}</span></div>
      <div class="pk-sub">${esc(mult || "valuation pending")}</div></div>
    <div class="pk-score">
      <div class="qbadge ${vClass}">${num1(p.quality_score)}</div>
      <div class="qlabel">${esc(p.verdict || "")}</div></div>
    <button class="pk-star ${_watched.has(p.symbol) ? "on" : ""}" data-star="${esc(p.symbol)}" title="${_watched.has(p.symbol) ? "On watchlist" : "Add to watchlist"}">${_watched.has(p.symbol) ? "★" : "☆"}</button>
    ${isOwned ? "" : `<button class="pk-x" data-close="${esc(p.symbol)}" title="Not interested">✕</button>`}</div>`;
}

async function openPick(symbol) {
  openDrawer(`<div class="dw-h"><h2>${esc(symbol)}</h2><button class="btn-ghost btn-sm x" onclick="closeDrawer()">Close</button></div>
    <div class="dw-b"><div class="dw-loading"><span class="spinner"></span> Loading score &amp; chart…</div></div>`);
  let detail;
  try {
    detail = await api(`/picks/${encodeURIComponent(symbol)}`);       // fast: score, chart, profile, raw news
    renderPickDetail(symbol, detail);
  } catch (e) {
    const b = $("#drawer .dw-b"); if (b) b.innerHTML = `<div class="empty">${ICONS.check}<div>Could not load ${esc(symbol)}: ${esc(e.message)}</div></div>`;
    return;
  }
  // Second phase: the Claude analysis (thesis, competitors, semantic news). Fill loaders when ready.
  if (detail.analysis) { fillAnalysis(detail.analysis); }
  else {
    try { fillAnalysis(await api(`/picks/${encodeURIComponent(symbol)}/analysis`)); }
    catch (e) { fillAnalysis({ error: e.message }); }
  }
}

const analyzing = () => `<div class="analyzing"><span class="spinner"></span> Analysis in the works…</div>`;

function renderPickDetail(symbol, d) {
  const s = d.scores || {}, sub = s.subscores || {}, m = (s.multiples) || {}, prof = d.profile || {};
  const vClass = VERDICT_CLASS[s.verdict] || "v-watch";
  const bars = [["Profitability", sub.profitability], ["Growth", sub.growth], ["Balance sheet", sub.balance_sheet], ["Cash conversion", sub.cash_conversion]]
    .map(([k, v]) => `<div class="sb"><span class="sb-l">${k}</span><div class="sb-t"><div class="sb-f" style="width:${Math.max(0, Math.min(100, v || 0))}%"></div></div><span class="sb-v">${num1(v)}</span></div>`).join("");
  const multRow = [["P/E", m.pe], ["P/S", m.ps], ["P/B", m.pb], ["EV/EBITDA", m.ev_ebitda], ["PEG", m.peg]]
    .map(([k, v]) => `<div class="mchip"><span>${k}</span><b>${num1(v)}</b></div>`).join("");
  const newsList = (d.news || []).slice(0, 8).map((n, i) => `<a class="news" data-newsidx="${i}" href="${esc(n.url) || "#"}" target="_blank" rel="noopener">
      <div class="news-h">${esc(n.headline || "")}</div><div class="news-m">${esc(n.date || "")} · ${esc(n.source || "")}</div>
      <div class="news-ai" data-newsai="${i}"></div></a>`).join("")
    || `<div class="sublabel" style="color:var(--dim)">No recent headlines.</div>`;

  const officers = (d.officers || []).slice(0, 6).map((o) => `<div class="off">
      <div class="off-n">${esc(o.name)}</div>
      <div class="off-t">${esc(o.title)}${o.age ? ` · ${o.age}` : ""}${o.total_pay ? ` · ${money(o.total_pay)}` : ""}</div></div>`).join("")
    || `<div class="sublabel" style="color:var(--dim)">No officer data.</div>`;

  el("drawer").innerHTML = `<div class="dw-h">
      <div><h2>${esc(symbol)} <span class="qbadge ${vClass}" style="vertical-align:middle">${num1(s.quality_score)}</span></h2>
        <div class="sublabel">${esc(prof.sector || "")}${prof.industry ? " · " + esc(prof.industry) : ""}${s.verdict ? " · " + esc(s.verdict) : ""}</div></div>
      <button class="pk-star dw-star ${_watched.has(symbol) ? "on" : ""}" data-star="${esc(symbol)}" onclick="toggleWatch('${esc(symbol)}')" title="Watchlist">${_watched.has(symbol) ? "★" : "☆"}</button>
      <button class="btn-ghost btn-sm x" onclick="closeDrawer()">Close</button></div>
    <div class="dw-b">
      <div class="dw-chart">${chartControls(symbol)}</div>
      ${explainer("chart")}
      <div class="ctx"><div class="lbl">Mispricing read ${infoBtn("mispricing")}</div>${explainer("mispricing")}<div class="mispr" id="dw-mispricing">${analyzing()}</div></div>
      <h3>Why our method picks it</h3><div id="dw-reasons">${analyzing()}</div>
      <h3>What it does</h3><div id="dw-problem" class="para">${analyzing()}</div>
      <h3>Leadership ${infoBtn("leadership")}</h3>${explainer("leadership")}
      <div class="offlist">${officers}</div>
      <div id="dw-leader">${analyzing()}</div>
      <h3>Score breakdown ${infoBtn("score")}</h3>${explainer("score")}<div class="subbars">${bars}</div>
      ${renderMethodology(s.methodology)}
      <h3>Valuation ${infoBtn("valuation")}</h3>${explainer("valuation")}<div class="mrow">${multRow}</div>
      <h3>Industry health</h3><div id="dw-industry" class="para">${analyzing()}</div>
      <h3>Competitors</h3><div id="dw-comps" class="comps">${analyzing()}</div>
      <h3>Risks</h3><div id="dw-risks">${analyzing()}</div>
      <h3>MD&amp;A — management narrative ${infoBtn("mdna")}</h3>${explainer("mdna")}
      <div id="dw-mdnaread">${analyzing()}</div>
      <details class="mdna-box" id="mdna-box"><summary>Read the filing excerpt${d.mdna_available ? "" : " (fetched from SEC on open)"}</summary>
        <div id="mdna-txt" class="mdna-txt"><div class="analyzing"><span class="spinner"></span> Loading MD&amp;A…</div></div></details>
      <h3>News — last quarter ${infoBtn("news")}</h3>${explainer("news")}
      <div id="dw-sentiment" class="sentrow"></div>
      <div class="newslist">${newsList}</div>
    </div>`;
  initChart(symbol, d.series || []);
  wireExplainers();
  wireMdna(symbol);
}

// ---- Graphic explainers: what each visual in the breakout actually shows ----
const EXPLAIN = {
  chart: "Every line is rebased to 0% at the window start, so slopes compare relative performance: the stock vs its GICS-sector SPDR ETF, SPY (whole market), and an equal-weight basket of its sub-industry peers. A stock sinking while its benchmarks rise is the visual form of our mispricing signal — price action the group doesn't explain. Click legend chips to hide/show a series; the % button switches to raw adjusted close (stock only, since mixed price scales aren't comparable).",
  mispricing: "The screener flags 'mispriced' mathematically: quality score ≥ 60 AND the valuation multiple in the cheapest third of its GICS sector (EV/EBITDA preferred, else P/E, else P/S). This paragraph only explains that computed flag against the multiples — it does not make the decision.",
  leadership: "The roster (name, title, age, pay) is factual data from filings/market data. The career history and assessment below it are generated by Claude from its knowledge of well-documented executive careers — treat uncertain entries as leads to verify, not facts.",
  score: "Each bar is a pillar scored 0-100: its metrics map linearly from a floor (=0) to a target (=100), clamped, then averaged. The headline badge is the weighted mean of the four pillars (weights under 'How this score is computed'). Bars show where the quality comes from — a strong badge with one weak pillar tells you exactly what to watch.",
  valuation: "Price-based multiples: latest fiscal-year SEC facts + current market price. P/E — price per $1 of earnings; P/S — per $1 of revenue; P/B — vs book equity; EV/EBITDA — whole-enterprise price (equity + debt − cash) per $1 of operating cash profit, the sector-comparison workhorse; PEG — P/E per point of earnings growth (≈1 means growth is fairly priced, <1 cheap for its growth).",
  mdna: "MD&A (Management's Discussion & Analysis) is management's own narrative from the latest SEC filing. The checks below compare what management claims against what our computed metrics show — match, partial, or mismatch. A mismatch is a forensic flag: the story and the numbers disagree.",
  news: "Articles from the last quarter, frozen at analysis time so the report is stable — each is read by Claude in the company + industry context (sentiment × relevance). The two gauges aggregate those reads deterministically: positive=+1 / negative=−1, weighted by relevance (high 1.0 / medium 0.6 / low 0.3), scaled to −100…+100. Quarter = all articles in the window; Short-term = the last two weeks only. A positive quarter with a negative short-term reading = sentiment deteriorating into our mispricing signal.",
};
function infoBtn(key) { return `<button class="info-btn" data-expl="${key}" title="What is this?">i</button>`; }
function explainer(key) { return `<div class="expl-body" data-explbody="${key}">${esc(EXPLAIN[key] || "")}</div>`; }
function wireExplainers() {
  document.querySelectorAll("#drawer .info-btn").forEach((b) => (b.onclick = (e) => {
    e.stopPropagation();
    const body = document.querySelector(`#drawer [data-explbody="${b.dataset.expl}"]`);
    if (body) body.classList.toggle("show");
  }));
}

// Lazy-load the MD&A excerpt the first time the box is opened (SEC fetch on miss).
function wireMdna(symbol) {
  const box = document.getElementById("mdna-box");
  if (!box) return;
  let loaded = false;
  box.addEventListener("toggle", async () => {
    if (!box.open || loaded) return;
    loaded = true;
    try {
      const r = await api(`/picks/${encodeURIComponent(symbol)}/mdna`);
      const docs = r.docs || [];
      const el2 = document.getElementById("mdna-txt");
      if (!docs.length) { el2.innerHTML = `<div class="sublabel" style="color:var(--dim)">${esc(r.error || "No MD&A available for this filer.")}</div>`; return; }
      el2.innerHTML = docs.map((doc) => `<div class="mdna-doc">
          <div class="mdna-h">${esc(doc.form)} · filed ${esc(doc.date)}${doc.truncated ? " · excerpt" : ""}</div>
          <pre class="mdna-pre">${esc(doc.text)}</pre></div>`).join("");
    } catch (e) {
      document.getElementById("mdna-txt").innerHTML = `<div class="sublabel" style="color:var(--dim)">MD&A load failed: ${esc(e.message)}</div>`;
    }
  });
}

// Render the exact math behind each score graphic (from scores.methodology).
function renderMethodology(mo) {
  if (!mo || !mo.pillars) return "";
  const rows = mo.pillars.map((p) => `<div class="mth-p"><div class="mth-ph"><b>${esc(p.name)}</b> <span class="mth-w">weight ${p.weight}</span></div>
    ${(p.metrics || []).map((mt) => `<div class="mth-m"><span>${esc(mt.label)}</span><span class="mth-d">${esc(mt.definition)}</span></div>`).join("")}</div>`).join("");
  const vb = mo.verdict_buckets || {};
  const buckets = Object.keys(vb).map((k) => `${k} ${vb[k]}`).join(" · ");
  return `<details class="mth"><summary>How this score is computed</summary>
    <div class="mth-body"><p class="para">${esc(mo.summary || "")}</p>${rows}
    <div class="mth-m"><span>Verdict</span><span class="mth-d">${esc(buckets)}</span></div>
    <div class="sublabel" style="color:var(--dim);margin-top:6px">Each metric scores 0–100 across floor→target; pillar = mean of its metrics; quality = weighted mean of pillars.</div></div></details>`;
}

// Fill the Claude-generated sections once the analysis call returns.
function fillAnalysis(a) {
  const set = (id, html) => { const n = document.getElementById(id); if (n) n.innerHTML = html; };
  if (!a || a.error) {
    const msg = `<div class="sublabel" style="color:var(--dim)">${a && a.error ? esc(a.error) : "Analysis unavailable."}</div>`;
    ["dw-mispricing", "dw-reasons", "dw-problem", "dw-industry", "dw-comps", "dw-risks", "dw-leader", "dw-mdnaread"].forEach((id) => set(id, msg));
    return;
  }
  set("dw-mispricing", a.mispricing_note ? esc(a.mispricing_note) : "—");
  // Leadership: CEO career history + assessment (Claude knowledge, roster is factual).
  const lead = a.leadership || {};
  const ceo = lead.ceo || {};
  const hist = (ceo.history || []).map((h) => `<div class="lh-row">
      <span class="lh-co">${esc(h.company || "")}</span>
      <span class="lh-role">${esc(h.role || "")}</span>
      <span class="lh-yrs">${esc(h.years || "")}</span></div>`).join("");
  set("dw-leader", (ceo.name || lead.assessment) ? `
    ${ceo.name ? `<div class="lh-ceo"><b>${esc(ceo.name)}</b>${ceo.tenure ? ` <span class="tag">${esc(ceo.tenure)}</span>` : ""}</div>` : ""}
    ${hist ? `<div class="lh-hist">${hist}</div>` : ""}
    ${ceo.track_record ? `<p class="para">${esc(ceo.track_record)}</p>` : ""}
    ${lead.assessment ? `<p class="para lh-assess">${esc(lead.assessment)}</p>` : ""}
    ${(lead.notes || []).length ? `<ul class="reasons">${lead.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}`
    : `<div class="sublabel" style="color:var(--dim)">No leadership analysis.</div>`);
  // MD&A read: management tone + narrative-vs-numbers checks.
  const mr = a.mdna_read;
  set("dw-mdnaread", (mr && (mr.management_tone || (mr.checks || []).length)) ? `
    ${mr.management_tone ? `<p class="para"><b>Tone:</b> ${esc(mr.management_tone)}</p>` : ""}
    ${(mr.checks || []).length ? `<div class="mrchecks">${mr.checks.map((c) => `<div class="mrc">
        <span class="mrc-v mrc-${esc((c.verdict || "partial").toLowerCase())}">${esc(c.verdict || "")}</span>
        <div class="mrc-b"><div class="mrc-claim">"${esc(c.claim || "")}"</div><div class="mrc-data">${esc(c.our_data || "")}</div></div></div>`).join("")}</div>` : ""}`
    : `<div class="sublabel" style="color:var(--dim)">No MD&A on file for this name yet — open the filing excerpt below to fetch it, then regenerate.</div>`);
  set("dw-reasons", (a.reasons || []).length ? `<ul class="reasons">${a.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : "—");
  set("dw-problem", esc(a.problem_solved || "—"));
  set("dw-industry", esc(a.industry_health || "—"));
  set("dw-comps", (a.competitors || []).length
    ? a.competitors.map((c) => `<div class="comp"><b>${esc(c.name || c.ticker || "")}</b>${c.ticker ? ` <span class="tag">${esc(c.ticker)}</span>` : ""}<div class="comp-c">${esc(c.compare || "")}</div></div>`).join("")
    : `<div class="sublabel" style="color:var(--dim)">No competitors identified.</div>`);
  set("dw-risks", (a.risks || []).length ? `<ul class="reasons risk">${a.risks.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : "—");

  // News: render from the analysis SNAPSHOT (deterministic — annotation indices
  // refer to this exact list, not today's live feed), with sentiment gauges.
  const snap = a.news_snapshot;
  const annos = {};
  (a.news_analysis || []).forEach((na) => { if (na && na.index != null) annos[na.index] = na; });
  if (Array.isArray(snap) && snap.length) {
    const listEl = document.querySelector("#drawer .newslist");
    if (listEl) listEl.innerHTML = snap.map((n, i) => {
      const na = annos[i];
      const ai = na ? `<div class="news-ai"><span class="ns ns-${esc((na.sentiment || "neutral").toLowerCase())}">${esc(na.sentiment || "neutral")}</span><span class="nr">${esc(na.relevance || "")} relevance</span><span class="nw">${esc(na.why_it_matters || "")}</span></div>` : "";
      return `<a class="news" href="${esc(n.url) || "#"}" target="_blank" rel="noopener">
        <div class="news-h">${esc(n.headline || "")}</div><div class="news-m">${esc(n.date || "")} · ${esc(n.source || "")}</div>${ai}</a>`;
    }).join("");
  } else {
    (a.news_analysis || []).forEach((na) => {
      const n = document.querySelector(`[data-newsai="${na.index}"]`);
      if (!n) return;
      n.innerHTML = `<span class="ns ns-${esc((na.sentiment || "neutral").toLowerCase())}">${esc(na.sentiment || "neutral")}</span><span class="nr">${esc(na.relevance || "")} relevance</span><span class="nw">${esc(na.why_it_matters || "")}</span>`;
    });
  }
  // Sentiment gauges: whole-quarter and last-2-weeks aggregates.
  const st = a.sentiment;
  const gaugeEl = document.getElementById("dw-sentiment");
  if (gaugeEl && st) {
    const chip = (label, v, n) => {
      if (v == null) return `<div class="sent-chip"><span class="sc-l">${label}</span><b class="sc-v" style="color:var(--dim)">—</b><span class="sc-n">${n} articles</span></div>`;
      const cls = v > 15 ? "up" : v < -15 ? "down" : "flat";
      return `<div class="sent-chip sc-${cls}"><span class="sc-l">${label}</span><b class="sc-v">${v > 0 ? "+" : ""}${v.toFixed(0)}</b><span class="sc-n">${n} articles</span></div>`;
    };
    gaugeEl.innerHTML = chip(`Quarter (${st.window_days || 92}d)`, st.long_term, st.n_quarter || 0)
      + chip(`Short-term (${st.short_days || 14}d)`, st.short_term, st.n_2wk || 0);
  }
}

// ---- Multi-series chart (normalized %, benchmark overlays) ----
let _chart = null; // {symbol, range, mode:'pct'|'price', bundle:{series:{name:[{date,close}]}}, hidden:Set}
const CHART_RANGES = [["1mo", "1M"], ["6mo", "6M"], ["1y", "1Y"], ["5y", "5Y"]];
const SERIES_COLORS = ["var(--up)", "var(--flow)", "var(--amber)", "var(--violet)", "var(--loss)"];

function chartControls(symbol) {
  return `<div id="chart-area" class="chart-area"><div class="sublabel" style="color:var(--dim)">Loading chart…</div></div>
    <div class="chart-bar">
      <div class="ch-ranges">${CHART_RANGES.map(([r, l]) => `<button class="chbtn" data-range="${r}">${l}</button>`).join("")}</div>
      <button class="chbtn" data-mode="1">% change</button>
      <button class="chbtn" id="chart-expand" title="Break the chart out beside the report">⤢ Expand</button>
    </div>
    <div id="chart-legend" class="chart-legend"></div>`;
}

function initChart(symbol, stockSeries) {
  _chart = { symbol, range: "1y", mode: "pct", bundle: { series: { [symbol]: stockSeries || [] } }, hidden: new Set() };
  wireChartControls();
  redrawCharts();
  loadChartBundle();
}

async function loadChartBundle() {
  const sym = _chart.symbol;
  try {
    const b = await api(`/picks/${encodeURIComponent(sym)}/chart?range=${_chart.range}`);
    if (_chart && _chart.symbol === sym) { _chart.bundle = b; redrawCharts(); }
  } catch (e) { /* keep the stock-only paint */ }
}

function wireChartControls() {
  document.querySelectorAll("#drawer [data-range]").forEach((b) => (b.onclick = () => {
    _chart.range = b.dataset.range; redrawCharts(); loadChartBundle();
  }));
  const mb = document.querySelector("#drawer [data-mode]");
  if (mb) mb.onclick = () => { _chart.mode = _chart.mode === "pct" ? "price" : "pct"; redrawCharts(); };
  const xb = document.getElementById("chart-expand");
  if (xb) xb.onclick = expandChart;
}

function _dayNum(d) { return Date.parse((d || "").length > 10 ? d.replace(" ", "T") : d + "T00:00:00Z") || 0; }

// Shared series prep for both the drawer mini-chart and the expanded pane:
// normalize each visible series (rebased % or raw price) and compute extents.
function _chartData() {
  if (!_chart) return null;
  const bundle = _chart.bundle || { series: {} };
  const stock = _chart.symbol;
  let names = Object.keys(bundle.series).filter((n) => (bundle.series[n] || []).length > 1);
  if (_chart.mode === "price") names = names.filter((n) => n === stock);   // mixed price scales aren't comparable
  names = names.filter((n) => !_chart.hidden.has(n));
  const all = Object.keys(bundle.series);
  const norm = (pts) => {
    const base = pts[0].close || 1;
    return pts.map((p) => ({ t: _dayNum(p.date), v: _chart.mode === "pct" ? (p.close / base * 100 - 100) : p.close, date: p.date, close: p.close }));
  };
  const seriesN = names.map((n) => ({
    name: n, color: SERIES_COLORS[all.indexOf(n) % SERIES_COLORS.length],
    dash: n === "Peer basket", data: norm(bundle.series[n]),
  }));
  if (!seriesN.length) return null;
  let tMin = Infinity, tMax = -Infinity, yMin = Infinity, yMax = -Infinity;
  seriesN.forEach((s) => s.data.forEach((p) => { tMin = Math.min(tMin, p.t); tMax = Math.max(tMax, p.t); yMin = Math.min(yMin, p.v); yMax = Math.max(yMax, p.v); }));
  const yPad = (yMax - yMin) * 0.08 || 1;
  return { stock, all, bundle, seriesN, tMin, tMax, yMin: yMin - yPad, yMax: yMax + yPad };
}

function _legendHtml(d) {
  return d.all.map((n) => {
    const ci = d.all.indexOf(n) % SERIES_COLORS.length;
    const off = _chart.hidden.has(n) || (_chart.mode === "price" && n !== d.stock);
    const pts = d.bundle.series[n] || [];
    const last = pts.slice(-1)[0], first = pts[0];
    const pct = last && first && first.close ? (last.close / first.close * 100 - 100) : null;
    return `<button class="lg ${off ? "off" : ""}" data-series="${esc(n)}"><span class="lg-dot" style="background:${SERIES_COLORS[ci]}"></span>${esc(n)}${pct != null ? ` <b class="${pct >= 0 ? "up" : "dn"}">${pct >= 0 ? "+" : ""}${pct.toFixed(1)}%</b>` : ""}</button>`;
  }).join("");
}

function _wireLegend(container) {
  container.querySelectorAll("[data-series]").forEach((b) => (b.onclick = () => {
    const n = b.dataset.series;
    if (_chart.hidden.has(n)) _chart.hidden.delete(n); else _chart.hidden.add(n);
    redrawCharts();
  }));
}

function redrawCharts() {
  drawChart();
  if (document.getElementById("chart-pop")) drawBigChart();
}

function drawChart() {
  if (!_chart) return;
  const area = document.getElementById("chart-area"), legend = document.getElementById("chart-legend");
  if (!area) return;
  const d = _chartData();
  if (!d) { area.innerHTML = `<div class="sublabel" style="color:var(--dim)">No price history available.</div>`; if (legend) legend.innerHTML = ""; return; }

  const W = 640, H = 200;
  const x = (t) => (d.tMax === d.tMin ? 0 : (t - d.tMin) / (d.tMax - d.tMin) * W);
  const y = (v) => H - (d.yMax === d.yMin ? 0.5 : (v - d.yMin) / (d.yMax - d.yMin)) * H;
  const grid = [0.25, 0.5, 0.75].map((f) => `<line x1="0" x2="${W}" y1="${(H * f).toFixed(0)}" y2="${(H * f).toFixed(0)}" class="pc-grid"/>`).join("");
  const paths = d.seriesN.map((s) => {
    const path = s.data.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
    return `<path class="pc-line" d="${path}" stroke="${s.color}"${s.dash ? ` stroke-dasharray="5 4"` : ""}/>`;
  }).join("");
  const zero = _chart.mode === "pct" ? `<line x1="0" x2="${W}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}" class="pc-zero"/>` : "";
  const first = d.seriesN[0].data[0], last = d.seriesN[0].data[d.seriesN[0].data.length - 1];
  area.innerHTML = `<svg class="pchart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${grid}${zero}${paths}</svg>
    <div class="pc-ax"><span>${esc(first ? first.date : "")}</span>
      <span>${_chart.mode === "pct" ? "% change" : money(d.yMax)}</span>
      <span>${esc(last ? last.date : "")}</span></div>`;

  if (legend) { legend.innerHTML = _legendHtml(d); _wireLegend(legend); }
  document.querySelectorAll("#drawer [data-range]").forEach((b) => b.classList.toggle("on", b.dataset.range === _chart.range));
  const mb = document.querySelector("#drawer [data-mode]"); if (mb) mb.classList.toggle("on", _chart.mode === "pct");
}

// ---- Expanded chart: breaks out of the drawer and fills the rest of the screen ----

function expandChart() {
  if (!_chart || document.getElementById("chart-pop")) return;
  const pop = document.createElement("div");
  pop.id = "chart-pop";
  pop.className = "chart-pop";
  pop.innerHTML = `<div class="cp-h">
      <h2>${esc(_chart.symbol)} <span class="cp-sub">detailed chart — vs benchmarks</span></h2>
      <div class="ch-ranges">${CHART_RANGES.map(([r, l]) => `<button class="chbtn" data-brange="${r}">${l}</button>`).join("")}</div>
      <button class="chbtn" data-bmode="1">% change</button>
      <button class="btn-ghost btn-sm" id="cp-close">✕ Close</button></div>
    <div id="big-chart-area" class="cp-area"></div>
    <div id="big-chart-legend" class="chart-legend cp-legend"></div>`;
  document.body.appendChild(pop);
  requestAnimationFrame(() => pop.classList.add("show"));
  pop.querySelectorAll("[data-brange]").forEach((b) => (b.onclick = () => {
    _chart.range = b.dataset.brange; redrawCharts(); loadChartBundle();
  }));
  const mb = pop.querySelector("[data-bmode]");
  if (mb) mb.onclick = () => { _chart.mode = _chart.mode === "pct" ? "price" : "pct"; redrawCharts(); };
  pop.querySelector("#cp-close").onclick = collapseChart;
  window.addEventListener("resize", drawBigChart);
  document.addEventListener("keydown", _chartPopEsc);
  drawBigChart();
}

function collapseChart() {
  const pop = document.getElementById("chart-pop");
  if (!pop) return;
  pop.remove();
  window.removeEventListener("resize", drawBigChart);
  document.removeEventListener("keydown", _chartPopEsc);
}
function _chartPopEsc(e) { if (e.key === "Escape") collapseChart(); }

const _fmtT = (t) => { const d = new Date(t); return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`; };

function drawBigChart() {
  const area = document.getElementById("big-chart-area");
  if (!area || !_chart) return;
  const legend = document.getElementById("big-chart-legend");
  const d = _chartData();
  if (!d) { area.innerHTML = `<div class="sublabel" style="color:var(--dim)">No price history available.</div>`; if (legend) legend.innerHTML = ""; return; }

  const rect = area.getBoundingClientRect();
  const W = Math.max(560, rect.width || 0), H = Math.max(300, rect.height || 0);
  const padL = 64, padR = 88, padT = 16, padB = 28;
  const x = (t) => padL + (d.tMax === d.tMin ? 0 : (t - d.tMin) / (d.tMax - d.tMin)) * (W - padL - padR);
  const y = (v) => padT + (1 - (d.yMax === d.yMin ? 0.5 : (v - d.yMin) / (d.yMax - d.yMin))) * (H - padT - padB);
  const fmtV = (v) => _chart.mode === "pct" ? `${v >= 0 ? "+" : ""}${v.toFixed(1)}%` : `$${v >= 100 ? v.toFixed(0) : v.toFixed(2)}`;

  // Gridlines with value labels (6 rows) + ~7 date ticks.
  let grid = "";
  for (let i = 0; i <= 5; i++) {
    const v = d.yMin + (i / 5) * (d.yMax - d.yMin);
    const gy = y(v).toFixed(1);
    grid += `<line x1="${padL}" x2="${W - padR}" y1="${gy}" y2="${gy}" class="pc-grid"/>
      <text x="${padL - 8}" y="${gy}" class="bc-yl" text-anchor="end" dominant-baseline="middle">${fmtV(v)}</text>`;
  }
  let xticks = "";
  for (let i = 0; i <= 6; i++) {
    const t = d.tMin + (i / 6) * (d.tMax - d.tMin);
    const gx = x(t).toFixed(1);
    xticks += `<text x="${gx}" y="${H - 8}" class="bc-xl" text-anchor="${i === 0 ? "start" : i === 6 ? "end" : "middle"}">${_fmtT(t)}</text>`;
  }
  const zero = _chart.mode === "pct"
    ? `<line x1="${padL}" x2="${W - padR}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}" class="pc-zero"/>` : "";
  const paths = d.seriesN.map((s) => {
    const path = s.data.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
    return `<path class="pc-line bc-line" d="${path}" stroke="${s.color}"${s.dash ? ` stroke-dasharray="6 5"` : ""}/>`;
  }).join("");
  // Last-value markers at the right edge.
  const lastMarks = d.seriesN.map((s) => {
    const p = s.data[s.data.length - 1];
    return `<circle cx="${x(p.t).toFixed(1)}" cy="${y(p.v).toFixed(1)}" r="3.5" fill="${s.color}"/>
      <text x="${W - padR + 8}" y="${y(p.v).toFixed(1)}" class="bc-last" fill="${s.color}" dominant-baseline="middle">${esc(s.name)} ${fmtV(p.v)}</text>`;
  }).join("");

  area.innerHTML = `<svg id="bc-svg" class="bc-svg" viewBox="0 0 ${W.toFixed(0)} ${H.toFixed(0)}">
      ${grid}${xticks}${zero}${paths}${lastMarks}
      <g id="bc-cross"></g>
      <rect id="bc-capture" x="${padL}" y="${padT}" width="${(W - padL - padR).toFixed(0)}" height="${(H - padT - padB).toFixed(0)}" fill="transparent"/>
    </svg><div id="bc-tip" class="bc-tip" style="display:none"></div>`;

  // Crosshair + tooltip: nearest point per visible series at the hovered time.
  const svg = document.getElementById("bc-svg"), cross = document.getElementById("bc-cross"), tip = document.getElementById("bc-tip");
  const capture = document.getElementById("bc-capture");
  capture.addEventListener("mousemove", (ev) => {
    const box = svg.getBoundingClientRect();
    const mx = (ev.clientX - box.left) * (W / box.width);
    const t = d.tMin + Math.max(0, Math.min(1, (mx - padL) / (W - padL - padR))) * (d.tMax - d.tMin);
    let rows = "", dots = "", tipDate = "";
    d.seriesN.forEach((s) => {
      let best = s.data[0], bd = Infinity;
      for (const p of s.data) { const dist = Math.abs(p.t - t); if (dist < bd) { bd = dist; best = p; } }
      dots += `<circle cx="${x(best.t).toFixed(1)}" cy="${y(best.v).toFixed(1)}" r="4" fill="${s.color}" stroke="rgba(0,0,0,.5)"/>`;
      rows += `<div class="bt-row"><span class="lg-dot" style="background:${s.color}"></span>${esc(s.name)}<b>${fmtV(best.v)}</b>${_chart.mode === "pct" && s.name === d.stock ? `<span class="bt-px">${money(best.close)}</span>` : ""}</div>`;
      if (s.name === d.stock || !tipDate) tipDate = best.date || _fmtT(best.t);
    });
    cross.innerHTML = `<line x1="${x(t).toFixed(1)}" x2="${x(t).toFixed(1)}" y1="${padT}" y2="${H - padB}" class="bc-xline"/>` + dots;
    tip.innerHTML = `<div class="bt-date">${esc(tipDate)}</div>${rows}`;
    tip.style.display = "block";
    const tipX = ev.clientX - box.left + 16, flip = tipX > box.width - 220;
    tip.style.left = flip ? `${ev.clientX - box.left - 16 - tip.offsetWidth}px` : `${tipX}px`;
    tip.style.top = `${Math.min(ev.clientY - box.top + 12, box.height - tip.offsetHeight - 8)}px`;
  });
  capture.addEventListener("mouseleave", () => { cross.innerHTML = ""; tip.style.display = "none"; });

  if (legend) { legend.innerHTML = _legendHtml(d); _wireLegend(legend); }
  document.querySelectorAll("#chart-pop [data-brange]").forEach((b) => b.classList.toggle("on", b.dataset.brange === _chart.range));
  const mb = document.querySelector("#chart-pop [data-bmode]"); if (mb) mb.classList.toggle("on", _chart.mode === "pct");
}

function openDrawer(html) {
  el("drawer").innerHTML = html;
  el("drawer").classList.add("show");
  const sc = el("drawer-scrim"); sc.classList.add("show"); sc.onclick = closeDrawer;
}
function closeDrawer() {
  collapseChart();
  el("drawer").classList.remove("show");
  el("drawer-scrim").classList.remove("show");
}
window.closeDrawer = closeDrawer;

// ============================================================ SOCCER (WC 2026 model)
const scPct = (v, d = 0) => (v == null ? "—" : (v * 100).toFixed(d) + "%");
const scDec = (v) => (v == null ? "—" : (+v).toFixed(2));
let soccerBook = null;      // selected sportsbook (persisted in settings)
let soccerData = null;      // last /api/soccer payload (all books ship at once)
let soccerLineupKick = 0;   // last auto lineup-refresh, to avoid ws-driven loops

const scPill = (p) => (p >= 0.85 ? "v-strong" : p >= 0.7 ? "v-solid" : p >= 0.55 ? "v-watch" : "v-weak");
const scPrice = (sel, book) => (sel.prices && sel.prices[book] != null ? sel.prices[book] : null);
const LINEUP_CHIP = {
  confirmed: `<span class="lineup-chip ok">✓ lineups in</span>`,
  probable: `<span class="lineup-chip mid">~ probable XI</span>`,
  unknown: `<span class="lineup-chip off">lineups pending</span>`,
};

VIEWS.soccer = async () => {
  const d = await api("/soccer").catch(() => null);
  soccerData = d;
  const root = el("view-soccer");
  if (!d || d.available === false) {
    root.innerHTML = `<div class="viewhead"><h1>Soccer</h1></div>
      <div class="card glass connect" style="margin-top:16px">
        <div style="font-size:16px;font-weight:650">WC 2026 model not found</div>
        <div class="steps">Atlas shells into the sibling World Cup model to price matches.
        Point <code>ATLAS_WC_ROOT</code> at the project (default <code>~/Desktop/WC 2026</code>) and make sure its venv exists.</div></div>`;
    return;
  }
  if (!soccerBook) soccerBook = (await api("/settings/soccer_book").catch(() => ({}))).value || (d.books || [])[0] || null;
  if (soccerBook && d.books?.length && !d.books.includes(soccerBook)) soccerBook = d.books[0];

  const matches = d.matches || [];
  const header = `<div class="viewhead"><h1>Soccer</h1>
      <div class="sub">World Cup 2026 · Dixon-Coles model vs market</div><div class="spacer"></div>
      ${d.demo_odds ? `<span class="chip due" title="No ODDS_API_KEY — synthetic book prices from the model">demo lines</span>` : ""}
      ${d.lineups_checked_at ? `<span class="chip p-low">lineups ${agoMin(d.lineups_checked_at)}m ago</span>` : ""}
      ${d.books?.length ? `<select id="sc-book" title="Sportsbook">${d.books.map((b) => `<option value="${esc(b)}" ${b === soccerBook ? "selected" : ""}>${esc(bookName(b))}</option>`).join("")}</select>` : ""}
      <button class="btn-ghost btn-sm" id="sc-lineups" data-op="soccer:lineups" data-op-label="Checking…">Check lineups</button>
      <button class="btn-flow btn-sm" id="sc-run" data-op="soccer:refresh" data-op-label="Running model…">Refresh model</button></div>`;

  if (!matches.length) {
    root.innerHTML = header + `<div class="card glass" style="margin-top:16px"><div class="empty">${ICONS.check}
      <div>No priced matches cached yet. Hit <b>Refresh model</b> to run the pipeline (fixtures → features → model → odds).</div></div></div>`;
    wireSoccerButtons(); return;
  }

  // Flatten selections; lineup-cleared ones rank, pending ones dim at the bottom.
  const rows = [];
  matches.forEach((m) => (m.selections || []).forEach((s, i) => rows.push({ m, s, i })));
  const cleared = rows.filter((r) => r.m.lineup?.cleared).sort((a, b) => (b.s.p_model - a.s.p_model) || (b.s.ev - a.s.ev));
  const pending = rows.filter((r) => !r.m.lineup?.cleared).sort((a, b) => b.s.p_model - a.s.p_model);

  root.innerHTML = header
    + `<div class="grid cols-3" style="margin-top:16px">${(d.parlays || []).map(parlayCard).join("")}</div>`
    + `<div class="cat-h" style="margin-top:20px">Best singles — lineup-checked, sorted by probability to hit</div>
       <div class="picklist">${cleared.slice(0, 30).map((r, i) => soccerRow(r, i + 1)).join("")
         || `<div class="card glass"><div class="empty">${ICONS.check}<div>No lineup-cleared picks yet — check lineups closer to kickoff.</div></div></div>`}</div>`
    + (pending.length ? `<div class="cat-h" style="margin-top:20px">Not recommended — lineups pending or key players out</div>
       <div class="picklist sc-dim">${pending.slice(0, 12).map((r) => soccerRow(r, null)).join("")}</div>` : "");

  wireSoccerButtons();
  const sel = $("#sc-book");
  if (sel) sel.onchange = () => {
    soccerBook = sel.value;
    api("/settings", { method: "PUT", body: { key: "soccer_book", value: soccerBook } }).catch(() => {});
    render("soccer");
  };
  root.querySelectorAll("[data-scsel]").forEach((c) => (c.onclick = (e) => {
    e.stopPropagation();
    const [mid, idx] = c.dataset.scsel.split(":");
    openSoccerSel(+mid, +idx);
  }));
  root.querySelectorAll("[data-scparlay]").forEach((c) => (c.onclick = (e) => {
    if (e.target.closest("[data-scsel]")) return;
    openSoccerParlay(+c.dataset.scparlay);
  }));

  // Lineups go stale fast around kickoff — quietly re-pull if older than 15 min.
  const stale = !d.lineups_checked_at || (Date.now() / 1000 - d.lineups_checked_at) > 900;
  if (stale && Date.now() - soccerLineupKick > 120000) {
    soccerLineupKick = Date.now();
    api("/soccer/lineups/refresh", { method: "POST" }).catch(() => {});
  }
};
const agoMin = (ts) => Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
const bookName = (b) => ({ fanatics: "Fanatics", fanduel: "FanDuel", bet365: "bet365", draftkings: "DraftKings", betmgm: "BetMGM", pinnacle: "Pinnacle", williamhill_us: "Caesars", betonlineag: "BetOnline", lowvig: "LowVig", mybookieag: "MyBookie", betrivers: "BetRivers", ballybet: "Bally Bet", espnbet: "ESPN BET", hardrockbet: "Hard Rock", unibet_uk: "Unibet UK", unibet_nl: "Unibet NL", betfair_ex_uk: "Betfair", demo_a: "Demo A", demo_b: "Demo B", demo_c: "Demo C" }[b] || b);

function wireSoccerButtons() {
  const run = $("#sc-run");
  if (run) run.onclick = async () => {
    await runOp("soccer:refresh", "Running model…", async () => {
      try {
        const r = await api("/soccer/refresh", { method: "POST", body: {} });
        if (r.error) toast(r.error, "err"); else toast(`Model refreshed — ${r.matches} matches priced${r.demo_odds ? " (demo lines)" : ""}`);
      } catch (err) { toast(err.message, "err"); }
    });
    render("soccer");
  };
  const lu = $("#sc-lineups");
  if (lu) lu.onclick = async () => {
    await runOp("soccer:lineups", "Checking…", async () => {
      try { const r = await api("/soccer/lineups/refresh", { method: "POST" }); toast(`Lineups checked for ${r.checked} match${r.checked === 1 ? "" : "es"}`); }
      catch (err) { toast(err.message, "err"); }
    });
    render("soccer");
  };
}

function soccerRow({ m, s, i }, rank) {
  const price = scPrice(s, soccerBook) ?? s.best_odds;
  const atBook = scPrice(s, soccerBook) != null;
  const ev = s.p_model * price - 1;
  const lu = m.lineup || { status: "unknown" };
  const outs = [...(lu.key_out?.home || []), ...(lu.key_out?.away || [])];
  return `<div class="pick" data-scsel="${m.match_id}:${i}">
    <div class="pk-rank">${rank ? "#" + rank : ""}</div>
    <div class="pk-main">
      <div class="pk-top"><span class="sym">${esc(s.label)}</span>
        <span class="tag">${esc(s.market_label)}</span>
        ${LINEUP_CHIP[lu.status] || ""}
        <span class="pk-price">${scDec(price)}${atBook ? "" : ` <span class="tag" title="not priced at ${esc(bookName(soccerBook))} — best book shown">${esc(bookName(s.best_book))}</span>`}</span></div>
      <div class="pk-sub">${esc(m.home)} v ${esc(m.away)} · ${esc(m.date)}${m.time ? " · " + esc(m.time) : ""} · ${esc(m.stage)}
        ${outs.length ? ` · <span style="color:var(--loss)">out: ${esc(outs.join(", "))}</span>` : ""}</div></div>
    <div class="pk-score">
      <div class="qbadge ${scPill(s.p_model)}">${scPct(s.p_model)}</div>
      <div class="qlabel">${ev >= 0 ? `<span class="pos">EV +${(ev * 100).toFixed(1)}%</span>` : `<span class="neg">EV ${(ev * 100).toFixed(1)}%</span>`}</div></div></div>`;
}

function parlayCard(p, idx) {
  if (!p.legs?.length) {
    return `<div class="card glass parlay"><div class="ch"><span class="rec-badge ${p.badge}">Recommended</span> ${esc(p.label)} 4-leg
        <span class="count">${scPct(p.target)} target</span></div>
      <div class="sublabel" style="color:var(--dim)">${esc(p.note || "Not enough cleared matches yet.")}</div></div>`;
  }
  let payout = 1, allAtBook = true;
  p.legs.forEach((l) => { const pr = scPrice(l, soccerBook); if (pr == null) allAtBook = false; payout *= pr ?? l.best_odds; });
  const ev = p.p_combined * payout - 1;
  return `<div class="card glass parlay" data-scparlay="${idx}">
    <div class="ch"><span class="rec-badge ${p.badge}">Recommended</span> ${esc(p.label)} 4-leg
      <span class="count">${scPct(p.target)} target</span></div>
    <div class="parlay-legs">${p.legs.map((l) => `
      <div class="pleg" data-scsel="${l.match_id}:${soccerSelIdx(l)}">
        <span class="pl-check">✓</span>
        <div class="pl-main"><div class="pl-t">${esc(l.label)}</div><div class="pl-s">${esc(l.match)} · ${esc(l.date)}</div></div>
        <span class="pl-p">${scPct(l.p_model)}</span></div>`).join("")}</div>
    <div class="row" style="margin-top:12px;align-items:baseline">
      <div class="bignum sm">${scPct(p.p_combined)}</div>
      <div class="sublabel" style="margin:0">to hit · pays ${payout.toFixed(2)}×${allAtBook ? ` at ${esc(bookName(soccerBook))}` : " (best-book mix)"} · EV ${ev >= 0 ? "+" : ""}${(ev * 100).toFixed(1)}%</div></div>
    ${p.note ? `<div class="sublabel" style="color:var(--amber)">${esc(p.note)}</div>` : ""}</div>`;
}
// A parlay leg carries its own selection copy; find its index in the match for the modal.
function soccerSelIdx(leg) {
  const m = (soccerData?.matches || []).find((x) => x.match_id === leg.match_id);
  if (!m) return 0;
  const i = (m.selections || []).findIndex((s) => s.market === leg.market && s.outcome === leg.outcome && s.line === leg.line);
  return Math.max(0, i);
}

function openSoccerParlay(idx) {
  const p = (soccerData?.parlays || [])[idx];
  if (!p || !p.legs?.length) return;
  const prod = p.legs.map((l) => scPct(l.p_model, 1)).join(" × ");
  let payout = 1; p.legs.forEach((l) => (payout *= scPrice(l, soccerBook) ?? l.best_odds));
  openModal(`<div class="modal-h"><h2>${esc(p.label)} parlay — the math</h2><button class="btn-ghost btn-sm x" onclick="closeModal()">Close</button></div>
    <div class="modal-b">
      <div class="ctx"><div class="lbl">Combined probability</div>
        <div class="mispr">${prod} = <b>${scPct(p.p_combined, 1)}</b> chance all four legs hit${p.fair_combined ? ` · market consensus (de-vigged) says ${scPct(p.fair_combined, 1)}` : ""}.</div></div>
      <div class="para" style="margin-bottom:14px">Legs multiply because each comes from a <b>different match</b> — outcomes across matches are
        independent, while two bets inside one match share the same Dixon-Coles scoreline grid and would be correlated.
        Every leg passed the lineup check before qualifying. Pays <b>${payout.toFixed(2)}×</b> at ${esc(bookName(soccerBook))} →
        EV ${((p.p_combined * payout - 1) * 100).toFixed(1)}% per unit staked.</div>
      <div class="cat-h" style="margin:0 0 8px">Each leg — click for the full math</div>
      ${p.legs.map((l) => `<div class="list-item" data-scsel="${l.match_id}:${soccerSelIdx(l)}" style="cursor:pointer"><div class="main"><div class="t">${esc(l.label)}</div>
        <div class="s">${esc(l.match)} · model ${scPct(l.p_model, 1)} vs market ${l.fair_p ? scPct(l.fair_p, 1) : "—"} · pays ${scDec(scPrice(l, soccerBook) ?? l.best_odds)}${scPrice(l, soccerBook) ? "" : " (best book)"}</div></div>
        <span class="qbadge ${scPill(l.p_model)}">${scPct(l.p_model)}</span></div>`).join("")}
      ${p.note ? `<div class="sublabel" style="color:var(--amber);margin-top:10px">${esc(p.note)}</div>` : ""}
    </div>`);
  el("modal").querySelectorAll("[data-scsel]").forEach((c) => (c.onclick = () => {
    const [mid, i] = c.dataset.scsel.split(":");
    openSoccerSel(+mid, +i);
  }));
}

async function openSoccerSel(matchId, selIdx) {
  let d;
  try { d = await api(`/soccer/${matchId}`); } catch (e) { toast(e.message, "err"); return; }
  const m = d.match, mp = d.model_params || {}, s = (m.selections || [])[selIdx];
  if (!s) return;
  const price = scPrice(s, soccerBook) ?? s.best_odds;
  const implied = 1 / price;
  const lu = m.lineup || { status: "unknown" };
  const outs = [...(lu.key_out?.home || []), ...(lu.key_out?.away || [])];

  // λ decomposition — the exact expected_goals() math, re-derived for display.
  const eloH = m.elo?.home, eloA = m.elo?.away;
  const sup = eloH != null && eloA != null ? (eloH - eloA) * (mp.goal_per_elo || 0) : 0;
  const confH = m.team_confidence?.home, confA = m.team_confidence?.away;
  const attH = m.attack_index?.home != null ? 1 + confH * (mp.attack_sensitivity || 0) * (m.attack_index.home - 1) : 1;
  const attA = m.attack_index?.away != null ? 1 + confA * (mp.attack_sensitivity || 0) * (m.attack_index.away - 1) : 1;
  const host = m.is_host_home ? mp.home_advantage || 0 : 0;
  const lamRows = [
    ["Neutral baseline", `${scDec(mp.baseline_goals)} expected goals per side (${scDec(2 * mp.baseline_goals)} total, moment-matched to international scoring)`],
    ...(eloH != null ? [["Elo supremacy", `(${Math.round(eloH)} − ${Math.round(eloA)}) Elo × ${mp.goal_per_elo} goals/pt = ${sup >= 0 ? "+" : ""}${scDec(sup)} goal margin, split ±${scDec(Math.abs(sup) / 2)} per side`]] : []),
    [`${esc(m.home)} club-xG nudge`, `rate × ${attH.toFixed(3)} — attack index ${scDec(m.attack_index?.home)}, trusted at ${scPct(confH)} data confidence`],
    [`${esc(m.away)} club-xG nudge`, `rate × ${attA.toFixed(3)} — attack index ${scDec(m.attack_index?.away)}, trusted at ${scPct(confA)}`],
    ...(host ? [["Host home advantage", `+${scDec(host)} goals to ${esc(m.home)} (host nation playing at home)`]] : []),
  ];

  openModal(`<div class="modal-h"><h2>${esc(s.label)}</h2><button class="btn-ghost btn-sm x" onclick="closeModal()">Close</button></div>
    <div class="modal-b">
      <div class="sublabel" style="margin-bottom:12px">${esc(m.home)} v ${esc(m.away)} · ${esc(m.date)}${m.time ? " · " + esc(m.time) : ""} · ${esc(m.stage)} ${LINEUP_CHIP[lu.status] || ""}${outs.length ? ` <span style="color:var(--loss)">out: ${esc(outs.join(", "))}</span>` : ""}</div>
      <div class="ctx"><div class="lbl">The number</div><div class="mispr">
        Model: <b>${scPct(s.p_model, 1)}</b> to hit · de-vigged market consensus: <b>${s.fair_p ? scPct(s.fair_p, 1) : "—"}</b>
        (${s.n_books} book${s.n_books === 1 ? "" : "s"}) · pays <b>${scDec(price)}</b> at ${esc(bookName(scPrice(s, soccerBook) != null ? soccerBook : s.best_book))}, implying ${scPct(implied, 1)}${s.derived ? " (stake-split from that book's 1X2 prices)" : ""}.
        ${s.prob_edge != null ? `Model edge over the market: <b>${s.prob_edge >= 0 ? "+" : ""}${scPct(s.prob_edge, 1)}</b>.` : ""}</div></div>

      <div class="cat-h" style="margin:14px 0 6px">Expected goals — how λ is built</div>
      <div class="para" style="margin-bottom:8px">Final rates: <b>${esc(m.home)} ${scDec(m.lam_home)}</b> · <b>${esc(m.away)} ${scDec(m.lam_away)}</b> expected goals
        (total ${scDec(m.exp_total)}, supremacy ${m.exp_supremacy >= 0 ? "+" : ""}${scDec(m.exp_supremacy)}).</div>
      ${lamRows.map(([label, note]) => `<div class="mth-m"><span>${label}</span><span class="mth-d">${note}</span></div>`).join("")}

      <div class="cat-h" style="margin:18px 0 6px">Scoreline probabilities — Poisson grid, Dixon-Coles ρ = ${mp.dixon_coles_rho}</div>
      ${scoreHeatmap(m)}
      <div class="para" style="margin-top:6px">Every market probability is a sum over this grid — "${esc(s.label)}" adds the cells where it cashes.
        Most likely score: <b>${esc(m.ml_score)}</b> (${scPct(m.ml_score_p, 1)}).</div>

      <div class="cat-h" style="margin:18px 0 6px">When goals come — P(≥1 goal) per 15-min bin</div>
      ${timingChart(m)}

      <div class="cat-h" style="margin:18px 0 6px">Model vs each book — this selection</div>
      ${bookBars(s)}

      <details class="mth"><summary>Method &amp; calibration</summary><div class="mth-body">
        <p class="para">Team strength blends international <b>Elo</b> (top-down) with <b>player-aggregated club xG</b> from Understat
        (bottom-up), trusted in proportion to how much of the projected XI has club data — ${scPct(m.data_confidence)} for this fixture
        (the weaker side's coverage). Goals follow independent Poissons with a Dixon-Coles low-score correction; handicap markets use a
        deliberately conservative grid (margin ×${mp.spread_margin_shrink}, variance ×${mp.spread_variance_inflation}).
        Market probabilities are de-vigged with the ${esc(mp.devig_method || "power")} method and averaged across books.</p>
        <div class="mth-m"><span>Calibration</span><span class="mth-d">goal_per_elo, baseline &amp; home edge grid-searched on 2021–24 internationals (RPS${d.calibration?.calibrated?.rps ? " " + (+d.calibration.calibrated.rps).toFixed(4) : ""}), validated on a 2025–26 holdout</span></div>
        <div class="mth-m"><span>Lineup check</span><span class="mth-d">${lu.status === "confirmed" ? "starters posted on ESPN — key players verified in" : lu.status === "probable" ? "matchday rosters visible, starters pending" : "no team sheet yet — excluded from recommendations until lineups post"}${outs.length ? " · MISSING: " + esc(outs.join(", ")) : ""}</span></div>
      </div></details>
    </div>`);
}

function scoreHeatmap(m) {
  const g = m.score_grid || [];
  if (!g.length) return "";
  const N = g.length, cell = 44, gut = 30, W = gut + N * cell + 4, H = gut + N * cell + 4;
  const maxP = Math.max(...g.flat());
  const [mi, mj] = (m.ml_score || "0-0").split("-").map(Number);
  let cells = "";
  for (let i = 0; i < N; i++) for (let j = 0; j < N; j++) {
    const p = g[i][j], a = maxP ? Math.pow(p / maxP, 0.6) : 0;
    const isML = i === mi && j === mj;
    cells += `<rect x="${gut + j * cell + 1}" y="${gut + i * cell + 1}" width="${cell - 2}" height="${cell - 2}" rx="6"
        fill="rgba(43,255,154,${(a * 0.85).toFixed(3)})" ${isML ? 'stroke="var(--flow)" stroke-width="2"' : 'stroke="rgba(255,255,255,.06)"'}/>`
      + (p >= 0.005 ? `<text x="${gut + j * cell + cell / 2}" y="${gut + i * cell + cell / 2 + 4}" text-anchor="middle" fill="${a > 0.5 ? "#04231a" : "var(--muted)"}" font-size="10.5" font-weight="600">${(p * 100).toFixed(1)}</text>` : "");
  }
  const axes = [...Array(N)].map((_, k) => `
    <text x="${gut + k * cell + cell / 2}" y="${gut - 6}" text-anchor="middle" fill="var(--dim)" font-size="10">${k}</text>
    <text x="${gut - 10}" y="${gut + k * cell + cell / 2 + 4}" text-anchor="middle" fill="var(--dim)" font-size="10">${k}</text>`).join("");
  return `<div class="sc-heat"><svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:440px;display:block">
    <text x="${gut + (N * cell) / 2}" y="9" text-anchor="middle" fill="var(--muted)" font-size="9" font-weight="700" letter-spacing="1">${esc(m.away.toUpperCase())} GOALS →</text>
    <text x="8" y="${gut + (N * cell) / 2}" text-anchor="middle" fill="var(--muted)" font-size="9" font-weight="700" letter-spacing="1" transform="rotate(-90 8 ${gut + (N * cell) / 2})">${esc(m.home.toUpperCase())} GOALS →</text>
    ${axes}${cells}</svg></div>`;
}

function timingChart(m) {
  const tb = m.time_buckets || [];
  if (!tb.length) return "";
  const maxP = Math.max(...tb.map((b) => b.p_goal));
  return `<div class="tb-chart">${tb.map((b) => `
    <div class="tb-col"><div class="tb-v">${scPct(b.p_goal)}</div>
      <div class="tb-bar"><div class="tb-fill" style="height:${maxP ? Math.round((b.p_goal / maxP) * 100) : 0}%"></div></div>
      <div class="tb-l">${esc(b.bin)}'</div></div>`).join("")}</div>
    <div class="sublabel" style="color:var(--dim)">Expected total ${scDec(m.exp_total)} split by the goal-timing prior (rises late in each half; 1H ${scDec(m.xg_1h)} xG, 2H ${scDec(m.xg_2h)} xG).</div>`;
}

function bookBars(s) {
  const rows = Object.entries(s.prices || {}).map(([b, o]) => [bookName(b), 1 / o, o, b === soccerBook]).sort((a, b) => b[1] - a[1]);
  const maxP = Math.max(s.p_model, s.fair_p || 0, ...rows.map((r) => r[1])) || 1;
  const bar = (label, p, extra, hot) => `<div class="sb"><span class="sb-l" style="width:130px;flex:0 0 130px${hot ? ";color:var(--text);font-weight:700" : ""}">${label}</span>
    <div class="sb-t"><div class="sb-f" style="width:${Math.round((p / maxP) * 100)}%${hot ? "" : ";background:rgba(255,255,255,.25)"}"></div></div>
    <span class="sb-v" style="width:96px">${scPct(p, 1)}${extra || ""}</span></div>`;
  return `<div class="subbars">
    ${bar("Model", s.p_model, "", true)}
    ${s.fair_p ? bar("Market (fair)", s.fair_p, "", true) : ""}
    ${rows.map(([n, p, o, on]) => bar(n + (on ? " ●" : ""), p, ` <span style="color:var(--dim)">@${scDec(o)}</span>`, false)).join("")}</div>
  <div class="sublabel" style="color:var(--dim)">Book bars show vig-included implied probability (1/odds); "Market (fair)" removes the vig. Model above a book = value at that price.</div>`;
}

// ============================================================ PROFILE
VIEWS.profile = async () => {
  const d = await api("/profile").catch(() => null);
  const root = el("view-profile");
  if (!d) { root.innerHTML = `<div class="viewhead"><h1>Profile</h1></div><div class="card glass" style="margin-top:16px"><div class="empty">${ICONS.trash}<div>Could not load your profile.</div></div></div>`; return; }
  const p = d.profile || {}, roles = p.roles || [], skills = p.skills || [], edu = p.education || [], hist = d.history || [];

  const pend = chatPending["profile"];
  let chatLog = hist.length ? hist.map(msgBubble).join("")
    : `<div class="pf-intro">Hey — I'm <b>Atlas</b>. This is your master profile — the CV every résumé is built from.
       I've pre-loaded it from your existing résumés (roles, education, skills). Talk to me to refine it:
       <em>"add my new role at Seaport"</em>, <em>"add a fact to Vincere about the 2.44 Sharpe"</em>,
       <em>"update my summary"</em>.</div>`;
  if (pend) chatLog += msgBubble({ role: "user", text: pend }) + workingBubble("Atlas is thinking…");

  const panel = `<div class="pf-panel">
    <div class="pf-sec"><div class="pf-h">Summary</div>
      <div class="pf-summary">${p.summary ? esc(p.summary) : '<span class="pf-dim">Not set — tell Atlas your one-liner.</span>'}</div></div>
    <div class="pf-sec"><div class="pf-h">Education <span class="pf-cnt">${edu.length}</span></div>
      ${edu.length ? edu.map((e, i) => `<div class="pf-role">
        <div class="pf-role-top"><b>${esc(e.degree || "")}</b>
          <button class="rz-mini" data-deledu="${i}" title="Remove">${ICONS.trash}</button></div>
        <div class="pf-role-sub">${esc(e.school || "")}${e.location ? " · " + esc(e.location) : ""}${e.dates ? " · " + esc(e.dates) : ""}</div>
        ${e.coursework ? `<div class="pf-edu-line"><b>Coursework:</b> ${esc(e.coursework)}</div>` : ""}
        ${e.honors ? `<div class="pf-edu-line"><b>Honors:</b> ${esc(e.honors)}</div>` : ""}</div>`).join("")
        : '<div class="pf-dim">None yet.</div>'}</div>
    <div class="pf-sec"><div class="pf-h">Roles <span class="pf-cnt">${roles.length}</span></div>
      ${roles.length ? roles.map((r, i) => `<div class="pf-role">
        <div class="pf-role-top"><b>${esc(r.role)}</b> · ${esc(r.company)}
          <button class="rz-mini" data-delrole="${i}" title="Remove">${ICONS.trash}</button></div>
        <div class="pf-role-sub">${esc(r.dates || "")}${r.category_hint ? " · " + esc(r.category_hint) : ""}</div>
        <ul class="pf-facts">${(r.facts || []).map((f) => `<li>${esc(f)}</li>`).join("")}</ul></div>`).join("")
        : '<div class="pf-dim">No roles captured yet.</div>'}</div>
    <div class="pf-sec"><div class="pf-h">Skills <span class="pf-cnt">${skills.length}</span></div>
      <div class="pf-skills">${skills.length ? skills.map((s) => `<span class="pf-skill" data-delskill="${encodeURIComponent(s)}">${esc(s)} <b>✕</b></span>`).join("") : '<span class="pf-dim">None yet.</span>'}</div></div>
  </div>`;

  root.innerHTML = `<div class="viewhead"><h1>Profile</h1>
      <div class="sub">Your master career record — every résumé is built from this</div><div class="spacer"></div>
      ${d.claude_available ? "" : '<span class="chip due">Claude offline</span>'}</div>
    <div class="pf-layout">
      <div class="pf-chatcol">
        <div class="pf-log" id="pf-log">${chatLog}</div>
        <div class="pf-inbar">
          <textarea id="pf-in" class="pf-input" rows="1" ${pend ? "disabled" : ""} placeholder="Talk to Atlas — add a role, a project, skills, or your summary…">${esc(inputDraft["profile"] || "")}</textarea>
          <button class="btn-flow" id="pf-send" ${pend ? "disabled" : ""}>Send</button>
        </div>
      </div>
      <div class="pf-resizer" id="pf-resizer" title="Drag to resize · double-click to reset"></div>
      ${panel}
    </div>`;

  const log = el("pf-log"); log.scrollTop = log.scrollHeight;
  const send = async () => {
    const t = $("#pf-in").value.trim(); if (!t || chatPending["profile"]) return;
    $("#pf-in").value = ""; delete inputDraft["profile"]; ensureNotify();
    chatPending["profile"] = t;
    $("#pf-send").disabled = true; $("#pf-in").disabled = true;
    log.insertAdjacentHTML("beforeend", msgBubble({ role: "user", text: t }) + workingBubble("Atlas is thinking…"));
    log.scrollTop = log.scrollHeight;
    try { const r = await api("/profile/chat", { method: "POST", body: { message: t } }); notify("Atlas updated your profile", r.reply); }
    catch (e) { toast(e.message, "err"); }
    finally { delete chatPending["profile"]; }
    render("profile");
  };
  $("#pf-send").onclick = send;
  autoGrow($("#pf-in"));
  if (!pend) $("#pf-in").focus();
  $("#pf-in").oninput = () => { inputDraft["profile"] = $("#pf-in").value; };
  $("#pf-in").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } };
  root.querySelectorAll("[data-delrole]").forEach((b) => b.onclick = async () => { await api(`/profile/role/${b.dataset.delrole}`, { method: "DELETE" }); toast("Role removed"); render("profile"); });
  root.querySelectorAll("[data-deledu]").forEach((b) => b.onclick = async () => { await api(`/profile/education/${b.dataset.deledu}`, { method: "DELETE" }); render("profile"); });
  root.querySelectorAll("[data-delskill]").forEach((b) => b.onclick = async () => { await api(`/profile/skill/${b.dataset.delskill}`, { method: "DELETE" }); render("profile"); });
  mountResizer($("#pf-resizer"), $(".pf-layout"), "--pf-panel-w", "atlas.pfPanelW", { min: 320, minLeft: 360 });
};

function msgBubble(m) { return `<div class="msgbub ${m.role === "user" ? "user" : "ai"}">${esc(m.text)}</div>`; }
function autoGrow(ta) { if (!ta) return; const f = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 140) + "px"; }; ta.addEventListener("input", f); f(); }
function ensureNotify() { try { if ("Notification" in window && Notification.permission === "default") Notification.requestPermission(); } catch (e) {} }
function notify(title, body) { try { if ("Notification" in window && Notification.permission === "granted") new Notification(title, { body: (body || "").slice(0, 180), tag: "atlas-resume" }); } catch (e) {} }

// ============================================================ RESUME
let resumeCat = "swe";
let resumeDocId = null;

VIEWS.resume = async () => {
  const d = await api("/resume").catch(() => null);
  const root = el("view-resume");
  if (!d) { root.innerHTML = `<div class="viewhead"><h1>Resume</h1></div><div class="card glass" style="margin-top:16px"><div class="empty">${ICONS.trash}<div>Could not load the resume engine.</div></div></div>`; return; }

  const cats = d.categories || [];
  if (!cats.some((c) => c.key === resumeCat)) resumeCat = (cats[0] || {}).key;
  const docs = (d.docs || {})[resumeCat] || [];
  let cur = docs.find((x) => x.id === resumeDocId) || docs.find((x) => x.base) || docs[0] || null;
  resumeDocId = cur ? cur.id : null;
  const doc = cur ? await api(`/resume/${cur.id}`).catch(() => null) : null;
  const chat = cur ? await api(`/resume/${cur.id}/chat`).catch(() => ({ history: [] })) : { history: [] };

  const tct = d.tectonic_available;
  const gh = d.corpus?.github || {}, lo = d.corpus?.local || {};
  const corpusChip = (icon, o, unit) => o.count
    ? `<span class="chip p-low" title="${o.fetched_at ? "refreshed " + agoMin(o.fetched_at) + "m ago" : ""}">${icon} ${o.count} ${unit}</span>`
    : `<span class="chip due">${icon} no ${unit}</span>`;

  const header = `<div class="viewhead"><h1>Resume</h1>
    <div class="sub">Chat to refine · visually verified with Tectonic</div><div class="spacer"></div>
    ${tct ? "" : `<span class="chip due" title="brew install tectonic">no compiler</span>`}
    ${corpusChip("GH", gh, "repos")}
    ${corpusChip("PC", lo, "projects")}
    <button class="btn-ghost btn-sm" id="rz-corpus" data-op="resume:corpus" data-op-label="Scanning…">Refresh corpus</button>
    <button class="btn-flow btn-sm" id="rz-tailor">Tailor to job</button></div>`;

  const pills = `<div class="rz-cats">${cats.map((c) =>
    `<button class="rz-cat ${c.key === resumeCat ? "on" : ""}" data-cat="${c.key}">${esc(c.label)}
       <span class="rz-cnt">${((d.docs || {})[c.key] || []).length}</span></button>`).join("")}</div>`;

  const list = docs.length ? docs.map((x) => `
    <div class="rz-item ${x.id === resumeDocId ? "on" : ""}" data-doc="${x.id}">
      <div class="rz-item-main">
        <div class="rz-item-label">${x.base ? '<span class="rz-base">BASE</span>' : ""}${esc(x.label)}</div>
        <div class="rz-item-sub">${x.variant ? esc(x.variant) + " · " : ""}${rzDate(x.updated_at)} ${fitBadge(x.fit)}</div>
      </div>
      <div class="rz-item-acts">
        <button class="rz-mini" data-rn="${x.id}" title="Rename">✎</button>
        ${x.base ? "" : `<button class="rz-mini" data-base="${x.id}" title="Set as base">★</button>`}
        ${x.base ? "" : `<button class="rz-mini" data-del="${x.id}" title="Delete">${ICONS.trash}</button>`}
      </div>
    </div>`).join("") : `<div class="rz-empty">No resumes in this category yet.</div>`;

  const rzKey = doc ? "resume:" + doc.id : null;
  const rzPend = rzKey ? chatPending[rzKey] : null;
  let chatLog = (chat.history || []).length ? chat.history.map(msgBubble).join("")
    : `<div class="pf-intro">Ask for changes in plain English —<br>
       <em>"make the Vincere role focus more on latency"</em>,
       <em>"lengthen the last bullet by discussing the A/B race"</em>,
       <em>"pull in my Seaport role from my profile"</em>.<br><br>
       I rewrite the résumé, recompile, and keep it to one clean page.</div>`;
  if (rzPend) chatLog += msgBubble({ role: "user", text: rzPend }) + workingBubble("Atlas is making changes — refining & recompiling…");

  const workspace = doc ? `
    <div class="rz-preview">
      <div class="rz-bar">
        <span class="rz-doc-title">${esc(doc.label)}</span>
        <span class="rz-fit" id="rz-fit">${fitBadge(doc.meta?.fit, true)}</span>
        <div class="spacer"></div>
        <button class="btn-ghost btn-sm" id="rz-refine" data-op="resume:refine:${doc.id}" data-op-label="Fitting…" ${tct ? "" : "disabled"} title="Force back to one clean page">Fix fit</button>
        <button class="btn-ghost btn-sm" id="rz-open" title="Open PDF in a new tab">Open</button>
      </div>
      <iframe id="rz-pdf" class="rz-pdf" src="/api/resume/${doc.id}/pdf?t=${Date.now()}"></iframe>
    </div>
    <div class="rz-chatcol">
      <div class="rz-chat-h">Refine with Atlas</div>
      <div class="pf-log" id="rz-log">${chatLog}</div>
      <div class="pf-inbar">
        <textarea id="rz-in" class="pf-input" rows="1" ${rzPend ? "disabled" : ""} placeholder="Describe a change…">${esc(inputDraft[rzKey] || "")}</textarea>
        <button class="btn-flow" id="rz-send" ${tct && !rzPend ? "" : "disabled"}>Send</button>
      </div>
    </div>` : `<div class="rz-preview"><div class="card glass"><div class="empty">${ICONS.image}
      <div>No résumé here yet. Hit <b>Tailor to job</b> to build one from a description, or add roles in <b>Profile</b>.</div></div></div></div>`;

  root.innerHTML = header + pills + `<div class="rz-layout"><div class="rz-list">${list}</div>${workspace}</div>`;

  // --- wiring ---
  root.querySelectorAll("[data-cat]").forEach((b) => b.onclick = () => { resumeCat = b.dataset.cat; resumeDocId = null; render("resume"); });
  root.querySelectorAll("[data-doc]").forEach((it) => it.onclick = (e) => {
    if (e.target.closest("[data-rn],[data-base],[data-del]")) return;
    resumeDocId = +it.dataset.doc; render("resume");
  });
  root.querySelectorAll("[data-rn]").forEach((b) => b.onclick = async () => {
    const x = docs.find((y) => y.id === +b.dataset.rn);
    const name = prompt("Rename resume", x ? x.label : "");
    if (name && name.trim()) { await api(`/resume/${b.dataset.rn}`, { method: "PATCH", body: { label: name.trim() } }); toast("Renamed"); render("resume"); }
  });
  root.querySelectorAll("[data-base]").forEach((b) => b.onclick = async () => {
    await api(`/resume/${b.dataset.base}/base`, { method: "POST" }); toast("Set as base template"); render("resume");
  });
  root.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("Delete this resume iteration?")) return;
    await api(`/resume/${b.dataset.del}`, { method: "DELETE" }); toast("Deleted");
    if (+b.dataset.del === resumeDocId) resumeDocId = null;
    render("resume");
  });

  $("#rz-corpus").onclick = async () => {
    await runOp("resume:corpus", "Scanning…", async () => {
      try { await api("/resume/corpus/refresh", { method: "POST", body: { github: true, local: true } }); toast("Corpus refreshed"); }
      catch (err) { toast(err.message, "err"); }
    });
    render("resume");
  };
  $("#rz-tailor").onclick = () => openResumeTailor(d);

  if (doc) {
    const id = doc.id;
    const rf = $("#rz-refine");
    if (rf) rf.onclick = async () => {
      await runOp("resume:refine:" + id, "Fitting…", async () => {
        try { const r = await api(`/resume/${id}/refine`, { method: "POST" }); const f = r.fit || {};
          toast(f.ok ? "Fits one clean page ✓" : `Best effort — ${f.page_count || "?"} page(s)`, f.ok ? "ok" : "err"); }
        catch (err) { toast(err.message, "err"); }
      });
      render("resume");
    };
    $("#rz-open").onclick = () => window.open(`/api/resume/${id}/pdf?t=${Date.now()}`, "_blank");
    const clog = el("rz-log"); if (clog) clog.scrollTop = clog.scrollHeight;
    autoGrow($("#rz-in"));
    $("#rz-in").oninput = () => { inputDraft[rzKey] = $("#rz-in").value; };
    const send = async () => {
      const t = $("#rz-in").value.trim(); if (!t || chatPending[rzKey]) return;
      $("#rz-in").value = ""; delete inputDraft[rzKey]; ensureNotify();
      chatPending[rzKey] = t;
      $("#rz-send").disabled = true; $("#rz-in").disabled = true;
      clog.insertAdjacentHTML("beforeend", msgBubble({ role: "user", text: t }) + workingBubble("Atlas is making changes — refining & recompiling…"));
      clog.scrollTop = clog.scrollHeight;
      try { const r = await api(`/resume/${id}/chat`, { method: "POST", body: { message: t } }); notify("Atlas finished your résumé", r.reply || "Refinement complete."); }
      catch (err) { toast(err.message, "err"); notify("Résumé refinement failed", err.message); }
      finally { delete chatPending[rzKey]; }
      render("resume");
    };
    $("#rz-send").onclick = send;
    $("#rz-in").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } };
  }
};

function openResumeTailor(d) {
  const cats = d.categories || [];
  openModal(`<div class="modal-h"><h2>Tailor to a job</h2><button class="btn-ghost btn-sm x" onclick="closeModal()">Close</button></div>
    <div class="modal-b">
      <div class="rz-form">
        <div class="rz-row"><input id="rt-label" placeholder="Company / role this is for (used as the label)">
          <select id="rt-cat">${cats.map((c) => `<option value="${c.key}" ${c.key === resumeCat ? "selected" : ""}>${esc(c.label)} base</option>`).join("")}</select></div>
        <textarea id="rt-jd" class="rz-ta" placeholder="Paste the full job description. Claude rewrites the base résumé to foreground the relevant work, pitched to this category, and auto-iterates until it fits one clean page."></textarea>
        <div class="modal-f"><button class="btn-flow" id="rt-go">Build tailored resume</button></div>
      </div>
    </div>`);
  $("#rt-go").onclick = async (e) => {
    const label = $("#rt-label").value.trim(), jd = $("#rt-jd").value.trim();
    if (!label || !jd) { toast("Label and job description are required", "err"); return; }
    const cat = $("#rt-cat").value;
    const base = ((d.docs || {})[cat] || []).find((x) => x.base) || ((d.docs || {})[cat] || [])[0];
    if (!base) { toast("No base template for that category", "err"); return; }
    spin(e, "Tailoring + fitting…");
    try {
      const r = await api("/resume/tailor", { method: "POST", body: { base_id: base.id, job_description: jd, label } });
      const f = r.fit || {};
      toast(f.ok ? "Tailored & fits one page ✓" : `Tailored — ${f.page_count || "?"} page(s), ${f.overfull || 0} overflow(s)`, f.ok ? "ok" : "err");
      resumeCat = cat; resumeDocId = r.doc?.id ?? null;
      closeModal(); render("resume");
    } catch (err) { toast(err.message, "err"); unspin(e, "Build tailored resume"); }
  };
}

// resume helpers
function reloadResumePdf(url) { const f = $("#rz-pdf"); if (f) f.src = url + (url.includes("?") ? "&" : "?") + "t=" + Date.now(); }
function rzDate(ts) { return ts ? new Date(ts * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : ""; }
function fitBadge(fit, big) {
  if (!fit) return "";
  if (fit.error) return `<span class="rz-fb err">compile error</span>`;
  const ok = fit.ok;
  const pg = fit.page_count != null ? `${fit.page_count}pg` : "?";
  const of = fit.overfull ? ` · ${fit.overfull} overflow` : "";
  return `<span class="rz-fb ${ok ? "ok" : "warn"}">${ok ? "1pg ✓" : pg + of}</span>`;
}
function spin(e, label) { const b = e.currentTarget || e.target; b._old = b.innerHTML; b.disabled = true; b.innerHTML = `<span class="spinner"></span>${label ? " " + label : ""}`; }
function unspin(e, label) { const b = e.currentTarget || e.target; b.disabled = false; b.innerHTML = label || b._old || ""; }

// ============================================================ LAYOUT ENGINE
// Widgets (cards) drag-reorder by their header within their container; order and
// width-span persist server-side under settings.layout_state, with named presets.
const LAYOUT = { live: { order: {}, span: {} }, presets: {}, activeName: "Default", icons: {} };
let layoutSaveT;
function layoutSave() {
  clearTimeout(layoutSaveT);
  layoutSaveT = setTimeout(() => api("/settings", { method: "PUT", body: { key: "layout_state",
    value: { presets: LAYOUT.presets, active: LAYOUT.activeName, live: LAYOUT.live, icons: LAYOUT.icons } } }).catch(() => {}), 600);
}
async function layoutBoot() {
  const s = await api("/settings/layout_state").catch(() => null);
  const v = (s && s.value) || {};
  LAYOUT.presets = v.presets || {};
  LAYOUT.activeName = v.active || "Default";
  LAYOUT.live = v.live || { order: {}, span: {} };
  LAYOUT.icons = v.icons || {};
  if (!LAYOUT.presets["Default"]) { LAYOUT.presets["Default"] = { order: {}, span: {} }; layoutSave(); }
  applyTabIcons();
}
const slugW = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 28) || "card";
function tagWidgets(view, root) {
  const seen = {};
  root.querySelectorAll(".card").forEach((c) => {
    const h = c.querySelector(".ch");
    let id = `${view}:${slugW(h ? (h.childNodes[0]?.textContent || h.textContent || "card") : "card")}`;
    if (seen[id] != null) { seen[id]++; id += "-" + seen[id]; } else seen[id] = 0;
    c.dataset.w = id;
  });
}
function applyLayout(view) {
  const root = document.querySelector(`.view[data-view="${view}"]`);
  if (!root) return;
  tagWidgets(view, root);
  const L = LAYOUT.live;
  const parents = new Set();
  root.querySelectorAll(".card[data-w]").forEach((c) => parents.add(c.parentElement));
  [...parents].forEach((p, ci) => {
    const cards = [...p.children].filter((x) => x.dataset && x.dataset.w);
    if (cards.length < 2) { cards.forEach((c) => wireWidget(c, p, null)); return; }
    const key = `${view}#${ci}`;
    const order = L.order[key];
    if (order && order.length) {
      [...cards].sort((a, b) => {
        const ia = order.indexOf(a.dataset.w), ib = order.indexOf(b.dataset.w);
        return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
      }).forEach((c) => p.appendChild(c));
    }
    [...p.children].filter((x) => x.dataset && x.dataset.w).forEach((c) => wireWidget(c, p, key));
  });
  root.querySelectorAll(".card[data-w]").forEach((c) => {
    if (L.span[c.dataset.w] === 2) c.classList.add("span-2");
    else if (L.span[c.dataset.w] === 1) c.classList.remove("span-2");
  });
}
function wireWidget(card, parent, orderKey) {
  const h = card.querySelector(".ch");
  if (!h || h.dataset.dnd) return;
  h.dataset.dnd = "1";
  h.classList.add("drag-handle");
  if (parent.classList.contains("grid") && !h.querySelector(".spanbtn")) {
    const b = document.createElement("button");
    b.className = "spanbtn"; b.title = "Toggle width"; b.textContent = "⇔";
    b.onclick = (e) => { e.stopPropagation();
      const now = card.classList.toggle("span-2");
      LAYOUT.live.span[card.dataset.w] = now ? 2 : 1; layoutSave();
    };
    h.appendChild(b);
  }
  if (!orderKey) return;
  h.addEventListener("mousedown", (e) => { if (!e.target.closest("button,input,select")) card.draggable = true; });
  card.addEventListener("dragstart", (e) => { card.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; });
  card.addEventListener("dragover", (e) => {
    e.preventDefault();
    const dragging = parent.querySelector(".card.dragging");
    if (!dragging || dragging === card) return;
    const r = card.getBoundingClientRect();
    parent.insertBefore(dragging, (e.clientY - r.top) < r.height / 2 ? card : card.nextSibling);
  });
  card.addEventListener("drop", (e) => e.preventDefault());
  card.addEventListener("dragend", () => {
    card.draggable = false; card.classList.remove("dragging");
    LAYOUT.live.order[orderKey] = [...parent.children].filter((x) => x.dataset && x.dataset.w).map((x) => x.dataset.w);
    layoutSave();
  });
}
function layoutSavePreset(name) { LAYOUT.presets[name] = JSON.parse(JSON.stringify(LAYOUT.live)); LAYOUT.activeName = name; layoutSave(); }
function layoutApplyPreset(name) {
  if (!LAYOUT.presets[name]) return;
  LAYOUT.live = JSON.parse(JSON.stringify(LAYOUT.presets[name]));
  LAYOUT.activeName = name; layoutSave(); render(active);
}
function layoutReset() { LAYOUT.live = { order: {}, span: {} }; layoutSave(); render(active); }

// ---- Rail icon customization ----
const ICON_LIB = {
  home: '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/>',
  checklist: '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2"/>',
  dollar: '<path d="M12 1v22"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
  chart: '<path d="M3 17l5-6 4 4 8-9"/><path d="M3 21h18"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="1"/>',
  ball: '<circle cx="12" cy="12" r="9"/><path d="M12 3v4l3.5 2.5L19 8M12 7L8.5 9.5 5 8m7 6.5l3.5 2.5 1 4m-11-4l3.5-2.5L8 21m4-6.5V11"/>',
  heart: '<path d="M20 8.5C20 5.5 17.5 4 15.5 4 14 4 12.7 4.8 12 6c-.7-1.2-2-2-3.5-2C6.5 4 4 5.5 4 8.5c0 5 8 11 8 11s8-6 8-11Z"/>',
  mail: '<path d="M3 7l9 6 9-6"/><rect x="3" y="5" width="18" height="14" rx="2"/>',
  calendar: '<rect x="3" y="4" width="18" height="17" rx="2"/><path d="M8 2v4M16 2v4M3 10h18"/>',
  person: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.5-6 8-6s8 2 8 6"/>',
  doc: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M9 13h6M9 17h6"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 0 0-2-1.2L14 2h-4l-.5 2.6a7 7 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6A7 7 0 0 0 5 12a7 7 0 0 0 .1 1.2l-2 1.6 2 3.4 2.4-1a7 7 0 0 0 2 1.2L10 22h4l.5-2.6a7 7 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6A7 7 0 0 0 19 12Z"/>',
  bolt: '<path d="M13 2L4 14h6l-1 8 9-12h-6l1-8z"/>',
  star: '<path d="M12 2l2.9 6.3 6.9.8-5.1 4.7 1.4 6.8L12 17l-6.1 3.6 1.4-6.8L2.2 9.1l6.9-.8L12 2z"/>',
  flame: '<path d="M12 2c1 4-4 6-4 11a4 4 0 0 0 8 0c0-2-1-3-1-3s3 1 3 5a6 6 0 0 1-12 0C6 9 11 7 12 2z"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/>',
  book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V4H6.5A2.5 2.5 0 0 0 4 6.5v13z"/><path d="M4 19.5A2.5 2.5 0 0 0 6.5 22H20v-2.5"/>',
  briefcase: '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2M3 12h18"/>',
};
function iconSvg(key) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICON_LIB[key] || ICON_LIB.home}</svg>`;
}
function applyTabIcons() {
  document.querySelectorAll(".tab[data-view]").forEach((t) => {
    const key = LAYOUT.icons[t.dataset.view];
    if (key && ICON_LIB[key]) t.innerHTML = iconSvg(key);
  });
}

// ============================================================ ATLAS COPILOT
let cpEl = null;
const SEND_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4 20-7z"/></svg>';
function ensureCopilot() {
  if (cpEl) return cpEl;
  const o = document.createElement("div"); o.className = "cp-overlay"; o.id = "cp-overlay";
  o.innerHTML = `<div class="cp-backdrop"></div>
    <div class="cp-orb" id="cp-orb"></div>
    <div class="cp-panel" id="cp-panel">
      <div class="cp-h"><span class="cp-dot"></span><h2>Atlas Copilot</h2><span class="cp-ctx" id="cp-ctx"></span><button class="btn-ghost btn-sm x" id="cp-close">Close</button></div>
      <div class="cp-log" id="cp-log"><div class="cp-msg ai">I can read and act on any part of Atlas. Tell me what to do — consolidate résumés, fill your profile from a résumé, capture what you changed on a tab, add todos, tweak settings…</div></div>
      <div class="cp-suggest" id="cp-suggest"></div>
      <div class="cp-in"><textarea id="cp-input" placeholder="Ask Atlas to do something…  (Enter to send)"></textarea><button class="cp-send" id="cp-send" title="Send">${SEND_SVG}</button></div>
    </div>`;
  document.body.appendChild(o);
  o.querySelector(".cp-backdrop").onclick = closeCopilot;
  o.querySelector("#cp-close").onclick = closeCopilot;
  const input = o.querySelector("#cp-input");
  const go2 = () => { const m = input.value.trim(); if (m) { input.value = ""; cpSend(m); } };
  o.querySelector("#cp-send").onclick = go2;
  input.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go2(); } };
  cpEl = o; return o;
}
function openCopilot() {
  const o = ensureCopilot();
  const orb = o.querySelector("#cp-orb"), panel = o.querySelector("#cp-panel");
  orb.className = "cp-orb"; orb.style.display = ""; panel.classList.remove("show");
  o.classList.add("show");
  const r = document.querySelector(".logo").getBoundingClientRect();
  const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
  orb.style.left = cx + "px"; orb.style.top = cy + "px";
  orb.style.setProperty("--dx", (window.innerWidth / 2 - cx) + "px");
  orb.style.setProperty("--dy", (window.innerHeight / 2 - cy) + "px");
  o.querySelector("#cp-ctx").textContent = "· acting on " + (document.querySelector(`.tab[data-view="${active}"]`)?.dataset.label || active);
  cpRenderSuggest();
  setTimeout(() => orb.classList.add("fly"), 20);   // not rAF: fires even when tab is backgrounded
  setTimeout(() => { orb.classList.add("boom"); panel.classList.add("show"); o.querySelector("#cp-input").focus(); }, 950);
  setTimeout(() => { orb.style.display = "none"; }, 1550);
  document.addEventListener("keydown", cpEsc);
}
function closeCopilot() { if (cpEl) cpEl.classList.remove("show"); document.removeEventListener("keydown", cpEsc); }
function cpEsc(e) { if (e.key === "Escape") closeCopilot(); }
function cpRenderSuggest() {
  const label = document.querySelector(`.tab[data-view="${active}"]`)?.dataset.label || active;
  const s = cpEl.querySelector("#cp-suggest");
  const sugs = [`Update Atlas with what I changed on ${label}`, "Fill my profile from my latest résumé", "Consolidate my résumés into one"];
  s.innerHTML = sugs.map((t) => `<button data-sug="${esc(t)}">${esc(t)}</button>`).join("");
  s.querySelectorAll("[data-sug]").forEach((b) => (b.onclick = () => cpSend(b.dataset.sug)));
}
async function cpSend(message) {
  const log = cpEl.querySelector("#cp-log");
  log.insertAdjacentHTML("beforeend", `<div class="cp-msg user">${esc(message)}</div><div class="cp-msg ai working" id="cp-pending"><span class="spinner"></span> Working…</div>`);
  log.scrollTop = log.scrollHeight;
  try {
    const r = await api("/copilot", { method: "POST", body: { message, view: active } });
    cpEl.querySelector("#cp-pending")?.remove();
    const chips = [...(r.applied || []).map((a) => `<span class="ac">${esc(a)}</span>`),
                   ...(r.errors || []).map((e) => `<span class="ac err">${esc(e)}</span>`)].join("");
    log.insertAdjacentHTML("beforeend", `<div class="cp-msg ai">${esc(r.reply || "Done.")}${chips ? `<div class="cp-applied">${chips}</div>` : ""}</div>`);
    log.scrollTop = log.scrollHeight;
    if (r.applied && r.applied.length) render(active);
    if (r.navigate && r.navigate !== active) go(r.navigate);
  } catch (e) {
    cpEl.querySelector("#cp-pending")?.remove();
    log.insertAdjacentHTML("beforeend", `<div class="cp-msg ai">⚠️ ${esc(e.message)}</div>`);
    log.scrollTop = log.scrollHeight;
  }
}

// ---------- boot ----------
// Wrap every view so in-flight op spinners (data-op buttons) are restored after each
// render — this is what keeps loading state alive when you leave a tab and come back.
// applyLayout runs after every render so widget order/span/drag survive re-renders.
Object.keys(VIEWS).forEach((k) => {
  const orig = VIEWS[k];
  VIEWS[k] = async (...a) => { const r = await orig(...a); restoreOps(); applyLayout(k); return r; };
});
connectWS();
document.querySelector(".logo").onclick = openCopilot;   // the orb → Atlas Copilot
layoutBoot().finally(() => {
  go("home");
  // Reingest Google Calendar on every dashboard load — pulls ALL visible calendars,
  // then repaints whatever view is active. Silently skipped when Google isn't connected.
  api("/calendar/sync", { method: "POST" }).then(() => render(active)).catch(() => {});
});
