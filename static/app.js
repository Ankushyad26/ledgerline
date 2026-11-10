/* Ledgerline UI — vanilla JS single-page app (no build step). */
const $ = (s, el = document) => el.querySelector(s);
const app = $("#app");
const state = { poll: null, filter: "ALL", search: "", toggled: {}, docRun: null, tab: "doc", openPO: {} };

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function inr(x) {
  if (x === null || x === undefined || x === "") return "—";
  const n = Number(x);
  const s = Math.abs(n).toLocaleString("en-IN", { minimumFractionDigits: n % 1 ? 2 : 0, maximumFractionDigits: 2 });
  return (n < 0 ? "-" : "") + "₹" + s;
}
const LABEL = { APPROVED: "Approved", ON_HOLD: "Flagged for review", REJECTED: "Rejected", ERROR: "Error", SUPERSEDED: "Superseded" };
const confPill = (c, required) => {
  if (c === null || c === undefined) return `<span class="conf na">not printed</span>`;
  if (c === 0) return `<span class="conf low">${required ? "missing" : "—"}</span>`;
  const lvl = c >= 0.9 ? "high" : c >= 0.6 ? "mid" : "low";
  return `<span class="conf ${lvl}"><span class="cbar"><span style="width:${Math.round(c * 100)}%"></span></span>${Math.round(c * 100)}%</span>`;
};
const SEVTXT = { hold: "review", reject: "reject", warn: "note", pass: "ok", info: "info" };
const MARK = { pass: "✓", info: "i", warn: "!", hold: "‖", reject: "✕", error: "✕", skipped: "–", pending: "", running: "" };
const time = (iso) => (iso ? iso.replace("T", " ").slice(5, 16) : "");

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2600);
}
function stopPoll() { if (state.poll) { clearTimeout(state.poll); state.poll = null; } }

/* ------------------------------------------------------------------ router */
async function route() {
  stopPoll();
  const [, view, id] = (location.hash || "#/new").split("/");
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  try {
    if (view === "run" && id) return renderRun(id);
    if (view === "dashboard") return renderDashboard();
    if (view === "ledger") return renderLedger();
    if (view === "vendors") return renderVendors();
    if (view === "rules") return renderRules();
    return renderNew();
  } catch (e) { app.innerHTML = `<div class="card pad">Something went wrong: ${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", route);

/* ------------------------------------------------------------------ new run */
const ARROW = '<span class="dither"></span>';
const head = (eyebrow, title, sub, actions = "") => `<div class="page-head"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1>${sub ? `<p class="sub">${sub}</p>` : ""}</div>${actions}</div>`;
const STEPS = [
  ["Intake", "Fingerprint the file"], ["Read", "Text layer or scan?"], ["Extract", "AI vision + parser cross-check"],
  ["Validate", "Fields, maths, dates, bill-to"], ["Vendor", "Master, status, GSTIN, bank"], ["De-duplicate", "Format-insensitive numbers"],
  ["Match PO", "Explicit or inferred"], ["Reconcile", "PO × goods receipt × invoice"], ["Decide", "Approve · review · reject"],
  ["Route", "Post, or send to an owner"]];

async function renderNew() {
  const samples = await api("/api/samples");
  app.innerHTML = `
  <section class="hero">
    <div>
      <div class="eyebrow">Accounts payable · digital employee</div>
      <h1>Every invoice, read, checked and decided.</h1>
      <p class="sub">Drop a vendor invoice — digital PDF, scan or phone photo. Ledgerline reads it, checks it against the vendor master,
        purchase orders, goods receipts and invoice history, and returns a decision with every step visible.</p>
      <div class="hero-points"><span>Approve &amp; post</span><span>Flag for review with an owner</span><span>Reject with a reason</span></div>
    </div>
    <label class="drop" id="drop">
      <input type="file" id="file" accept=".pdf,.png,.jpg,.jpeg,.webp" multiple hidden />
      <div class="big">↑</div><b>Upload invoices</b>
      <span class="muted">PDF, PNG or JPG · one file or a whole batch</span>
      <div style="margin-top:16px"><span class="btn primary">Choose files ${ARROW}</span></div>
    </label>
  </section>

  <div class="section-head"><div><div class="eyebrow">Demo library · ${samples.length} invoices</div><h2>A happy path, and the cases that cost AP teams money</h2></div>
    <button class="btn" id="run-all">Run all in order ${ARROW}</button></div>
  <div class="samples">${samples.map((s, i) => `
    <div class="card sample">
      <div class="top"><span class="num">${String(i + 1).padStart(2, "0")}</span><span class="tag ${s.kind}">${s.kind === "happy" ? "Happy path" : "Edge case"}</span></div>
      <div class="t">${esc(s.title)}</div>
      <div class="d">${esc(s.description)}</div>
      <div class="row"><span class="pill ${s.expected}">${LABEL[s.expected] || "—"}</span>
        <span class="actions"><a class="btn small" href="/api/samples/${encodeURIComponent(s.file)}" target="_blank" title="Open PDF">PDF</a>
        <button class="btn small primary" data-run="${esc(s.file)}">Run ${ARROW}</button></span></div>
    </div>`).join("")}
  </div>

  <div class="section-head"><div><div class="eyebrow">How it works</div><h2>Ten steps, every one visible in the live run</h2></div></div>
  <div class="card steps">${STEPS.map(([h, x], i) => `<div class="s"><div class="n">${String(i + 1).padStart(2, "0")}</div><div class="h">${h}</div><div class="x">${x}</div></div>`).join("")}</div>`;
  app.querySelectorAll("[data-run]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    try { const r = await api("/api/runs/sample", { method: "POST", body: JSON.stringify({ file: b.dataset.run }) }); location.hash = `#/run/${r.run_id}`; }
    catch (e) { toast(e.message); b.disabled = false; }
  }));
  $("#run-all").onclick = async () => {
    if (!confirm("Run all demo invoices in order? Results will appear on the dashboard.")) return;
    const r = await api("/api/runs/sample-batch", { method: "POST", body: JSON.stringify({ files: samples.map((s) => s.file) }) });
    toast(`${r.run_ids.length} invoices queued — processed in order`); location.hash = "#/dashboard";
  };
  const drop = $("#drop"), input = $("#file");
  const send = async (files) => {
    files = [...files];
    if (!files.length) return;
    const fd = new FormData();
    if (files.length === 1) fd.append("file", files[0]); else files.forEach((f) => fd.append("files", f));
    const r = await fetch(files.length === 1 ? "/api/runs/upload" : "/api/runs/batch", { method: "POST", body: fd });
    const d = await r.json(); if (!r.ok) return toast(d.detail || "Upload failed");
    if (files.length === 1) location.hash = `#/run/${d.run_id}`;
    else { toast(`Batch ${d.batch}: ${d.run_ids.length} invoices queued`); location.hash = "#/dashboard"; }
  };
  input.onchange = () => send(input.files);
  ["dragenter", "dragover"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (ev) => send(ev.dataTransfer.files));
}

/* ------------------------------------------------------------------ run view */
function chk(c) {
  return `<div class="chk ${c.severity}"><div class="m">${MARK[c.severity] || "•"}</div><div>
    <div class="ct">${esc(c.title)}</div>${c.detail ? `<div class="cd">${esc(c.detail)}</div>` : ""}
    ${c.action && ["hold", "reject"].includes(c.severity) ? `<div class="ca"><b>${esc(c.owner || "AP")}:</b> ${esc(c.action)}</div>` : ""}</div></div>`;
}
function block(b) {
  if (b.type === "kv") return `<div class="kv">${b.items.map(([k, v]) => `<div><span class="k">${esc(k)}</span><span class="v">${esc(v ?? "—")}</span></div>`).join("")}</div>`;
  if (b.type === "table") {
    if (!b.rows.length) return "";
    return `${b.title ? `<div class="blk-title">${esc(b.title)}</div>` : ""}<div class="tbl-wrap"><table class="t"><thead><tr>${b.columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>
      <tbody>${b.rows.map((r) => `<tr>${r.map((v) => `<td>${["pass", "hold", "warn", "reject"].includes(v) ? `<span class="pill ${v}">${SEVTXT[v] || v}</span>` : esc(v ?? "—")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  }
  if (b.type === "fields") return `<div class="blk-title">Extracted fields · ${esc(b.method)}</div>
    <div class="tbl-wrap"><table class="t"><thead><tr><th>Field</th><th>Value</th><th>Confidence</th></tr></thead><tbody>
    ${b.rows.map((r) => `<tr><td class="muted">${esc(r.label)}${r.required ? "" : ' <span class="faint">(optional)</span>'}</td><td>${r.value ? esc(r.value) : '<span class="faint">—</span>'}</td><td>${confPill(r.confidence, r.required)}</td></tr>`).join("")}
    </tbody></table></div>${b.reader && b.reader !== "text layer" ? `<div class="faint" style="font-size:12px;margin:-6px 0 12px">Reader: ${esc(b.reader)}</div>` : ""}`;
  if (b.type === "text") return `<div class="blk-title">${esc(b.title || "")}</div><pre class="note">${esc(b.text)}</pre>`;
  if (b.type === "decision") return b.reasons.length ? `<div class="checks">${b.reasons.map(chk).join("")}</div>` : `<div class="muted">No exceptions — every rule passed.</div>`;
  if (b.type === "routing") return b.routing.map(routeCard).join("");
  if (b.type === "email") return `<div class="blk-title">Drafted message to vendor</div>${emailCard(b.email)}`;
  return "";
}
function routeCard(r) {
  return `<div class="route"><div class="o">→ ${esc(r.owner)}</div><ul>${r.items.map((i) => `<li><b>${esc(i.title)}</b>${i.action ? ` — ${esc(i.action)}` : ""}</li>`).join("")}</ul></div>`;
}
function emailCard(e) {
  return `<div class="email"><div class="eh"><div><span class="muted">To:</span> ${esc(e.to)}</div><div><span class="muted">Subject:</span> <b>${esc(e.subject)}</b></div></div>
    <pre>${esc(e.body)}</pre><div style="padding:0 12px 12px"><button class="btn small" data-copy>Copy message</button></div></div>`;
}
function stageOpen(s) {
  if (s.key in state.toggled) return state.toggled[s.key];
  if (s.key === "decision" || s.key === "action") return false; // shown in the decision panel instead
  return s.status === "running" || ["warn", "hold", "reject", "error"].includes(s.status);
}
function renderStages(run) {
  const done = run.stages.filter((s) => !["pending", "running"].includes(s.status)).length;
  $("#progress").style.width = `${(done / run.stages.length) * 100}%`;
  $("#stages").innerHTML = run.stages.map((s, i) => `
    <div class="stage ${s.status} ${stageOpen(s) && s.status !== "pending" ? "open" : ""}" data-key="${s.key}">
      <div class="stage-row">
        <div class="sicon ${s.status}">${MARK[s.status] ?? ""}</div>
        <div><div class="stage-title"><span class="stage-num">${String(i + 1).padStart(2, "0")}</span>${esc(s.title)}
          ${["hold", "reject", "warn"].includes(s.status) ? `<span class="pill ${s.status}">${SEVTXT[s.status]}</span>` : ""}</div>
          <div class="stage-sum">${s.status === "running" ? "Working…" : esc(s.summary || (s.status === "pending" ? "Waiting" : ""))}</div></div>
        <div class="stage-meta">${s.ms != null ? `${s.ms} ms` : ""}${s.status !== "pending" ? '<span class="chev">›</span>' : ""}</div>
      </div>
      <div class="stage-body">${(s.checks || []).length ? `<div class="checks">${s.checks.map(chk).join("")}</div>` : ""}${(s.blocks || []).map(block).join("")}</div>
    </div>`).join("");
  $("#stages").querySelectorAll(".stage").forEach((el) => {
    $(".stage-row", el).onclick = () => {
      if (el.classList.contains("pending")) return;
      state.toggled[el.dataset.key] = !el.classList.contains("open");
      el.classList.toggle("open");
    };
  });
  bindCopy($("#stages"));
}
function bindCopy(root) {
  root.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = (ev) => {
    ev.stopPropagation();
    navigator.clipboard.writeText(b.closest(".email").querySelector("pre").textContent).then(() => toast("Copied to clipboard"));
  }));
}
function renderDecision(run) {
  const slot = $("#decision-slot");
  if (run.status === "queued") {
    slot.innerHTML = `<div class="card processing"><div class="spinner"></div><b>Queued</b><div>${run.queue_position ? `Position ${run.queue_position} in the processing queue` : "Starting…"}</div>
      <div class="faint" style="font-size:12px;margin-top:6px">Invoices are processed one at a time, in arrival order, so two invoices never draw on the same PO balance at once.</div></div>`;
    return;
  }
  if (run.status === "running") {
    const cur = run.stages.find((s) => s.status === "running");
    slot.innerHTML = `<div class="card processing"><div class="spinner"></div><b>Processing</b><div>${esc(cur ? cur.title : "Starting")}…</div></div>`;
    return;
  }
  if (run.status === "error") { slot.innerHTML = `<div class="card decision REJECTED"><div class="label">Run failed</div><div class="why">${esc(run.summary)}</div></div>`; return; }
  const res = run.result || {}, f = res.fields || {}, rv = run.review;
  if (run.decision === "SUPERSEDED") {
    slot.innerHTML = `<div class="card decision"><div class="label">Superseded</div><div class="big" style="color:var(--ink-2)">${rv.action === "rerun" ? "↻ Re-run" : "✎ Corrected"}</div>
      <div class="why">${esc(run.summary)}</div>
      ${(rv.changes || []).length ? `<div class="tbl-wrap"><table class="t"><thead><tr><th>Field</th><th>Was</th><th>Corrected to</th></tr></thead><tbody>
      ${rv.changes.map((c) => `<tr><td>${esc(c.label)}</td><td class="muted">${esc(c.old ?? "—")}</td><td><b>${esc(c.new ?? "—")}</b></td></tr>`).join("")}</tbody></table></div>` : ""}
      <div class="muted" style="font-size:13px;margin-bottom:12px">${esc(rv.by)} · ${time(rv.at)}${rv.note ? ` · “${esc(rv.note)}”` : ""}</div>
      <a class="btn primary" href="#/run/${rv.new_run}">Open re-run ${rv.new_run} →</a></div>`;
    return;
  }
  const blocking = (res.checks || []).filter((c) => ["hold", "reject"].includes(c.severity));
  const notes = (res.checks || []).filter((c) => c.severity === "warn");
  let reviewHtml = "";
  if (run.decision === "ON_HOLD" && !rv) {
    const cands = res.candidates || [];
    const poSel = res.po_inferred || (!res.po && cands.length)
      ? `<label class="muted" style="font-size:12px;display:block;margin-bottom:4px">Approve against PO</label><select id="rv-po">${cands.map((c) => `<option value="${c.po_number}" ${c.po_number === res.po ? "selected" : ""}>${c.po_number} — ${esc(c.description)} (${c.confidence}% match, ${inr(c.remaining)} open)</option>`).join("")}</select>` : "";
    reviewHtml = `<div class="card pad review"><h2>Human review</h2>
      <p class="muted" style="margin:0 0 10px;font-size:13px">The process has done the legwork; a reviewer makes the call on the exceptions above. Every action is written to the audit trail.</p>
      <div style="margin-bottom:10px;display:flex;gap:6px;flex-wrap:wrap"><button class="btn small" id="rv-correct">✎ Correct extracted data</button><button class="btn small" id="rv-rerun">↻ Re-run</button></div>
      ${poSel}<textarea id="rv-note" placeholder="Reviewer note (e.g. 'Confirmed with Priya in procurement — 7 units received, PO amended')"></textarea>
      <div class="btns"><button class="btn ok" id="rv-approve" ${res.vendor ? "" : "disabled"}>Approve &amp; post ${ARROW}</button><button class="btn bad" id="rv-reject">✕ Reject</button></div></div>`;
  }
  slot.innerHTML = `
    <div class="card decision ${run.decision}">
      <div class="label">Decision ${rv ? `<span class="pill manual" style="margin-left:6px">after human review</span>` : `<span class="pill neutral" style="margin-left:6px">automated</span>`}</div>
      <div class="big">${{ APPROVED: "✓", ON_HOLD: "‖", REJECTED: "✕" }[run.decision] || ""} ${LABEL[run.decision] || run.decision}</div>
      <div class="why">${esc(run.summary)}</div>
      <div class="facts">
        <div class="fact"><div class="k">Vendor</div><div class="v">${esc(run.vendor_name || f.vendor_name || "—")}</div></div>
        <div class="fact"><div class="k">Invoice no.</div><div class="v">${esc(f.invoice_number || "— missing")}</div></div>
        <div class="fact"><div class="k">Invoice total</div><div class="v">${inr(f.total)}</div></div>
        <div class="fact"><div class="k">Purchase order</div><div class="v">${esc(run.po_number || "—")}</div></div>
        ${res.due_date ? `<div class="fact"><div class="k">Payment due</div><div class="v">${esc(res.due_date)}</div></div>` : ""}
        <div class="fact"><div class="k">Payment terms</div><div class="v">${esc(res.payment_terms || "—")}</div></div>
        <div class="fact"><div class="k">Data confidence</div><div class="v">${res.confidence === null || res.confidence === undefined ? "—" : confPill(res.confidence, true)}</div></div>
        <div class="fact"><div class="k">Processing time</div><div class="v">${run.duration_ms ? (run.duration_ms >= 1000 ? (run.duration_ms / 1000).toFixed(1) + " s" : run.duration_ms + " ms") : "—"}</div></div>
      </div>
      ${blocking.length ? `<h3>${rv ? "Exceptions raised" : "Why"}</h3><div class="reasons">${blocking.map(chk).join("")}</div>` : ""}
      ${notes.length ? `<h3 style="margin-top:12px">Notes within tolerance</h3><div class="reasons">${notes.map(chk).join("")}</div>` : ""}
      ${(res.trace || []).length ? `<details class="trace" ${blocking.length ? "" : "open"}><summary>How the decision was reached</summary><ol>
        ${res.trace.map((t) => `<li class="${t.status}"><span class="tm">${MARK[t.status] || "•"}</span><div><b class="tt">${esc(t.title)}</b><span>${esc(t.summary)}</span></div></li>`).join("")}
        <li class="${{ APPROVED: "pass", ON_HOLD: "hold", REJECTED: "reject" }[run.decision] || "info"}"><span class="tm">=</span><div><b class="tt">Rule applied</b><span>Any reject → Rejected · any review flag → Flagged for review · otherwise → Approved. Result: <b>${LABEL[run.decision] || run.decision}</b>${rv ? " (then human review)" : ""}.</span></div></li>
      </ol></details>` : ""}
      ${rv ? `<div class="chk info" style="margin-top:12px"><div class="m">i</div><div><div class="ct">${rv.action === "approve" ? "Approved" : "Rejected"} by ${esc(rv.by)}${rv.po ? ` against ${esc(rv.po)}` : ""}</div><div class="cd">${esc(rv.note || "No note")} · ${time(rv.at)}</div></div></div>` : ""}
    </div>
    ${reviewHtml}
    ${run.decision === "REJECTED" && !rv ? `<div class="card pad"><h2>Misread?</h2><p class="muted" style="margin:0 0 10px;font-size:13px">If the reader got a field wrong, correct it and every check re-runs on the corrected data.</p><button class="btn small" id="rv-correct">✎ Correct extracted data</button> <button class="btn small" id="rv-rerun">↻ Re-run</button></div>` : ""}
    ${run.decision !== "APPROVED" && (res.routing || []).length && !rv ? `<div class="card pad"><h2>Routed to</h2>${res.routing.map(routeCard).join("")}${res.email ? `<div class="blk-title">Drafted message to vendor</div>${emailCard(res.email)}` : ""}</div>` : ""}`;
  bindCopy(slot);
  const ap = $("#rv-approve"), rj = $("#rv-reject");
  const act = async (action) => {
    try {
      const body = { action, note: $("#rv-note").value, po_number: $("#rv-po") ? $("#rv-po").value : null };
      const r = await api(`/api/runs/${run.id}/review`, { method: "POST", body: JSON.stringify(body) });
      toast(action === "approve" ? `Approved and posted to ${r.po_number}` : "Invoice rejected");
      paintRun(r); renderAudit(r);
    } catch (e) { toast(e.message); }
  };
  if (ap) ap.onclick = () => act("approve");
  const cb = $("#rv-correct"); if (cb) cb.onclick = () => openCorrection(run);
  const rr = $("#rv-rerun"); if (rr) rr.onclick = async () => {
    try { const r = await api(`/api/runs/${run.id}/rerun`, { method: "POST" }); toast(`Re-running as ${r.run_id}`); location.hash = `#/run/${r.run_id}`; }
    catch (e) { toast(e.message); }
  };
  if (rj) rj.onclick = () => act("reject");
}
function renderAudit(run) {
  if (state.tab !== "audit") return;
  $("#doc-body").innerHTML = `<div class="audit">${(run.audit || []).map((a) => `<div class="ev"><div class="at">${time(a.at)}</div><div><span class="who">${esc(a.actor)}</span> · ${esc(a.event)}</div></div>`).join("") || '<div class="muted">No events yet</div>'}</div>`;
}
function paintRun(run) {
  const busy = run.status === "running" || run.status === "queued";
  $("#run-status").outerHTML = `<span id="run-status" class="pill ${busy ? "running" : run.decision === "SUPERSEDED" ? "neutral" : run.decision}">${run.status === "queued" ? "Queued" : run.status === "running" ? "● Processing" : LABEL[run.decision] || run.decision}</span>`;
  $("#run-method").textContent = run.extraction_method ? `Extraction: ${run.extraction_method}` : "";
  renderStages(run);
  renderDecision(run);
}
async function renderRun(id) {
  state.toggled = {}; state.tab = "doc";
  const run = await api(`/api/runs/${id}`);
    app.innerHTML = `
    <div class="run-head">
      <a class="btn small" href="#/dashboard">← All runs</a>
      <h1>${esc(run.filename)}</h1><span class="id">${run.id}</span><span id="run-status"></span>
      ${run.parent_run ? `<span class="pill manual">correction of <a href="#/run/${run.parent_run}" style="margin-left:4px">${run.parent_run}</a></span>` : ""}
      <span class="muted" id="run-method" style="font-size:12.5px"></span>
      <a class="btn small" href="#/new" style="margin-left:auto">New invoice ${ARROW}</a>
    </div>
    <div class="run-grid">
      <div class="card"><div class="run-card-head"><div><div class="eyebrow">Live run</div><h2 style="margin:4px 0 0">Ten steps, as they execute</h2></div><span class="faint" style="font-size:12px">click a step for detail</span></div><div class="progress"><div id="progress"></div></div><div class="stages" id="stages"></div></div>
      <div class="side">
        <div id="decision-slot"></div>
        <div class="card doc"><div class="tabs"><button data-tab="doc" class="on">Document</button><button data-tab="audit">Audit trail</button></div>
          <div id="doc-body"><div class="preview"><img src="/api/runs/${id}/preview.png" alt="Invoice preview" /></div>
            <div class="doc-foot"><span class="muted">Page 1 preview</span><a href="/api/runs/${id}/file" target="_blank">Open original ↗</a></div></div></div>
      </div>
    </div>`;
  const docHtml = $("#doc-body").innerHTML;
  let latest = run;
  app.querySelectorAll(".tabs button").forEach((b) => (b.onclick = () => {
    state.tab = b.dataset.tab;
    app.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("on", x === b));
    if (state.tab === "doc") $("#doc-body").innerHTML = docHtml; else renderAudit(latest);
  }));
  paintRun(run);
  const tick = async () => {
    if (!location.hash.includes(id)) return;
    latest = await api(`/api/runs/${id}`);
    paintRun(latest); renderAudit(latest);
    if (["running", "queued"].includes(latest.status)) state.poll = setTimeout(tick, 350);
  };
  if (["running", "queued"].includes(run.status)) state.poll = setTimeout(tick, 250);
}

/* ------------------------------------------------------------------ dashboard */
async function renderDashboard() {
  const [st, runs] = await Promise.all([api("/api/stats"), api("/api/runs")]);
  const pct = (n) => (st.total ? (n / st.total) * 100 : 0);
  const filtered = runs.filter((r) => (state.filter === "ALL" || r.decision === state.filter || (state.filter === "RUNNING" && r.status === "running"))
    && (!state.search || JSON.stringify(r).toLowerCase().includes(state.search.toLowerCase())));
  const maxR = Math.max(1, ...st.top_reasons.map((r) => r[1]));
  const NICE = { QTY_EXCEEDS_OPEN: "Qty exceeds open PO", AMOUNT_EXCEEDS_PO: "Amount exceeds PO", PRICE_VARIANCE: "Price above tolerance",
    PO_INFERRED: "No PO on invoice", DUPLICATE: "Duplicate invoice", BANK_MISMATCH: "Bank details changed", MISSING_FIELDS: "Missing mandatory fields",
    VENDOR_BLOCKED: "Blocked vendor", UNKNOWN_VENDOR: "Unknown vendor", GST_TYPE: "Wrong GST type", GST_RATE: "GST rate mismatch", LINE_NOT_ON_PO: "Item not on PO",
    UNREADABLE: "Unreadable document", AI_UNAVAILABLE: "AI reader busy", GOODS_NOT_RECEIVED: "Billed before delivery", BUNDLED: "Bundled lines", NOT_INVOICE: "Not an invoice", LOW_CONFIDENCE: "Low read confidence", PO_NOT_FOUND: "PO not found", NEAR_DUPLICATE: "Possible duplicate",
    LINES_SUM: "Lines don't add up", TOTAL_MATH: "Totals don't add up", DUPLICATE_FILE: "Same file re-sent", BILL_TO: "Wrong bill-to GSTIN" };
  app.innerHTML = `
    ${head("Overview", "Dashboard", "Every invoice processed, its decision, and why.")}
    <div class="kpis">
      <div class="card kpi"><div class="k">Invoices processed</div><div class="v">${st.total}</div><div class="s">${inr(st.value_processed)} total value</div></div>
      <div class="card kpi feature"><div class="k">Straight-through rate</div><div class="v">${st.straight_through_rate}%</div><div class="s">auto-approved, no human touch</div></div>
      <div class="card kpi"><div class="k">Approved</div><div class="v" style="color:var(--ok)">${st.approved}</div><div class="s">${inr(st.value_approved)} posted</div></div>
      <div class="card kpi"><div class="k">Flagged for review</div><div class="v" style="color:var(--hold)">${st.on_hold}</div><div class="s">waiting for a reviewer</div></div>
      <div class="card kpi"><div class="k">Rejected</div><div class="v" style="color:var(--bad)">${st.rejected}</div><div class="s">${inr(st.value_blocked)} payment blocked</div></div>
      <div class="card kpi"><div class="k">Avg. processing time</div><div class="v">${st.avg_seconds < 1 ? Math.round(st.avg_seconds * 1000) + " ms" : st.avg_seconds + " s"}</div><div class="s">vs ~10–15 min manual</div></div>
    </div>
    <div class="dash-grid">
      <div class="card pad"><h2>Decision mix</h2>
        <div class="stack"><div style="width:${pct(st.approved)}%;background:#0a0a0a"></div><div style="width:${pct(st.on_hold)}%;background:#ea580c"></div><div style="width:${pct(st.rejected)}%;background:#b42318"></div></div>
        <div class="legend"><span style="--c:#0a0a0a">Approved ${Math.round(pct(st.approved))}%</span><span style="--c:#ea580c">Flagged ${Math.round(pct(st.on_hold))}%</span><span style="--c:#b42318">Rejected ${Math.round(pct(st.rejected))}%</span></div>
        <div class="mix-rows">
          <div class="m"><div class="k">Auto-approved</div><div class="v">${st.auto_approved}</div><div class="s">no human touch</div></div>
          <div class="m"><div class="k">Resolved by reviewers</div><div class="v">${st.reviewed}</div><div class="s">exceptions closed with a note</div></div>
          <div class="m"><div class="k">Fraud / duplicate risk caught</div><div class="v">${inr(st.value_risk)}</div><div class="s">bank changes, duplicates, blocked vendors</div></div>
        </div>
      </div>
      <div class="card pad"><h2>Top exception reasons</h2><div class="bars">${st.top_reasons.length ? st.top_reasons.map(([k, n]) => `<div class="bar"><span>${esc(NICE[k] || k)}</span><div class="track"><div class="fill" style="width:${(n / maxR) * 100}%"></div></div><b>${n}</b></div>`).join("") : '<div class="muted">No exceptions yet</div>'}</div></div>
    </div>
    <div class="card">
      <div class="toolbar">${["ALL", "APPROVED", "ON_HOLD", "REJECTED"].map((f) => `<button class="chip ${state.filter === f ? "on" : ""}" data-f="${f}">${f === "ALL" ? "All" : LABEL[f]} ${f === "ALL" ? runs.length : runs.filter((r) => r.decision === f).length}</button>`).join("")}
        <input class="search" id="search" placeholder="Search vendor, invoice, PO…" value="${esc(state.search)}" /></div>
      ${filtered.length ? `<div style="overflow-x:auto"><table class="t"><thead><tr><th>Run</th><th>Received</th><th>Vendor</th><th>Invoice no.</th><th class="right">Amount</th><th>PO</th><th>Decision</th><th>Reason</th><th class="right">Time</th></tr></thead><tbody>
        ${filtered.map((r) => `<tr class="click" data-id="${r.id}"><td class="mono">${r.id}</td><td class="nowrap muted">${time(r.created_at)}</td><td>${esc(r.vendor_name || "—")}</td><td class="mono">${esc(r.invoice_number || "—")}</td>
          <td class="right nowrap">${inr(r.total)}</td><td class="nowrap">${esc(r.po_number || "—")}</td>
          <td>${r.status === "running" ? '<span class="pill running">Processing</span>' : r.status === "queued" ? '<span class="pill running">Queued</span>' : `<span class="pill ${r.decision === "SUPERSEDED" ? "neutral" : r.decision}">${LABEL[r.decision] || r.decision}</span>`}${r.review && r.decision !== "SUPERSEDED" ? ' <span class="pill manual">reviewed</span>' : ""}${r.parent_run ? ' <span class="pill manual">corrected</span>' : ""}</td>
          <td class="muted" style="max-width:340px">${esc((r.summary || "").replace(/^(Approved|Flagged for review|Rejected) — /, ""))}</td><td class="right mono muted">${r.duration_ms ? (r.duration_ms >= 1000 ? (r.duration_ms / 1000).toFixed(1) + "s" : r.duration_ms + "ms") : ""}</td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty">No runs yet. <a href="#/new">Process your first invoice →</a></div>`}
    </div>`;
  app.querySelectorAll("[data-f]").forEach((b) => (b.onclick = () => { state.filter = b.dataset.f; renderDashboard(); }));
  app.querySelectorAll("tr[data-id]").forEach((tr) => (tr.onclick = () => (location.hash = `#/run/${tr.dataset.id}`)));
  const s = $("#search");
  s.oninput = () => { state.search = s.value; clearTimeout(s._t); s._t = setTimeout(() => { renderDashboard().then(() => { const n = $("#search"); n.focus(); n.setSelectionRange(n.value.length, n.value.length); }); }, 250); };
  if (runs.some((r) => ["running", "queued"].includes(r.status))) state.poll = setTimeout(() => location.hash.startsWith("#/dashboard") && renderDashboard(), 1500);
}

/* ------------------------------------------------------------------ ledger */
async function renderLedger() {
  const pos = await api("/api/ledger");
  app.innerHTML = `
    <div class="page-head"><div><div class="eyebrow">Procurement data</div><h1>Purchase order ledger</h1><p class="sub">Live PO balances with goods receipts. Approved invoices consume PO quantities; billing is capped at what stores actually received (three-way match).</p></div></div>
    <div class="card" style="overflow-x:auto"><table class="t"><thead><tr><th>PO</th><th>Vendor</th><th>Description</th><th>Date</th><th class="right">PO value</th><th class="right">Invoiced</th><th class="right">Open</th><th>Consumed</th><th>Invoices</th></tr></thead><tbody>
    ${pos.map((p) => {
      const used = p.value ? (p.invoiced_value / p.value) * 100 : 0;
      return `<tr class="click" data-po="${p.po_number}"><td class="mono"><b>${p.po_number}</b></td><td>${esc(p.vendor_name)}</td><td class="muted">${esc(p.description)}</td><td class="nowrap muted">${p.po_date}</td>
        <td class="right nowrap">${inr(p.value)}</td><td class="right nowrap">${inr(p.invoiced_value)}</td><td class="right nowrap"><b>${inr(p.remaining_value)}</b></td>
        <td><div class="meter ${used > 100.5 ? "over" : used >= 99.5 ? "full" : ""}"><div style="width:${Math.min(100, used)}%"></div></div><span class="faint" style="font-size:11.5px">${Math.round(used)}%${p.status === "closed" ? " · closed" : ""}</span></td>
        <td>${p.invoices.length} <span class="faint">${state.openPO[p.po_number] ? "▾" : "▸"}</span></td></tr>
        ${state.openPO[p.po_number] ? `<tr class="sub-row"><td></td><td colspan="8">
          <table class="t" style="margin-bottom:6px"><thead><tr><th>PO line</th><th class="right">Ordered</th><th class="right">Received (GRN)</th><th class="right">Invoiced</th><th class="right">Unit price</th></tr></thead><tbody>${p.lines.map((l) => `<tr><td>${esc(l.item)}</td><td class="right">${l.qty}</td><td class="right">${p.receipt_required ? l.received_qty : '<span class="faint">service</span>'}</td><td class="right">${l.invoiced_qty}</td><td class="right">${inr(l.unit_price)}</td></tr>`).join("")}</tbody></table>
          ${p.grns.length ? `<div class="faint" style="font-size:12px;margin:0 0 8px">Goods receipts: ${p.grns.map((g) => `${esc(g.grn_number)} (line ${g.line_no}: ${g.received_qty} on ${g.grn_date})`).join(" · ")}</div>` : ""}
          ${p.invoices.length ? `<table class="t"><thead><tr><th>Invoice</th><th>Date</th><th class="right">Pre-tax</th><th class="right">Total</th><th>Source</th></tr></thead><tbody>${p.invoices.map((i) => `<tr><td class="mono">${esc(i.invoice_number || "—")}</td><td>${i.invoice_date || "—"}</td><td class="right">${inr(i.subtotal)}</td><td class="right">${inr(i.total)}</td><td>${i.run_id ? `<a href="#/run/${i.run_id}">${i.run_id}</a>` : esc(i.source)}</td></tr>`).join("")}</tbody></table>` : '<div class="muted">No invoices posted yet</div>'}
        </td></tr>` : ""}`;
    }).join("")}</tbody></table></div>`;
  app.querySelectorAll("tr[data-po]").forEach((tr) => (tr.onclick = () => { state.openPO[tr.dataset.po] = !state.openPO[tr.dataset.po]; renderLedger(); }));
}

/* ------------------------------------------------------------------ vendors */
async function renderVendors() {
  const vs = await api("/api/vendors");
  app.innerHTML = `
    <div class="page-head"><div><div class="eyebrow">Procurement data</div><h1>Vendor master</h1><p class="sub">The source of truth invoices are checked against — identity, GSTIN, status and the only bank account we will pay.</p></div></div>
    <div class="card" style="overflow-x:auto"><table class="t"><thead><tr><th>ID</th><th>Vendor</th><th>GSTIN</th><th>State</th><th>Bank on file</th><th>IFSC</th><th>Terms</th><th>AP contact</th><th>Status</th></tr></thead><tbody>
    ${vs.map((v) => `<tr><td class="mono">${v.vendor_id}</td><td><b>${esc(v.name)}</b></td><td class="mono">${v.gstin}</td><td>${esc(v.state)}</td><td>${esc(v.bank_name)} ••••${v.bank_account.slice(-4)}</td><td class="mono">${v.ifsc}</td><td class="nowrap">Net ${v.payment_terms_days}</td><td class="muted">${esc(v.email)}</td>
      <td><span class="pill ${v.status === "active" ? "pass" : "reject"}">${v.status}</span>${v.status_note ? `<div class="faint" style="font-size:12px">${esc(v.status_note)}</div>` : ""}</td></tr>`).join("")}
    </tbody></table></div>`;
}

/* ------------------------------------------------------------------ rules */
async function renderRules() {
  const c = await api("/api/config");
  app.innerHTML = `
    <div class="page-head"><div><div class="eyebrow">Policy</div><h1>Rules &amp; policy</h1><p class="sub">Plain-English policy the process enforces. Thresholds and duplicate rules live in the reference dataset, so finance can change them without code.</p></div></div>
    <div class="card pad principle" style="margin-bottom:16px"><div class="eyebrow" style="color:#3d3d3d">Design principle</div><h2 style="font-size:24px;letter-spacing:-.035em;margin:6px 0 10px">AI reads. Rules decide. Humans own the exceptions.</h2>
      <div>A vision model turns messy PDFs and scans into structured data, cross-checked by an independent rule-based parser.
      Every approve / review / reject traces to a named rule below, with an owner and a next action — so the decision is explainable to an auditor and nothing pays out on a guess.</div>
      <div style="margin-top:10px" class="muted">Reader in use: <b>${c.llm.enabled ? `${esc(c.llm.provider)} · ${esc(c.llm.model)}` : "offline mode — PDF text layer + Tesseract OCR"}</b> · Buyer: <b>${esc(c.buyer.name)}</b> (${c.buyer.gstin})</div></div>
    <div class="rules-grid">${c.rulebook.map((s) => `<div class="card pad"><h2>${esc(s.section)}</h2><ul>${s.rules.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></div>`).join("")}</div>
    <div class="rules-grid" style="margin-top:16px">
      <div class="card pad"><h2>Thresholds <span class="faint" style="font-weight:500;font-size:12px">· data/seed/policy.json</span></h2>
        <table class="t">${Object.entries(c.rules).map(([k, v]) => `<tr><td class="muted">${esc(k.replace(/_/g, " "))}</td><td class="right"><b>${esc(v)}</b></td></tr>`).join("")}</table></div>
      <div class="card pad"><h2>Duplicate-detection rules <span class="faint" style="font-weight:500;font-size:12px">· policy.json</span></h2>
        <table class="t">${c.duplicate_rules.map((d) => `<tr><td>${esc(d.rule)}</td><td class="right"><span class="pill ${d.action === "reject" ? "reject" : "hold"}">${d.action === "reject" ? "Reject" : "Review"}</span></td></tr>`).join("")}</table></div>
    </div>`;
}

/* ------------------------------------------------------------------ boot */
(async function boot() {
  try {
    const c = await api("/api/config");
    $("#company").innerHTML = `Buyer · <b>${esc(c.buyer.name)}</b> · GSTIN ${esc(c.buyer.gstin)}`;
    const chip = $("#ai-chip");
    chip.innerHTML = c.llm.enabled ? `<span class="dot"></span>${esc(c.llm.provider)} · ${esc(c.llm.model)}` : `<span class="dot"></span>Offline · text + OCR`;
    chip.classList.toggle("off", !c.llm.enabled);
  } catch (e) { /* ignore */ }
  $("#reset-btn").onclick = async () => {
    if (!confirm("Reset master data and clear all runs?")) return;
    await api("/api/reset", { method: "POST" }); toast("Demo data reset"); route();
  };
  route();
})();

/* ------------------------------------------------------------------ correction modal */
async function openCorrection(run) {
  const defs = await api("/api/editable-fields");
  const f = (run.result || {}).fields || {};
  const fc = f.field_confidence || {};
  const confKey = { vendor_name: "vendor", vendor_gstin: "vendor", invoice_number: "invoice_number", invoice_date: "invoice_date",
    po_reference: "po_reference", total: "total", payment_terms: "payment_terms", bank_account: "bank_account" };
  const lines = (f.line_items || []).map((l) => ({ ...l }));
  const m = document.createElement("div");
  m.className = "modal-bg";
  m.innerHTML = `<div class="modal card">
    <div class="modal-head"><div><h2 style="margin:0">Correct extracted data</h2><div class="muted" style="font-size:13px">Fix what the reader got wrong. Every check re-runs on the corrected values; the original run is kept in the audit trail.</div></div><button class="btn small" data-x>✕</button></div>
    <div class="modal-body"><div class="modal-grid">
      <div class="preview" style="max-height:560px"><img src="/api/runs/${run.id}/preview.png" /></div>
      <div>
        <div class="form-grid">${defs.map((d) => `<label><span>${esc(d.label)} ${confKey[d.key] ? confPill(fc[confKey[d.key]], ["vendor", "invoice_number", "invoice_date", "total"].includes(confKey[d.key])) : ""}</span>
          <input data-k="${d.key}" value="${esc(f[d.key] ?? "")}" placeholder="${d.key === "invoice_date" ? "YYYY-MM-DD" : ""}" /></label>`).join("")}</div>
        <div class="blk-title" style="margin-top:14px">Line items</div>
        <table class="t edit-lines"><thead><tr><th>Description</th><th>Qty</th><th>Unit price</th><th>Amount</th><th></th></tr></thead><tbody></tbody></table>
        <button class="btn small" data-add style="margin-top:6px">＋ Add line</button>
        <label class="note-l"><span>Reviewer note (why the change)</span><textarea data-note placeholder="e.g. Vendor confirmed invoice no. SIC/1201 by phone"></textarea></label>
      </div></div></div>
    <div class="modal-foot"><button class="btn" data-x>Cancel</button><button class="btn primary" data-save>Save &amp; re-run ${ARROW}</button></div></div>`;
  document.body.appendChild(m);
  const tbody = m.querySelector(".edit-lines tbody");
  const drawLines = () => {
    tbody.innerHTML = lines.map((l, i) => `<tr>
      <td><input data-i="${i}" data-f="description" value="${esc(l.description ?? "")}" /></td>
      <td><input data-i="${i}" data-f="quantity" value="${esc(l.quantity ?? "")}" style="width:70px" /></td>
      <td><input data-i="${i}" data-f="unit_price" value="${esc(l.unit_price ?? "")}" style="width:100px" /></td>
      <td class="nowrap">${inr(l.amount)}</td><td><button class="btn small" data-del="${i}">✕</button></td></tr>`).join("");
    tbody.querySelectorAll("input").forEach((inp) => (inp.oninput = () => {
      const l = lines[+inp.dataset.i]; l[inp.dataset.f] = inp.value;
      const q = parseFloat(String(l.quantity).replace(/,/g, "")), p = parseFloat(String(l.unit_price).replace(/,/g, ""));
      l.amount = isFinite(q * p) ? Math.round(q * p * 100) / 100 : null;
      inp.closest("tr").children[3].textContent = inr(l.amount);
    }));
    tbody.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => { lines.splice(+b.dataset.del, 1); drawLines(); }));
  };
  drawLines();
  m.querySelector("[data-add]").onclick = () => { lines.push({ description: "", quantity: 1, unit_price: 0, amount: 0 }); drawLines(); };
  m.querySelectorAll("[data-x]").forEach((b) => (b.onclick = () => m.remove()));
  m.onclick = (e) => { if (e.target === m) m.remove(); };
  m.querySelector("[data-save]").onclick = async () => {
    const fields = {};
    m.querySelectorAll("[data-k]").forEach((i) => (fields[i.dataset.k] = i.value.trim()));
    try {
      const r = await api(`/api/runs/${run.id}/correct`, { method: "POST", body: JSON.stringify({
        fields, line_items: lines, note: m.querySelector("[data-note]").value }) });
      m.remove(); toast(`Corrections saved — re-running as ${r.run_id}`); location.hash = `#/run/${r.run_id}`;
    } catch (e) { toast(e.message); }
  };
}
