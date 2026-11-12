/* =========================================================
   Touchstone — shared UI logic
   Reads /config on load, renders ghost cards, wires Apply.
   RENDERERS[domain](diff, resolution) → HTML string for
   the evidence body inside each card.
   ========================================================= */

"use strict";

// ----- state -------------------------------------------------
let CONFIG = {};
let REPORT = null;      // latest Report from /compare or /report/:id
let _applying = new Set();

// ----- RENDERERS registry ------------------------------------
// Each renderer receives (difference, resolution) and returns
// an HTML string for the .card-evidence element.
// Template owns layout/badges/actions; renderers own only evidence.

const RENDERERS = {
  database: renderDatabaseEvidence,
  api:      renderApiEvidence,
  cache:    renderCacheEvidence,
};

function pill(k, v, cls = "") {
  return `<span class="pill ${cls}"><span class="pk">${esc(k)}</span><span class="pv">${v}</span></span>`;
}

function severityPill(sev) {
  const s = (sev || "HIGH").toUpperCase();
  const cls = s === "HIGH" ? "pill-warn" : s === "MEDIUM" ? "" : "";
  return pill("severity", s, cls);
}

function renderDatabaseEvidence(diff, resolution) {
  const ctx = diff.context || {};
  const pills = [];

  pills.push(pill("table",  esc(ctx.table  || "—"), "pill-code"));
  if (ctx.column) pills.push(pill("column", esc(ctx.column), "pill-code"));
  pills.push(severityPill(diff.severity));

  if (diff.kind === "TYPE_CHANGED") {
    if (ctx.original_actual_type) pills.push(pill("live",     esc(ctx.original_actual_type), "pill-warn"));
    if (ctx.expected_type)        pills.push(pill("declared", esc(ctx.expected_type),        "pill-code"));
  }

  if (resolution && resolution.cached) {
    pills.push(`<span class="badge badge-cached" style="font-size:9px">CACHED</span>`);
  }

  const pillsHtml = `<div class="card-pills">${pills.join("")}</div>`;

  let sqlHtml = "";
  if (resolution && resolution.proposed_change && resolution.proposed_change.statement) {
    sqlHtml = `<pre class="card-sql">${esc(resolution.proposed_change.statement)}</pre>`;
  }

  let rationaleHtml = "";
  if (resolution && resolution.rationale) {
    rationaleHtml = `<p class="card-rationale">${esc(resolution.rationale)}</p>`;
  }

  return pillsHtml + sqlHtml + rationaleHtml;
}

function renderApiEvidence(diff, resolution) {
  const ctx = diff.context || {};
  const pills = [];

  if (ctx.method) pills.push(pill("method", esc(ctx.method), "pill-code"));
  if (ctx.path)   pills.push(pill("path",   esc(ctx.path),   "pill-code"));
  if (ctx.field)  pills.push(pill("field",  esc(ctx.field),  "pill-code"));
  if (ctx.status_code) pills.push(pill("status", String(ctx.status_code), "pill-warn"));
  pills.push(severityPill(diff.severity));

  const pillsHtml = `<div class="card-pills">${pills.join("")}</div>`;

  let patchHtml = "";
  if (resolution && resolution.proposed_change && resolution.proposed_change.forward_patch) {
    patchHtml = `<pre class="card-sql">${esc(resolution.proposed_change.forward_patch)}</pre>`;
  }

  return pillsHtml + patchHtml;
}

function renderCacheEvidence(diff, resolution) {
  const ctx = diff.context || {};
  const pills = [];

  pills.push(pill("key", esc(ctx.key || diff.target), "pill-code"));
  pills.push(severityPill(diff.severity));

  if (diff.kind === "KEY_MISSING") {
    pills.push(pill("declared", "yes", "pill-code"));
    pills.push(pill("in redis",  "no",  "pill-warn"));
  } else if (diff.kind === "UNDECLARED_KEY") {
    pills.push(pill("declared", "no",  "pill-warn"));
    pills.push(pill("in redis",  "yes", "pill-code"));
  }

  return `<div class="card-pills">${pills.join("")}</div>`;
}

// ----- helpers -----------------------------------------------
function kv(k, v) {
  return `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`;
}
function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const RITUAL_STAGES = {
  api: [
    "Awakening the server…",
    "Reaching into the API…",
    "Listening for echoes…",
    "Ghosts identified",
  ],
  database: [
    "Opening the crypt…",
    "Reading the schema…",
    "Comparing declarations…",
    "Ghosts identified",
  ],
  cache: [
    "Rattling the cache…",
    "Fetching declared keys…",
    "Probing Redis…",
    "Ghosts identified",
  ],
};

// Stage timing (seconds): stage advances at these elapsed thresholds
const RITUAL_THRESHOLDS = [0, 20, 45, 65];

let _ritualTimer   = null;
let _ritualElapsed = 0;

function showRitual() {
  const grid = document.getElementById("card-grid");
  if (!grid) return;
  const stages = RITUAL_STAGES[CONFIG.domain] || RITUAL_STAGES.database;

  grid.innerHTML = `
    <div class="ritual-wrap" id="ritual-wrap">
      <div class="ritual-candles">
        <span class="ritual-candle">🕯️</span>
        <span class="ritual-planchette" id="ritual-planchette">✦</span>
        <span class="ritual-candle">🕯️</span>
      </div>
      <div class="ritual-board">
        ${stages.map((s, i) => `<div class="ritual-stage" id="ritual-stage-${i}">${esc(s)}</div>`).join("")}
      </div>
      <div class="ritual-timer" id="ritual-timer">0s</div>
    </div>`;

  _ritualElapsed = 0;
  _activateRitualStage(0);

  _ritualTimer = setInterval(() => {
    _ritualElapsed++;
    const el = document.getElementById("ritual-timer");
    if (el) el.textContent = `${_ritualElapsed}s`;
    // advance stage based on elapsed thresholds
    for (let i = RITUAL_THRESHOLDS.length - 1; i >= 0; i--) {
      if (_ritualElapsed >= RITUAL_THRESHOLDS[i]) {
        _activateRitualStage(i);
        break;
      }
    }
  }, 1000);
}

function _activateRitualStage(idx) {
  const stages = RITUAL_STAGES[CONFIG.domain] || RITUAL_STAGES.database;
  for (let i = 0; i < stages.length; i++) {
    const el = document.getElementById(`ritual-stage-${i}`);
    if (!el) continue;
    el.classList.toggle("ritual-stage-active",    i === idx);
    el.classList.toggle("ritual-stage-done",      i < idx);
    el.classList.toggle("ritual-stage-pending",   i > idx);
  }
  // slide planchette to current stage
  const pct = stages.length > 1 ? (idx / (stages.length - 1)) * 100 : 0;
  const p = document.getElementById("ritual-planchette");
  if (p) p.style.left = `calc(${pct}% - 12px)`;
}

function stopRitual() {
  if (_ritualTimer) { clearInterval(_ritualTimer); _ritualTimer = null; }
}

const IDLE_COPY = {
  database: {
    headline: "The crypt is quiet… for now.",
    tombstone: "R.I.P. undeclared drift",
    sub: "scan your database schema",
  },
  api: {
    headline: "The spirits are silent… for now.",
    tombstone: "R.I.P. undeclared contract drift",
    sub: "probe your API contracts",
  },
  cache: {
    headline: "The cache is undisturbed… for now.",
    tombstone: "R.I.P. undeclared cache ghosts",
    sub: "scan your Redis cache",
  },
};

const KIND_LABELS = {
  // Database domain
  TYPE_CHANGED:       "Type Wraith",
  INDEX_MISSING:      "Missing Specter",
  INDEX_EXTRA:        "Phantom Index",
  COLUMN_MISSING:     "Vanished Column",
  TABLE_MISSING:      "Lost Table",
  NULLABLE_CHANGED:   "Null Shade",
  CONSTRAINT_MISSING: "Broken Ward",
  CONSTRAINT_EXTRA:   "Phantom Ward",
  DEFAULT_CHANGED:    "Altered Omen",
  // API contract domain
  FIELD_EXTRA:        "Shadow Field",
  FIELD_MISSING:      "Vanished Field",
  PATH_MISSING:       "Lost Endpoint",
  PATH_EXTRA:         "Phantom Endpoint",
  // Cache domain
  KEY_MISSING:        "Missing Cache Specter",
  UNDECLARED_KEY:     "Phantom Cache Key",
};

function ghostKind(kind) {
  return KIND_LABELS[kind] || kind;
}

function stateBadge(state) {
  const map = {
    PENDING:          ["badge-pending",       "👻 HAUNTING"],
    REVIEW_REQUIRED:  ["badge-review",        "🕯 SÉANCE NEEDED"],
    BLOCKED:          ["badge-blocked",       "⛓ BOUND"],
    RESOLVED:         ["badge-resolved",      "✝ LAID TO REST"],
    STILL_PRESENT:    ["badge-still-present", "😱 STILL HAUNTING"],
    EXECUTION_FAILED: ["badge-failed",        "💀 RITUAL FAILED"],
    SUPERSEDED:       ["badge-superseded",    "🌫 FADED"],
  };
  const [cls, label] = map[state] || ["badge-blocked", state];
  return `<span class="badge ${cls}">${label}</span>`;
}

function safeFixBadge(diff) {
  if (diff.context && diff.context.safe_fix === true) {
    return `<span class="badge badge-auto">AUTO-FIX</span>`;
  }
  return "";
}

// ----- card rendering ----------------------------------------
function renderCard(diff, report, index) {
  const resolution = report.resolutions ? report.resolutions[diff.id] : null;
  const outcome    = report.outcomes    ? report.outcomes[diff.id]    : null;
  const state      = diff.state || "PENDING";

  const renderer     = RENDERERS[CONFIG.domain] || renderDatabaseEvidence;
  const evidenceHtml = renderer(diff, resolution);

  // API domain: executor handles everything — Apply is always available for non-resolved ghosts
  const canApply    = (state === "PENDING" || state === "REVIEW_REQUIRED" || (state === "BLOCKED" && CONFIG.domain !== "cache")) &&
    (CONFIG.domain === "api" || CONFIG.domain === "cache" || (resolution && resolution.proposed_change));
  const canRollback = (state === "RESOLVED" || state === "STILL_PRESENT") && CONFIG.domain !== "database";

  const applyBtn = canApply
    ? `<button class="btn btn-primary" data-action="apply" data-id="${esc(diff.id)}" ${_applying.has(diff.id) ? "disabled" : ""}>
         ${_applying.has(diff.id) ? '<span class="spinner"></span>' : "⚡ "}Exorcise
       </button>`
    : "";

  const rollbackBtn = canRollback
    ? `<button class="btn btn-danger" data-action="rollback" data-id="${esc(diff.id)}">↩ Rollback</button>`
    : "";

  const timeStr = diff.detected_at
    ? new Date(diff.detected_at).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})
    : "";

  const verifyNote = outcome && outcome.verification
    ? (outcome.verification.resolved
        ? `<span style="color:var(--resolved);font-family:var(--font-mono);font-size:10px">✓ verified</span>`
        : `<span style="color:var(--still-present);font-family:var(--font-mono);font-size:10px">⚠ still haunting</span>`)
    : "";

  const hasActions = applyBtn || rollbackBtn || verifyNote;

  return `
<div class="ghost-card" data-state="${esc(state)}" data-id="${esc(diff.id)}"
     style="animation-delay: ${index * 50}ms">
  <div class="card-hero">
    <span class="card-ghost-name">${esc(ghostKind(diff.kind))}</span>
    <span class="card-state-badge">${stateBadge(state)}${safeFixBadge(diff)}</span>
  </div>
  <div class="card-target-row">
    <span class="card-target">${esc(diff.target)}</span>
    ${timeStr ? `<span class="card-time">${timeStr}</span>` : ""}
  </div>
  ${evidenceHtml}
  ${hasActions ? `<hr class="card-divider"><div class="card-actions">${applyBtn}${rollbackBtn}${verifyNote}</div>` : ""}
</div>`;
}

// ----- health bar --------------------------------------------
function updateHealthBar(report) {
  if (!report || !report.differences) return;

  const diffs = report.differences;
  const total      = diffs.length;
  const resolved   = diffs.filter(d => d.state === "RESOLVED").length;
  const review     = diffs.filter(d => d.state === "REVIEW_REQUIRED").length;
  const blocked    = diffs.filter(d => d.state === "BLOCKED").length;
  const superseded = diffs.filter(d => d.state === "SUPERSEDED").length;
  const pending    = diffs.filter(d => d.state === "PENDING").length;

  const pct = total > 0 ? Math.round((resolved / total) * 100) : 0;

  const statsEl = document.getElementById("health-stats");
  const fillEl  = document.getElementById("health-fill");

  if (statsEl) {
    statsEl.innerHTML =
      `<span>${resolved}</span> of <span>${total}</span> resolved` +
      (pending    ? ` &middot; <span>${pending}</span> pending`    : "") +
      (review     ? ` &middot; <span>${review}</span> review`      : "") +
      (blocked    ? ` &middot; <span>${blocked}</span> blocked`    : "") +
      (superseded ? ` &middot; <span>${superseded}</span> superseded` : "");
  }

  if (fillEl) {
    const prevWidth = fillEl.style.width || "0%";
    fillEl.style.setProperty("--bar-from", prevWidth);
    fillEl.style.setProperty("--bar-to",   pct + "%");
    fillEl.style.width = pct + "%";

    fillEl.classList.remove("mid", "high");
    if (pct >= 80) fillEl.classList.add("high");
    else if (pct >= 40) fillEl.classList.add("mid");

    // re-trigger cascade animation
    fillEl.style.animation = "none";
    fillEl.offsetHeight;  // reflow
    fillEl.style.animation = "cascade-fill 0.5s ease-out forwards";
  }
}

// ----- roaming ghosts ----------------------------------------
const GHOST_EMOJIS = ["👻","🕷️","🦇","💀","🕸️","☠️","🫥"];
let _ghostTimer = null;

function spawnGhost() {
  const el = document.createElement("span");
  el.className = "roam-ghost";
  el.textContent = GHOST_EMOJIS[Math.floor(Math.random() * GHOST_EMOJIS.length)];

  // start position: random edge
  const edge = Math.floor(Math.random() * 4);
  let sx, sy;
  if (edge === 0) { sx = Math.random() * 100; sy = -5; }
  else if (edge === 1) { sx = 105; sy = Math.random() * 100; }
  else if (edge === 2) { sx = Math.random() * 100; sy = 105; }
  else { sx = -5; sy = Math.random() * 100; }

  el.style.left = sx + "vw";
  el.style.top  = sy + "vh";

  const dur  = 6 + Math.random() * 6;
  const size = 24 + Math.floor(Math.random() * 28);
  const dx1  = (Math.random() - 0.5) * 60 + "vw";
  const dy1  = (Math.random() - 0.5) * 60 + "vh";
  const dx2  = (Math.random() - 0.5) * 60 + "vw";
  const dy2  = (Math.random() - 0.5) * 60 + "vh";
  const dx3  = (Math.random() - 0.5) * 80 + "vw";
  const dy3  = (Math.random() - 0.5) * 80 + "vh";

  el.style.setProperty("--gd",  dur + "s");
  el.style.setProperty("--gs",  size + "px");
  el.style.setProperty("--gx1", dx1);
  el.style.setProperty("--gy1", dy1);
  el.style.setProperty("--gx2", dx2);
  el.style.setProperty("--gy2", dy2);
  el.style.setProperty("--gx3", dx3);
  el.style.setProperty("--gy3", dy3);

  document.body.appendChild(el);
  el.addEventListener("animationend", () => el.remove());
}

function startGhostSwarm(count) {
  if (_ghostTimer) { clearInterval(_ghostTimer); _ghostTimer = null; }
  // burst: spawn `count` ghosts immediately, staggered
  for (let i = 0; i < count; i++) {
    setTimeout(spawnGhost, i * 300);
  }
  // then keep trickling one every few seconds
  _ghostTimer = setInterval(spawnGhost, 4000);
}

function stopGhosts() {
  if (_ghostTimer) { clearInterval(_ghostTimer); _ghostTimer = null; }
  document.querySelectorAll(".roam-ghost").forEach(g => g.remove());
}

// ----- card grid ---------------------------------------------
function renderCards(report) {
  const grid = document.getElementById("card-grid");
  if (!grid) return;

  if (!report || !report.differences || report.differences.length === 0) {
    stopGhosts();
    grid.innerHTML = `
      <div class="state-empty">
        <span class="idle-ghost">✝</span>
        <div class="idle-headline">All spirits laid to rest.</div>
        <div class="idle-tombstone">Schema matches declared state</div>
        <p class="idle-sub">No drift detected. The crypt is clean.</p>
      </div>`;
    return;
  }

  grid.innerHTML = report.differences
    .map((diff, i) => renderCard(diff, report, i))
    .join("\n");

  // spawn ghosts proportional to unresolved drift count
  const unresolvedCount = report.differences.filter(d =>
    d.state !== "RESOLVED" && d.state !== "SUPERSEDED"
  ).length;
  if (unresolvedCount > 0) {
    startGhostSwarm(unresolvedCount + 1);
  } else {
    stopGhosts();
  }
}

// ----- API calls ---------------------------------------------
async function apiCompare() {
  const btn = document.getElementById("btn-compare");
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Hunting…'; }

  showRitual();

  try {
    const res = await fetch("/compare", { method: "POST" });
    if (!res.ok) throw new Error(`/compare returned ${res.status}`);
    const report = await res.json();
    REPORT = report;
    // update latest_report_id on server via config re-fetch isn't needed —
    // server updates it internally; we just hold the report in memory.
    renderCards(REPORT);
    updateHealthBar(REPORT);
  } catch (err) {
    showError(err.message);
  } finally {
    stopRitual();
    if (btn) { btn.disabled = false; btn.innerHTML = "Hunt Ghosts"; }
  }
}

async function apiApply(diffId) {
  _applying.add(diffId);
  renderCards(REPORT);  // show spinner

  try {
    const res = await fetch("/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ difference_id: diffId }),
    });

    if (!res.ok) throw new Error(`/apply returned ${res.status}`);

    const outcome = await res.json();

    // patch the in-memory report with the new state
    if (REPORT) {
      const diff = REPORT.differences.find(d => d.id === diffId);
      if (diff) diff.state = outcome.state;
      if (!REPORT.outcomes) REPORT.outcomes = {};
      REPORT.outcomes[diffId] = outcome;
    }

    renderCards(REPORT);
    updateHealthBar(REPORT);
  } catch (err) {
    showError(err.message);
  } finally {
    _applying.delete(diffId);
  }
}

async function apiRollback(diffId) {
  try {
    const res = await fetch("/rollback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ difference_id: diffId }),
    });
    if (!res.ok) throw new Error(`/rollback returned ${res.status}`);
    // rollback doesn't change state — just re-fetch the report
    await loadLatestReport();
  } catch (err) {
    showError(err.message);
  }
}

async function loadLatestReport() {
  if (!CONFIG.latest_report_id) return;
  try {
    const res = await fetch(`/report/${CONFIG.latest_report_id}`);
    if (!res.ok) return;
    REPORT = await res.json();
    renderCards(REPORT);
    updateHealthBar(REPORT);
  } catch (_) { /* silent — no prior report yet */ }
}

// ----- confirm dialog ----------------------------------------
function showApplyDialog(diffId) {
  if (!REPORT) return;
  const diff       = REPORT.differences.find(d => d.id === diffId);
  const resolution = REPORT.resolutions ? REPORT.resolutions[diffId] : null;
  if (!diff || !resolution) return;

  const change = resolution.proposed_change;
  const sql    = change && change.statement ? change.statement : "(no SQL)";
  const isDb   = CONFIG.domain === "database";

  document.getElementById("dialog-title").textContent   = `Apply fix — ${diff.target}`;
  document.getElementById("dialog-change").textContent  = sql;
  document.getElementById("dialog-warn").textContent    = isDb
    ? "⚠  Automatic rollback is unsupported. The originally observed state and the executed SQL are preserved for manual recovery."
    : "⚠  File patches are atomic. A rollback event will be recorded if you need to revert.";

  document.getElementById("dialog-confirm").onclick = () => {
    hideDialog();
    apiApply(diffId);
  };

  document.getElementById("dialog-backdrop").classList.remove("hidden");
}

function hideDialog() {
  document.getElementById("dialog-backdrop").classList.add("hidden");
}

// ----- demo reset --------------------------------------------
async function apiReset() {
  const btn = document.getElementById("btn-reset");
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span>Resetting…'; }

  try {
    const res = await fetch("/demo/reset", { method: "POST" });
    if (!res.ok) throw new Error(`/demo/reset returned ${res.status}`);

    REPORT = null;
    renderCards(null);
    updateHealthBar(null);

    const banner = document.getElementById("reset-banner");
    if (banner) {
      banner.style.display = "block";
      setTimeout(() => { banner.style.display = "none"; }, 4000);
    }
  } catch (err) {
    showError(err.message);
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = "↺ Reset Demo"; }
  }
}

// ----- error display -----------------------------------------
function showError(msg) {
  const grid = document.getElementById("card-grid");
  if (grid) {
    grid.innerHTML = `<div class="state-error">Error: ${esc(msg)}</div>`;
  }
}

// ----- event delegation --------------------------------------
function handleCardClick(e) {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;

  const action = btn.dataset.action;
  const id     = btn.dataset.id;

  if (action === "apply") {
    if (CONFIG.domain === "api" || CONFIG.domain === "cache") {
      apiApply(id);
    } else {
      showApplyDialog(id);
    }
  }
  if (action === "rollback") apiRollback(id);
}

// ----- scan line (fires once) --------------------------------
function triggerScanLine() {
  const el = document.createElement("div");
  el.className = "scan-line";
  document.body.appendChild(el);
  el.addEventListener("animationend", () => el.remove(), { once: true });
}

// ----- boot --------------------------------------------------
async function boot() {
  try {
    const res = await fetch("/config");
    if (!res.ok) throw new Error("config unavailable");
    CONFIG = await res.json();
  } catch (err) {
    CONFIG = { domain: "database", accent: "#f97316", title: "The Crypt", latest_report_id: null };
  }

  // apply accent colour
  document.documentElement.style.setProperty("--accent", CONFIG.accent || "#f97316");

  // set title
  const titleEl = document.getElementById("app-title");
  if (titleEl) titleEl.textContent = CONFIG.title || "Touchstone";
  document.title = `${CONFIG.title || "Touchstone"} — Touchstone`;

  const domainEl = document.getElementById("app-domain");
  if (domainEl) domainEl.textContent = CONFIG.domain || "";

  // populate repo selector
  const repoSelect = document.getElementById("repo-select");
  if (repoSelect && CONFIG.repos && CONFIG.repos.length > 0) {
    repoSelect.innerHTML = CONFIG.repos.map(r =>
      `<option value="${esc(r.id)}" ${r.id === CONFIG.active_repo ? "selected" : ""}>${esc(r.name)}</option>`
    ).join("");
    repoSelect.addEventListener("change", async () => {
      await fetch("/repos/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ repo_id: repoSelect.value }),
      });
      REPORT = null;
      renderCards(null);
      updateHealthBar(null);
    });
  }

  // wire compare button
  const compareBtn = document.getElementById("btn-compare");
  if (compareBtn) compareBtn.addEventListener("click", apiCompare);

  // wire reset button
  const resetBtn = document.getElementById("btn-reset");
  if (resetBtn) resetBtn.addEventListener("click", apiReset);

  // wire card grid clicks (delegation)
  const grid = document.getElementById("card-grid");
  if (grid) grid.addEventListener("click", handleCardClick);

  // wire dialog cancel
  const cancelBtn = document.getElementById("dialog-cancel");
  if (cancelBtn) cancelBtn.addEventListener("click", hideDialog);

  const backdrop = document.getElementById("dialog-backdrop");
  if (backdrop) backdrop.addEventListener("click", e => {
    if (e.target === backdrop) hideDialog();
  });

  // scan line on load
  triggerScanLine();

  // restore previous report if available
  await loadLatestReport();

  // show demo banner if no report yet
  if (!CONFIG.latest_report_id) {
    const grid2 = document.getElementById("card-grid");
    if (grid2) grid2.innerHTML = `
      <div class="state-empty">
        <span class="idle-ghost">👻</span>
        <div class="idle-headline">${IDLE_COPY[CONFIG.domain]?.headline || "The crypt is quiet… for now."}</div>
        <div class="idle-tombstone">${IDLE_COPY[CONFIG.domain]?.tombstone || "R.I.P. undeclared drift"}</div>
        <p class="idle-sub">
          Press <em>Hunt Ghosts</em> to ${IDLE_COPY[CONFIG.domain]?.sub || "scan your database schema"}<br>
          and reveal what lurks in the shadows.
        </p>
      </div>`;
  }
}

document.addEventListener("DOMContentLoaded", boot);
