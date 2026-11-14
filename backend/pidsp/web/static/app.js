"use strict";
// PiDSP web UI. Plain JS, no build step. Pages are rendered once by html()
// and refreshed by update(status) every poll.

const POLL_MS = 3000;
const LEVELS_MS = 400;
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const minus = (s) => String(s).replace(/^-/, "−");

let status = null;
let statusError = null;

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg).join("; ") : body.detail || detail;
    } catch (_) { /* not JSON */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function set(id, html) {
  const el = document.getElementById(id);
  if (el && el.innerHTML !== html) el.innerHTML = html;
}

// ---------- formatting ----------

function fmtDuration(s) {
  if (s == null) return "—";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
}
const fmtBytes = (b) => (b == null ? "—" : b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.round(b / 1e6)} MB`);
const dot = (kind) => `<span class="dot ${kind}" aria-hidden="true"></span>`;

function engine(s) {
  // One summary of CamillaDSP's state, shared by the header pill and the pages.
  const c = s?.camilladsp;
  if (!c) return { kind: "bad", text: "PiDSP not reachable" };
  if (!c.reachable) return { kind: "bad", text: "CamillaDSP not reachable" };
  const rate = c.capture_rate ? ` · ${(c.capture_rate / 1000).toFixed(c.capture_rate % 1000 ? 1 : 0)} kHz` : "";
  if (s.measuring) return { kind: "warn", text: "Measuring…", input: "In use for measuring" };
  if (s.guard?.silenced) return { kind: "bad", text: `Silenced: the TV sends ${s.guard.format}`, input: `${s.guard.format}, not PCM` };
  if (c.config_path?.endsWith("/measure.yml") && c.state !== "Inactive")
    return { kind: "warn", text: "CamillaDSP running a measurement setup", input: "In use for measuring" };
  switch (c.state) {
    case "Running": return { kind: "ok", text: `CamillaDSP running${rate} · optical locked${c.config_path?.endsWith("/compare.yml") ? " · room correction off" : ""}`, input: "Locked" };
    case "Stalled": return { kind: "warn", text: "CamillaDSP waiting for an optical signal", input: "No signal" };
    case "Paused": return { kind: "warn", text: "CamillaDSP paused (no signal)", input: "No signal" };
    case "Starting": return { kind: "warn", text: "CamillaDSP starting…", input: "—" };
    default: return { kind: "", text: "CamillaDSP idle · not calibrated yet", input: "Not in use yet" };
  }
}

// ---------- chart ----------

const F_MIN = 20, F_MAX = 20000;
function logspace(n) { return Array.from({ length: n }, (_, i) => F_MIN * Math.pow(F_MAX / F_MIN, i / (n - 1))); }

function chart({ series, yMin = -15, yMax = 15, height = 220, width = 640, label, fMin = F_MIN, fMax = F_MAX }) {
  if (window.innerWidth < 700) {  // keep axis text readable on phones
    width = Math.max(280, window.innerWidth - 74);
    height = Math.min(height, 240);
  }
  const W = width, H = height, L = 34, B = 20, T = 6;
  const x = (f) => L + ((Math.log10(f) - Math.log10(fMin)) / (Math.log10(fMax) - Math.log10(fMin))) * (W - L);
  const y = (db) => T + ((yMax - Math.max(yMin, Math.min(yMax, db))) / (yMax - yMin)) * (H - T - B);
  const inRange = (f) => f >= fMin && f <= fMax;
  let g = "";
  for (const f of [20, 30, 40, 50, 60, 80, 100, 200, 300, 500, 1000, 2000, 5000, 10000, 20000].filter(inRange))
    g += `<line class="grid-line" x1="${x(f)}" x2="${x(f)}" y1="${T}" y2="${H - B}"/>`;
  for (let db = Math.ceil(yMin / 5) * 5; db <= yMax; db += 5) {
    g += `<line class="grid-line" x1="${L}" x2="${W}" y1="${y(db)}" y2="${y(db)}"/>`;
    if (db % 10 === 0) g += `<text class="axis" x="${L - 6}" y="${y(db) + 4}" text-anchor="end">${minus(db > 0 ? "+" + db : db)}</text>`;
  }
  const ticks = fMax <= 2000 ? [[20, "20"], [50, "50"], [100, "100"], [200, "200"], [500, "500"], [1000, "1k"], [2000, "2k"]]
    : [[20, "20"], [100, "100"], [1000, "1k"], [10000, "10k"], [20000, "20k"]];
  const shown = ticks.filter(([f]) => inRange(f));
  shown.forEach(([f, t], i) => {
    const last = i === shown.length - 1;
    g += `<text class="axis" x="${x(f)}" y="${H - 4}" text-anchor="${i === 0 ? "start" : last ? "end" : "middle"}">${t}${last ? " Hz" : ""}</text>`;
  });
  const paths = series.map((s) => {
    const pts = s.f.map((f, i) => [f, s.db[i]]).filter(([f]) => inRange(f));
    const d = pts.map(([f, db], i) => `${i ? "L" : "M"}${x(f).toFixed(1)},${y(db).toFixed(1)}`).join("");
    return `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width || 1.8}"${s.dash ? ' stroke-dasharray="5 4"' : ""} stroke-linejoin="round"/>`;
  }).join("");
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">${g}${paths}</svg></div>`;
}

// Synthetic curves for the example graphs, until real measurements exist.
const EX = (() => {
  const f = logspace(240);
  const lf = (v) => Math.log2(v);
  const bump = (v, fc, gain, q) => gain / (1 + Math.pow(q * (lf(v) - lf(fc)) * 2, 2));
  const target = f.map((v) => (v < 200 ? 4 * Math.min(1, (lf(200) - lf(v)) / 3.3) : 0) - (v > 1000 ? 0.9 * (lf(v) - lf(1000)) : 0));
  const err = f.map((v) => bump(v, 42, 9, 2.2) + bump(v, 95, -10, 3) + bump(v, 160, 4, 2) + bump(v, 380, -3, 1.5)
    + 1.2 * Math.sin(lf(v) * 7.3) * Math.min(1, v / 300) + 0.6 * Math.sin(lf(v) * 23.1) + (v > 12000 ? -3 * (lf(v) - lf(12000)) : 0)
    - (v < 28 ? 8 * (lf(28) - lf(v)) : 0));
  const measured = target.map((t, i) => t + err[i]);
  const after = target.map((t, i) => t + err[i] * (f[i] < 500 ? 0.18 : 0.3) - (f[i] < 25 ? 1.5 : 0));
  return { f, target, measured, after };
})();

function exampleChart(withAfter = true) {
  const series = [
    { f: EX.f, db: EX.target, color: "#8C939E", dash: true, width: 1.4 },
    { f: EX.f, db: EX.measured, color: "#F2A93B", width: 1.6 },
  ];
  if (withAfter) series.push({ f: EX.f, db: EX.after, color: "#5AA9FF", width: 2 });
  return chart({ series, label: "Example frequency response: measured, target and corrected" });
}

const legend = (after = "After") => `<div class="legend">
  <span><i style="background:#F2A93B"></i>Before</span>
  <span><i style="background:#5AA9FF"></i>${after}</span>
  <span><i class="dash"></i>Target</span></div>`;

const EXAMPLE_TAG = `<span class="tag example" title="Example data until you calibrate">Example</span>`;

const EXAMPLE_PLAN = {
  crossover_hz: 80, crossover_order: 4, summation_db: -0.3,
  channels: { left: { delay_ms: 11.55, trim_db: 0 }, right: { delay_ms: 10.95, trim_db: -0.8 }, sub: { delay_ms: 0, trim_db: 1.5, inverted: true } },
};
const fmtDb = (v) => minus(`${v > 0 ? "+" : ""}${Number(v).toFixed(1)} dB`);

function bassTiles(plan) {
  const ch = plan.channels, sum = plan.summation_db;
  const tile = (k, v, sub, style = "") => `<div class="tile"><div class="k">${k}</div><div class="v"${style}>${v}</div><div class="k">${sub}</div></div>`;
  return `<div class="tiles">
    ${tile("Crossover", `${plan.crossover_hz} Hz`, `LR${plan.crossover_order || 4}, ${6 * (plan.crossover_order || 4)} dB/oct`)}
    ${tile("Left delay", `${ch.left.delay_ms.toFixed(2)} ms`, `level ${fmtDb(ch.left.trim_db)}`)}
    ${tile("Right delay", `${ch.right.delay_ms.toFixed(2)} ms`, `level ${fmtDb(ch.right.trim_db)}`)}
    ${tile("Sub delay", `${ch.sub.delay_ms.toFixed(2)} ms`, `${fmtDb(ch.sub.trim_db)}${ch.sub.inverted ? " · polarity inverted" : ""}`)}
    ${tile("Summation", fmtDb(sum), "mains + sub at crossover", sum > -1 ? ' style="color:var(--ok)"' : sum > -3 ? ' style="color:var(--warn)"' : ' style="color:var(--bad)"')}
  </div>`;
}
const exampleBass = bassTiles(EXAMPLE_PLAN);

function fmtDate(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

// A modal yes/no question. Resolves true when confirmed.
function confirmDialog({ title, text, ok, danger = false }) {
  const dlg = $("#dlg");
  $("#dlg-q").textContent = title;
  $("#dlg-t").innerHTML = text;
  const okBtn = $("#dlg-ok");
  okBtn.textContent = ok;
  okBtn.className = danger ? "btn danger" : "btn primary";
  dlg.returnValue = "";
  dlg.showModal();
  return new Promise((resolve) => { dlg.onclose = () => resolve(dlg.returnValue === "ok"); });
}

// ---------- pages ----------

const dashboard = {
  title: "Dashboard",
  html: () => `
  <div class="head">
    <div><h1>Dashboard</h1><p>2.1 room correction · left, right and sub</p></div>
    <a class="btn primary" href="#/calibrate">
      <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v1a7 7 0 0 1-14 0v-1M12 18v4"/></svg>
      Start calibration</a>
  </div>
  <div id="d-guard"></div>
  <div class="grid">
    <section class="card" aria-labelledby="h-sig"><h2 id="h-sig">Signal path</h2>
      <div class="rows" id="d-sig"></div>
      <p class="note">Latency is fixed once filters are built, so the Apple TV's Wireless Audio Sync keeps holding.</p>
    </section>
    <section class="card" aria-labelledby="h-vol"><div class="card-head"><h2 id="h-vol">Volume</h2><span class="tag" id="v-tag">—</span></div>
      <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap"><span class="big" id="v-big">—</span><span class="muted" id="v-db">Powernode volume</span></div>
      <div class="rows" id="v-rows"></div>
      <div>
        <div style="display:flex;justify-content:space-between;align-items:baseline"><label for="v-sub" style="font-weight:500">Sub level</label><span class="mono" id="v-subval">—</span></div>
        <input type="range" id="v-sub" min="-10" max="10" step="0.5" value="0" aria-describedby="v-subnote" style="width:100%">
        <div class="muted small" id="v-subnote">On top of the calibrated level.</div>
      </div>
      <div class="switch-row"><div><div style="font-weight:500">Sub follows the Powernode</div><div class="muted small" id="v-follownote">Read from BluOS, applied to the sub only</div></div>
        <button type="button" class="switch" id="v-follow" aria-pressed="false" aria-label="Sub follows the Powernode's volume"></button></div>
      <form id="v-hostform" class="stack" hidden>
        <label class="field">Powernode address<input type="text" name="host" placeholder="e.g. 192.168.1.50" autocomplete="off" inputmode="url"></label>
        <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><button class="btn" type="submit">Connect</button><span class="msg" id="v-hostmsg" role="status"></span></div>
      </form>
      <span class="msg" id="v-msg" role="status"></span>
    </section>
    <section class="card" aria-labelledby="h-cal"><h2 id="h-cal">Calibration</h2><div id="d-cal" style="display:contents"></div></section>
  </div>
  <div class="grid wide">
    <section class="card" aria-labelledby="h-lv"><div class="card-head"><h2 id="h-lv">Levels</h2><span class="tiny muted" id="d-lvnote">dBFS</span></div>
      <div class="meters" id="d-meters">
        ${["In L", "In R", "Out L", "Out R", "Out Sub"].map((n, i) => `<span class="muted">${n}</span><div class="meter${i === 4 ? " sub" : ""}"><div id="m${i}"></div></div><span class="mono" style="text-align:right" id="v${i}">—</span>`).join("")}
      </div>
    </section>
    <section class="card" aria-labelledby="h-rsp" id="d-rsp"></section>
  </div>
  <section class="card" aria-labelledby="h-bm" id="d-bm"></section>
  <footer class="foot" id="d-foot"></footer>`,
  mount() {
    $("#d-cal").addEventListener("click", async (ev) => {
      const b = ev.target.closest("[data-ab]");
      if (!b || b.getAttribute("aria-pressed") === "true") return;
      b.parentElement.querySelectorAll("button").forEach((x) => (x.disabled = true));
      try { await api("/api/compare", { method: "POST", body: JSON.stringify({ room_correction: b.dataset.ab === "true" }) }); }
      catch (e) { alert(e.message); }
      await poll();
    });
    const slider = $("#v-sub"), msg = $("#v-msg");
    const send = async (body) => {
      try {
        const v = await api("/api/volume", { method: "PUT", body: JSON.stringify(body) });
        if (status) { status.volume = v; renderVolume(status); }
        msg.textContent = "";
      } catch (e) { msg.className = "msg bad"; msg.textContent = e.message; }
    };
    slider.addEventListener("input", () => { $("#v-subval").textContent = fmtDb(Number(slider.value)); });
    slider.addEventListener("change", () => send({ sub_level_db: Number(slider.value) }));
    $("#v-follow").addEventListener("click", (ev) => send({ follow_volume: ev.currentTarget.getAttribute("aria-pressed") !== "true" }));
    $("#v-hostform").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const m = $("#v-hostmsg");
      m.className = "msg"; m.textContent = "Connecting…";
      try {
        const v = await api("/api/volume", { method: "PUT", body: JSON.stringify({ bluos_host: ev.target.host.value }) });
        m.textContent = ""; volumeUi.editHost = false;
        if (status) { status.volume = v; renderVolume(status); }
      } catch (e) { m.className = "msg bad"; m.textContent = e.message; }
    });
    $("#v-rows").addEventListener("click", async (ev) => {
      const a = ev.target.closest("[data-vact]");
      if (!a) return;
      ev.preventDefault();
      if (a.dataset.vact === "host") { volumeUi.editHost = true; renderVolume(status); $("#v-hostform").host.focus(); }
      if (a.dataset.vact === "ref") {
        const ok = await confirmDialog({ title: "Use the current volume as the reference?",
          text: "The sub then plays at its calibrated level when the Powernode is at its current volume. Use this if the volume was different when you measured.", ok: "Use current volume" });
        if (!ok) return;
        try {
          const v = await api("/api/volume/reference", { method: "POST" });
          if (status) { status.volume = v; renderVolume(status); }
        } catch (e) { msg.className = "msg bad"; msg.textContent = e.message; }
      }
    });
  },
  update(s) {
    const c = s.camilladsp || {}, d = s.devices || {}, e = engine(s);
    renderVolume(s);
    set("d-guard", s.guard?.silenced ? `<section class="card" role="alert" style="border-color:#5A2E2E">
      <div style="display:flex;gap:12px;align-items:flex-start">${dot("bad")}<div>
        <div style="font-weight:600">Sound stopped: the TV sends ${esc(s.guard.format)}, not plain stereo (PCM)</div>
        <div class="muted small">Played as PCM, that data would come out of the speakers as loud noise. On the TV, set the digital audio output (optical / S/PDIF) to <b>PCM</b>. PiDSP starts the sound again by itself as soon as it receives PCM.</div></div></div></section>` : "");
    set("d-sig", [
      ["Input", `Optical from TV · ${e.input || "—"}`],
      ["Output", d.dac?.present ? "CM6206 · L, R, Sub" : `<span style="color:var(--bad)">CM6206 not found</span>`],
      ["Clock drift", c.clock_ppm != null ? `<span class="mono">${minus((c.clock_ppm > 0 ? "+" : "") + c.clock_ppm)} ppm, tracked</span>` : "—"],
      ["DSP load", c.load != null && c.state === "Running" ? `<span class="mono">${c.load.toFixed(1)} %</span>` : "—"],
      ["Latency", "Set when calibrated"],
    ].map(([k, v]) => `<div class="row"><span>${k}</span><span>${v}</span></div>`).join(""));
    const cal = s.calibration;
    set("d-cal", cal ? `
      <div><div style="font-size:18px;font-weight:600">${cal.room ? `Room correction · ${TARGET_NAMES[cal.room.target]} target` : `Bass setup · ${cal.plan.crossover_hz} Hz crossover`}</div>
        <div class="muted small">Applied ${esc(fmtDate(cal.applied))}${cal.room ? ` · ${cal.room.positions} mic position${cal.room.positions === 1 ? "" : "s"}` : ". Room correction comes next."}</div></div>
      ${cal.room ? `<div class="row small"><span>Predicted deviation</span><span class="mono">±${cal.room.sides.left.deviation_after_db.toFixed(1)} dB (was ±${cal.room.sides.left.deviation_before_db.toFixed(1)})</span></div>
      <div><div class="muted small" style="margin-bottom:6px">Room correction, to compare by ear (same level both ways)</div>
        <div class="seg" role="group" aria-label="Room correction">${[[true, "On"], [false, "Off"]].map(([on, t]) => `<button type="button" data-ab="${on}" aria-pressed="${on !== !!c.config_path?.endsWith("/compare.yml")}" ${s.measuring ? "disabled" : ""}>${t}</button>`).join("")}</div></div>` : ""}
      <div class="row small"><span>Summation at crossover</span><span class="mono">${fmtDb(cal.plan.summation_db)}</span></div>
      <a class="small" href="#/filters" style="font-weight:500">Open filters →</a>` : `
      <div class="empty"><div class="title">Not calibrated yet</div>
        <div class="muted small">Measure the room with the UMIK-1 to set up the crossover, delays and room correction.</div></div>
      <a class="small" href="#/calibrate" style="font-weight:500">Go to calibration →</a>`);
    const rc = cal?.room?.curves;
    set("d-rsp", `<div class="card-head"><h2 id="h-rsp">At the seat</h2>${rc ? `<span class="muted small">left + sub · ${TARGET_NAMES[cal.room.target]} target</span>` : EXAMPLE_TAG}</div>
      ${legend()}${rc ? chart({ label: "Measured and corrected response at the seat, left side", series: [
        { f: rc.freq, db: rc.target, color: "#8C939E", dash: true, width: 1.4 },
        { f: rc.freq, db: rc.left_measured, color: "#F2A93B", width: 1.6 },
        { f: rc.freq, db: rc.left_predicted, color: "#5AA9FF", width: 2 }] }) : exampleChart()}`);
    set("d-bm", `<div class="card-head"><h2 id="h-bm">Bass management</h2>${cal ? `<a class="small" href="#/filters">Details →</a>` : EXAMPLE_TAG}</div>${bassTiles(cal ? cal.plan : EXAMPLE_PLAN)}`);
    footer("d-foot", s);
    levels.wanted = c.state === "Running";
    if (!levels.wanted) {
      set("d-lvnote", c.reachable ? "No signal" : "dBFS");
      for (let i = 0; i < 5; i++) meter(i, null);
    }
  },
};

const volumeUi = { editHost: false };

function renderVolume(s) {
  const v = s?.volume;
  if (!v || !$("#v-big")) return;
  const p = v.player, g = v.sub || {}, host = v.bluos_host;
  const tag = $("#v-tag");
  tag.textContent = !host ? "Not set up" : v.error ? "Not reachable" : g.following ? "Following" : "Not following";
  tag.style.cssText = g.following && !v.error ? "background:#16301F;color:var(--ok)" : v.error ? "background:#2A1717;color:var(--bad)" : "";
  set("v-big", p ? (p.mute ? "Muted" : esc(p.volume)) : "—");
  set("v-db", p ? `${p.db != null ? minus(p.db.toFixed(1)) + " dB · " : ""}Powernode volume` : host ? "Powernode volume" : "Connect the Powernode to follow its volume");
  const rows = [];
  if (host) {
    rows.push(["Powernode", `<span class="mono">${esc(host)}</span> · <a href="#" data-vact="host">change</a>`]);
    if (v.error) rows.push(["Status", `<span style="color:var(--bad)">${esc(v.error)}</span>`]);
    if (p) rows.push(["Input", p.analog ? esc(p.input) : `<span style="color:var(--warn)">${esc(p.input || "another source")} · TV sound from PiDSP is not playing</span>`]);
    const ref = v.reference || {};
    rows.push(["Calibrated at", ref.db != null ? `volume ${esc(ref.volume ?? "?")} (${minus(ref.db.toFixed(1))} dB) · <a href="#" data-vact="ref">use current</a>`
      : `not recorded · <a href="#" data-vact="ref">use current volume</a>`]);
  }
  rows.push(["Sub now", g.mute ? `<span style="color:var(--warn)">muted: ${esc(g.mute_reason)}</span>`
    : `<span class="mono">${fmtDb(g.fader_db ?? 0)}</span>${g.following ? ` <span class="muted small">(level ${fmtDb(g.sub_level_db ?? 0)}, volume ${fmtDb(g.offset_db ?? 0)})</span>` : ""}${g.clamped ? ` <span style="color:var(--warn)" class="small">at its maximum</span>` : ""}`]);
  set("v-rows", rows.map(([k, val]) => `<div class="row"><span>${k}</span><span>${val}</span></div>`).join(""));
  const slider = $("#v-sub");
  if (document.activeElement !== slider) {
    slider.value = v.sub_level_db ?? 0;
    $("#v-subval").textContent = fmtDb(Number(slider.value));
  }
  const sw = $("#v-follow");
  sw.setAttribute("aria-pressed", String(!!v.follow_volume));
  sw.disabled = !host;
  set("v-follownote", host ? "The sub moves with the Powernode's volume, and mutes when it is muted or on another input." : "Connect the Powernode below first.");
  $("#v-hostform").hidden = !!host && !volumeUi.editHost;
}

function footer(id, s) {
  const sys = s.system || {};
  set(id, `<span>PiDSP ${esc(s.version)}</span>` +
    (sys.cpu_temp_c != null ? `<span>${sys.cpu_temp_c.toFixed(0)} °C</span>` : "") +
    `<span>${esc(sys.hostname)}.local</span>` + (sys.addresses?.[0] ? `<span class="mono">${esc(sys.addresses[0])}</span>` : "") +
    `<a href="#/system" style="margin-left:auto">System →</a>`);
}

function meter(i, db) {
  const bar = document.getElementById("m" + i), val = document.getElementById("v" + i);
  if (!bar) return;
  const pct = db == null || !isFinite(db) ? 0 : Math.max(0, Math.min(100, (db + 60) / 60 * 100));
  bar.style.width = pct + "%";
  bar.parentElement.classList.toggle("hot", db != null && db > -0.5);
  val.textContent = db == null || !isFinite(db) || db < -99 ? "—" : minus(db.toFixed(1));
}

const levels = {
  wanted: false, timer: null,
  start() {
    this.stop();
    this.timer = setInterval(async () => {
      if (!this.wanted || document.hidden) return;
      try {
        const { levels: l } = await api("/api/levels");
        if (!l) return;
        const vals = [l.capture_peak?.[0], l.capture_peak?.[1], l.playback_peak?.[0], l.playback_peak?.[1], l.playback_peak?.[2]];
        vals.forEach((v, i) => meter(i, v));
        set("d-lvnote", "dBFS peak");
      } catch (_) { /* next tick */ }
    }, LEVELS_MS);
  },
  stop() { clearInterval(this.timer); this.timer = null; },
};

const STEPS = ["Prepare", "Bass setup", "Room correction", "Build filters", "Check"];
const calState = { job: null, bass: null, mic: null, room: null, side: "left", roomSide: "left", target: "room", timer: null };
const TARGET_NAMES = { flat: "Neutral", warm: "Warm", room: "Room" };
const C_MAIN = "#F2A93B", C_SUB = "#B48CF2", C_SUM = "#5AA9FF";

const calibrate = {
  title: "Calibrate",
  html: () => `
  <div class="head"><div><h1>Calibrate</h1><p>About 15 minutes. The room should be quiet while sweeps play.</p></div></div>
  <ol class="steps" aria-label="Calibration steps" id="c-steps"></ol>
  <section class="card" id="c-job" aria-live="polite" hidden></section>
  <section class="card" id="c-result" aria-labelledby="h-res" hidden></section>
  <section class="card" id="c-room" aria-labelledby="h-room2" hidden></section>
  <section class="card" id="c-check" aria-labelledby="h-chk" hidden></section>
  <div class="grid wide">
    <section class="card" aria-labelledby="h-chk"><h2 id="h-chk">Before you start</h2>
      <ul class="checks" id="c-checks"></ul>
      <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center">
        <button class="btn primary" type="button" id="c-start" disabled>Start bass setup</button>
        <button class="btn" type="button" id="c-selftest" disabled title="Measures simulated speakers inside the Pi. Makes no sound.">Silent self-test</button>
      </div>
      <p class="muted small" style="margin:0" id="c-hint" role="status"></p>
    </section>
    <div style="display:flex;flex-direction:column;gap:20px;min-width:0">
      <section class="card" aria-labelledby="h-mic"><h2 id="h-mic">Microphone calibration</h2>
        <div id="c-mic"></div>
        <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center">
          <button class="btn" type="button" id="c-micbtn">Upload calibration file…</button>
          <input type="file" id="c-micfile" accept=".txt,.cal,.frd,text/plain" hidden>
          <button class="btn" type="button" id="c-micdel" hidden>Remove</button>
          <span class="msg" id="c-micmsg" role="status"></span>
        </div>
      </section>
      <section class="card" aria-labelledby="h-room"><h2 id="h-room">Microphone positions</h2>
        ${roomSvg()}
        <ol class="small muted" style="margin:0;padding-left:20px;display:flex;flex-direction:column;gap:4px">
          <li>Bass setup: the mic at your main seat, ear height, pointing at the ceiling.</li>
          <li>Room correction: 5 positions in a small area around the seat, about 30 cm apart.</li>
          <li>Keep still and quiet while each sweep plays.</li>
        </ol>
      </section>
    </div>
  </div>`,
  async mount() {
    $("#c-start").addEventListener("click", startBass);
    $("#c-selftest").addEventListener("click", () => runBass(true));
    $("#c-micbtn").addEventListener("click", () => $("#c-micfile").click());
    $("#c-micfile").addEventListener("change", uploadMic);
    $("#c-micdel").addEventListener("click", deleteMic);
    try {
      const [mic, bass, job] = await Promise.all([api("/api/mic"), api("/api/bass"), api("/api/measure/job")]);
      calState.mic = mic.calibration; calState.bass = bass.result; calState.bassOther = bass.other; calState.job = job;
      await loadRoomAndCheck();
      const room = calState.room;
      if (room.build) calState.target = room.build.target;
    } catch (_) { /* shown by the status pill */ }
    renderMic(); renderResult(); renderRoom(); renderCheck(); renderJob();
    if (status) calibrate.update(status);
    if (calState.job?.state === "running") pollJob();
  },
  unmount() { clearTimeout(calState.timer); },
  update(s) {
    const d = s.devices || {}, c = s.camilladsp || {}, e = engine(s);
    const mic = d.mic || {};
    const items = [
      [d.dac?.present ? "ok" : "bad", "Sound card", d.dac?.present ? "CM6206 connected" : "CM6206 not found. Check the USB cable."],
      [mic.card ? "ok" : "warn", "Measurement microphone", mic.card ? `${esc(mic.name)} connected${mic.gain_db != null ? ` · ${mic.gain_db} dB gain version` : ""}` : "Plug the UMIK-1 into the Pi."],
      [calState.mic ? "ok" : "warn", "Microphone calibration file", calState.mic ? `Serial ${esc(calState.mic.serial || "unknown")}` : "Recommended: upload it on the right."],
      [c.reachable ? "ok" : "bad", "DSP engine", c.reachable ? `CamillaDSP ${esc(c.version)} ready` : "CamillaDSP is not running."],
      [d.loopback?.present ? "ok" : "bad", "Measurement path", d.loopback?.present ? "ALSA loopback ready" : "ALSA loopback missing. Rerun the install script."],
      ["", "Speaker level", "Set the Powernode to your usual listening volume, then leave it there."],
      ["", "Subwoofer settings", "Turn the sub's own low-pass filter off, or to its highest setting (LFE), in its app. PiDSP sets the crossover; two would stack."],
      [e.input === "Locked" ? "ok" : "", "Optical input", e.input === "Locked" ? "Signal locked" : "Not needed for measuring"],
    ];
    set("c-checks", items.map(([k, what, how]) => `<li>${dot(k)}<div><div class="what">${what}</div><div class="muted small">${how}</div></div></li>`).join(""));
    const busy = calState.job?.state === "running" || s.measuring;
    const base = c.reachable && d.loopback?.present && !busy;
    const ready = base && d.dac?.present && mic.card;
    $("#c-start").disabled = !ready;
    $("#c-selftest").disabled = !base;
    $("#c-start").textContent = calState.bass && !calState.bass.selftest ? "Measure bass again" : "Start bass setup";
    set("c-hint", busy ? "A measurement is running." : !c.reachable ? "Waiting for CamillaDSP." : !d.dac?.present ? "Connect the sound card to start." : !mic.card ? "Connect the microphone to start." : "");
    const room = calState.room;
    const step = calState.check && room?.build && calState.check.built === room.build.built ? 4 : room?.build ? 3 : calState.bass ? 2 : busy ? 1 : 0;
    set("c-steps", STEPS.map((t, i) => `<li${i === step ? ' aria-current="step"' : ""}>${t}</li>`).join(""));
  },
};

async function startBass() {
  const ok = await confirmDialog({
    title: "Play test sweeps?",
    text: `<p>Sweeps will play through the left, right and sub speakers, about 40 seconds in all.</p>
      <ul style="padding-left:18px">
        <li>Set the Powernode to your normal listening volume, not louder.</li>
        <li>Turn the sub's own low-pass filter off (or to LFE) in its app.</li>
        <li>Put the microphone at your main seat, at ear height, pointing at the ceiling.</li>
        <li>Keep the room quiet and stay still.</li></ul>
      <p>First come two short, quiet test sounds: one on the main speakers, then one on the sub alone. PiDSP sets each speaker's sweep level from what the microphone hears, up to a safe maximum, and stops if a speaker is already loud at the quiet level.</p>
      <p>If anything sounds too loud, press Cancel.</p>
      ${calState.mic ? "" : "<p style='color:var(--warn)'>No microphone calibration file yet. The bass setup still works.</p>"}`,
    ok: "Start",
  });
  if (ok) runBass(false);
}

async function runBass(selftest) {
  try {
    calState.job = await api("/api/measure/bass", { method: "POST", body: JSON.stringify({ selftest }) });
    renderJob();
    if (status) calibrate.update(status);
    pollJob();
  } catch (e) {
    set("c-hint", `<span style="color:var(--bad)">${esc(e.message)}</span>`);
  }
}

// Room positions, build and check of whichever bass result is shown (real or self-test).
async function loadRoomAndCheck() {
  const sel = !!calState.bass?.selftest;
  const [room, check] = await Promise.all([api(`/api/room?selftest=${sel}`), api(`/api/check?selftest=${sel}`)]);
  calState.room = room; calState.check = check.result;
}

async function showBass(selftest) {
  const b = await api(`/api/bass?selftest=${selftest}`);
  calState.bass = b.result; calState.bassOther = b.other;
  await loadRoomAndCheck();
  if (calState.room?.build) calState.target = calState.room.build.target;
  renderResult(); renderRoom(); renderCheck();
  if (status) calibrate.update(status);
}

async function pollJob() {
  clearTimeout(calState.timer);
  try {
    calState.job = await api("/api/measure/job");
  } catch (_) { /* try again */ }
  renderJob();
  if (calState.job?.state === "running") {
    calState.timer = setTimeout(pollJob, 700);
    return;
  }
  if (calState.job?.state === "done") {
    const kind = calState.job.kind, roomJob = kind.startsWith("room"), checkJob = kind.startsWith("check");
    const sel = kind.endsWith("selftest");
    if (!roomJob && !checkJob) {
      const b = await api(`/api/bass?selftest=${sel}`);
      calState.bass = b.result; calState.bassOther = b.other;
    }
    await loadRoomAndCheck();
    renderResult(); renderRoom(); renderCheck();
    $(checkJob ? "#c-check" : roomJob ? "#c-room" : "#c-result")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  if (status) calibrate.update(status);
}

function renderJob() {
  const el = $("#c-job");
  if (!el) return;
  const j = calState.job;
  if (!j || j.state === "idle" || j.state === "done") { el.hidden = true; return; }
  el.hidden = false;
  const kind = j.kind.endsWith("selftest") ? `<span class="tag">Silent self-test</span>` : "";
  if (j.state === "running") {
    el.innerHTML = `<div class="card-head"><h2>Measuring</h2>${kind}</div>
      <div style="font-size:18px;font-weight:600">${esc(j.step)}…</div>
      <div class="meter" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(j.progress * 100)}"><div style="width:${j.progress * 100}%;background:var(--measured)"></div></div>
      <div><button class="btn" type="button" id="c-cancel">Cancel</button></div>`;
    $("#c-cancel").addEventListener("click", () => api("/api/measure/cancel", { method: "POST" }).then(pollJob));
  } else {
    const failed = j.state === "failed";
    el.innerHTML = `<div class="card-head"><h2>${failed ? "Measurement failed" : "Measurement cancelled"}</h2>${kind}</div>
      <p style="margin:0;display:flex;gap:10px">${dot(failed ? "bad" : "")}<span>${failed ? esc(j.error) : "Nothing was changed."}</span></p>`;
  }
}

function crossoverTable(plan, canChoose) {
  const opts = plan.crossover_options;
  if (!opts) return "";
  const num = (v) => (v == null ? "" : minus(Number(v).toFixed(2)));
  const manual = plan.crossover_choice === "listener";
  const rows = opts.map((o) => o.excluded
    ? `<tr class="off"><td class="mono">${o.crossover_hz} Hz</td><td colspan="5">${esc(o.excluded)}</td></tr>`
    : `<tr class="${o.chosen ? "chosen" : ""}${canChoose && !o.chosen ? " pick" : ""}"${canChoose && !o.chosen ? ` data-xo="${o.crossover_hz}" tabindex="0" role="button" aria-label="Use ${o.crossover_hz} Hz"` : ""}>
        <td class="mono">${o.crossover_hz} Hz${o.chosen ? " ✓" : ""}</td>
        <td class="mono"><b>${num(o.score)}</b></td><td class="mono">${num(o.bumpiness_db)}</td>
        <td class="mono">${num(o.summation_db)}</td><td class="mono">${num(o.strain_db)}</td><td class="mono">${num(o.localisation_db)}</td></tr>`).join("");
  return `<details class="xo" id="c-xo"${calState.xoOpen ? " open" : ""}>
    <summary>Why ${plan.crossover_hz} Hz? ${manual ? "Chosen by you" : "Every crossover compared"}${canChoose ? " · choose another" : ""}</summary>
    <div class="xo-wrap"><table>
      <thead><tr><th>Crossover</th><th>Score</th><th>Bumpiness</th><th>Summation</th><th>Mains strain</th><th>Localisation</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <p class="muted small" style="margin:8px 0 0">Lower score is better, in dB. Score = bumpiness of the predicted bass at your seat (25 to 250 Hz, both sides) + 2 × any summation loss at the crossover (room correction can't fill a cancellation) + mains strain (crossover below an octave above the main speakers' roll-off) + localisation (crossover above 80 Hz, where you start to hear where the sub stands). Close scores count as equal; then the crossover nearest an octave above the mains' roll-off wins.</p>
    ${canChoose ? `<p class="muted small" style="margin:6px 0 0">Click a row to use that crossover instead. It is worked out again from this measurement, with no new sweeps.${manual ? ` <a href="#" id="c-xo-auto">Back to the automatic choice</a>` : ""}</p>` : ""}
  </details>`;
}

async function chooseCrossover(fc) {
  try {
    const r = await api("/api/bass/crossover", { method: "POST", body: JSON.stringify({ crossover_hz: fc, selftest: !!calState.bass?.selftest }) });
    calState.bass = r.result;
    renderResult();
  } catch (e) { alert(e.message); }
}

function renderResult() {
  const el = $("#c-result");
  if (!el) return;
  const r = calState.bass;
  if (!r) { el.hidden = true; return; }
  el.hidden = false;
  const plan = r.plan, c = r.curves, side = calState.side;
  const applied = status?.calibration && JSON.stringify(status.calibration.plan) === JSON.stringify(plan);
  const verdict = r.verdict ? `<ul class="checks">${r.verdict.checks.map((k) => `<li>${dot(k.ok ? "ok" : "bad")}<div><div class="what">${esc(k.what)}: ${esc(k.got)}</div><div class="muted small">expected ${esc(k.expected)}</div></div></li>`).join("")}</ul>
    <p class="note">${r.verdict.passed ? "Passed. The measurement chain on this Pi works end to end." : "Failed. Something in the measurement chain is off; the details above show what."}</p>` : "";
  el.innerHTML = `
    <div class="card-head"><h2 id="h-res">${r.selftest ? "Self-test result" : "Bass setup result"}</h2>
      <span style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">${calState.bassOther ? `<a href="#" class="small" id="c-other">${r.selftest ? "Show your room's result" : "Show the self-test result"}</a>` : ""}
      <span class="tag${r.selftest ? "" : " example"}" style="${r.selftest ? "" : "background:#16273A;color:var(--after)"}">${r.selftest ? "Simulated speakers" : "Measured " + esc(fmtDate(r.measured))}</span></span></div>
    ${verdict}
    <div style="display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between;align-items:center">
      <div class="seg" role="group" aria-label="Side" style="min-width:260px">
        <button type="button" data-side="left" aria-pressed="${side === "left"}">Left + sub</button>
        <button type="button" data-side="right" aria-pressed="${side === "right"}">Right + sub</button></div>
      <div class="legend"><span><i style="background:${C_MAIN}"></i>${side === "left" ? "Left" : "Right"} alone</span><span><i style="background:${C_SUB}"></i>Sub alone</span><span><i style="background:${C_SUM}"></i>Together, with this setup</span></div>
    </div>
    ${chart({ fMax: 2000, yMin: -30, yMax: 15, height: 340, width: 1100, label: `Measured ${side} and sub responses and their predicted sum`, series: [
      { f: c.freq, db: c[side], color: C_MAIN, width: 1.6 },
      { f: c.sub_freq || c.freq, db: c.sub, color: C_SUB, width: 1.6 },
      { f: c.freq, db: c["sum_" + side], color: C_SUM, width: 2.2 },
    ] })}
    ${bassTiles(plan)}
    ${crossoverTable(plan, !(calState.job?.state === "running"))}
    ${(plan.warnings || []).map((w) => `<p class="note" style="color:var(--warn)">${esc(w)}</p>`).join("")}
    <div class="muted small">Sweep level ${minus(r.level.sweep_db)} dBFS${r.level.sub_sweep_db != null ? `, sub ${minus(r.level.sub_sweep_db)} dBFS` : ""}${r.selftest ? " · digital loopback, no noise" : ` · room noise ${minus(r.level.noise_db)} dBFS at the microphone · ${r.mic_calibrated ? "microphone calibration applied" : "no microphone calibration"}`}</div>
    ${r.selftest ? "" : `<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center">
      <button class="btn primary" type="button" id="c-apply" ${applied ? "disabled" : ""}>${applied ? "Applied" : "Apply bass setup"}</button>
      <span class="muted small">${applied ? "This is the active setup." : "Sound from the TV then goes through this crossover and these delays."}</span></div>`}`;
  el.querySelectorAll("[data-side]").forEach((b) => b.addEventListener("click", () => { calState.side = b.dataset.side; renderResult(); }));
  $("#c-xo")?.addEventListener("toggle", (e) => { calState.xoOpen = e.target.open; });
  $("#c-other")?.addEventListener("click", (e) => { e.preventDefault(); showBass(!r.selftest); });
  el.querySelectorAll("[data-xo]").forEach((row) => {
    const go = () => chooseCrossover(Number(row.dataset.xo));
    row.addEventListener("click", go);
    row.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
  });
  $("#c-xo-auto")?.addEventListener("click", (e) => { e.preventDefault(); chooseCrossover(null); });
  $("#c-apply")?.addEventListener("click", applyBass);
}

function renderRoom() {
  const el = $("#c-room");
  if (!el) return;
  const r = calState.bass, room = calState.room;
  if (!r || !room) { el.hidden = true; return; }
  el.hidden = false;
  const selftest = !!r.selftest;
  const current = new Set(room.positions.filter((p) => p.current).map((p) => p.index));
  const n = room.recommended;
  const next = Array.from({ length: n }, (_, i) => i + 1).find((i) => !current.has(i));
  const busy = calState.job?.state === "running";
  const chips = Array.from({ length: n }, (_, i) => i + 1).map((i) =>
    `<span class="tag" style="font-size:12px;padding:6px 10px;${current.has(i) ? "background:#16301F;color:var(--ok)" : ""}">${current.has(i) ? "✓ " : ""}Position ${i}</span>`).join("");
  const b = room.build && room.build.selftest === selftest ? room.build : null;
  const side = calState.roomSide;
  let built = "";
  if (b) {
    const c = b.curves, sd = b.sides[side];
    built = `
      <div style="display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between;align-items:center">
        <div class="seg" role="group" aria-label="Side" style="min-width:260px">
          <button type="button" data-rside="left" aria-pressed="${side === "left"}">Left + sub</button>
          <button type="button" data-rside="right" aria-pressed="${side === "right"}">Right + sub</button></div>
        ${legend("Predicted")}</div>
      ${chart({ yMin: -15, yMax: 15, height: 300, width: 1100, label: `Room correction for ${side}: measured, target and predicted`, series: [
        { f: c.freq, db: c.target, color: "#8C939E", dash: true, width: 1.4 },
        { f: c.freq, db: c[side + "_measured"], color: "#F2A93B", width: 1.6 },
        { f: c.freq, db: c[side + "_predicted"], color: "#5AA9FF", width: 2.2 },
      ] })}
      <div class="tiles">
        <div class="tile"><div class="k">Deviation before</div><div class="v">±${sd.deviation_before_db.toFixed(1)} dB</div><div class="k">40 Hz – 16 kHz, 90 % of the band</div></div>
        <div class="tile"><div class="k">Predicted after</div><div class="v" style="color:var(--ok)">±${sd.deviation_after_db.toFixed(1)} dB</div><div class="k">dips are only half filled, on purpose</div></div>
        ${b.bass_lift_db != null ? `<div class="tile"><div class="k">Sub level</div><div class="v">${fmtDb(b.bass_lift_db)}</div><div class="k">brings the sub's range to the target</div></div>` : ""}
        <div class="tile"><div class="k">Headroom</div><div class="v">${fmtDb(b.preamp_db)}</div><div class="k">preamp, so boosts can't clip</div></div>
        <div class="tile"><div class="k">Built from</div><div class="v">${b.positions}</div><div class="k">mic position${b.positions === 1 ? "" : "s"} · ${TARGET_NAMES[b.target]} target</div></div>
      </div>
      ${selftest ? "" : `<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center">
        <button class="btn primary" type="button" id="c-rapply">Apply room correction</button>
        <span class="muted small">Replaces the current setup with the bass setup plus this correction.</span></div>`}`;
  }
  el.innerHTML = `
    <div class="card-head"><h2 id="h-room2">Room correction</h2>${selftest ? `<span class="tag">Self-test · simulated speakers</span>` : ""}</div>
    <p class="muted small" style="margin:0">Measures left + sub and right + sub through the bass setup at up to ${n} microphone positions around your seat (see the diagram below). More positions give a correction that holds for a wider area. About 25 seconds each.</p>
    <div style="display:flex;flex-wrap:wrap;gap:8px">${chips}</div>
    <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center">
      <button class="btn${b ? "" : " primary"}" type="button" id="c-rmeasure" ${busy ? "disabled" : ""}>${next ? `Measure position ${next}` : "All positions measured"}</button>
      ${current.size ? `<button class="btn" type="button" id="c-rclear" ${busy ? "disabled" : ""}>Start over</button>` : ""}
    </div>
    ${current.size ? `<div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center">
      <div class="seg" role="group" aria-label="Target" style="min-width:300px">${room.targets.map((t) => `<button type="button" data-target="${t}" aria-pressed="${calState.target === t}">${TARGET_NAMES[t]}</button>`).join("")}</div>
      <button class="btn${b ? "" : " primary"}" type="button" id="c-rbuild" ${busy ? "disabled" : ""}>${b ? "Build again" : "Build correction"}</button>
      <span class="msg" id="c-rmsg" role="status"></span></div>` : ""}
    ${built}`;
  if (next === undefined) $("#c-rmeasure").disabled = true;
  $("#c-rmeasure")?.addEventListener("click", () => measureRoom(next, selftest));
  $("#c-rclear")?.addEventListener("click", async () => {
    if (!(await confirmDialog({ title: "Start over?", text: "Removes the room correction measurements. What is applied stays.", ok: "Start over", danger: true }))) return;
    await api(`/api/room?selftest=${!!calState.bass?.selftest}`, { method: "DELETE" });
    await loadRoomAndCheck(); renderRoom(); renderCheck();
  });
  el.querySelectorAll("[data-target]").forEach((x) => x.addEventListener("click", () => { calState.target = x.dataset.target; renderRoom(); }));
  el.querySelectorAll("[data-rside]").forEach((x) => x.addEventListener("click", () => { calState.roomSide = x.dataset.rside; renderRoom(); }));
  $("#c-rbuild")?.addEventListener("click", buildRoom);
  $("#c-rapply")?.addEventListener("click", applyRoom);
}

async function measureRoom(position, selftest) {
  if (!selftest) {
    const ok = await confirmDialog({
      title: `Measure position ${position}?`,
      text: `<p>Put the microphone at position ${position} (see the diagram), at ear height, pointing at the ceiling. Then keep quiet: sweeps play through left + sub, then right + sub, about 25 seconds.</p>`,
      ok: "Measure",
    });
    if (!ok) return;
  }
  try {
    calState.job = await api("/api/measure/room", { method: "POST", body: JSON.stringify({ position, selftest }) });
    renderJob(); renderRoom();
    pollJob();
  } catch (e) {
    set("c-hint", `<span style="color:var(--bad)">${esc(e.message)}</span>`);
  }
}

async function buildRoom() {
  const msg = $("#c-rmsg");
  msg.className = "msg"; msg.textContent = "Building…";
  try {
    const r = await api("/api/room/build", { method: "POST", body: JSON.stringify({ target: calState.target, selftest: !!calState.bass?.selftest }) });
    calState.room.build = r.build;
    renderRoom();
    if (status) calibrate.update(status);
  } catch (e) { msg.className = "msg bad"; msg.textContent = e.message; }
}

function renderCheck() {
  const el = $("#c-check");
  if (!el) return;
  const r = calState.bass, room = calState.room, b = room?.build;
  const selftest = !!r?.selftest;
  const applied = status?.calibration?.kind === "room";
  if (!r || !b || b.selftest !== selftest || (!selftest && !applied)) { el.hidden = true; return; }
  el.hidden = false;
  const c = calState.check && calState.check.built === b.built && !!calState.check.selftest === selftest ? calState.check : null;
  const busy = calState.job?.state === "running";
  const side = calState.checkSide || "left";
  let res = "";
  if (c) {
    const cv = c.curves, sd = c.sides[side];
    const series = [{ f: cv.freq, db: cv.target, color: "#8C939E", dash: true, width: 1.4 }];
    if (cv[side + "_before"]) series.push({ f: cv.freq, db: cv[side + "_before"], color: "#F2A93B", width: 1.6 });
    series.push({ f: cv.freq, db: cv[side + "_after"], color: "#5AA9FF", width: 2.2 });
    res = `
      <div style="display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between;align-items:center">
        <div class="seg" role="group" aria-label="Side" style="min-width:260px">
          <button type="button" data-cside="left" aria-pressed="${side === "left"}">Left + sub</button>
          <button type="button" data-cside="right" aria-pressed="${side === "right"}">Right + sub</button></div>
        <div class="legend">${cv[side + "_before"] ? `<span><i style="background:#F2A93B"></i>Before (position 1)</span>` : ""}<span><i style="background:#5AA9FF"></i>Measured now, corrected</span><span><i class="dash"></i>Target</span></div></div>
      ${chart({ yMin: -15, yMax: 15, height: 300, width: 1100, label: `Check at the main seat, ${side}: before, after and target`, series })}
      <div class="tiles">
        ${sd.deviation_before_db != null ? `<div class="tile"><div class="k">Before, at this seat</div><div class="v">±${sd.deviation_before_db.toFixed(1)} dB</div><div class="k">position 1, without correction</div></div>` : ""}
        <div class="tile"><div class="k">Now, corrected</div><div class="v" style="color:var(--ok)">±${sd.deviation_after_db.toFixed(1)} dB</div><div class="k">40 Hz – 16 kHz, 90 % of the band</div></div>
        <div class="tile"><div class="k">Predicted (all positions)</div><div class="v">±${b.sides[side].deviation_after_db.toFixed(1)} dB</div><div class="k">from the build</div></div>
      </div>
      ${(c.warnings || []).map((w) => `<p class="note" style="color:var(--warn)">${esc(w)}</p>`).join("")}
      <div class="muted small">Measured ${esc(fmtDate(c.measured))}</div>`;
  }
  el.innerHTML = `
    <div class="card-head"><h2 id="h-chk">Check</h2>${selftest ? `<span class="tag">Self-test · simulated speakers</span>` : ""}</div>
    <p class="muted small" style="margin:0">${selftest ? "Measures the simulated speakers through the correction." : "Put the microphone back at the main seat (position 1). This measures through the room correction and compares with the measurement there before. About 25 seconds."}</p>
    <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center">
      <button class="btn${c ? "" : " primary"}" type="button" id="c-cmeasure" ${busy ? "disabled" : ""}>${c ? "Check again" : "Check the result"}</button></div>
    ${res}`;
  $("#c-cmeasure").addEventListener("click", () => measureCheck(selftest));
  el.querySelectorAll("[data-cside]").forEach((x) => x.addEventListener("click", () => { calState.checkSide = x.dataset.cside; renderCheck(); }));
}

async function measureCheck(selftest) {
  if (!selftest) {
    const ok = await confirmDialog({
      title: "Check the result?",
      text: "<p>Put the microphone at the main seat (position 1), at ear height, pointing at the ceiling, and keep quiet. Sweeps play through left + sub, then right + sub, with the room correction on.</p>",
      ok: "Measure",
    });
    if (!ok) return;
  }
  try {
    calState.job = await api("/api/measure/check", { method: "POST", body: JSON.stringify({ selftest }) });
    renderJob(); renderCheck();
    pollJob();
  } catch (e) {
    set("c-hint", `<span style="color:var(--bad)">${esc(e.message)}</span>`);
  }
}

async function applyRoom() {
  const ok = await confirmDialog({
    title: "Apply room correction?",
    text: "<p>Sound from the TV then goes through the bass setup and this correction. When you have finished, run Wireless Audio Sync again on the Apple TV.</p>",
    ok: "Apply",
  });
  if (!ok) return;
  try {
    await api("/api/room/apply", { method: "POST" });
    await poll();
    renderRoom(); renderResult(); renderCheck();
    $("#c-rmsg") && ($("#c-rmsg").textContent = "Applied.");
  } catch (e) {
    const m = $("#c-rmsg"); if (m) { m.className = "msg bad"; m.textContent = e.message; }
  }
}

async function applyBass() {
  const ok = await confirmDialog({
    title: "Apply this bass setup?",
    text: "<p>It replaces the current sound setup straight away. When you have finished calibrating, run Wireless Audio Sync again on the Apple TV.</p>",
    ok: "Apply",
  });
  if (!ok) return;
  try {
    await api("/api/bass/apply", { method: "POST" });
    await poll();
    renderResult();
  } catch (e) {
    set("c-hint", `<span style="color:var(--bad)">${esc(e.message)}</span>`);
  }
}

function renderMic() {
  const m = calState.mic;
  set("c-mic", m ? `<div class="rows">
      <div class="row"><span>File</span><span class="mono small">${esc(m.filename)}</span></div>
      <div class="row"><span>Serial</span><span class="mono">${esc(m.serial || "—")}</span></div>
      <div class="row"><span>Sensitivity</span><span class="mono">${m.sensitivity_db != null ? fmtDb(m.sensitivity_db) : "—"}</span></div>
      <div class="row"><span>Range</span><span class="mono">${Math.round(m.range_hz[0])} Hz – ${(m.range_hz[1] / 1000).toFixed(0)} kHz</span></div></div>`
    : `<p class="muted small" style="margin:0">miniDSP has a file for every UMIK-1: look up the serial number on the mic's label on miniDSP's website. Pointing the mic at the ceiling, use the <b>90°</b> file.</p>`);
  const del = $("#c-micdel");
  if (del) del.hidden = !m;
  const btn = $("#c-micbtn");
  if (btn) btn.textContent = m ? "Replace file…" : "Upload calibration file…";
}

async function uploadMic(ev) {
  const file = ev.target.files[0], msg = $("#c-micmsg");
  ev.target.value = "";
  if (!file) return;
  msg.className = "msg"; msg.textContent = "Reading…";
  try {
    if (file.size > 1_000_000) throw new Error("That file is too large to be a calibration file.");
    const r = await api("/api/mic", { method: "PUT", body: JSON.stringify({ filename: file.name, text: await file.text() }) });
    calState.mic = r.calibration;
    msg.className = "msg ok"; msg.textContent = "Uploaded.";
  } catch (e) { msg.className = "msg bad"; msg.textContent = e.message; }
  renderMic();
  if (status) calibrate.update(status);
}

async function deleteMic() {
  await api("/api/mic", { method: "DELETE" });
  calState.mic = null;
  $("#c-micmsg").textContent = "";
  renderMic();
  if (status) calibrate.update(status);
}

function roomSvg() {
  const spk = (x, y, t) => `<rect x="${x - 14}" y="${y - 10}" width="28" height="20" rx="4" fill="#232932" stroke="#3A424D"/><text x="${x}" y="${y + 4}" text-anchor="middle" fill="#E8EAED" font-size="11" font-weight="600">${t}</text>`;
  const pos = [[200, 200, 1], [170, 186, 2], [230, 186, 3], [170, 214, 4], [230, 214, 5]];
  return `<svg viewBox="0 0 400 270" role="img" aria-label="Room seen from above: TV and speakers at the front, sofa with five microphone positions" style="width:100%;height:auto">
    <rect x="10" y="10" width="380" height="250" rx="10" fill="none" stroke="#3A424D" stroke-dasharray="6 5"/>
    <rect x="150" y="22" width="100" height="10" rx="3" fill="#3A424D"/><text x="200" y="48" text-anchor="middle" fill="#8C939E" font-size="11">TV</text>
    ${spk(95, 40, "L")}${spk(305, 40, "R")}${spk(345, 82, "Sub")}
    <rect x="130" y="228" width="140" height="22" rx="6" fill="#1C2128" stroke="#3A424D"/><text x="200" y="243" text-anchor="middle" fill="#8C939E" font-size="11">Sofa</text>
    <circle cx="200" cy="200" r="44" fill="none" stroke="#2A2416"/>
    ${pos.map(([x, y, n]) => `<circle cx="${x}" cy="${y}" r="9" fill="${n === 1 ? "#F2A93B" : "#2A2416"}" stroke="#F2A93B"/><text x="${x}" y="${y + 4}" text-anchor="middle" font-size="10" font-weight="700" fill="${n === 1 ? "#1A1206" : "#F2A93B"}">${n}</text>`).join("")}
  </svg>`;
}

let filtersSide = "left";
const filters = {
  title: "Filters",
  html: () => `
  <div class="head"><div><h1>Filters</h1><p>Room correction, crossover and delays built from your measurements.</p></div></div>
  <section class="card" id="f-empty"><div class="empty"><div class="title">No room correction yet</div>
    <div class="muted small">Once you have calibrated, the measured response and the correction appear here. Below is a preview with example data.</div>
    <a class="btn" href="#/calibrate">Go to calibration</a></div></section>
  <section class="card" aria-labelledby="h-fr" id="f-resp"></section>
  <section class="card" aria-labelledby="h-fb"><div id="f-bm" style="display:contents"></div>
    <p class="note">Applying new filters can change PiDSP's latency. Afterwards, run Wireless Audio Sync again on the Apple TV (Settings → Video and Audio → Wireless Audio Sync).</p>
  </section>`,
  update(s) {
    const cal = s.calibration, rc = cal?.room;
    $("#f-empty").hidden = !!rc;
    const side = filtersSide;
    set("f-resp", `<div class="card-head"><h2 id="h-fr">Response at the seat</h2>${rc ? `<span class="muted small">${TARGET_NAMES[rc.target]} target · ${rc.positions} position${rc.positions === 1 ? "" : "s"} · preamp ${fmtDb(rc.preamp_db)}</span>` : EXAMPLE_TAG}</div>
      <div style="display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between;align-items:center">
        <div class="seg" role="group" aria-label="Side" style="min-width:260px">
          <button type="button" data-fside="left" aria-pressed="${side === "left"}" ${rc ? "" : "disabled"}>Left + sub</button>
          <button type="button" data-fside="right" aria-pressed="${side === "right"}" ${rc ? "" : "disabled"}>Right + sub</button></div>
        ${legend("Corrected")}</div>
      ${rc ? chart({ height: 300, width: 1100, label: `Measured and corrected response, ${side}`, series: [
        { f: rc.curves.freq, db: rc.curves.target, color: "#8C939E", dash: true, width: 1.4 },
        { f: rc.curves.freq, db: rc.curves[side + "_measured"], color: "#F2A93B", width: 1.6 },
        { f: rc.curves.freq, db: rc.curves[side + "_predicted"], color: "#5AA9FF", width: 2.2 }] }) : exampleChart()}
      ${rc ? `<div class="tiles">
        <div class="tile"><div class="k">Deviation before</div><div class="v">±${rc.sides[side].deviation_before_db.toFixed(1)} dB</div><div class="k">40 Hz – 16 kHz</div></div>
        <div class="tile"><div class="k">Corrected (predicted)</div><div class="v" style="color:var(--ok)">±${rc.sides[side].deviation_after_db.toFixed(1)} dB</div><div class="k">minimum-phase FIR, 8192 taps</div></div></div>` : ""}`);
    document.querySelectorAll("[data-fside]").forEach((b) => b.addEventListener("click", () => { filtersSide = b.dataset.fside; filters.update(status); }));
    set("f-bm", `<div class="card-head"><h2 id="h-fb">Bass management</h2>${cal ? `<span class="muted small">applied ${esc(fmtDate(cal.applied))}</span>` : EXAMPLE_TAG}</div>${bassTiles(cal ? cal.plan : EXAMPLE_PLAN)}`);
  },
};

const systemPage = {
  title: "System",
  html: () => `
  <div class="head"><div><h1>System</h1><p>Software, devices, network and maintenance.</p></div></div>
  <div class="grid">
    <section class="card" aria-labelledby="h-sw"><h2 id="h-sw">Software</h2><div class="rows" id="s-sw"></div>
      <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center"><button class="btn" type="button" disabled>Check for updates</button><span class="muted small">Updates from GitHub Releases are coming.</span></div>
    </section>
    <section class="card" aria-labelledby="h-dev"><h2 id="h-dev">Audio devices</h2><ul class="checks" id="s-dev"></ul></section>
    <section class="card" aria-labelledby="h-net"><h2 id="h-net">Network</h2><div class="rows" id="s-net"></div></section>
    <section class="card" aria-labelledby="h-hw"><h2 id="h-hw">Hardware</h2><div class="rows" id="s-hw"></div></section>
    <section class="card" aria-labelledby="h-pw"><h2 id="h-pw">Login password</h2>
      <p class="muted small" style="margin:0">Changes the password of the <span class="mono">pidsp</span> user on the Pi (used for SSH and sudo).</p>
      <form class="stack" id="pw-form" autocomplete="off">
        <label class="field">New password<input type="password" name="p1" minlength="8" maxlength="128" required autocomplete="new-password"></label>
        <label class="field">Repeat new password<input type="password" name="p2" minlength="8" maxlength="128" required autocomplete="new-password"></label>
        <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap"><button class="btn" type="submit">Change password</button><span class="msg" id="pw-msg" role="status"></span></div>
      </form>
    </section>
    <section class="card" aria-labelledby="h-power"><h2 id="h-power">Power</h2>
      <p class="muted small" style="margin:0">Shutting down stops the sound until the Pi is powered on again.</p>
      <div style="display:flex;gap:12px;flex-wrap:wrap"><button class="btn" type="button" data-power="reboot">Restart</button><button class="btn danger" type="button" data-power="poweroff">Shut down</button></div>
      <span class="msg" id="power-msg" role="status"></span>
    </section>
  </div>`,
  mount() {
    $("#pw-form").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const f = ev.target, msg = $("#pw-msg");
      if (f.p1.value !== f.p2.value) { msg.className = "msg bad"; msg.textContent = "The passwords do not match."; return; }
      msg.className = "msg"; msg.textContent = "Saving…";
      try {
        await api("/api/system/password", { method: "POST", body: JSON.stringify({ password: f.p1.value }) });
        f.reset(); msg.className = "msg ok"; msg.textContent = "Password changed.";
      } catch (e) { msg.className = "msg bad"; msg.textContent = e.message; }
    });
    document.querySelectorAll("[data-power]").forEach((b) => b.addEventListener("click", async () => {
      const act = b.dataset.power, reboot = act === "reboot";
      const ok = await confirmDialog({
        title: reboot ? "Restart PiDSP?" : "Shut down PiDSP?",
        text: reboot ? "Sound stops for about a minute while the Pi restarts. This page reconnects by itself." : "You will need to unplug and reconnect power to start it again.",
        ok: reboot ? "Restart" : "Shut down", danger: !reboot,
      });
      if (!ok) return;
      const msg = $("#power-msg");
      try {
        await api(`/api/system/${act}`, { method: "POST" });
        msg.className = "msg ok"; msg.textContent = reboot ? "Restarting…" : "Shutting down…";
      } catch (e) { msg.className = "msg bad"; msg.textContent = e.message; }
    }));
  },
  update(s) {
    const sys = s.system || {}, c = s.camilladsp || {}, d = s.devices || {};
    const rows = (id, list) => set(id, list.map(([k, v]) => `<div class="row"><span>${k}</span><span>${v}</span></div>`).join(""));
    rows("s-sw", [
      ["PiDSP", `<span class="mono">${esc(s.version)}</span>`],
      ["CamillaDSP", c.reachable ? `<span class="mono">${esc(c.version)}</span> · ${esc(c.state)}` : `<span style="color:var(--bad)">not reachable</span>`],
      ["Active config", c.config_path ? `<span class="mono small">${esc(c.config_path)}</span>` : "none yet"],
    ]);
    const mic = d.mic || {};
    const known = {
      ICUSBAUDIO7D: ["CM6206 USB sound card", "Output and optical input"],
      Loopback: ["ALSA loopback", "Measurement path"],
    };
    if (mic.card) known[mic.card] = [esc(mic.name), `Measurement microphone${mic.gain_db != null ? ` · ${mic.gain_db} dB gain version` : ""}`];
    const cards = (d.cards || []).filter((x) => !x.id.startsWith("vc4hdmi"));
    const devs = cards.map((x) => [known[x.id] ? "ok" : "", known[x.id]?.[0] || esc(x.name), `${known[x.id]?.[1] || "Sound card"} · <span class="mono">${esc(x.id)}</span>`]);
    if (!d.dac?.present) devs.unshift(["bad", "CM6206 USB sound card", "Not connected"]);
    if (!mic.present) devs.push(["warn", "Measurement microphone", "Not connected"]);
    set("s-dev", devs.map(([k, a, b]) => `<li>${dot(k)}<div><div class="what">${a}</div><div class="muted small">${b}</div></div></li>`).join(""));
    rows("s-net", [
      ["Hostname", `<span class="mono">${esc(sys.hostname)}.local</span>`],
      ["Address", (sys.addresses || []).filter((a) => !a.includes(":")).map((a) => `<span class="mono">${esc(a)}</span>`).join(", ") || "—"],
      ["Web interface", `<span class="mono">http://${esc(sys.hostname)}.local</span>`],
    ]);
    rows("s-hw", [
      ["Board", esc(sys.model) || "—"],
      ["System", esc(sys.os) || "—"],
      ["Kernel", `<span class="mono">${esc(sys.kernel)}</span>`],
      ["CPU temperature", sys.cpu_temp_c != null ? `<span class="mono">${sys.cpu_temp_c.toFixed(1)} °C</span>` : "—"],
      ["Uptime", fmtDuration(sys.uptime_s)],
      ["Memory free", `${fmtBytes(sys.memory?.available)} of ${fmtBytes(sys.memory?.total)}`],
      ["Storage free", `${fmtBytes(sys.disk?.free)} of ${fmtBytes(sys.disk?.total)}`],
    ]);
  },
};

// ---------- shell ----------

const ROUTES = { "": dashboard, calibrate, filters, system: systemPage };
let page = null;

function route() {
  const key = location.hash.replace(/^#\/?/, "").split("/")[0];
  page?.unmount?.();
  page = ROUTES[key] || dashboard;
  document.querySelectorAll(".nav a").forEach((a) => (a.dataset.route === (ROUTES[key] ? key : "") ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  document.title = page === dashboard ? "PiDSP" : `${page.title} · PiDSP`;
  $("#view").innerHTML = page.html();
  page.mount?.();
  levels.wanted = false;
  if (page === dashboard) levels.start(); else levels.stop();
  if (status) page.update(status);
  window.scrollTo(0, 0);
}

function pill() {
  const e = statusError ? { kind: "bad", text: "PiDSP not reachable" } : engine(status);
  set("pill", `${dot(e.kind)}<span>${esc(e.text)}</span>`);
}

async function poll() {
  try {
    status = await api("/api/status");
    statusError = null;
    page?.update(status);
  } catch (e) {
    statusError = e;
  }
  pill();
}

window.addEventListener("hashchange", route);
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
route();
poll();
setInterval(() => { if (!document.hidden) poll(); }, POLL_MS);
