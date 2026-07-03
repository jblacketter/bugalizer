"use strict";
/* Bugalizer queue dashboard (§5.4 + §5b Cycle 1).
 *
 * Design spine (§5b.1): everywhere analysis appears, show WHICH TIER did the
 * work — local LLM (emerald, free) vs cloud AI (violet, paid) — and WHAT
 * ALREADY RAN, so a re-scan is always a deliberate choice.
 */
const $ = (s, el = document) => el.querySelector(s);
const POLL_MS = 5000;

// Column layout: plan §5.4 — submitted → triaged → analyzing → fix_proposed → terminal.
const COLUMNS = [
  { key: "submitted",    label: "Submitted",    statuses: ["submitted", "validating"] },
  { key: "triaged",      label: "Triaged",      statuses: ["triaged", "clarification_needed", "deferred"] },
  { key: "analyzing",    label: "Analyzing",    statuses: ["analyzing", "fix_proposing"] },
  { key: "fix_proposed", label: "Fix proposed", statuses: ["fix_proposed", "fix_approved", "fix_committed", "verified"] },
  { key: "terminal",     label: "Terminal",     statuses: ["closed", "rejected", "duplicate"] },
];

// Tier identity (§5b.1): ollama = local (free); any other provider = cloud (paid).
function tierOf(provider) {
  if (!provider) return null;
  return provider === "ollama" ? "local" : "cloud";
}
const TIER_LABEL = { local: "local", cloud: "cloud" };

let apiKey = localStorage.getItem("bugalizer_api_key") || "";
let projects = {};          // id -> name
let projectsCache = [];     // full project objects (filter select + Projects modal)
let lastReports = [];       // last poll data — lets filters/toggles re-render
let lastCounts = {};        //   without waiting for the next poll
const filters = { project: "", severity: "", q: "", attn: false };
let terminalOpen = localStorage.getItem("bugalizer_terminal_open") === "1";
let terminalShowAll = false;
let openReportId = null;    // detail drawer state
let cloudArmed = false;     // inline confirm for the paid cloud call
let localArmed = false;     // inline confirm for re-analyzing an analyzed report
let pollFailures = 0;       // consecutive failed polls — debounce the "offline" banner
let detailReady = false;    // drawer has rendered good content at least once
const confirmArmed = () => cloudArmed || localArmed;

class ApiError extends Error {
  constructor(status, detail) { super(detail); this.status = status; }
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (apiKey) headers["X-API-Key"] = apiKey;
  const res = await fetch("/api/v1" + path, { ...opts, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* not JSON */ }
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

// ---------------------------------------------------------------------------
// Rendering helpers
// ---------------------------------------------------------------------------

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function age(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return Math.max(1, Math.round(s)) + "s";
  if (s < 5400) return Math.round(s / 60) + "m";
  if (s < 129600) return Math.round(s / 3600) + "h";
  return Math.round(s / 86400) + "d";
}

// Scan-state chip: "✓ local 12m" — which tier already ran, and how fresh.
function tierChip(tier, at, big = false) {
  const cls = big ? "chip big" : "chip";
  if (!at) return `<span class="${cls} off">○ ${TIER_LABEL[tier]}</span>`;
  return `<span class="${cls} tier-${tier}" title="Last completed ${TIER_LABEL[tier]} analysis">` +
         `<span class="led ${tier}"></span>✓ ${TIER_LABEL[tier]} ${age(at)}</span>`;
}

// Section provenance chip: tier LED + model + age, from an analyses row.
function runChip(run) {
  if (!run) return "";
  const tier = tierOf(run.llm_provider);
  if (!tier) return "";
  const when = run.completed_at || run.created_at;
  return `<span class="chip tier-${tier}"><span class="led ${tier}"></span>` +
         `${TIER_LABEL[tier]} · ${esc(run.llm_model || run.llm_provider)} · ${age(when)}</span>`;
}

// ---------------------------------------------------------------------------
// Board
// ---------------------------------------------------------------------------

function cardHtml(r) {
  const badges = [
    `<span class="badge sev" style="background:var(--sev-${esc(r.severity)},var(--text-muted))">${esc(r.severity)}</span>`,
    `<span class="badge">${esc(projects[r.project_id] || r.project_id)}</span>`,
    `<span class="badge">${age(r.created_at)}</span>`,
  ];
  if (r.analysis_mode && r.analysis_mode !== "auto")
    badges.push(`<span class="badge mode">${esc(r.analysis_mode)}</span>`);
  // What already ran, at whose cost (§5b.1): only show tiers that have run.
  if (r.last_local_analysis_at) badges.push(tierChip("local", r.last_local_analysis_at));
  if (r.last_cloud_analysis_at) badges.push(tierChip("cloud", r.last_cloud_analysis_at));
  if (r.failed_stage)
    badges.push(`<span class="badge fail" title="${esc(r.last_error)}">⚠ ${esc(r.failed_stage)} failed</span>`);

  let cardCls = "card";
  if (r.status === "clarification_needed") {
    cardCls += " attn";
    badges.push(`<span class="badge attn">? needs input</span>`);
  } else if (r.status === "deferred") {
    cardCls += " dim";
    badges.push(`<span class="badge">deferred</span>`);
  } else if (!COLUMNS.some(c => c.statuses[0] === r.status)) {
    badges.push(`<span class="badge">${esc(r.status)}</span>`);
  }
  return `<div class="${cardCls}" data-id="${esc(r.id)}">
    <div class="title">${esc(r.title)}</div>
    <div class="badges">${badges.join("")}</div>
  </div>`;
}

// Client-side filters (§5b.3). Board only — the drawer always shows the
// full report; global counts fall back to filtered-row counts while active.
const filtersActive = () =>
  !!(filters.project || filters.severity || filters.q || filters.attn);

function applyFilters(rows) {
  const q = filters.q.toLowerCase();
  return rows.filter(r =>
    (!filters.project || r.project_id === filters.project) &&
    (!filters.severity || r.severity === filters.severity) &&
    (!q || r.title.toLowerCase().includes(q)) &&
    (!filters.attn || r.status === "clarification_needed"));
}

const TERMINAL_CARD_CAP = 15;

function renderBoard(reports, counts) {
  lastReports = reports;
  lastCounts = counts;
  const active = filtersActive();
  const visible = applyFilters(reports);
  const board = $("#board");
  board.innerHTML = COLUMNS.map(col => {
    const rows = visible.filter(r => col.statuses.includes(r.status));
    // Unfiltered counts come from /queue (they include reports beyond the
    // list limit); with filters on, count what's actually shown.
    const count = active ? rows.length
      : col.statuses.reduce((n, s) => n + (counts[s] || 0), 0);
    const attn = active
      ? rows.filter(r => r.status === "clarification_needed").length
      : (counts["clarification_needed"] || 0);
    const attnHtml = (col.statuses.includes("clarification_needed") && attn)
      ? `<span class="attn-count" title="reports needing human input">? ${attn}</span>` : "";
    // Terminal column collapses to a count (§5b.3) — it only grows over time.
    let toggle = "", cardsHtml = "";
    if (col.key === "terminal") {
      toggle = `<span class="toggle" id="terminal-toggle" title="${terminalOpen ? "collapse" : "expand"}">${terminalOpen ? "▾" : "▸"}</span>`;
      if (terminalOpen) {
        const shown = terminalShowAll ? rows : rows.slice(0, TERMINAL_CARD_CAP);
        cardsHtml = shown.map(cardHtml).join("");
        if (!terminalShowAll && rows.length > TERMINAL_CARD_CAP)
          cardsHtml += `<button class="show-all" id="terminal-show-all">show all ${rows.length}</button>`;
      }
    } else {
      cardsHtml = rows.map(cardHtml).join("");
    }
    return `<div class="col">
      <h2>${col.label} <span class="count">${count}</span>${attnHtml}${toggle}</h2>
      <div class="cards">${cardsHtml}</div>
    </div>`;
  }).join("");
  board.querySelectorAll(".card").forEach(el =>
    el.addEventListener("click", () => openDetail(el.dataset.id)));
  const tog = $("#terminal-toggle");
  if (tog) tog.addEventListener("click", () => {
    terminalOpen = !terminalOpen;
    terminalShowAll = false;
    localStorage.setItem("bugalizer_terminal_open", terminalOpen ? "1" : "0");
    renderBoard(lastReports, lastCounts);
  });
  const all = $("#terminal-show-all");
  if (all) all.addEventListener("click", () => {
    terminalShowAll = true;
    renderBoard(lastReports, lastCounts);
  });
}

// Health LEDs (§5b.3): database / ollama / worker from /health. worker=null
// means the queue worker is disabled (grey), not broken.
function renderHealth(h) {
  const el = $("#health-strip");
  if (!h || !h.checks) { el.innerHTML = ""; return; }
  const items = [["db", h.checks.database], ["ollama", h.checks.ollama], ["worker", h.checks.worker]];
  el.innerHTML = items.map(([name, v]) => {
    const cls = v === true ? "up" : v === false ? "down" : "off";
    const state = v === true ? "up" : v === false ? "DOWN" : "disabled";
    return `<span class="health-item" title="${name}: ${state}"><span class="led ${cls}"></span>${name}</span>`;
  }).join("");
}

function renderUsage(u) {
  const tokens = (u.total_prompt_tokens + u.total_completion_tokens).toLocaleString();
  const cost = u.total_estimated_cost_usd ? ` · $${u.total_estimated_cost_usd.toFixed(4)}` : "";
  const per = Object.entries(u.by_provider)
    .map(([k, v]) => `${k}: ${(v.prompt_tokens + v.completion_tokens).toLocaleString()}`)
    .join("  |  ");
  $("#usage").innerHTML = `tokens <b>${tokens}</b>${cost}`;
  $("#usage").title = per || "no usage yet";
}

// ---------------------------------------------------------------------------
// Poll loop
// ---------------------------------------------------------------------------

async function refresh() {
  try {
    const [queue, reportsResp, projectsResp, usage, health] = await Promise.all([
      api("/queue"),
      api("/reports?limit=200"),
      api("/projects"),
      api("/usage"),
      // /health is public (no /api/v1 prefix, no key) and must not fail the
      // poll — a degraded component is data here, not an error.
      fetch("/health").then(r => r.json()).catch(() => null),
    ]);
    projects = Object.fromEntries(projectsResp.projects.map(p => [p.id, p.name]));
    projectsCache = projectsResp.projects;
    syncProjectFilter();
    renderBoard(reportsResp.reports, queue.by_status);
    renderHealth(health);
    renderUsage(usage);
    setPollState(true, `${queue.total} reports · updated ${new Date().toLocaleTimeString()}`);
    hideBanner();
    pollFailures = 0;
    // Keep the drawer live — but don't re-render out from under an armed
    // confirm (re-analyze / cloud), or we'd wipe the user's pending choice.
    if (openReportId && !confirmArmed()) refreshDetail(openReportId);
  } catch (e) {
    if (e.status === 401 || e.status === 403) {
      pollFailures = 0;                    // auth is a real config issue, not a blip
      setPollState(false, "auth required");
      showBanner("API key required or invalid — enter it in the top-right box.");
      return;
    }
    // A dropped keep-alive/QUIC connection can fail one poll; the board keeps
    // its last-good render. Don't alarm the user until several polls in a row
    // fail — otherwise a momentary blip flashes a scary banner every few seconds.
    pollFailures++;
    setPollState(false, pollFailures < 3 ? "reconnecting…" : "offline");
    if (pollFailures >= 3) {
      showBanner("Cannot reach the Bugalizer API (retrying): " + esc(e.message));
    }
  }
}

function setPollState(ok, text) {
  $("#poll-dot").className = ok ? "ok" : "err";
  $("#poll-info").textContent = text;
}
function showBanner(msg) { const b = $("#banner"); b.innerHTML = msg; b.classList.add("show"); }
function hideBanner() { $("#banner").classList.remove("show"); }

// ---------------------------------------------------------------------------
// Detail drawer
// ---------------------------------------------------------------------------

async function openDetail(id) {
  openReportId = id;
  cloudArmed = false;
  localArmed = false;
  detailReady = false;
  $("#overlay").classList.add("show");
  $("#detail").classList.add("show");
  $("#detail").innerHTML = `<p class="kv">Loading…</p>`;
  await refreshDetail(id);
}

function closeDetail() {
  openReportId = null;
  cloudArmed = false;
  localArmed = false;
  $("#overlay").classList.remove("show");
  $("#detail").classList.remove("show");
}

async function refreshDetail(id) {
  let report, analyses, fixes, localization = null;
  try {
    [report, analyses, fixes] = await Promise.all([
      api(`/reports/${id}`),
      api(`/reports/${id}/analyses`),
      api(`/reports/${id}/fix_proposals`),
    ]);
    try { localization = await api(`/reports/${id}/localization`); } catch (e) { /* none yet */ }
  } catch (e) {
    // Keep the last-good drawer through a transient blip; only surface an error
    // if we never managed to load it.
    if (!detailReady) {
      $("#detail").innerHTML = `<p class="kv">Failed to load report: ${esc(e.message)}</p>`;
    }
    return;
  }
  if (openReportId !== id) return;   // drawer changed/closed mid-request
  detailReady = true;
  renderDetail(report, analyses.analyses, localization, fixes.fix_proposals);
}

function latestCompleted(analyses, phase) {
  return analyses.find(a => a.phase === phase && a.status === "completed") || null;
}

// Formatted triage (§5b.2) — no more raw JSON dump. Clarification questions
// are the main human action item; they get the prominent amber block.
function triageSection(analyses) {
  const t = latestCompleted(analyses, "triage");
  if (!t || !t.result) return `<p class="kv">No triage result yet.</p>`;
  const res = t.result;
  const known = new Set(["severity", "category", "confidence", "summary",
                         "needs_clarification", "clarification_questions"]);
  const rows = [];
  if (res.severity) rows.push(["severity",
    `<span class="badge sev" style="background:var(--sev-${esc(res.severity)},var(--text-muted))">${esc(res.severity)}</span>`]);
  if (res.category) rows.push(["category", esc(res.category)]);
  if (typeof res.confidence === "number")
    rows.push(["confidence", `${Math.round(res.confidence * 100)}%`]);
  for (const [k, v] of Object.entries(res)) {
    if (known.has(k) || v == null || typeof v === "object") continue;
    rows.push([esc(k), esc(String(v))]);
  }
  const grid = rows.length
    ? `<dl class="triage-grid">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>` : "";
  const summary = res.summary ? `<p>${esc(res.summary)}</p>` : "";
  const questions = (res.clarification_questions || []).filter(Boolean);
  const clarify = (res.needs_clarification || questions.length)
    ? `<div class="clarify">
         <div class="clarify-head">? Needs clarification</div>
         ${questions.length
           ? `<ol>${questions.map(q => `<li>${esc(q)}</li>`).join("")}</ol>`
           : `<span class="kv">The model asked for clarification but returned no questions.</span>`}
       </div>` : "";
  return grid + summary + clarify;
}

function localizationSection(loc) {
  if (!loc) return `<p class="kv">No localization yet.</p>`;
  const cands = (loc.localizations.length ? loc.localizations : loc.candidate_files)
    .map(c => `<li><b>${esc(c.file || c.path)}</b>${c.function ? " · " + esc(c.function) : ""}` +
              `${c.reason ? ` <span class="kv">— ${esc(c.reason)}</span>` : ""}</li>`)
    .join("");
  return `<p class="kv">sha <b>${esc((loc.repo_sha || "").slice(0, 10))}</b>` +
         ` · confidence <b>${loc.confidence}</b>` +
         (loc.root_cause_hypothesis ? ` · <b>${esc(loc.root_cause_hypothesis)}</b>` : "") +
         `</p><ul>${cands || "<li class='kv'>no candidates</li>"}</ul>`;
}

// Client-side unified-diff colorization (§5b.2) — no dependencies.
function renderDiff(diff) {
  const lines = String(diff || "").split("\n").map(line => {
    let cls = "";
    if (line.startsWith("+++") || line.startsWith("---")) cls = "diff-file";
    else if (line.startsWith("@@")) cls = "diff-hunk";
    else if (line.startsWith("+")) cls = "diff-add";
    else if (line.startsWith("-")) cls = "diff-del";
    else if (line.startsWith("diff ") || line.startsWith("index ")) cls = "diff-meta";
    return `<code class="${cls}">${esc(line) || " "}</code>`;
  });
  return `<pre class="diff">${lines.join("")}</pre>`;
}

function fixSection(fixes) {
  if (!fixes.length) return `<p class="kv">No fix proposals yet.</p>`;
  return fixes.map(f => `
    <p class="kv">confidence <b>${f.confidence}</b> · files <b>${esc((f.files_changed || []).join(", "))}</b>
       · ${age(f.created_at)} ago</p>
    <p>${esc(f.root_cause)}</p>
    <p class="kv">${esc(f.explanation)}</p>
    ${renderDiff(f.diff)}`).join("<hr>");
}

// Run history (§5b.2): every analysis row — stage, tier, model, duration,
// tokens, cost, outcome. The audit trail behind the scan-state chips.
function timelineSection(analyses) {
  if (!analyses.length) return `<p class="kv">No analysis runs yet.</p>`;
  const rows = analyses.map(a => {
    const tier = tierOf(a.llm_provider);
    const led = tier ? `<span class="led ${tier}"></span>` : `<span class="led steel"></span>`;
    const model = a.llm_model ? esc(a.llm_model) : "—";
    let dur = "—";
    if (a.started_at && a.completed_at) {
      const s = (new Date(a.completed_at) - new Date(a.started_at)) / 1000;
      dur = s < 60 ? `${s.toFixed(1)}s` : `${Math.round(s / 60)}m${Math.round(s % 60)}s`;
    }
    const tokens = (a.prompt_tokens || 0) + (a.completion_tokens || 0);
    const cost = a.estimated_cost_usd ? ` · $${a.estimated_cost_usd.toFixed(4)}` : "";
    const failed = a.status === "failed";
    const err = failed && a.result && a.result.error
      ? `<span class="err-txt">${esc(String(a.result.error).slice(0, 200))}</span>` : "";
    return `<div class="run-row${failed ? " failed" : ""}">
      ${led}<span class="phase">${esc(a.phase)}</span>
      <span class="muted">${model}</span>
      <span class="muted">${dur}</span>
      <span class="muted">${tokens.toLocaleString()} tok${cost}</span>
      <span class="right">${esc(a.status)} · ${age(a.completed_at || a.created_at)} ago</span>
      ${err}
    </div>`;
  });
  return `<div class="timeline">${rows.join("")}</div>`;
}

function renderDetail(r, analyses, loc, fixes) {
  let failure = "";
  if (r.failed_stage === "fix") {
    // A fix-stage failure isn't a broken report — triage/localization below are
    // intact. Local models often can't produce a valid patch; a fix needs a
    // capable cloud provider. Keep this calm and actionable, not alarming.
    failure = `<section><h3>Automated fix</h3><p class="kv">No automated fix was
      generated for this report. Triage and localization are unaffected. A fix
      proposal needs a capable cloud fix provider — use <b>Analyze (cloud · $)</b>
      once one is configured.</p></section>`;
  } else if (r.failed_stage) {
    failure = `<section><h3>Failure</h3><p class="badge fail">⚠ ${esc(r.failed_stage)}</p>
       <pre>${esc(r.last_error || "")}</pre></section>`;
  }
  // "Analyzed" = a completed localization exists (implies triage ran) —
  // drives whether the local button is a first-run or a guarded re-run.
  const locDone = latestCompleted(analyses, "localization");
  const analyzed = !!locDone;

  const chips = `<div class="analysis-chips">
    ${tierChip("local", r.last_local_analysis_at, true)}
    ${tierChip("cloud", r.last_cloud_analysis_at, true)}
  </div>`;

  const triageRun = latestCompleted(analyses, "triage");
  const fixRun = latestCompleted(analyses, "fix");

  $("#detail").innerHTML = `
    <button id="close-detail">✕</button>
    <h2>${esc(r.title)}</h2>
    <div class="meta">
      <b>${esc(r.status)}</b> · ${esc(r.severity)} · ${esc(projects[r.project_id] || r.project_id)}
      · by ${esc(r.reporter)} · ${age(r.created_at)} old · id ${esc(r.id)}
    </div>
    ${chips}
    <div class="actions">
      <span id="local-slot">
        <button id="act-local" class="tier-local">${analyzed ? "Re-analyze (local · free)" : "Analyze (local · free)"}</button>
      </span>
      <span id="cloud-slot">
        <button id="act-cloud" class="tier-cloud">Analyze (cloud · $)</button>
      </span>
      ${r.failed_stage ? `<button id="act-retry" title="Clear the failed ${esc(r.failed_stage)} attempts so the pipeline can retry">Retry</button>` : ""}
      <label class="kv">mode
        <select id="mode-select" title="auto — pipeline dispatches automatically · local_only — never uses paid cloud · hold — nothing runs until you analyze manually">
          ${["auto", "local_only", "hold"].map(m =>
            `<option value="${m}" ${r.analysis_mode === m ? "selected" : ""}>${m}</option>`).join("")}
        </select>
      </label>
    </div>
    ${failure}
    <section><h3>Description</h3><pre>${esc(r.description)}</pre></section>
    <section><h3>Triage ${runChip(triageRun)}</h3>${triageSection(analyses)}</section>
    <section><h3>Localization ${runChip(locDone)}</h3>${localizationSection(loc)}</section>
    <section><h3>Fix proposals ${runChip(fixRun)}</h3>${fixSection(fixes)}</section>
    <section><h3>Run history</h3>${timelineSection(analyses)}</section>
  `;
  $("#close-detail").addEventListener("click", closeDetail);
  bindLocalButton(r.id, analyzed);
  bindCloudButton(r.id, r.last_cloud_analysis_at);
  const retryBtn = $("#act-retry");
  if (retryBtn) retryBtn.addEventListener("click", () => doRetry(r.id));
  $("#mode-select").addEventListener("change", ev => doSetMode(r.id, ev.target.value));
}

// Local analysis button. First run fires immediately (expected action, no
// friction — it's free). A re-run on an already-analyzed report arms an inline
// confirm with an explicit Cancel, so a redundant analysis is deliberate.
function bindLocalButton(id, analyzed) {
  const btn = $("#act-local");
  if (!btn) return;
  btn.addEventListener("click", () => {
    if (!analyzed) { doAnalyze(id, "local"); return; }
    localArmed = true;
    const slot = $("#local-slot");
    slot.innerHTML = `<span class="reconfirm">
      <span class="kv">⚠ Re-run local analysis? It re-does triage &amp; localization.</span>
      <button id="local-yes" class="danger-confirm">Yes, re-analyze</button>
      <button id="local-no">Cancel</button>
    </span>`;
    const disarm = () => {
      localArmed = false;
      slot.innerHTML = `<button id="act-local" class="tier-local">Re-analyze (local · free)</button>`;
      bindLocalButton(id, true);
    };
    $("#local-yes").addEventListener("click", () => { localArmed = false; doAnalyze(id, "local"); });
    $("#local-no").addEventListener("click", disarm);
    setTimeout(() => { if (localArmed) disarm(); }, 8000);  // auto-cancel if ignored
  });
}

// Cloud analysis is a paid API call — it NEVER fires from a single click
// (§5b success criterion 2). The inline confirm states the cost, and if a
// cloud run already exists, its age — so a redundant paid re-run is explicit.
function bindCloudButton(id, lastCloudAt) {
  const btn = $("#act-cloud");
  if (!btn) return;
  btn.addEventListener("click", () => {
    cloudArmed = true;
    const slot = $("#cloud-slot");
    const already = lastCloudAt
      ? ` Cloud already ran <b>${age(lastCloudAt)} ago</b> — run again?` : "";
    slot.innerHTML = `<span class="reconfirm">
      <span class="kv">$ Cloud analysis is a paid API call.${already}</span>
      <button id="cloud-yes" class="danger-confirm">Yes, run cloud ($)</button>
      <button id="cloud-no">Cancel</button>
    </span>`;
    const disarm = () => {
      cloudArmed = false;
      slot.innerHTML = `<button id="act-cloud" class="tier-cloud">Analyze (cloud · $)</button>`;
      bindCloudButton(id, lastCloudAt);
    };
    $("#cloud-yes").addEventListener("click", () => { cloudArmed = false; doAnalyze(id, "cloud"); });
    $("#cloud-no").addEventListener("click", disarm);
    setTimeout(() => { if (cloudArmed) disarm(); }, 8000);  // auto-cancel if ignored
  });
}

async function doAnalyze(id, tier) {
  try {
    const res = await api(`/reports/${id}/analyze`, {
      method: "POST", body: JSON.stringify({ tier }),
    });
    toast(res.detail || `${tier} analysis dispatched`);
    await refreshDetail(id);
  } catch (e) { toast(e.message, true); }
}

async function doRetry(id) {
  try {
    const res = await api(`/queue/${id}/retry`, { method: "POST" });
    toast(res.message || "retry counters reset");
    await refreshDetail(id);
  } catch (e) { toast(e.message, true); }
}

async function doSetMode(id, mode) {
  try {
    await api(`/reports/${id}/analysis_mode`, {
      method: "PATCH", body: JSON.stringify({ analysis_mode: mode }),
    });
    toast(`analysis_mode → ${mode}`);
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------------------
// Modals: new report + project management (§5b.4)
// ---------------------------------------------------------------------------

function openModal(html) {
  $("#modal").innerHTML = html;
  $("#modal-wrap").classList.add("show");
  const close = $("#modal-close");
  if (close) close.addEventListener("click", closeModal);
}
function closeModal() {
  $("#modal-wrap").classList.remove("show");
  $("#modal").innerHTML = "";
}
const modalOpen = () => $("#modal-wrap").classList.contains("show");

function projectOptions(selected) {
  return projectsCache.map(p =>
    `<option value="${esc(p.id)}" ${p.id === selected ? "selected" : ""}>${esc(p.name)}</option>`).join("");
}

// "New report" — for demo/fake reports and quick manual capture.
function showNewReport() {
  if (!projectsCache.length) { toast("Create a project first", true); return; }
  openModal(`
    <h2>New report <button class="close" id="modal-close">✕</button></h2>
    <div class="form-grid">
      <label>project</label><select id="nr-project">${projectOptions()}</select>
      <label>title</label><input id="nr-title" autofocus>
      <label>severity</label>
      <select id="nr-sev"><option>critical</option><option>high</option>
        <option selected>medium</option><option>low</option></select>
      <label>reporter</label><input id="nr-reporter" value="dashboard">
      <label>description</label><textarea id="nr-desc"></textarea>
    </div>
    <div class="form-actions"><button id="nr-submit" class="tier-local">Submit report</button></div>`);
  $("#nr-submit").addEventListener("click", async () => {
    const body = {
      project_id: $("#nr-project").value,
      title: $("#nr-title").value.trim(),
      description: $("#nr-desc").value.trim(),
      severity: $("#nr-sev").value,
      reporter: $("#nr-reporter").value.trim() || "dashboard",
    };
    if (!body.title || !body.description) { toast("Title and description are required", true); return; }
    try {
      const res = await api("/reports", { method: "POST", body: JSON.stringify(body) });
      const warn = (res.warnings || []).length ? ` (${res.warnings.length} warnings)` : "";
      toast(`Report created${warn}`);
      closeModal();
      refresh();
    } catch (e) { toast(e.message, true); }
  });
}

// Projects list — name, repo, and which LLMs its analyses will use, in tier
// colors. Everything the API supports without curl: create, edit, clone.
function showProjects() {
  const rows = projectsCache.map(p => {
    const fix = p.fix_llm_provider || p.fix_llm_model
      ? `${esc(p.fix_llm_provider || "?")} · ${esc(p.fix_llm_model || "?")}`
      : "global default";
    return `<div class="proj-row">
      <span class="name">${esc(p.name)}</span>
      <span class="chip tier-local" title="local stages (triage + localization)"><span class="led local"></span>${esc(p.llm_provider)} · ${esc(p.llm_model)}</span>
      <span class="chip tier-cloud" title="Stage 4 fix proposals (paid)"><span class="led cloud"></span>fix: ${fix}</span>
      <span class="spacer"></span>
      <button data-edit="${esc(p.id)}">Edit</button>
      <button data-clone="${esc(p.id)}" title="Clone/refresh the repo so localization can run">Clone repo</button>
      <span class="repo">${esc(p.repo_url)}</span>
    </div>`;
  }).join("") || `<p class="kv">No projects yet.</p>`;
  openModal(`
    <h2>Projects <button class="close" id="modal-close">✕</button></h2>
    ${rows}
    <div class="form-actions"><button id="proj-new" class="tier-local">+ New project</button></div>`);
  $("#proj-new").addEventListener("click", () => showProjectForm(null));
  $("#modal").querySelectorAll("[data-edit]").forEach(b =>
    b.addEventListener("click", () => {
      const p = projectsCache.find(x => x.id === b.dataset.edit);
      if (p) showProjectForm(p);
    }));
  $("#modal").querySelectorAll("[data-clone]").forEach(b =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      b.textContent = "Cloning…";
      toast("Cloning repo — this can take a minute…");
      try {
        await api(`/projects/${b.dataset.clone}/clone`, { method: "POST" });
        toast("Repo cloned");
      } catch (e) { toast(e.message, true); }
      b.disabled = false;
      b.textContent = "Clone repo";
      refresh();
    }));
}

function showProjectForm(p) {
  const isNew = !p;
  openModal(`
    <h2>${isNew ? "New project" : `Edit — ${esc(p.name)}`}
      <button class="close" id="modal-close">✕</button></h2>
    <div class="form-grid">
      <label>name</label><input id="pf-name" value="${esc(p?.name || "")}">
      <label>repo url</label><input id="pf-repo" value="${esc(p?.repo_url || "")}" placeholder="https://github.com/owner/repo">
      <label>branch</label><input id="pf-branch" value="${esc(p?.default_branch || "main")}">
    </div>
    <fieldset class="tier-box local">
      <legend>● Local LLM — triage + localization (free)</legend>
      <div class="form-grid">
        <label>provider</label><input id="pf-lprov" value="${esc(p?.llm_provider || "ollama")}">
        <label>model</label><input id="pf-lmodel" value="${esc(p?.llm_model || "qwen2.5-coder:7b")}">
      </div>
    </fieldset>
    <fieldset class="tier-box cloud">
      <legend>● Cloud fix LLM — Stage 4 proposals ($)</legend>
      <div class="form-grid">
        <label>provider</label><input id="pf-fprov" value="${esc(p?.fix_llm_provider || "")}" placeholder="empty = global default">
        <label>model</label><input id="pf-fmodel" value="${esc(p?.fix_llm_model || "")}" placeholder="empty = global default">
      </div>
    </fieldset>
    <div class="form-actions">
      <button id="pf-back">Back</button>
      <button id="pf-save" class="tier-local">${isNew ? "Create project" : "Save changes"}</button>
    </div>`);
  $("#pf-back").addEventListener("click", showProjects);
  $("#pf-save").addEventListener("click", async () => {
    const name = $("#pf-name").value.trim();
    const repo = $("#pf-repo").value.trim();
    if (!name || !repo) { toast("Name and repo url are required", true); return; }
    const body = {
      name,
      repo_url: repo,
      default_branch: $("#pf-branch").value.trim() || "main",
      llm_provider: $("#pf-lprov").value.trim() || "ollama",
      llm_model: $("#pf-lmodel").value.trim() || "qwen2.5-coder:7b",
      // Explicit null clears the override → global fix settings (§5.3).
      fix_llm_provider: $("#pf-fprov").value.trim() || null,
      fix_llm_model: $("#pf-fmodel").value.trim() || null,
    };
    try {
      if (isNew) await api("/projects", { method: "POST", body: JSON.stringify(body) });
      else await api(`/projects/${p.id}`, { method: "PATCH", body: JSON.stringify(body) });
      toast(isNew ? "Project created" : "Project saved");
      await refresh();
      showProjects();
    } catch (e) { toast(e.message, true); }
  });
}

let toastTimer = null;
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ""; }, 4000);
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

$("#api-key").value = apiKey;
$("#api-key").addEventListener("change", ev => {
  apiKey = ev.target.value.trim();
  localStorage.setItem("bugalizer_api_key", apiKey);
  refresh();
});
$("#theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  const next = root.dataset.theme === "light" ? "slate" : "light";
  if (next === "light") root.dataset.theme = "light";
  else delete root.dataset.theme;
  try { localStorage.setItem("bugalizer_theme", next); } catch (e) { /* fine */ }
});
$("#btn-new-report").addEventListener("click", showNewReport);
$("#btn-projects").addEventListener("click", showProjects);

// Filter bar (§5b.3)
let projOptionsKey = "";
function syncProjectFilter() {
  const key = projectsCache.map(p => p.id).join(",");
  if (key === projOptionsKey) return;   // don't rebuild under an open dropdown
  projOptionsKey = key;
  const sel = $("#f-project");
  const cur = sel.value;
  sel.innerHTML = `<option value="">all projects</option>` + projectOptions();
  sel.value = cur;
}
function filtersChanged() {
  $("#f-clear").classList.toggle("hidden", !filtersActive());
  $("#f-attn").classList.toggle("on", filters.attn);
  renderBoard(lastReports, lastCounts);
}
$("#f-project").addEventListener("change", ev => { filters.project = ev.target.value; filtersChanged(); });
$("#f-severity").addEventListener("change", ev => { filters.severity = ev.target.value; filtersChanged(); });
$("#f-search").addEventListener("input", ev => { filters.q = ev.target.value.trim(); filtersChanged(); });
$("#f-attn").addEventListener("click", () => { filters.attn = !filters.attn; filtersChanged(); });
$("#f-clear").addEventListener("click", () => {
  filters.project = filters.severity = filters.q = "";
  filters.attn = false;
  $("#f-project").value = "";
  $("#f-severity").value = "";
  $("#f-search").value = "";
  filtersChanged();
});

$("#overlay").addEventListener("click", closeDetail);
$("#modal-wrap").addEventListener("click", ev => { if (ev.target.id === "modal-wrap") closeModal(); });
document.addEventListener("keydown", ev => {
  if (ev.key !== "Escape") return;
  if (modalOpen()) closeModal();
  else closeDetail();
});

refresh();
setInterval(refresh, POLL_MS);
