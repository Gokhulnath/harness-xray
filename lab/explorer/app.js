/* Agent lab explorer — reads data/index.json and data/<harness>/<run>.json (built by analyze/build_explorer.py). */
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = {
  ms: (v) => (v == null ? "–" : v >= 10000 ? (v / 1000).toFixed(1) + " s" : v >= 100 ? v.toFixed(0) + " ms" : v >= 1 ? v.toFixed(1) + " ms" : v.toFixed(2) + " ms"),
  mb: (v) => (v == null ? "–" : v >= 1024 ? (v / 1024).toFixed(2) + " GB" : v.toFixed(v < 10 ? 1 : 0) + " MB"),
  bytes: (v) => (v == null ? "–" : v >= 1073741824 ? (v / 1073741824).toFixed(2) + " GB" : v >= 1048576 ? (v / 1048576).toFixed(1) + " MB" : v >= 1024 ? (v / 1024).toFixed(1) + " kB" : Math.round(v) + " B"),
  n: (v) => (v == null ? "–" : v >= 1e6 ? (v / 1e6).toFixed(2) + "M" : v >= 1e4 ? (v / 1e3).toFixed(1) + "k" : String(Math.round(v * 100) / 100)),
  pct: (v) => (v == null ? "–" : v.toFixed(0) + "%"),
  s: (v) => (v == null ? "–" : v.toFixed(2) + " s"),
};
const tip = $("#tip");
function showTip(html, ev) {
  tip.innerHTML = html;
  tip.style.display = "block";
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
  if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}
const hideTip = () => (tip.style.display = "none");
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

const ST = { index: null, harness: null, cache: {}, zoom: null };
let ROLE_INFO = {};
const ROLE_ORDER = ["agent", "agent:nested", "tool:shell", "tool:command", "tool:search", "tool:sudo", "tool:root", "agent:helper", "tui:tmux", "lab:docker-exec", "lab:harness", "container:init"];
const roleColor = (r) => (ROLE_INFO[r] ? ROLE_INFO[r][1] : r && r.startsWith("unclassified") ? "#ff00aa" : "#999");
const roleName = (r) => (ROLE_INFO[r] ? ROLE_INFO[r][0] : r);
const roleRank = (r) => { const i = ROLE_ORDER.indexOf(r); return i < 0 ? 99 : i; };
const PALETTE = ["#58a6ff", "#f0883e", "#3fb950", "#d2a8ff", "#ffa657", "#79c0ff", "#ff7b72", "#a371f7", "#56d4dd", "#e3b341", "#7ee787", "#db61a2"];

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-cache" });
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}
async function runData(h, run) {
  const k = h + "/" + run;
  if (!ST.cache[k]) ST.cache[k] = await getJSON(`data/${encodeURIComponent(h)}/${encodeURIComponent(run)}.json`);
  return ST.cache[k];
}
const rowsToObjs = (rows, cols) => rows.map((r) => Object.fromEntries(cols.map((c, i) => [c, r[i]])));

// ------------------------------------------------------------------ generic table
function table(el, rows, cols, opts = {}) {
  // cols: [{k, label, l (left), fmt, wrap, w}]
  let sortK = opts.sort || null, asc = false, filter = "";
  const wrap = document.createElement("div");
  if (opts.search !== false) {
    const tb = document.createElement("div");
    tb.className = "toolbar";
    tb.innerHTML = `<input type="search" placeholder="filter…" style="min-width:220px"> <span class="note">${rows.length} rows</span>`;
    tb.querySelector("input").oninput = (e) => { filter = e.target.value.toLowerCase(); render(); };
    wrap.appendChild(tb);
  }
  const box = document.createElement("div");
  box.className = "scroll";
  if (opts.maxh) box.style.maxHeight = opts.maxh + "px";
  wrap.appendChild(box);
  el.appendChild(wrap);
  function render() {
    let rs = rows;
    if (filter) rs = rs.filter((r) => cols.some((c) => String(r[c.k] ?? "").toLowerCase().includes(filter)));
    if (sortK) rs = rs.slice().sort((a, b) => { const x = a[sortK], y = b[sortK]; const v = typeof x === "number" && typeof y === "number" ? x - y : String(x ?? "").localeCompare(String(y ?? "")); return asc ? v : -v; });
    const lim = opts.limit || 2000;
    box.innerHTML = `<table class="t"><thead><tr>${cols.map((c) => `<th class="${c.l ? "l" : ""}" data-k="${c.k}" title="${esc(c.title || "")}">${esc(c.label || c.k)}${sortK === c.k ? (asc ? " ▴" : " ▾") : ""}</th>`).join("")}</tr></thead><tbody>${rs.slice(0, lim).map((r, i) => `<tr data-i="${rows.indexOf(r)}">${cols.map((c) => `<td class="${c.l ? "l" : ""} ${c.wrap ? "wrap" : ""}" ${c.style ? `style="${c.style(r)}"` : ""}>${c.html ? c.html(r) : esc(c.fmt ? c.fmt(r[c.k], r) : r[c.k] ?? "")}</td>`).join("")}</tr>`).join("")}</tbody></table>${rs.length > lim ? `<p class="note">showing ${lim} of ${rs.length}</p>` : ""}`;
    box.querySelectorAll("th").forEach((th) => (th.onclick = () => { const k = th.dataset.k; if (sortK === k) asc = !asc; else { sortK = k; asc = false; } render(); }));
    if (opts.onRow) box.querySelectorAll("tbody tr").forEach((tr) => (tr.onclick = () => opts.onRow(rows[+tr.dataset.i], tr)));
  }
  render();
  return { render };
}

// ------------------------------------------------------------------ charts
function lineChart(el, cfg) {
  // cfg: {series:[{name,color,points:[[x,y]],area?,dash?}], height, yLabel, xLabel, stacked, xDomain, yFmt, onBrush, markers:[{x,label}], yMax}
  const W = el.clientWidth || 600, H = cfg.height || 170, m = { l: 48, r: 12, t: 8, b: 22 };
  const svg = d3.select(el).append("svg").attr("width", W).attr("height", H);
  let series = cfg.series.filter((s) => s.points && s.points.length);
  if (!series.length) { svg.append("text").attr("x", m.l).attr("y", H / 2).text("no data"); return; }
  const allX = series.flatMap((s) => s.points.map((p) => p[0]));
  const x = d3.scaleLinear().domain(cfg.xDomain || d3.extent(allX)).range([m.l, W - m.r]);
  let ymax;
  let stackedData = null;
  if (cfg.stacked) {
    const xs = Array.from(new Set(allX)).sort((a, b) => a - b);
    const maps = series.map((s) => new Map(s.points.map((p) => [p[0], p[1]])));
    const rowsS = xs.map((xx) => { const o = { x: xx }; series.forEach((s, i) => (o[s.name] = maps[i].get(xx) || 0)); return o; });
    stackedData = d3.stack().keys(series.map((s) => s.name))(rowsS);
    ymax = d3.max(stackedData, (l) => d3.max(l, (d) => d[1])) || 1;
  } else ymax = d3.max(series, (s) => d3.max(s.points, (p) => p[1])) || 1;
  if (cfg.yMax) ymax = Math.max(ymax, cfg.yMax);
  const y = d3.scaleLinear().domain([0, ymax * 1.08]).nice().range([H - m.b, m.t]);
  svg.append("g").attr("class", "grid").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4).tickSize(-(W - m.l - m.r)).tickFormat(""));
  svg.append("g").attr("class", "axis").attr("transform", `translate(0,${H - m.b})`).call(d3.axisBottom(x).ticks(Math.max(3, W / 90)).tickFormat((v) => v + (cfg.xUnit || "s")));
  svg.append("g").attr("class", "axis").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(y).ticks(4).tickFormat(cfg.yFmt || d3.format("~s")));
  if (cfg.yLabel) svg.append("text").attr("x", 4).attr("y", 10).text(cfg.yLabel).style("font-size", "10px");
  const clipId = "c" + Math.random().toString(36).slice(2);
  svg.append("clipPath").attr("id", clipId).append("rect").attr("x", m.l).attr("y", 0).attr("width", W - m.l - m.r).attr("height", H);
  const g = svg.append("g").attr("clip-path", `url(#${clipId})`);
  if (stackedData && cfg.bars) {
    // per-window aggregates: each value covers (x - bars, x]
    stackedData.forEach((l, i) => g.append("g").selectAll("rect").data(l).join("rect")
      .attr("x", (d) => x(d.data.x - cfg.bars)).attr("width", (d) => Math.max(1, x(d.data.x) - x(d.data.x - cfg.bars) - 1))
      .attr("y", (d) => y(d[1])).attr("height", (d) => Math.max(0, y(d[0]) - y(d[1]))).attr("fill", series[i].color).attr("opacity", 0.9));
  } else if (stackedData) {
    const area = d3.area().x((d) => x(d.data.x)).y0((d) => y(d[0])).y1((d) => y(d[1])).curve(d3.curveStepAfter);
    stackedData.forEach((l, i) => g.append("path").attr("d", area(l)).attr("fill", series[i].color).attr("opacity", 0.85));
  } else {
    series.forEach((s) => {
      if (s.area) g.append("path").attr("d", d3.area().x((p) => x(p[0])).y0(y(0)).y1((p) => y(p[1])).curve(d3.curveMonotoneX)(s.points)).attr("fill", s.color).attr("opacity", 0.18);
      g.append("path").attr("d", d3.line().x((p) => x(p[0])).y((p) => y(p[1])).curve(cfg.step ? d3.curveStepAfter : d3.curveMonotoneX)(s.points)).attr("fill", "none").attr("stroke", s.color).attr("stroke-width", s.width || 1.4).attr("stroke-dasharray", s.dash || null);
    });
  }
  (cfg.markers || []).forEach((mk) => {
    if (mk.x < x.domain()[0] || mk.x > x.domain()[1]) return;
    g.append("line").attr("x1", x(mk.x)).attr("x2", x(mk.x)).attr("y1", m.t).attr("y2", H - m.b).attr("stroke", css("--c-req")).attr("stroke-opacity", 0.35).attr("stroke-dasharray", "3,3");
  });
  // hover
  const hover = svg.append("line").attr("y1", m.t).attr("y2", H - m.b).attr("stroke", css("--faint")).style("display", "none");
  svg.append("rect").attr("x", m.l).attr("y", m.t).attr("width", W - m.l - m.r).attr("height", H - m.t - m.b).attr("fill", "transparent")
    .on("mousemove", (ev) => {
      const [mx] = d3.pointer(ev);
      const xv = x.invert(mx);
      hover.attr("x1", mx).attr("x2", mx).style("display", null);
      const lines = series.map((s) => { const i = d3.bisector((p) => p[0]).center(s.points, xv); const p = s.points[i]; return p ? `<div><i class="bar" style="width:9px;background:${s.color}"></i> ${esc(s.name)}: <b>${(cfg.valFmt || fmt.n)(p[1])}</b></div>` : ""; });
      showTip(`<b>t = ${xv.toFixed(2)} s</b>${lines.join("")}`, ev);
    })
    .on("mouseleave", () => { hover.style("display", "none"); hideTip(); });
  if (cfg.onBrush) {
    const br = d3.brushX().extent([[m.l, m.t], [W - m.r, H - m.b]]).on("end", (ev) => { if (!ev.selection) return; cfg.onBrush(ev.selection.map(x.invert)); });
    svg.append("g").attr("class", "brush").call(br);
  }
}

function hbar(el, rows, opts = {}) {
  // rows: [{label, value, color, sub}]
  const max = d3.max(rows, (r) => r.value) || 1;
  el.innerHTML += `<div>${rows.map((r) => `<div style="display:grid;grid-template-columns:${opts.lw || 210}px 1fr ${opts.vw || 90}px;gap:8px;align-items:center;margin:2px 0" title="${esc(r.title || "")}">
    <div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:12px">${r.html || esc(r.label)}</div>
    <div style="background:var(--panel2);border-radius:3px;height:12px"><div style="width:${(100 * r.value) / max}%;background:${r.color || css("--accent")};height:12px;border-radius:3px"></div></div>
    <div style="text-align:right;font-variant-numeric:tabular-nums;font-size:12px">${esc((opts.fmt || fmt.n)(r.value))}${r.sub ? ` <span class="note">${esc(r.sub)}</span>` : ""}</div></div>`).join("")}</div>`;
}

function stackBar(roles, total) {
  // roles: [[role, cpu, ...]]
  const tot = total || d3.sum(roles, (r) => r[1] || 0) || 1;
  return `<div class="stackbar">${roles.filter((r) => r[1] > 0).sort((a, b) => roleRank(a[0]) - roleRank(b[0])).map((r) => `<div title="${esc(roleName(r[0]))}: ${fmt.ms(r[1])}" style="width:${(100 * r[1]) / tot}%;background:${roleColor(r[0])}"></div>`).join("")}</div>`;
}

// ------------------------------------------------------------------ gantt (process timeline)
function gantt(el, procs, B, xDomain, onSelect) {
  const W = el.clientWidth || 900, m = { l: 150, r: 12, t: 6, b: 22 };
  // group by role band; lane-pack within band
  const bands = d3.groups(procs.filter((p) => p.start != null), (p) => p.role).sort((a, b) => roleRank(a[0]) - roleRank(b[0]));
  const laneH = procs.length > 600 ? 4 : procs.length > 200 ? 6 : 9;
  let yOff = m.t;
  const placed = [];
  const bandInfo = [];
  for (const [role, ps] of bands) {
    const lanes = [];
    ps.sort((a, b) => a.start - b.start);
    for (const p of ps) {
      const end = p.end != null ? p.end : xDomain[1];
      let li = lanes.findIndex((e) => e < p.start - 0.0005);
      if (li < 0) { li = lanes.length; lanes.push(end); } else lanes[li] = end;
      placed.push({ p, lane: li, y: yOff + li * (laneH + 1), end });
    }
    const hgt = Math.max(1, lanes.length) * (laneH + 1);
    bandInfo.push({ role, y: yOff, h: hgt, n: ps.length });
    yOff += hgt + 6;
  }
  const H = yOff + m.b;
  const x = d3.scaleLinear().domain(xDomain).range([m.l, W - m.r]);
  const svg = d3.select(el).append("svg").attr("width", W).attr("height", H);
  bandInfo.forEach((b, i) => {
    svg.append("rect").attr("x", 0).attr("y", b.y - 2).attr("width", W).attr("height", b.h + 3).attr("fill", i % 2 ? "transparent" : css("--panel2")).attr("opacity", 0.6);
    svg.append("text").attr("x", 6).attr("y", b.y + Math.min(b.h, 12)).text(`${roleName(b.role)} (${b.n})`).style("fill", roleColor(b.role)).style("font-size", "10.5px");
  });
  svg.append("g").attr("class", "axis").attr("transform", `translate(0,${H - m.b})`).call(d3.axisBottom(x).ticks(Math.max(4, W / 100)).tickFormat((v) => v + "s"));
  const clipId = "g" + Math.random().toString(36).slice(2);
  svg.append("clipPath").attr("id", clipId).append("rect").attr("x", m.l).attr("y", 0).attr("width", W - m.l - m.r).attr("height", H);
  const g = svg.append("g").attr("clip-path", `url(#${clipId})`);
  const maxDensity = d3.max(placed, (d) => (d.p.cpu_ms || 0) / Math.max(1, (d.end - d.p.start) * 1000)) || 1;
  g.selectAll("rect.p").data(placed).join("rect").attr("class", "p")
    .attr("x", (d) => x(d.p.start)).attr("y", (d) => d.y)
    .attr("width", (d) => Math.max(1.2, x(d.end) - x(d.p.start))).attr("height", laneH)
    .attr("fill", (d) => roleColor(d.p.role))
    .attr("opacity", (d) => 0.35 + 0.65 * Math.min(1, ((d.p.cpu_ms || 0) / Math.max(1, (d.end - d.p.start) * 1000)) / Math.max(0.05, maxDensity)))
    .style("cursor", "pointer")
    .on("mousemove", (ev, d) => {
      const p = d.p;
      showTip(`<b style="color:${roleColor(p.role)}">${esc(p.label)}</b><div class="m">${esc(p.argv || p.comm)}</div>
        pid ${p.pid} (ns ${p.nspid ?? "?"}) · ppid ${p.ppid} · uid <b class="${p.uid === 0 ? "badge root" : ""}">${p.uid ?? "?"}</b><br>
        CPU <b>${fmt.ms(p.cpu_ms)}</b> · waited for CPU ${fmt.ms(p.runq_ms)} · peak RSS <b>${fmt.mb((p.rss_kb || 0) / 1024)}</b><br>
        lived ${fmt.ms((d.end - p.start) * 1000)} · threads ${p.threads} · read ${fmt.bytes(p.rchar)} · wrote ${fmt.bytes(p.wchar)}<br>
        page faults ${fmt.n(p.minflt)} minor / ${p.majflt} major · ctx switches ${fmt.n(p.nvcsw)} vol / ${fmt.n(p.nivcsw)} invol`, ev);
    })
    .on("mouseleave", hideTip)
    .on("click", (ev, d) => onSelect && onSelect(d.p));
  return svg;
}

function eventLanes(el, B, xDomain, opts) {
  const W = el.clientWidth || 900, m = { l: 150, r: 12 };
  const laneDefs = [];
  const turns = [];
  // agent turns from agent events (turn_start/turn_end per probe pid)
  const evs = B.events.filter((e) => e[1] === "agent");
  const open = {};
  evs.forEach((e) => { if (e[2] === "turn_start") open[e[4]] = e[0]; else if (e[2] === "turn_end" && open[e[4]] != null) { turns.push({ t0: open[e[4]], t1: e[0], pid: e[4] }); delete open[e[4]]; } });
  if (turns.length) laneDefs.push({ name: `agent turns (${turns.length})`, items: turns.map((t, i) => ({ t0: t.t0, t1: t.t1, color: i % 2 ? "#58a6ff" : "#1f6feb", html: `turn ${i + 1} (pid ${t.pid}) · ${fmt.ms((t.t1 - t.t0) * 1000)}` })) });
  const tools = rowsToObjs(B.tools, B.tool_cols).filter((t) => t.t0 != null);
  const toolNames = Array.from(new Set(tools.map((t) => t.tool)));
  if (tools.length) laneDefs.push({ name: `tool calls (${tools.length})`, items: tools.map((t) => ({ t0: t.t0, t1: t.t1 ?? t.t0, color: PALETTE[toolNames.indexOf(t.tool) % PALETTE.length], html: `<b>${esc(t.tool)}</b> ${fmt.ms(t.dur_ms)}${t.is_error ? ' <span class="badge err">error</span>' : ""}<div class="m">${esc((t.args || "").slice(0, 300))}</div>children ${t.child_procs} · child CPU ${fmt.ms(t.child_cpu_ms)} · result ${fmt.bytes(t.result_chars)}${t.truncated ? " (truncated)" : ""}` })) });
  const reqs = rowsToObjs(B.requests, B.request_cols).filter((r) => r.t != null);
  if (reqs.length) laneDefs.push({ name: `model requests (${reqs.length})`, items: reqs.map((r) => ({ t0: r.t, t1: r.t_end ?? r.t, color: r.injected ? "#f85149" : r.aux ? "#8b949e" : "#e3b341", html: `${r.injected ? "<b>injected " + r.injected + "</b> " : ""}${r.aux ? "auxiliary " : ""}request · step ${r.step ?? "–"} · ${fmt.bytes(r.req_bytes)} sent · ${r.n_messages ?? "?"} messages · ${fmt.ms(r.stream_ms)}<div class="m">${esc(JSON.stringify(r.reply || {}))}</div>` })) });
  const gcs = B.events.filter((e) => e[1] === "gc");
  if (gcs.length) laneDefs.push({ name: `GC pauses (${gcs.length})`, items: gcs.map((e) => ({ t0: e[0], t1: e[0], color: "#d2a8ff", html: esc(e[3]) })) });
  const ss = B.events.filter((e) => e[1] === "spawnSync");
  if (ss.length) laneDefs.push({ name: `spawnSync blocks (${ss.length})`, items: ss.map((e) => ({ t0: e[0], t1: e[0], color: "#d29922", html: esc(e[3]) })) });
  const net = B.events.filter((e) => e[1] === "tcp" || e[1] === "http");
  if (net.length) laneDefs.push({ name: `network (${net.length})`, items: net.map((e) => ({ t0: e[0], t1: e[0], color: e[1] === "tcp" ? "#3fb950" : "#7ee787", html: esc(e[3]) })) });
  const sig = B.events.filter((e) => e[1] === "signal");
  if (sig.length) laneDefs.push({ name: `signals (${sig.length})`, items: sig.map((e) => ({ t0: e[0], t1: e[0], color: e[2] === "SIGKILL" ? "#f85149" : "#ffa657", html: esc(e[3]) })) });
  const marks = B.events.filter((e) => e[1] === "mark");
  const lh = 14, H = laneDefs.length * (lh + 4) + 24;
  const x = d3.scaleLinear().domain(xDomain).range([m.l, W - m.r]);
  const svg = d3.select(el).append("svg").attr("width", W).attr("height", H);
  const clipId = "l" + Math.random().toString(36).slice(2);
  svg.append("clipPath").attr("id", clipId).append("rect").attr("x", m.l).attr("y", 0).attr("width", W - m.l - m.r).attr("height", H);
  laneDefs.forEach((ld, i) => {
    const y0 = 4 + i * (lh + 4);
    svg.append("text").attr("x", 6).attr("y", y0 + 11).text(ld.name).style("font-size", "10.5px");
    svg.append("g").attr("clip-path", `url(#${clipId})`).selectAll("rect").data(ld.items).join("rect")
      .attr("x", (d) => x(d.t0)).attr("y", y0).attr("width", (d) => Math.max(1.5, x(d.t1) - x(d.t0))).attr("height", lh).attr("fill", (d) => d.color).attr("opacity", 0.85)
      .on("mousemove", (ev, d) => showTip(`<b>t ${d.t0.toFixed(3)} s</b><br>${d.html}`, ev)).on("mouseleave", hideTip);
  });
  marks.forEach((mk) => { if (mk[0] >= xDomain[0] && mk[0] <= xDomain[1]) svg.append("line").attr("x1", x(mk[0])).attr("x2", x(mk[0])).attr("y1", 0).attr("y2", H - 20).attr("stroke", "#e3b341").attr("stroke-opacity", 0.4).attr("stroke-dasharray", "2,3").append("title").text(mk[3]); });
  svg.append("g").attr("class", "axis").attr("transform", `translate(0,${H - 20})`).call(d3.axisBottom(x).ticks(Math.max(4, W / 100)).tickFormat((v) => v + "s"));
}

// ------------------------------------------------------------------ process tree
function processTree(el, procs, B) {
  const byPid = new Map(procs.map((p) => [p.pid, p]));
  const kids = d3.group(procs, (p) => p.ppid);
  const roots = procs.filter((p) => !byPid.has(p.ppid)).sort((a, b) => (a.start || 0) - (b.start || 0));
  const subtreeCpu = new Map();
  const sc = (p) => { if (subtreeCpu.has(p.pid)) return subtreeCpu.get(p.pid); let s = p.cpu_ms || 0; (kids.get(p.pid) || []).forEach((c) => (s += sc(c))); subtreeCpu.set(p.pid, s); return s; };
  roots.forEach(sc);
  const maxCpu = d3.max(procs, (p) => p.cpu_ms) || 1;
  let count = 0;
  const node = (p, depth) => {
    if (count++ > 4000) return "";
    const ch = (kids.get(p.pid) || []).sort((a, b) => (a.start || 0) - (b.start || 0));
    const thr = B.threads[String(p.pid)];
    const thrHtml = thr ? `<div style="margin-left:22px;color:var(--faint)">threads: ${thr.slice(0, 12).map((t) => `${esc(t[1])} ${fmt.ms(t[2])}${t[3] > 0.5 ? ` (waited ${fmt.ms(t[3])})` : ""}`).join(" · ")}</div>` : "";
    const sum = `<summary><span class="cpu">${fmt.ms(p.cpu_ms)}</span><span class="rss">${fmt.mb((p.rss_kb || 0) / 1024)}</span><span class="life">${p.end != null && p.start != null ? fmt.ms((p.end - p.start) * 1000) : "alive"}</span> <span class="bar" style="width:${Math.max(1, (60 * (p.cpu_ms || 0)) / maxCpu)}px;background:${roleColor(p.role)}"></span> <b style="color:${roleColor(p.role)}">${esc(p.comm)}</b> <span class="pill" style="border-color:${roleColor(p.role)}">${esc(roleName(p.role))}</span> ${p.uid === 0 ? '<span class="badge root">root</span>' : p.uid != null && p.uid !== 1001 ? `<span class="badge">uid ${p.uid}</span>` : ""} <span class="argv">pid ${p.pid} · ${esc((p.argv || "").slice(0, 140))}</span>${ch.length ? ` <span class="note">(+${ch.length} children, subtree ${fmt.ms(subtreeCpu.get(p.pid))})</span>` : ""}</summary>`;
    return `<details class="${ch.length || thr ? "" : "leaf"}" ${depth < 2 && ch.length < 60 ? "open" : ""}>${sum}${thrHtml}${ch.map((c) => node(c, depth + 1)).join("")}</details>`;
  };
  el.innerHTML = `<div class="tree">${roots.map((r) => node(r, 0)).join("")}</div>${count > 4000 ? '<p class="note">tree truncated at 4000 nodes — use the process table</p>' : ""}`;
}

// ------------------------------------------------------------------ pages
function kpis(items) {
  return `<div class="kpis">${items.map(([l, v, t]) => `<div class="kpi" title="${esc(t || "")}"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`).join("")}</div>`;
}

function story(B, procs) {
  const k = B.kpi;
  const roles = Object.fromEntries(B.roles.map((r) => [r[0], r]));
  const agentCpu = (roles.agent ? roles.agent[1] : 0) + (roles["agent:nested"] ? roles["agent:nested"][1] : 0);
  const toolCpu = ["tool:shell", "tool:command", "tool:search", "tool:sudo", "tool:root"].reduce((s, r) => s + (roles[r] ? roles[r][1] : 0), 0);
  const labCpu = ["lab:docker-exec", "lab:harness", "tui:tmux", "container:init"].reduce((s, r) => s + (roles[r] ? roles[r][1] : 0), 0);
  const total = agentCpu + toolCpu + labCpu + (roles["agent:helper"] ? roles["agent:helper"][1] : 0);
  const topTool = B.commands.filter((c) => c[0].startsWith("tool"))[0];
  const agents = procs.filter((p) => p.role === "agent" || p.role === "agent:nested");
  const big = agents.sort((a, b) => b.rss_kb - a.rss_kb)[0];
  const sys = B.syscalls[0];
  const fileTop = B.files.find((f) => f[2] === "file");
  const lines = [];
  lines.push(`Over <b>${fmt.s(k.wall_s)}</b> the container used <b>${fmt.ms(k.cpu_ms)}</b> of CPU (${k.avg_cores ?? "–"} cores on average) and peaked at <b>${fmt.mb(k.mem_max_mb)}</b>.`);
  if (total) lines.push(`eBPF attributes <b>${fmt.ms(agentCpu)}</b> (${((100 * agentCpu) / total).toFixed(0)}%) to the agent process${agents.length > 1 ? "es" : ""}, <b>${fmt.ms(toolCpu)}</b> (${((100 * toolCpu) / total).toFixed(0)}%) to processes its tool calls started, and ${fmt.ms(labCpu)} to lab machinery (docker exec, runner, tmux).`);
  if (agents.length) lines.push(`${agents.length} agent process${agents.length > 1 ? "es" : ""}${big ? `; the largest peaked at <b>${fmt.mb(big.rss_kb / 1024)}</b> RSS with ${big.threads} threads` : ""}. ${k.processes} processes ran in total.`);
  if (topTool) lines.push(`Heaviest tool-side command: <b>${esc(topTool[1])}</b> (${roleName(topTool[0])}) × ${topTool[2]}, ${fmt.ms(topTool[3])} CPU.`);
  if (k.tool_calls) lines.push(`${k.tool_calls} tool calls${k.tool_errors ? ` (${k.tool_errors} errors)` : ""}; ${k.model_requests} model requests sent ${fmt.bytes(k.bytes_sent)} (largest single request ${fmt.bytes(k.max_request)})${k.summaries ? `, including ${k.summaries} compaction summaries` : ""}${k.injected_errors ? `, ${k.injected_errors} injected provider errors` : ""}.`);
  if (sys) lines.push(`Kernel view: ${fmt.n(k.syscalls)} syscalls (top: <code>${esc(sys[1])}</code> on thread ${esc(sys[0])} × ${fmt.n(sys[2])}), ${fmt.n(k.opens)} file opens, ${fmt.bytes(k.file_write)} written to regular files${fileTop ? ` (largest: ${esc(fileTop[4])} ${fmt.bytes(fileTop[5])} ${fileTop[0]} by ${esc(fileTop[1])})` : ""}, ${fmt.bytes(k.net_rx)} received / ${fmt.bytes(k.net_tx)} sent on the container's network.`);
  if (k.runq_wait_ms > 5) lines.push(`Processes spent <b>${fmt.ms(k.runq_wait_ms)}</b> runnable but waiting for a CPU (run-queue wait) — a direct measure of CPU contention.`);
  return `<div class="story">${lines.map((l) => `<p>${l}</p>`).join("")}</div>`;
}

async function pageRun(h, run) {
  const B = await runData(h, run);
  ROLE_INFO = B.role_info || ROLE_INFO;
  const procs = rowsToObjs(B.procs, B.proc_cols);
  const k = B.kpi;
  const v = $("#view");
  const end = Math.max(B.t1 ? B.t1 + 3 : 0, d3.max(procs, (p) => p.end) || 0, d3.max(B.cg["lab-agent"] || [], (r) => r[0]) || 0);
  const full = [0, end];
  ST.zoom = ST.zoom && ST.zoom.run === h + run ? ST.zoom : { run: h + run, d: full };
  v.innerHTML = `
  <h2 class="title">${esc(run)} <span class="note">· ${esc(B.harness_display || h)}</span></h2><p class="desc">${esc(B.desc)}</p>
  ${kpis([["wall time", fmt.s(k.wall_s)], ["container CPU", fmt.ms(k.cpu_ms), "cgroup cpu.stat delta over the run window"], ["avg cores", k.avg_cores ?? "–"], ["peak memory", fmt.mb(k.mem_max_mb), "cgroup memory.current max (incl. page cache)"], ["max pids", k.pids_max ?? "–"],
          ["processes", k.processes, "every process eBPF saw in the agent container"], ["agent procs", k.agents], ["tool calls", `${k.tool_calls}${k.tool_errors ? ` (${k.tool_errors} err)` : ""}`], ["model requests", k.model_requests], ["bytes to model", fmt.bytes(k.bytes_sent)],
          ["syscalls", fmt.n(k.syscalls)], ["file opens", fmt.n(k.opens)], ["file writes", fmt.bytes(k.file_write)], ["net rx/tx", `${fmt.bytes(k.net_rx)} / ${fmt.bytes(k.net_tx)}`], ["CPU wait", fmt.ms(k.runq_wait_ms), "run-queue wait summed over processes"], ["mock CPU", fmt.ms(k.mock_cpu_ms)]])}
  ${story(B, procs)}
  <h3>Where did the CPU go? <span class="hint">exact per-process CPU from eBPF, grouped by explicit role</span></h3>
  <div class="grid3"><div class="panel" id="rolesBox"></div><div class="panel" id="cmdBox"></div><div class="panel" id="thrBox"></div></div>
  <h3>Timeline <span class="hint">drag across the CPU chart to zoom every chart · <a href="#" id="zoomReset">reset zoom</a></span></h3>
  <div class="panel"><div id="tCpu"></div><div class="legend" id="tCpuLeg"></div></div>
  <div class="grid2" style="margin-top:12px"><div class="panel"><div id="tMem"></div><div class="legend"><span><i style="background:var(--c-mem)"></i>container memory.current</span><span><i style="background:#2ea043"></i>anon</span><span><i style="background:#8b949e"></i>page cache (file)</span><span><i style="background:var(--c-pi)"></i>agent RSS</span><span><i style="background:var(--c-heap)"></i>agent JS heap used</span></div></div>
  <div class="panel"><div id="tLoop"></div><div class="legend"><span><i style="background:var(--c-pi)"></i>event-loop utilization %</span><span><i style="background:var(--bad)"></i>loop lag p99 ms</span><span><i style="background:var(--c-tool)"></i>agent CPU % (probe)</span></div></div></div>
  <div class="grid2" style="margin-top:12px"><div class="panel"><div id="tNet"></div><div class="legend"><span><i style="background:var(--good)"></i>rx B/s</span><span><i style="background:var(--c-tool)"></i>tx B/s</span><span><i style="background:#8b949e"></i>loopback B/s</span></div></div>
  <div class="panel"><div id="tSys"></div><div class="legend" id="tSysLeg"></div></div></div>
  <div class="grid2" style="margin-top:12px"><div class="panel"><div id="tFile"></div><div class="legend" id="tFileLeg"></div></div><div class="panel"><div id="tVm"></div><div class="legend"><span><i style="background:#79c0ff"></i>VM CPU busy %</span><span><i style="background:#f85149"></i>agent container CPU pressure %</span><span><i style="background:#d29922"></i>VM CPU pressure %</span></div></div></div>
  <div class="panel" style="margin-top:12px"><div class="toolbar"><b>Process timeline</b> <span class="note">one bar per process, banded by role; brightness = CPU density; click a bar for details</span></div><div id="gantt" class="gantt-wrap"></div><div id="procDetail" class="detail"></div></div>
  <div class="panel" style="margin-top:12px"><div class="toolbar"><b>Event lanes</b> <span class="note">turns · tool calls · model requests · GC · blocking spawnSync · network · signals</span></div><div id="lanes"></div></div>
  <div id="threadSec"></div>
  <div id="agentSec"></div>
  <div id="turnSec"></div>
  <h3>Tool calls <span class="hint">timing from inside the agent; child processes and their CPU from eBPF</span></h3><div class="panel" id="toolTbl"></div>
  <h3>Model requests <span class="hint">what the harness sent every turn (mock log)</span></h3><div class="panel" id="reqTbl"></div>
  <h3>Kernel: syscalls, files, network <span class="hint">eBPF per-second aggregates, totals for this run</span></h3>
  <div class="grid2"><div class="panel" id="sysTbl"></div><div class="panel" id="fileTbl"></div></div>
  <div class="grid2" style="margin-top:12px"><div class="panel" id="openTbl"></div><div class="panel" id="netTbl"></div></div>
  <div class="grid2" style="margin-top:12px"><div class="panel" id="tcpTbl"></div><div class="panel" id="sigTbl"></div></div>
  <h3>Process tree <span class="hint">CPU · peak RSS · lifetime · role · threads</span></h3><div class="panel" id="tree"></div>
  <h3>All processes</h3><div class="panel" id="procTbl"></div>
  <h3>Event log</h3><div class="panel" id="evlog"></div>
  <h3>Run details <span class="hint">steps, disk usage, leftovers, sudo log, how to query</span></h3><div class="panel" id="details"></div>`;
  $("#zoomReset").onclick = (e) => { e.preventDefault(); ST.zoom.d = full; pageRun(h, run); };

  // ---- roles / commands / threads
  const tot = d3.sum(B.roles, (r) => r[1] || 0);
  $("#rolesBox").innerHTML = `<b>By role</b> <span class="note">total ${fmt.ms(tot)}</span>${stackBar(B.roles, tot)}<div style="height:6px"></div>`;
  hbar($("#rolesBox"), B.roles.slice().sort((a, b) => b[1] - a[1]).map((r) => ({ html: `<span style="color:${roleColor(r[0])}">■</span> ${esc(roleName(r[0]))} <span class="note">×${r[3]}</span>`, value: r[1] || 0, color: roleColor(r[0]), title: `peak RSS ${r[4]} MB · CPU wait ${fmt.ms(r[2])} · read ${fmt.bytes(r[5])} · wrote ${fmt.bytes(r[6])}` })), { fmt: fmt.ms, lw: 220 });
  $("#cmdBox").innerHTML = `<b>By command</b> <span class="note">top 15 · role colour</span>`;
  hbar($("#cmdBox"), B.commands.slice(0, 15).map((c) => ({ html: `${esc(c[1] || "?")} <span class="note">×${c[2]}</span>`, value: c[3] || 0, color: roleColor(c[0]), title: `${roleName(c[0])} · peak RSS ${c[4]} MB` })), { fmt: fmt.ms, lw: 170 });
  $("#thrBox").innerHTML = `<b>Agent threads</b> <span class="note">CPU by thread name, all agent processes</span>`;
  hbar($("#thrBox"), B.agent_threads.slice(0, 15).map((t) => ({ html: `${esc(t[0])} <span class="note">×${t[1]}</span>`, value: t[2] || 0, color: "#58a6ff", sub: t[3] > 1 ? `wait ${fmt.ms(t[3])}` : "" })), { fmt: fmt.ms, lw: 150, vw: 140 });

  // ---- timeline charts
  const xd = ST.zoom.d;
  const onBrush = (d) => { ST.zoom.d = d; pageRun(h, run); };
  const cg = B.cg["lab-agent"] || [], mk = B.cg["lab-mockllm"] || [];
  const marks = B.events.filter((e) => e[1] === "mark").map((e) => ({ x: e[0], label: e[3] }));
  // CPU by role over time: from /proc samples we only have per-pid; use cgroup total + per-role eBPF integrated? Use per-process 100 ms samples aggregated by role.
  const roleByPid = new Map(procs.map((p) => [p.pid, p.role]));
  const roleSeries = {};
  for (const [pid, s] of Object.entries(B.proc_series || {})) {
    const role = s.role;
    (roleSeries[role] = roleSeries[role] || []).push(...s.series.map((p) => [p[0], p[1]]));
  }
  const cpuSeries = [{ name: "agent container CPU % (cgroup)", color: "#58a6ff", points: cg.map((r) => [r[0], r[1]]), area: true }, { name: "mock LLM CPU %", color: "#e3b341", points: mk.map((r) => [r[0], r[1]]), dash: "4,3" }];
  for (const pr of B.probes) cpuSeries.push({ name: `agent ${pr.pid} CPU % (probe)`, color: "#f0883e", points: pr.samples.map((s) => [s[0], s[1]]), width: 1 });
  lineChart($("#tCpu"), { series: cpuSeries.slice(0, 8), height: 180, yLabel: "CPU % (100 = 1 core)", xDomain: xd, onBrush, markers: marks, valFmt: (v) => v.toFixed(1) + "%" });
  $("#tCpuLeg").innerHTML = `<span><i style="background:#58a6ff"></i>agent container (cgroup)</span><span><i style="background:#e3b341"></i>mock LLM container</span><span><i style="background:#f0883e"></i>each agent process (in-process probe)</span><span class="note">yellow dashed lines = runner marks</span>`;
  const memS = [{ name: "container memory.current MB", color: "#79c0ff", points: cg.map((r) => [r[0], r[2]]), area: true }, { name: "anon MB", color: "#2ea043", points: cg.map((r) => [r[0], r[3]]) }, { name: "page cache MB", color: "#8b949e", points: cg.map((r) => [r[0], r[4]]) }];
  B.probes.slice(0, 6).forEach((pr) => { memS.push({ name: `agent ${pr.pid} RSS MB`, color: "#58a6ff", points: pr.samples.map((s) => [s[0], s[2]]), width: 1 }); memS.push({ name: `agent ${pr.pid} heap MB`, color: "#d2a8ff", points: pr.samples.map((s) => [s[0], s[3]]), width: 1 }); });
  lineChart($("#tMem"), { series: memS, yLabel: "MB", xDomain: xd, markers: marks, valFmt: (v) => v.toFixed(1) + " MB" });
  const loopS = [];
  B.probes.slice(0, 6).forEach((pr) => { loopS.push({ name: `ELU % ${pr.pid}`, color: "#58a6ff", points: pr.samples.map((s) => [s[0], (s[7] || 0) * 100]) }); loopS.push({ name: `lag p99 ms ${pr.pid}`, color: "#f85149", points: pr.samples.map((s) => [s[0], s[8] || 0]), width: 1 }); });
  if (loopS.length) lineChart($("#tLoop"), { series: loopS, yLabel: "% / ms", xDomain: xd, markers: marks });
  else $("#tLoop").innerHTML = '<p class="note">no in-process probe for this run (non-Node harness or probe disabled)</p>';
  const net = B.net["lab-agent"] || [];
  lineChart($("#tNet"), { series: [{ name: "rx B/s", color: "#3fb950", points: net.map((r) => [r[0], r[1]]), area: true }, { name: "tx B/s", color: "#f0883e", points: net.map((r) => [r[0], r[2]]) }, { name: "loopback B/s", color: "#8b949e", points: net.map((r) => [r[0], r[3]]) }], yLabel: "agent container network B/s", xDomain: xd, markers: marks, valFmt: fmt.bytes });
  const ssr = B.syscall_series;
  if (ssr && ssr.rows.length) {
    lineChart($("#tSys"), { series: ssr.names.map((n, i) => ({ name: n, color: PALETTE[i % PALETTE.length], points: ssr.rows.map((r) => [r[0], r[i + 1]]) })), stacked: true, bars: 1, yLabel: "syscalls per second (top 8; bar = 1 s window)", xDomain: xd, markers: marks });
    $("#tSysLeg").innerHTML = ssr.names.map((n, i) => `<span><i style="background:${PALETTE[i % PALETTE.length]}"></i>${esc(n)}</span>`).join("");
  }
  const fs = B.file_series;
  if (fs && fs.rows.length) {
    lineChart($("#tFile"), { series: fs.kinds.map((kk, i) => ({ name: kk, color: PALETTE[i % PALETTE.length], points: fs.rows.map((r) => [r[0], r[i + 1]]) })), stacked: true, bars: 1, yLabel: "file I/O bytes per second by file type (1 s windows)", xDomain: xd, markers: marks, valFmt: fmt.bytes });
    $("#tFileLeg").innerHTML = fs.kinds.map((kk, i) => `<span><i style="background:${PALETTE[i % PALETTE.length]}"></i>${esc(kk.replace("_", " "))}</span>`).join("");
  }
  lineChart($("#tVm"), { series: [{ name: "VM CPU busy %", color: "#79c0ff", points: B.vm.map((r) => [r[0], r[1]]) }, { name: "container CPU pressure %", color: "#f85149", points: cg.map((r) => [r[0], r[7]]) }, { name: "VM CPU pressure %", color: "#d29922", points: B.vm.map((r) => [r[0], r[4]]) }], yLabel: "% (VM = Docker Desktop VM, 18 vCPU)", xDomain: xd, markers: marks, valFmt: (v) => v.toFixed(1) + "%" });

  // ---- gantt + lanes
  gantt($("#gantt"), procs, B, xd, (p) => {
    const ch = procs.filter((c) => c.ppid === p.pid);
    const thr = B.threads[String(p.pid)];
    $("#procDetail").innerHTML = `<div class="panel" style="margin-top:8px"><b style="color:${roleColor(p.role)}">${esc(p.label)}</b> — pid ${p.pid}
      <dl><dt>command</dt><dd>${esc(p.argv || p.comm)}</dd><dt>first exec / exe</dt><dd>${esc(p.first_exec || "–")} → ${esc(p.exe || "–")}</dd>
      <dt>parent</dt><dd>${p.ppid} ${esc((procs.find((q) => q.pid === p.ppid) || {}).comm || "")}</dd><dt>uid / ns pid</dt><dd>${p.uid} / ${p.nspid}</dd>
      <dt>CPU / waited</dt><dd>${fmt.ms(p.cpu_ms)} on-CPU · ${fmt.ms(p.runq_ms)} runnable-but-waiting · ${p.slices ?? "?"} timeslices</dd>
      <dt>memory</dt><dd>peak RSS ${fmt.mb((p.rss_kb || 0) / 1024)} · ${fmt.n(p.minflt)} minor / ${p.majflt} major faults</dd>
      <dt>I/O</dt><dd>read ${fmt.bytes(p.rchar)} (${fmt.bytes(p.read_bytes)} from storage) · wrote ${fmt.bytes(p.wchar)} (${fmt.bytes(p.write_bytes)} to storage)</dd>
      <dt>lifetime</dt><dd>${p.start?.toFixed(3)} s → ${p.end?.toFixed(3) ?? "alive"} s</dd>
      ${thr ? `<dt>threads</dt><dd>${thr.map((t) => `${esc(t[1])}#${t[0]} ${fmt.ms(t[2])}`).join(", ")}</dd>` : ""}
      ${ch.length ? `<dt>children</dt><dd>${ch.slice(0, 40).map((c) => `${esc(c.comm)}(${c.pid}) ${fmt.ms(c.cpu_ms)}`).join(", ")}${ch.length > 40 ? " …" : ""}</dd>` : ""}</dl></div>`;
  });
  eventLanes($("#lanes"), B, xd);

  // ---- threads over time
  if (B.thread_series && B.thread_series.length) {
    const sec = $("#threadSec");
    sec.innerHTML = `<h3>Agent threads over time <span class="hint">per-thread CPU % from 100 ms /proc task samples, summed by thread name; red line = run-queue wait ms/s</span></h3><div class="grid2" id="thrCharts"></div>`;
    B.thread_series.forEach((ts) => {
      const box = document.createElement("div");
      box.className = "panel";
      $("#thrCharts").appendChild(box);
      const head = document.createElement("div");
      head.innerHTML = `<b>agent pid ${ts.pid}</b> <span class="note">(container pid ${ts.nspid})</span>`;
      box.appendChild(head);
      const c = document.createElement("div");
      box.appendChild(c);
      lineChart(c, { series: ts.names.map((n, i) => ({ name: n, color: PALETTE[i % PALETTE.length], points: ts.rows.map((r) => [r[0], r[i + 1]]) })), stacked: true, yLabel: "thread CPU %", xDomain: xd, markers: marks, valFmt: (v) => v.toFixed(1) + "%" });
      const c2 = document.createElement("div");
      box.appendChild(c2);
      lineChart(c2, { series: [{ name: "run-queue wait ms/s", color: "#f85149", points: ts.rows.map((r) => [r[0], r[r.length - 1]]) }], height: 90, yLabel: "CPU wait ms/s", xDomain: xd });
      const leg = document.createElement("div");
      leg.className = "legend";
      leg.innerHTML = ts.names.map((n, i) => `<span><i style="background:${PALETTE[i % PALETTE.length]}"></i>${esc(n)}</span>`).join("");
      box.appendChild(leg);
    });
  }

  // ---- agent internals (probe)
  if (B.probes.length) {
    const sec = $("#agentSec");
    sec.innerHTML = `<h3>Agent internals <span class="hint">Node.js view from inside each agent process (in-process probe)</span></h3><div class="panel"><div class="toolbar"><select id="probeSel">${B.probes.map((p, i) => `<option value="${i}">pid ${p.pid} · ${esc((p.argv || "").slice(0, 90))}</option>`).join("")}</select></div><div id="probeBox"></div></div>`;
    const showProbe = (i) => {
      const pr = B.probes[i];
      const box = $("#probeBox");
      box.innerHTML = `${kpis([["CPU (user+sys)", fmt.ms(pr.cpu_ms)], ["max RSS", fmt.mb(pr.maxrss_mb)], ["startup → first event", fmt.ms(pr.startup_ms)], ["children CPU", fmt.ms((pr.child_cpu_s || 0) * 1000), "reaped children's CPU (cutime+cstime)"], ["fs writes", pr.fs_write ?? "–"], ["ctx switches", `${fmt.n(pr.vol_ctx)} / ${fmt.n(pr.invol_ctx)}`, "voluntary / involuntary"]])}
        <div class="grid2"><div id="pHeap"></div><div id="pLoop"></div></div>
        <div class="grid3" style="margin-top:8px"><div id="pGc"></div><div id="pSpaces"></div><div id="pMisc"></div></div>`;
      lineChart($("#pHeap"), { series: [{ name: "RSS", color: "#58a6ff", points: pr.samples.map((s) => [s[0], s[2]]), area: true }, { name: "heap total", color: "#79c0ff", points: pr.samples.map((s) => [s[0], s[4]]) }, { name: "heap used", color: "#d2a8ff", points: pr.samples.map((s) => [s[0], s[3]]) }, { name: "external", color: "#ffa657", points: pr.samples.map((s) => [s[0], s[5]]) }, { name: "array buffers", color: "#7ee787", points: pr.samples.map((s) => [s[0], s[6]]) }], yLabel: "MB", xDomain: xd, valFmt: (v) => v.toFixed(2) + " MB" });
      lineChart($("#pLoop"), { series: [{ name: "CPU %", color: "#f0883e", points: pr.samples.map((s) => [s[0], s[1]]), area: true }, { name: "ELU %", color: "#58a6ff", points: pr.samples.map((s) => [s[0], (s[7] || 0) * 100]) }, { name: "lag max ms", color: "#f85149", points: pr.samples.map((s) => [s[0], s[9] || 0]) }], yLabel: "% / ms", xDomain: xd });
      $("#pGc").innerHTML = `<b>GC</b>`;
      hbar($("#pGc"), pr.gc.map((g) => ({ label: `${g[0]} ×${g[1]}`, value: g[2] || 0, sub: `max ${fmt.ms(g[3])}`, color: "#d2a8ff" })), { fmt: fmt.ms, lw: 110, vw: 130 });
      $("#pSpaces").innerHTML = `<b>V8 heap spaces</b> <span class="note">at exit, used of size</span>`;
      hbar($("#pSpaces"), pr.heap_spaces.map((s) => ({ label: s[0].replace("_space", ""), value: s[1] / 1048576, sub: `/ ${(s[2] / 1048576).toFixed(1)}`, color: "#d2a8ff" })), { fmt: (v) => v.toFixed(2) + " MB", lw: 120, vw: 120 });
      $("#pMisc").innerHTML = `<b>Handles at end</b><div class="note">${Object.entries(pr.handles || {}).map(([kk, vv]) => `${esc(kk)} ×${vv}`).join(" · ") || "–"}</div><b>Sockets</b><div class="note">${(pr.sockets || []).map((s) => `${s[0]}:${s[1]} read ${fmt.bytes(s[2])} wrote ${fmt.bytes(s[3])} open ${fmt.ms(s[4])}`).join("<br>") || "–"}</div>
        ${B.mem_detail && Object.keys(B.mem_detail).length ? `<b>Memory map (latest, kB)</b><div class="note">${Object.entries(B.mem_detail).map(([pid, rows]) => { const l = rows[rows.length - 1]; return `pid ${pid}: RSS ${l[1]} · PSS ${l[2]} (anon ${l[3]}, file ${l[4]}, shmem ${l[5]}) · private dirty ${l[6]} · VmSize ${l[7]} · data ${l[8]} · PTE ${l[9]}`; }).join("<br>")}</div>` : ""}
        ${B.fd_types && Object.keys(B.fd_types).length ? `<b>Open file descriptors by kind (max)</b><div class="note">${Object.entries(B.fd_types).map(([pid, rows]) => `pid ${pid}: ${rows.map((r) => `${esc(r[0])} ×${r[1]}`).join(" · ")}`).join("<br>")}</div>` : ""}`;
    };
    $("#probeSel").onchange = (e) => showProbe(+e.target.value);
    showProbe(0);
  }

  // ---- turns
  const turns = rowsToObjs(B.turns, B.turn_cols);
  const mturns = rowsToObjs(B.model_turns, B.model_turn_cols);
  if (turns.length || mturns.length) {
    const sec = $("#turnSec");
    sec.innerHTML = `<h3>Turn anatomy <span class="hint">where each agent turn's time went; request size shows the conversation re-sent every turn</span></h3><div class="grid2"><div class="panel" id="wf"></div><div class="panel" id="wf2"></div></div>`;
    if (turns.length) {
      const pid0 = turns[0].probe_pid;
      const tt = turns.filter((t) => t.probe_pid === pid0 && t.pre_request_ms != null);
      const phases = [["pre_request_ms", "turn start → request sent (context build + serialize)", "#58a6ff"], ["model_ms", "model (mock) streaming", "#e3b341"], ["dispatch_ms", "stream end → tool start", "#d2a8ff"], ["tool_ms", "tool execution", "#f0883e"], ["post_tool_ms", "tool end → turn end", "#3fb950"]];
      lineChart($("#wf"), { series: phases.map(([kk, n, c]) => ({ name: n, color: c, points: tt.map((t) => [t.idx, t[kk] || 0]) })), stacked: true, yLabel: "ms per turn (agent pid " + pid0 + ")", xUnit: "", valFmt: (v) => v.toFixed(2) + " ms" });
      $("#wf").insertAdjacentHTML("beforeend", `<div class="legend">${phases.map(([, n, c]) => `<span><i style="background:${c}"></i>${n}</span>`).join("")}</div><p class="note">x = turn index. Medians: ${phases.map(([kk, n]) => `${n.split(" (")[0]} ${fmt.ms(d3.median(tt, (t) => t[kk]))}`).join(" · ")}</p>`);
    } else $("#wf").innerHTML = '<p class="note">no in-process turn events (non-pi harness); see the model-side view →</p>';
    const reqs = rowsToObjs(B.requests, B.request_cols).filter((r) => !r.aux && r.req_bytes);
    lineChart($("#wf2"), { series: [{ name: "request body KB", color: "#e3b341", points: reqs.map((r, i) => [i, r.req_bytes / 1024]), area: true }, { name: "harness time between requests ms", color: "#f0883e", points: mturns.map((t, i) => [i, t.harness_ms || 0]) }], yLabel: "KB / ms", xUnit: "", valFmt: (v) => v.toFixed(1) });
    $("#wf2").insertAdjacentHTML("beforeend", `<div class="legend"><span><i style="background:#e3b341"></i>request body KB (whole conversation re-sent)</span><span><i style="background:#f0883e"></i>harness-side time between model calls (tools + overhead)</span></div><p class="note">x = request index. Harness-agnostic: computed from the mock's log.</p>`);
  }

  // ---- tables
  const tools = rowsToObjs(B.tools, B.tool_cols);
  if (tools.length) table($("#toolTbl"), tools, [{ k: "t0", label: "t (s)", fmt: (v) => v?.toFixed(3) }, { k: "tool", l: 1 }, { k: "dur_ms", label: "ms", fmt: (v) => v?.toFixed(2) }, { k: "is_error", label: "err", fmt: (v) => (v ? "✗" : "") }, { k: "child_procs", label: "children" }, { k: "child_cpu_ms", label: "child CPU ms" }, { k: "child_peak_rss_kb", label: "child peak kB" }, { k: "result_chars", label: "result B" }, { k: "truncated", label: "trunc", fmt: (v) => (v ? "yes" : "") }, { k: "args", l: 1, wrap: 1, fmt: (v) => (v || "").slice(0, 160) }, { k: "result_head", label: "result", l: 1, wrap: 1, fmt: (v) => (v || "").slice(0, 140) }], { maxh: 360, onRow: (r) => { ST.zoom.d = [Math.max(0, r.t0 - 0.05), (r.t1 || r.t0) + 0.05]; pageRun(h, run); } });
  else $("#toolTbl").innerHTML = '<p class="note">no tool calls with in-process timing in this run</p>';
  const reqs = rowsToObjs(B.requests, B.request_cols);
  table($("#reqTbl"), reqs, [{ k: "t", label: "t (s)", fmt: (v) => v?.toFixed(3) }, { k: "proto", l: 1 }, { k: "step" }, { k: "aux", fmt: (v) => (v ? "aux" : "") }, { k: "summary", fmt: (v) => (v ? "summary" : "") }, { k: "req_bytes", label: "sent", fmt: fmt.bytes }, { k: "n_messages", label: "msgs" }, { k: "n_tools", label: "tools" }, { k: "tools_bytes", label: "tool schemas", fmt: fmt.bytes }, { k: "sys_bytes", label: "system", fmt: fmt.bytes }, { k: "tool_result_bytes", label: "tool results", fmt: fmt.bytes }, { k: "max_tokens", label: "max_tokens" }, { k: "stream_ms", label: "stream ms" }, { k: "injected" }, { k: "reply", l: 1, fmt: (v) => JSON.stringify(v || {}).slice(0, 120) }], { maxh: 320 });
  table($("#sysTbl"), B.syscalls.map((s) => ({ thread: s[0], name: s[1], count: s[2], total_ms: s[3] })), [{ k: "thread", l: 1 }, { k: "name", label: "syscall", l: 1 }, { k: "count" }, { k: "total_ms", label: "time in syscall ms", title: "sum of enter→exit time (includes blocking)" }], { maxh: 320 });
  const FT = (t) => t;
  table($("#fileTbl"), B.files.map((f) => ({ op: f[0], comm: f[1], type: f[2], file: (f[3] ? f[3] + "/" : "") + (f[4] || ""), bytes: f[5], ops: f[6] })), [{ k: "op", l: 1 }, { k: "comm", label: "process", l: 1 }, { k: "type", l: 1, fmt: FT }, { k: "file", l: 1, wrap: 1 }, { k: "bytes", fmt: fmt.bytes }, { k: "ops" }], { maxh: 320 });
  table($("#openTbl"), B.opens.map((o) => ({ comm: o[0], file: o[1], count: o[2] })), [{ k: "comm", label: "process", l: 1 }, { k: "file", label: `opened file (${fmt.n(B.opens_total)} opens total)`, l: 1, wrap: 1 }, { k: "count" }], { maxh: 300 });
  table($("#netTbl"), B.net_procs.map((n) => ({ comm: n[0], kind: n[1], bytes: n[2] })), [{ k: "comm", label: "process", l: 1 }, { k: "kind", l: 1 }, { k: "bytes", fmt: fmt.bytes }], { maxh: 300, search: false });
  $("#tcpTbl").innerHTML = `<b>TCP state changes</b> <span class="note">${B.tcp_events.length} events · connection table rows: ${B.tcp_conns.length}</span>`;
  table($("#tcpTbl"), B.tcp_events.map((t) => ({ t: t[0], pid: t[1], comm: t[2], from: t[3], to: t[4], local: t[5], remote: t[6] })), [{ k: "t", fmt: (v) => v?.toFixed(3) }, { k: "comm", l: 1 }, { k: "pid" }, { k: "from", l: 1 }, { k: "to", l: 1 }, { k: "local", l: 1 }, { k: "remote", l: 1 }], { maxh: 260 });
  $("#sigTbl").innerHTML = `<b>Signals</b> <span class="note">${B.signal_counts.map((s) => `${s[0]} ×${s[1]}`).join(" · ")} (SIGCHLD hidden below)</span>`;
  table($("#sigTbl"), B.signals.map((s) => ({ t: s[0], from: `${s[2]}(${s[1]})`, to: `${s[4]}(${s[3]})`, sig: s[5], result: s[6] })), [{ k: "t", fmt: (v) => v?.toFixed(3) }, { k: "sig", l: 1 }, { k: "from", l: 1 }, { k: "to", l: 1 }, { k: "result", title: "0 = delivered, 1 = ignored, 2 = already pending, 3 = overflow" }], { maxh: 260 });
  processTree($("#tree"), procs, B);
  table($("#procTbl"), procs, [{ k: "pid" }, { k: "ppid" }, { k: "uid" }, { k: "comm", l: 1 }, { k: "role", l: 1, html: (r) => `<span style="color:${roleColor(r.role)}">${esc(roleName(r.role))}</span>` }, { k: "cpu_ms", label: "CPU ms" }, { k: "runq_ms", label: "wait ms" }, { k: "rss_kb", label: "peak kB" }, { k: "threads", label: "thr" }, { k: "start", label: "start s", fmt: (v) => v?.toFixed(3) }, { k: "end", label: "end s", fmt: (v) => v?.toFixed(3) }, { k: "rchar", label: "read", fmt: fmt.bytes }, { k: "wchar", label: "wrote", fmt: fmt.bytes }, { k: "minflt", label: "minflt" }, { k: "nivcsw", label: "invol cs" }, { k: "argv", l: 1, wrap: 1, fmt: (v) => (v || "").slice(0, 120) }], { maxh: 420, sort: "cpu_ms" });
  // ---- event log
  const srcs = Array.from(new Set(B.events.map((e) => e[1])));
  const ev = $("#evlog");
  ev.innerHTML = `<div class="toolbar">${srcs.map((s) => `<label class="chk"><input type="checkbox" data-s="${s}" checked> <span class="s-${s}">${s}</span> (${B.events.filter((e) => e[1] === s).length})</label>`).join(" ")}<input type="search" placeholder="search events…" id="evq"></div><div class="evlog" id="evbox"></div>`;
  const renderEv = () => {
    const on = new Set(Array.from(ev.querySelectorAll("input[type=checkbox]")).filter((c) => c.checked).map((c) => c.dataset.s));
    const qv = ($("#evq").value || "").toLowerCase();
    const rows = B.events.filter((e) => on.has(e[1]) && (!qv || String(e[3]).toLowerCase().includes(qv)) && e[0] >= xd[0] - 0.001 && e[0] <= xd[1] + 0.001);
    $("#evbox").innerHTML = rows.slice(0, 3000).map((e) => `<div><span class="ts">${e[0]?.toFixed(3)}</span><span class="src s-${e[1]}">${e[1]}</span>${esc(e[3])}</div>`).join("") + (rows.length > 3000 ? `<p class="note">${rows.length - 3000} more — zoom the timeline or search</p>` : "");
  };
  ev.querySelectorAll("input").forEach((i) => (i.oninput = renderEv));
  renderEv();
  // ---- details
  $("#details").innerHTML = `<b>Steps</b>${B.steps.map((s) => `<div class="pill">${s.idx}. ${esc(s.kind)} ${esc(s.tag || "")} ${s.mode ? "· " + esc(s.mode) : ""} · ${s.wall_s ?? "–"} s · rc ${esc(s.rc)}</div>`).join("")}
    ${B.steps.filter((s) => s.kind === "shell" && s.detail.out).map((s) => `<p class="note">$ ${esc((s.detail.shell || "").slice(0, 200))}</p><pre class="mono" style="white-space:pre-wrap;max-height:220px;overflow:auto">${esc(s.detail.out)}</pre>`).join("")}
    ${B.sudo_log.length ? `<b>sudo log during the run</b><pre class="mono" style="white-space:pre-wrap">${esc(B.sudo_log.join("\n"))}</pre>` : ""}
    ${B.container ? `<b>Container</b><p class="note">${B.container.fresh ? "fresh container for this scenario" : "reused container"} · id ${esc((B.container.container_id || "").slice(0, 12))} · image ${esc((B.container.image || "").slice(7, 19))} · recreate ${B.container.recreate_s ?? "–"} s · eBPF attached ${B.container.bpf_attached ? "yes" : "?"}${B.container.cold ? " · VM page cache dropped (cold)" : ""}</p>` : ""}
    <b>Disk usage after the run</b><pre class="mono" style="white-space:pre-wrap;max-height:220px;overflow:auto">${esc(B.disk_usage || "")}</pre>
    <b>Processes left behind after the run</b><pre class="mono" style="white-space:pre-wrap;max-height:200px;overflow:auto">${esc(B.leftover || "")}</pre>
    <b>Raw data</b><p class="note">lab/out/${esc(h)}/runs/${esc(run)}/ — samples.jsonl, proc-events.jsonl, probe-*.jsonl, mockllm.jsonl, events-*.jsonl, meta.json</p>
    <b>Query this run</b><pre class="mono">sqlite3 lab/out/agentlab.sqlite "SELECT role, cpu_ms, n FROM v_cpu_by_role WHERE harness='${esc(h)}' AND run='${esc(run)}' ORDER BY cpu_ms DESC"
sqlite3 lab/out/agentlab.sqlite "SELECT thread, name, count FROM v_syscall_totals WHERE harness='${esc(h)}' AND run='${esc(run)}' ORDER BY count DESC LIMIT 20"</pre>`;
}

function pageOverview(h) {
  const H = ST.index.harnesses[h];
  const v = $("#view");
  v.innerHTML = `<h2 class="title">${esc(H.display)}</h2>
  <p class="desc">Each card is one load scenario run inside Docker with a mock LLM (or a real model). Every process the harness started was traced with eBPF (exact CPU, peak memory, I/O, run-queue wait), every thread sampled every 100 ms, every syscall/file/socket counted per second, and the Node agent instrumented from inside. Click a card for the full timeline.</p>
  <div class="panel"><div id="ovCpu"></div><div class="legend" id="ovLeg"></div></div>
  <div id="groups"></div>`;
  // stacked bars: CPU per scenario by role
  const runs = H.groups.flatMap((g) => g[1]);
  const roles = Array.from(new Set(runs.flatMap((r) => H.runs[r].roles.map((x) => x[0])))).sort((a, b) => roleRank(a) - roleRank(b));
  const el = $("#ovCpu");
  const W = el.clientWidth || 900, Hh = 260, m = { l: 140, r: 20, t: 10, b: 24 };
  const rowH = Math.max(16, Math.min(24, (Hh - m.t - m.b) / runs.length));
  const svg = d3.select(el).append("svg").attr("width", W).attr("height", runs.length * rowH + m.t + m.b);
  const data = runs.map((r) => { const o = { run: r }; H.runs[r].roles.forEach((x) => (o[x[0]] = x[1] || 0)); return o; });
  const stack = d3.stack().keys(roles).value((d, kk) => d[kk] || 0)(data);
  const xmax = d3.max(stack, (l) => d3.max(l, (d) => d[1])) || 1;
  const x = d3.scaleSymlog().constant(50).domain([0, xmax]).range([m.l, W - m.r]);
  const y = d3.scaleBand().domain(runs).range([m.t, m.t + runs.length * rowH]).padding(0.15);
  stack.forEach((l) => svg.append("g").selectAll("rect").data(l).join("rect").attr("x", (d) => x(d[0])).attr("y", (d) => y(d.data.run)).attr("width", (d) => Math.max(0, x(d[1]) - x(d[0]))).attr("height", y.bandwidth()).attr("fill", roleColor(l.key))
    .on("mousemove", (ev, d) => showTip(`<b>${esc(d.data.run)}</b><br>${esc(roleName(l.key))}: ${fmt.ms(d[1] - d[0])}`, ev)).on("mouseleave", hideTip));
  svg.append("g").attr("class", "axis").attr("transform", `translate(${m.l},0)`).call(d3.axisLeft(y).tickSize(0)).selectAll("text").style("cursor", "pointer").on("click", (ev, d) => (location.hash = `#/h/${h}/run/${d}`));
  svg.append("g").attr("class", "axis").attr("transform", `translate(0,${m.t + runs.length * rowH})`).call(d3.axisBottom(x).ticks(6, "~s").tickFormat((v) => fmt.ms(v)));
  $("#ovLeg").innerHTML = `<span class="note">CPU per scenario by role (symlog scale) —</span>` + roles.map((r) => `<span><i style="background:${roleColor(r)}"></i>${esc(roleName(r))}</span>`).join("");
  $("#groups").innerHTML = H.groups.map(([g, rs]) => `<h3>${esc(g)}</h3><div class="cards">${rs.map((r) => { const R = H.runs[r], k = R.kpi; return `<div class="card" onclick="location.hash='#/h/${h}/run/${r}'"><h4>${esc(r)}</h4><div class="d">${esc(R.desc)}</div>
    <div class="m"><span>wall <b>${fmt.s(k.wall_s)}</b></span><span>CPU <b>${fmt.ms(k.cpu_ms)}</b></span><span>mem <b>${fmt.mb(k.mem_max_mb)}</b></span><span>procs <b>${k.processes}</b></span></div>
    <div class="m"><span>tools <b>${k.tool_calls}</b></span><span>model calls <b>${k.model_requests}</b></span><span>syscalls <b>${fmt.n(k.syscalls)}</b></span><span>net <b>${fmt.bytes((k.net_rx || 0) + (k.net_tx || 0))}</b></span></div>${stackBar(R.roles)}</div>`; }).join("")}</div>`).join("");
}

function modeOf(argv) {
  const a = argv || "";
  const mode = /--mode json/.test(a) ? "json" : /--mode rpc/.test(a) ? "rpc" : / -p( |$)/.test(a) ? "print" : "TUI";
  const scn = (a.match(/SCENARIO: ([^']*)/) || [])[1] || "";
  return `${mode.padEnd(5)} ${scn.replace(/^stream /, "")}`.trim();
}

function pageCompare(h) {
  const C = ST.index.harnesses[h].compare;
  const v = $("#view");
  v.innerHTML = `<h2 class="title">Comparisons · ${esc(h)}</h2><p class="desc">Cross-scenario views built from exact eBPF per-process CPU.</p>
  <h3>Concurrency: N agents in parallel, identical 30-turn loops</h3><div class="grid2"><div class="panel" id="c1"></div><div class="panel" id="c1t"></div></div>
  <h3>Shared vs private config directory, 32 agents</h3><div class="panel" id="c2"></div>
  <h3>Output modes and the TUI</h3><div class="grid2"><div class="panel" id="c3"></div><div class="panel" id="c4"></div></div>
  <h3>Startup costs and measurement overhead</h3><div class="grid2"><div class="panel" id="c5"></div><div class="panel" id="c6"></div></div>`;
  const conc = (C.concurrency || []).filter((c) => !c.latency);
  if (conc.length) {
    lineChart($("#c1"), { series: [{ name: "median agent CPU ms", color: "#58a6ff", points: conc.map((c) => [c.n, c.cpu_ms_median]) }, { name: "median CPU wait ms", color: "#f85149", points: conc.map((c) => [c.n, c.runq_ms_median]) }, { name: "median wall ms", color: "#e3b341", points: conc.map((c) => [c.n, c.wall_ms_median]) }], xUnit: "", yLabel: "ms per agent vs N (agents in parallel)", valFmt: (v) => v.toFixed(0) + " ms" });
    $("#c1").insertAdjacentHTML("beforeend", `<div class="legend"><span><i style="background:#58a6ff"></i>CPU per agent</span><span><i style="background:#f85149"></i>run-queue wait per agent</span><span><i style="background:#e3b341"></i>wall per agent</span></div>`);
    table($("#c1t"), C.concurrency, [{ k: "n" }, { k: "latency", fmt: (v) => (v ? "400 ms model latency" : "none") }, { k: "cpu_ms_median", label: "CPU ms med" }, { k: "cpu_ms_max", label: "CPU ms max" }, { k: "runq_ms_median", label: "wait ms med" }, { k: "wall_ms_median", label: "wall ms med" }, { k: "rss_mb_median", label: "RSS MB med" }, { k: "container_mem_max_mb", label: "container MB" }], { search: false });
  }
  if ((C.lock || []).length) table($("#c2"), C.lock, [{ k: "private_home", label: "config dir", fmt: (v) => (v ? "private per agent" : "shared") }, { k: "n" }, { k: "cpu_ms_median", label: "CPU ms med" }, { k: "cpu_ms_p90", label: "CPU ms p90" }, { k: "runq_ms_median", label: "wait ms med" }, { k: "wall_ms_median", label: "wall ms med" }], { search: false });
  const sm = C["11-stream-modes"] || [];
  if (sm.length) { $("#c3").innerHTML = "<b>Same 40k-char answer in each mode — agent CPU</b>"; hbar($("#c3"), sm.map((p) => ({ label: modeOf(p.argv), value: p.cpu_ms || 0, sub: `${fmt.mb(p.rss_mb)} · ${fmt.s((p.life_ms || 0) / 1000)}`, color: /--mode json| -p/.test(p.argv) ? "#3fb950" : /rpc/.test(p.argv) ? "#56d4dd" : "#f0883e" })), { fmt: fmt.ms, lw: 260, vw: 190 }); $("#c3").insertAdjacentHTML("beforeend", '<p class="note">green = json/print, teal = rpc, orange = interactive TUI (real PTY). Sub-label: peak RSS · process lifetime.</p>'); }
  const tu = C["21-tui-scaling"] || [];
  if (tu.length) { $("#c4").innerHTML = "<b>TUI streaming 10k→80k chars (+resizes on 80k) vs JSON 80k</b>"; hbar($("#c4"), tu.map((p) => ({ label: modeOf(p.argv), value: p.cpu_ms || 0, sub: `${fmt.mb(p.rss_mb)} · ${fmt.s((p.life_ms || 0) / 1000)}`, color: /--mode json/.test(p.argv) ? "#3fb950" : "#f0883e" })), { fmt: fmt.ms, lw: 260, vw: 190 }); }
  const st = C["01-startup"] || [];
  if (st.length) { $("#c5").innerHTML = "<b>Every startup-scenario agent process</b>"; table($("#c5"), st, [{ k: "argv", l: 1, wrap: 1, fmt: (v) => (v || "").slice(0, 70) }, { k: "cpu_ms", label: "CPU ms" }, { k: "life_ms", label: "life ms" }, { k: "rss_mb", label: "peak MB" }], { search: false, maxh: 300 }); }
  const po = C["23-probe-overhead"] || [];
  if (po.length >= 20) {
    const a = po.slice(0, 10), b = po.slice(10, 20);
    const med = (xs, kk) => d3.median(xs, (x) => x[kk]);
    $("#c6").innerHTML = `<b>In-process probe overhead (eBPF-measured)</b>${kpis([["CPU with probe", fmt.ms(med(a, "cpu_ms"))], ["CPU without", fmt.ms(med(b, "cpu_ms"))], ["peak MB with", fmt.mb(med(a, "rss_mb"))], ["peak MB without", fmt.mb(med(b, "rss_mb"))], ["life with", fmt.ms(med(a, "life_ms"))], ["life without", fmt.ms(med(b, "life_ms"))]])}<p class="note">10 identical 20-turn runs each; medians.</p>`;
  }
}

function pagePredictions() {
  const P = ST.index.predictions || [];
  $("#view").innerHTML = `<h2 class="title">Predictions scorecard</h2><p class="desc">Predictions from reading pi's source (docs/ARCHITECTURE.md §10), checked against measurements.</p><div class="panel" id="pt"></div>`;
  if (!P.length) { $("#pt").innerHTML = '<p class="note">predictions.json not written yet</p>'; return; }
  table($("#pt"), P, [{ k: "id", label: "#" }, { k: "prediction", l: 1, wrap: 1 }, { k: "verdict", l: 1, html: (r) => `<span class="badge ${esc(r.verdict)}">${esc(r.verdict)}</span>` }, { k: "evidence", l: 1, wrap: 1 }, { k: "run", l: 1, html: (r) => (r.run ? `<a href="#/h/${esc(r.harness || "pi")}/run/${esc(r.run)}">${esc(r.run)}</a>` : "") }], { search: false });
}

async function pageDoc(name) {
  const v = $("#view");
  try {
    const r = await fetch(`data/docs/${name}`, { cache: "no-cache" });
    if (!r.ok) throw new Error(r.status);
    v.innerHTML = `<div class="md">${marked.parse(await r.text())}</div>`;
  } catch (e) {
    v.innerHTML = `<p class="note">${esc(name)} not available yet (${esc(e.message)})</p>`;
  }
}

function pageHarnesses() {
  const I = ST.index;
  const hs = Object.keys(I.harnesses);
  const runs = Array.from(new Set(hs.flatMap((h) => Object.keys(I.harnesses[h].runs)))).sort();
  $("#view").innerHTML = `<h2 class="title">Harnesses side by side</h2><p class="desc">Same scenarios, different agent harnesses (adapters in lab/harnesses/). Cells: agent-container CPU · peak memory · processes.</p><div class="panel" id="hx"></div>`;
  const rows = runs.map((r) => { const o = { run: r }; hs.forEach((h) => { const R = I.harnesses[h].runs[r]; o[h] = R ? `${fmt.ms(R.kpi.cpu_ms)} · ${fmt.mb(R.kpi.mem_max_mb)} · ${R.kpi.processes}p` : "—"; }); return o; });
  table($("#hx"), rows, [{ k: "run", l: 1 }, ...hs.map((h) => ({ k: h, l: 1, label: I.harnesses[h].display }))], { search: false });
}

// ------------------------------------------------------------------ nav + router
function renderNav() {
  const h = ST.harness;
  const H = ST.index.harnesses[h];
  const cur = location.hash;
  const item = (href, label, k) => `<a class="item ${cur === href ? "on" : ""}" href="${href}">${esc(label)}${k ? `<span class="k">${esc(k)}</span>` : ""}</a>`;
  $("#nav").innerHTML = item(`#/h/${h}/overview`, "Overview") + item(`#/h/${h}/compare`, "Comparisons") + item("#/predictions", "Predictions scorecard") + item("#/harnesses", "Harnesses side by side") +
    H.groups.map(([g, rs]) => `<div class="grp">${esc(g)}</div>` + rs.map((r) => item(`#/h/${h}/run/${r}`, r, fmt.ms(H.runs[r].kpi.cpu_ms))).join("")).join("") +
    `<div class="grp">Docs</div>` + item("#/doc/REPORT.md", "Report") + item("#/doc/ARCHITECTURE.md", "pi architecture") + item("#/doc/RESEARCH.md", "Existing tools survey") + item("#/doc/DATA.md", "Data & SQL guide") + item("#/doc/README.md", "Lab README");
}

async function route() {
  hideTip();
  const parts = location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  if (parts[0] === "h" && ST.index.harnesses[parts[1]]) {
    if (ST.harness !== parts[1]) { ST.harness = parts[1]; $("#harnessSel").value = parts[1]; }
  }
  renderNav();
  window.scrollTo(0, 0);
  try {
    if (parts[0] === "h" && parts[2] === "run") await pageRun(parts[1], parts[3]);
    else if (parts[0] === "h" && parts[2] === "compare") pageCompare(parts[1]);
    else if (parts[0] === "predictions") pagePredictions();
    else if (parts[0] === "harnesses") pageHarnesses();
    else if (parts[0] === "doc") await pageDoc(parts[1]);
    else pageOverview(ST.harness);
  } catch (e) {
    console.error(e);
    $("#view").innerHTML = `<p class="note">error: ${esc(e.message)}</p><pre class="mono">${esc(e.stack || "")}</pre>`;
  }
}

(async function main() {
  ST.index = await getJSON("data/index.json");
  ROLE_INFO = ST.index.role_info || {};
  const hs = Object.keys(ST.index.harnesses);
  ST.harness = hs.includes("pi") ? "pi" : hs[0];
  $("#harnessSel").innerHTML = hs.map((h) => `<option value="${esc(h)}">${esc(ST.index.harnesses[h].display)}</option>`).join("");
  $("#harnessSel").value = ST.harness;
  $("#harnessSel").onchange = (e) => { ST.harness = e.target.value; location.hash = `#/h/${ST.harness}/overview`; };
  addEventListener("hashchange", route);
  let rt;
  addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(route, 250); });
  if (!location.hash) location.hash = `#/h/${ST.harness}/overview`;
  else route();
})();
