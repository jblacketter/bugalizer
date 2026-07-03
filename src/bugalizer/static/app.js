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

function renderBoard(reports, counts) {
  const board = $("#board");
  board.innerHTML = COLUMNS.map(col => {
    const rows = reports.filter(r => col.statuses.includes(r.status));
    const count = col.statuses.reduce((n, s) => n + (counts[s] || 0), 0);
    const attn = counts["clarification_needed"] || 0;
    const attnHtml = (col.statuses.includes("clarification_needed") && attn)
      ? `<span class="attn-count" title="reports needing human input">? ${attn}</span>` : "";
    return `<div class="col">
      <h2>${col.label} <span class="count">${count}</span>${attnHtml}</h2>
      <div class="cards">${rows.map(cardHtml).join("") || ""}</div>
    </div>`;
  }).join("");
  board.querySelectorAll(".card").forEach(el =>
    el.addEventListener("click", () => openDetail(el.dataset.id)));
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
    const [queue, reportsResp, projectsResp, usage] = await Promise.all([
      api("/queue"),
      api("/reports?limit=200"),
      api("/projects"),
      api("/usage"),
    ]);
    projects = Object.fromEntries(projectsResp.projects.map(p => [p.id, p.name]));
    renderBoard(reportsResp.reports, queue.by_status);
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
      <button id="act-retry">Retry</button>
      <label class="kv">mode
        <select id="mode-select">
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
  $("#act-retry").addEventListener("click", () => doRetry(r.id));
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
$("#overlay").addEventListener("click", closeDetail);
document.addEventListener("keydown", ev => { if (ev.key === "Escape") closeDetail(); });

refresh();
setInterval(refresh, POLL_MS);
