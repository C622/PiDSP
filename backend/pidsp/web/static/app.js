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
  switch (c.state) {
    case "Running": return { kind: "ok", text: `CamillaDSP running${rate} · optical locked`, input: "Locked" };
    case "Stalled": return { kind: "warn", text: "CamillaDSP waiting for an optical signal", input: "No signal" };
    case "Paused": return { kind: "warn", text: "CamillaDSP paused (no signal)", input: "No signal" };
    case "Starting": return { kind: "warn", text: "CamillaDSP starting…", input: "—" };
    default: return { kind: "", text: "CamillaDSP idle · not calibrated yet", input: "Not in use yet" };
  }
}

// ---------- chart ----------

const F_MIN = 20, F_MAX = 20000;
function logspace(n) { return Array.from({ length: n }, (_, i) => F_MIN * Math.pow(F_MAX / F_MIN, i / (n - 1))); }

function chart({ series, yMin = -15, yMax = 15, height = 220, label }) {
  const W = 640, H = height, L = 34, B = 20, T = 6;
  const x = (f) => L + ((Math.log10(f) - Math.log10(F_MIN)) / (Math.log10(F_MAX) - Math.log10(F_MIN))) * (W - L);
  const y = (db) => T + ((yMax - Math.max(yMin, Math.min(yMax, db))) / (yMax - yMin)) * (H - T - B);
  let g = "";
  for (const f of [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000])
    g += `<line class="grid-line" x1="${x(f)}" x2="${x(f)}" y1="${T}" y2="${H - B}"/>`;
  for (let db = Math.ceil(yMin / 5) * 5; db <= yMax; db += 5) {
    g += `<line class="grid-line" x1="${L}" x2="${W}" y1="${y(db)}" y2="${y(db)}"/>`;
    if (db % 10 === 0) g += `<text class="axis" x="${L - 6}" y="${y(db) + 4}" text-anchor="end">${minus(db > 0 ? "+" + db : db)}</text>`;
  }
  for (const [f, t] of [[20, "20"], [100, "100"], [1000, "1k"], [10000, "10k"], [20000, "20k Hz"]])
    g += `<text class="axis" x="${x(f)}" y="${H - 4}" text-anchor="${f === 20 ? "start" : f === 20000 ? "end" : "middle"}">${t}</text>`;
  const paths = series.map((s) => {
    const d = s.f.map((f, i) => `${i ? "L" : "M"}${x(f).toFixed(1)},${y(s.db[i]).toFixed(1)}`).join("");
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

const exampleBass = `<div class="tiles">
  <div class="tile"><div class="k">Crossover</div><div class="v">80 Hz</div><div class="k">LR4, 24 dB/oct</div></div>
  <div class="tile"><div class="k">Left delay</div><div class="v">11.55 ms</div><div class="k">level 0.0 dB</div></div>
  <div class="tile"><div class="k">Right delay</div><div class="v">10.95 ms</div><div class="k">level −0.8 dB</div></div>
  <div class="tile"><div class="k">Sub</div><div class="v">0.00 ms</div><div class="k">+1.5 dB · polarity inverted</div></div>
  <div class="tile"><div class="k">Summation</div><div class="v" style="color:var(--ok)">−0.3 dB</div><div class="k">mains + sub at crossover</div></div>
</div>`;

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
  <div class="grid">
    <section class="card" aria-labelledby="h-sig"><h2 id="h-sig">Signal path</h2>
      <div class="rows" id="d-sig"></div>
      <p class="note">Latency is fixed once filters are built, so the Apple TV's Wireless Audio Sync keeps holding.</p>
    </section>
    <section class="card" aria-labelledby="h-vol"><div class="card-head"><h2 id="h-vol">Volume</h2><span class="tag">Coming soon</span></div>
      <div style="display:flex;align-items:baseline;gap:10px"><span class="big">—</span><span class="muted">dB on the Powernode</span></div>
      <div class="meter" aria-hidden="true"><div style="width:0;background:var(--after)"></div></div>
      <div class="switch-row"><div><div style="font-weight:500">Sub follows volume</div><div class="muted small">Read from BluOS, applied to the sub's fader</div></div>
        <button type="button" class="switch" aria-pressed="false" aria-label="Sub follows volume" disabled></button></div>
      <div class="switch-row"><div><div style="font-weight:500">Loudness</div><div class="muted small">Fuller bass and treble at low volume</div></div>
        <button type="button" class="switch" aria-pressed="false" aria-label="Loudness" disabled></button></div>
    </section>
    <section class="card" aria-labelledby="h-cal"><h2 id="h-cal">Calibration</h2>
      <div class="empty"><div class="title">Not calibrated yet</div>
        <div class="muted small">Measure the room with the UMIK-1 to set up the crossover, delays and room correction.</div></div>
      <div class="seg" role="group" aria-label="Compare">
        <button type="button" aria-pressed="false" disabled>Corrected</button>
        <button type="button" aria-pressed="true" disabled>Bypass</button></div>
      <a class="small" href="#/calibrate" style="font-weight:500">Go to calibration →</a>
    </section>
  </div>
  <div class="grid wide">
    <section class="card" aria-labelledby="h-lv"><div class="card-head"><h2 id="h-lv">Levels</h2><span class="tiny muted" id="d-lvnote">dBFS</span></div>
      <div class="meters" id="d-meters">
        ${["In L", "In R", "Out L", "Out R", "Out Sub"].map((n, i) => `<span class="muted">${n}</span><div class="meter${i === 4 ? " sub" : ""}"><div id="m${i}"></div></div><span class="mono" style="text-align:right" id="v${i}">—</span>`).join("")}
      </div>
    </section>
    <section class="card" aria-labelledby="h-rsp"><div class="card-head"><h2 id="h-rsp">At the seat</h2>${EXAMPLE_TAG}</div>
      ${legend()}${exampleChart()}
    </section>
  </div>
  <section class="card" aria-labelledby="h-bm"><div class="card-head"><h2 id="h-bm">Bass management</h2>${EXAMPLE_TAG}</div>${exampleBass}</section>
  <footer class="foot" id="d-foot"></footer>`,
  update(s) {
    const c = s.camilladsp || {}, d = s.devices || {}, e = engine(s);
    set("d-sig", [
      ["Input", `Optical from TV · ${e.input || "—"}`],
      ["Output", d.dac?.present ? "CM6206 · L, R, Sub" : `<span style="color:var(--bad)">CM6206 not found</span>`],
      ["Clock drift", c.clock_ppm != null ? `<span class="mono">${minus((c.clock_ppm > 0 ? "+" : "") + c.clock_ppm)} ppm, tracked</span>` : "—"],
      ["DSP load", c.load != null && c.state === "Running" ? `<span class="mono">${c.load.toFixed(1)} %</span>` : "—"],
      ["Latency", "Set when calibrated"],
    ].map(([k, v]) => `<div class="row"><span>${k}</span><span>${v}</span></div>`).join(""));
    footer("d-foot", s);
    levels.wanted = c.state === "Running";
    if (!levels.wanted) {
      set("d-lvnote", c.reachable ? "No signal" : "dBFS");
      for (let i = 0; i < 5; i++) meter(i, null);
    }
  },
};

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

const calibrate = {
  title: "Calibrate",
  html: () => `
  <div class="head"><div><h1>Calibrate</h1><p>About 15 minutes. The room should be quiet while sweeps play.</p></div></div>
  <ol class="steps" aria-label="Calibration steps">${STEPS.map((t, i) => `<li${i === 0 ? ' aria-current="step"' : ""}>${t}</li>`).join("")}</ol>
  <div class="grid wide">
    <section class="card" aria-labelledby="h-chk"><h2 id="h-chk">Before you start</h2>
      <ul class="checks" id="c-checks"></ul>
      <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center">
        <button class="btn primary" type="button" disabled>Start bass setup</button>
        <span class="muted small">Measuring arrives in the next version.</span>
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
  </div>`,
  update(s) {
    const d = s.devices || {}, c = s.camilladsp || {}, e = engine(s);
    const mic = d.mic || {};
    const items = [
      [d.dac?.present ? "ok" : "bad", "Sound card", d.dac?.present ? "CM6206 connected" : "CM6206 not found. Check the USB cable."],
      [mic.present ? "ok" : "warn", "Measurement microphone", mic.present ? `${esc(mic.name)} connected${mic.gain_db != null ? ` · ${mic.gain_db} dB gain version` : ""}` : "Plug the UMIK-1 into the Pi."],
      ["", "Microphone calibration file", `Upload the file for your mic's serial number (on its label)${mic.gain_db === 18 ? ", 18 dB gain version" : ""}. Coming in the next version.`],
      [c.reachable ? "ok" : "bad", "DSP engine", c.reachable ? `CamillaDSP ${esc(c.version)} ready` : "CamillaDSP is not running."],
      [d.loopback?.present ? "ok" : "bad", "Measurement path", d.loopback?.present ? "ALSA loopback ready" : "ALSA loopback missing. Rerun the install script."],
      ["", "Speaker level", "Set the Powernode to your usual listening volume, then leave it there."],
      [e.input === "Locked" ? "ok" : "", "Optical input", e.input === "Locked" ? "Signal locked" : "Not needed for measuring"],
    ];
    set("c-checks", items.map(([k, what, how]) => `<li>${dot(k)}<div><div class="what">${what}</div><div class="muted small">${how}</div></div></li>`).join(""));
  },
};

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

const filters = {
  title: "Filters",
  html: () => `
  <div class="head"><div><h1>Filters</h1><p>Room correction, crossover and delays built from your measurements.</p></div></div>
  <section class="card"><div class="empty"><div class="title">No filters yet</div>
    <div class="muted small">Once you have calibrated, the measured response and the planned correction appear here. Then pick a target curve and apply. Below is a preview with example data.</div>
    <a class="btn" href="#/calibrate">Go to calibration</a></div></section>
  <section class="card" aria-labelledby="h-fr"><div class="card-head"><h2 id="h-fr">Response at the seat</h2>${EXAMPLE_TAG}</div>
    <div class="seg" role="group" aria-label="Channel" style="max-width:360px"><button type="button" aria-pressed="true" disabled>Left + sub</button><button type="button" aria-pressed="false" disabled>Right + sub</button></div>
    ${legend("Predicted")}${exampleChart()}
    <div class="grid">
      <div class="rows"><div class="what" style="font-weight:500">Target</div>
        <div class="seg" role="group" aria-label="Target"><button type="button" aria-pressed="false" disabled>Neutral</button><button type="button" aria-pressed="false" disabled>Warm</button><button type="button" aria-pressed="true" disabled>Room</button></div></div>
      <div class="rows"><div class="what" style="font-weight:500">Correction</div>
        <div class="seg" role="group" aria-label="Correction"><button type="button" aria-pressed="false" disabled>PEQ</button><button type="button" aria-pressed="false" disabled>Detailed</button><button type="button" aria-pressed="true" disabled>FIR</button></div></div>
    </div>
  </section>
  <section class="card" aria-labelledby="h-fb"><div class="card-head"><h2 id="h-fb">Bass management</h2>${EXAMPLE_TAG}</div>${exampleBass}
    <p class="note">Applying new filters can change PiDSP's latency. Afterwards, run Wireless Audio Sync again on the Apple TV (Settings → Video and Audio → Wireless Audio Sync).</p>
  </section>`,
  update() {},
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
  </div>
  <dialog id="power-dlg"><h3 id="power-q"></h3><p class="muted small" id="power-t"></p>
    <form method="dialog" class="actions"><button class="btn" value="cancel">Cancel</button><button class="btn primary" value="ok" id="power-ok"></button></form></dialog>`,
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
    const dlg = $("#power-dlg");
    document.querySelectorAll("[data-power]").forEach((b) => b.addEventListener("click", () => {
      const act = b.dataset.power;
      $("#power-q").textContent = act === "reboot" ? "Restart PiDSP?" : "Shut down PiDSP?";
      $("#power-t").textContent = act === "reboot" ? "Sound stops for about a minute while the Pi restarts. This page reconnects by itself." : "You will need to unplug and reconnect power to start it again.";
      $("#power-ok").textContent = act === "reboot" ? "Restart" : "Shut down";
      dlg.returnValue = "";
      dlg.onclose = async () => {
        if (dlg.returnValue !== "ok") return;
        const msg = $("#power-msg");
        try {
          await api(`/api/system/${act}`, { method: "POST" });
          msg.className = "msg ok"; msg.textContent = act === "reboot" ? "Restarting…" : "Shutting down…";
        } catch (e) { msg.className = "msg bad"; msg.textContent = e.message; }
      };
      dlg.showModal();
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
