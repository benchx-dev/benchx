// The benchx dashboard: a static page over the JSON views written by `bx view`.
// No query engine runs here; every number shown was computed ahead of time.
// Routes: #/ overview, #/suites, #/s/<suite>, #/b/<suite>/<case>?params=&q=&node=,
// #/c/<sha>, #/about. Names, links and the upstream come from data/project.json.
// Data is fetched with cache-busting because static CDNs may cache for hours.

const DATA = "data/";
const SERIES_VARS = ["--series-1", "--series-2", "--series-3", "--series-4", "--series-5", "--series-6", "--series-7", "--series-8"];
const PAGE = 48;
// The benchx mark (logo.svg), inline so the legs follow the page's ink in
// both themes; the small variant (favicon.svg) is drawn heavier for 16-32 px.
const MARK = '<svg viewBox="0 0 64 64" aria-hidden="true" class="mark"><rect x="8" y="10" width="48" height="8" rx="2" fill="var(--brand)"/><path d="M18 24 L46 52 M46 24 L18 52" stroke="currentColor" stroke-width="7" stroke-linecap="round"/></svg>';
const MARK_SMALL = '<svg viewBox="0 0 64 64" aria-hidden="true" class="mark"><rect x="8" y="8" width="48" height="8" rx="2" fill="var(--brand)"/><path d="M18 26 L46 52 M46 26 L18 52" stroke="currentColor" stroke-width="8" stroke-linecap="round"/></svg>';
function wordmark(small) { return h("span", { class: "benchx" }, h("span", { html: small ? MARK_SMALL : MARK }), h("span", { class: "wm" }, "bench", h("b", {}, "x"))); }
const state = { v: "", project: null, manifest: null, landing: null, series: null, sparks: null, histories: new Map(), commits: new Map(), charts: [], cleanup: [], nodeSlots: new Map() };

// ---- utilities ------------------------------------------------------------------

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;            // only for trusted markup written in this file
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

function joinParts(parts, sep = " · ") {
  const out = [];
  for (const p of parts) {
    if (p == null || p === false || p === "") continue;
    if (out.length) out.push(sep);
    out.push(p);
  }
  return out;
}

async function getJSON(path, { fresh = false } = {}) {
  const url = DATA + path + (fresh ? `?t=${Date.now()}` : `?v=${encodeURIComponent(state.v)}`);
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

const UNITS = {
  s: [["ns", 1e-9], ["µs", 1e-6], ["ms", 1e-3], ["s", 1]],
  B: [["B", 1], ["KiB", 1024], ["MiB", 1024 ** 2], ["GiB", 1024 ** 3]],
};
function pickScale(values, unit) {
  const table = UNITS[unit];
  const m = Math.max(...values.filter((v) => v != null && isFinite(v)).map(Math.abs), 0);
  if (!table || !m) return [unit, 1];
  let best = table[0];
  for (const t of table) if (m >= t[1]) best = t;
  return best;
}
function fmtNum(v, digits = 3) {
  if (v == null || !isFinite(v)) return "–";
  const a = Math.abs(v);
  if (a >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (a >= 100) return v.toFixed(digits - 3 < 0 ? 0 : digits - 3);
  if (a >= 10) return v.toFixed(digits - 2);
  return v.toPrecision(digits);
}
function fmtValue(v, unit, scale) {
  if (v == null) return "–";
  const [label, factor] = scale || pickScale([v], unit);
  return `${fmtNum(v / factor)} ${label}`;
}
function fmtPct(x, digits = 1) {
  if (x == null || !isFinite(x)) return "–";
  const s = (x * 100).toFixed(digits);
  return (x > 0 ? "+" : "") + s + "%";
}
function fmtAbsPct(x, digits = 1) { return x == null || !isFinite(x) ? "–" : (Math.abs(x) * 100).toFixed(digits) + "%"; }
function parseDate(s) {
  if (!s) return null;
  const iso = s.includes("T") ? s : s.replace(" ", "T") + (s.endsWith("Z") || /[+-]\d\d:\d\d$/.test(s) ? "" : "Z");
  const d = new Date(iso);
  return isNaN(d) ? null : d;
}
function fmtDate(s) {
  const d = parseDate(s);
  return d ? d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "–";
}
function ago(s) {
  const d = parseDate(s);
  if (!d) return "";
  const m = Math.round((Date.now() - d) / 60000);
  if (m < 60) return `${m} min ago`;
  if (m < 48 * 60) return `${Math.round(m / 60)} h ago`;
  return `${Math.round(m / 1440)} d ago`;
}
function shortSuite(s) {
  let out = s.replace(/-benchmark$/g, "");
  const prefix = state.project?.name ? `${state.project.name}-` : "";
  if (prefix && out.startsWith(prefix)) out = out.slice(prefix.length);
  return out;
}
function safeFile(s) { return String(s).replace(/\//g, "_"); }
function upstreamCommit(sha) {
  if (!sha || sha.includes("-dirty-")) return null;
  const tpl = state.project?.upstream?.commit_url;
  return tpl ? tpl.replace("{sha}", sha) : null;
}
function runFiles(sha, node) {
  if (state.manifest?.source !== "runs") return null;
  const repo = state.project?.repository;
  return repo ? `${repo}/tree/main/runs/${sha}/${node}` : null;
}
function runLog(sha, node, hasLog = true) {
  return hasLog === false ? null : `data/logs/${encodeURIComponent(safeFile(sha))}/${encodeURIComponent(node)}.txt`;
}
function multiNode() { return (state.landing?.stats.nodes || []).length > 1; }

// A change is judged three ways: its direction (the quantity says whether up is
// worse), its size (under 0.5 % is flat), and its size against the series' noise
// (under twice the noise it is shown muted: observed, but not distinguishable
// from run-to-run variation). None of it is a verdict; see the about page.
const FLAT = 0.005;
function verdict(rel, direction) {
  if (rel == null || !isFinite(rel)) return "none";
  if (Math.abs(rel) < FLAT) return "flat";
  const worse = direction === "higher-is-better" ? rel < 0 : rel > 0;
  return worse ? "regressed" : "improved";
}
function changeWords(direction, unit) {
  if (unit === "s" && direction !== "higher-is-better") return { worse: "slower", better: "faster" };
  return { worse: "worse", better: "better" };
}
function withinNoise(rel, noise) { return noise != null && rel != null && Math.abs(rel) < 2 * noise; }
// delta(rel, direction, {noise, unit}): noise undefined means "not known here",
// null means "the series has too few points for one".
function delta(rel, direction, opts = {}) {
  const v = verdict(rel, direction);
  const real = v === "regressed" || v === "improved";
  const within = real && withinNoise(rel, opts.noise);
  const w = changeWords(direction, opts.unit);
  const glyph = v === "none" ? "" : v === "flat" ? "■" : rel > 0 ? "▲" : "▼";
  const word = { regressed: w.worse, improved: w.better, flat: "no change", none: "no comparison" }[v];
  let title = v === "none" ? word : `${fmtPct(rel, 2)}: ${word}`;
  const unknown = real && opts.noise === null;
  if (real && opts.noise != null) title += within ? ` · smaller than twice the series' usual commit-to-commit movement (${fmtAbsPct(opts.noise)}), which changes of this size are common within` : ` · ${(Math.abs(rel) / opts.noise).toFixed(1)}× the series' usual commit-to-commit movement (${fmtAbsPct(opts.noise)})`;
  else if (unknown) title += " · no noise estimate yet (fewer than 3 measurements): unconfirmed";
  return h("span", { class: `delta num ${v}${within ? " within" : ""}${unknown ? " unknown" : ""}`, title, "aria-label": title },
    glyph && h("span", { class: "glyph", "aria-hidden": "true" }, glyph), fmtPct(rel, opts.digits ?? 1));
}
function benchHref(s, { node } = {}) {
  const q = new URLSearchParams();
  if (s.params) q.set("params", s.params);
  if (s.quantity && s.quantity !== "wall-time") q.set("q", s.quantity);
  if (node && multiNode()) q.set("node", node);
  const qs = q.toString();
  return `#/b/${encodeURIComponent(s.suite)}/${encodeURIComponent(s.case)}${qs ? "?" + qs : ""}`;
}
function caseLabel(s) { return s.params ? `${s.case} ${s.params}` : s.case; }
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function nodeColor(node) {
  if (!state.nodeSlots.has(node)) state.nodeSlots.set(node, state.nodeSlots.size % SERIES_VARS.length);
  return cssVar(SERIES_VARS[state.nodeSlots.get(node)]);
}
function table(headers, rows, opts = {}) {
  return h("div", { class: "wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, headers.map((x) => h("th", { class: x.startsWith(">") ? "r" : "" }, x.replace(/^>/, ""))))),
    h("tbody", {}, rows.length ? rows.map((cells) => h("tr", {}, cells.map((c, i) => h("td", { class: headers[i].startsWith(">") ? "r num" : "" }, c))))
      : h("tr", {}, h("td", { colspan: headers.length, class: "empty" }, opts.empty || "nothing yet")))));
}
function tile(label, value, hint, small) {
  return h("div", { class: "tile" }, h("div", { class: "label" }, label), h("div", { class: `value ${small ? "small" : ""}` }, value), hint && h("div", { class: "hint" }, hint));
}
function chip(label, value, tone) {
  return h("span", { class: `chip ${tone || ""}` }, h("b", { class: "num" }, value), " ", label);
}
function failTag(reason, text = "failed") { return h("span", { class: "tag failed", title: reason || "" }, text); }
function gapTag(ch) {
  if (ch.prev_is_parent) return null;
  const title = `The baseline ${ch.prev_sha.slice(0, 12)} is not this commit's parent: the commits in between were not measured for this series, so this change may belong to any of them.`;
  return h("span", { class: "tag gap", title }, "not parent");
}
function stateTag(c) {
  if (c.state === "finished") return null;
  return h("span", { class: `tag ${c.state === "failed" ? "failed" : ""}` }, c.state === "failed" ? "run failed" : c.state);
}
function sharedLegend(nodes) {
  return nodes.length > 1 ? h("span", { class: "legend" }, nodes.map((n) => h("span", { class: "legend-key" }, h("span", { class: "key", style: `border-color:${nodeColor(n)}` }), n))) : null;
}

// ---- benchmark cards (overview and suite pages) ------------------------------------

// groups: one card per (suite, case, params, quantity); its nodes are lines on one chart.
function groupSeries(list, extra = () => ({})) {
  const groups = new Map();
  for (const s of list) {
    const key = `${s.suite}|${s.case}|${s.params}|${s.quantity}`;
    if (!groups.has(key)) groups.set(key, { suite: s.suite, case: s.case, params: s.params, quantity: s.quantity, unit: s.unit, direction: s.direction, list: [] });
    groups.get(key).list.push(s);
  }
  return [...groups.values()].map((g) => ({ ...g, ...extra(g) }));
}

function benchCard(g, { showSuite = true } = {}) {
  const n = g.newest;
  const failed = g.status && g.status !== "success";
  const plot = h("div", { class: "spark" });
  const sub = [g.quantity !== "wall-time" ? g.quantity : null, showSuite ? shortSuite(g.suite) : null, n && multiNode() ? n.node : null].filter(Boolean).join(" · ");
  const points = n?.points ?? 0;
  const card = h("a", { class: `mini${failed ? " failed" : ""}`, href: benchHref(g, { node: n?.node }) },
    h("div", { class: "mini-head" },
      h("span", { class: "mono name", title: caseLabel(g) }, caseLabel(g)),
      n ? delta(g.change, g.direction, { noise: n.noise, unit: g.unit }) : null),
    h("div", { class: "mini-value" }, h("span", { class: "num" }, n ? fmtValue(n.estimate ?? n.latest, g.unit) : "–"), sub && h("span", { class: "note" }, sub)),
    plot,
    h("div", { class: "mini-foot" },
      failed ? failTag(g.reason, "failed last run") : h("span", {}, points === 1 ? "1 measurement" : `${points} measurements`),
      h("span", {}, n?.noise != null ? `noise ${fmtAbsPct(n.noise)}` : points ? "noise: needs 3+" : "")));
  return [card, plot];
}

function drawSpark(el, seriesList, axis) {
  const index = new Map(axis.map((c, i) => [c.commit_order, i]));
  const xs = axis.map((_, i) => i);
  const ys = seriesList.map((s) => { const col = new Array(xs.length).fill(null); s.orders.forEach((o, k) => { if (s.estimates[k] != null && index.has(o)) col[index.get(o)] = s.estimates[k]; }); return col; });
  const vals = ys.flat().filter((v) => v != null);
  if (!vals.length) { el.append(h("div", { class: "spark-empty" }, "no successful measurement")); return; }
  if (typeof uPlot === "undefined") { el.append(h("div", { class: "spark-empty" }, "chart unavailable offline")); return; }
  // At least ±3 % of the middle stays visible, so a 0.5 % wiggle looks flat
  // instead of filling the card the way a 50 % jump does.
  const lo = Math.min(...vals), hi = Math.max(...vals), mid = (lo + hi) / 2;
  const half = Math.max((hi - lo) * 0.6, Math.abs(mid) * 0.03, 1e-12);
  const width = Math.max(120, el.clientWidth || 240);
  const chart = new uPlot({
    width, height: 52, legend: { show: false }, cursor: { show: false },
    scales: { x: { time: false, range: [-0.5, xs.length - 0.5] }, y: { range: [mid - half, mid + half] } },
    axes: [{ show: false }, { show: false }],
    series: [{}, ...seriesList.map((s) => ({ stroke: nodeColor(s.node), width: 1.5, spanGaps: true, points: { show: xs.length <= 12, size: 5, stroke: nodeColor(s.node), fill: cssVar("--surface"), width: 1.5 } }))],
  }, [xs, ...ys], el);
  state.charts.push(chart);
}

function drawCards(grid, cards, axisFor) {
  requestAnimationFrame(() => { for (const [g, plot] of cards) drawSpark(plot, g.list, axisFor(g)); });
}

// ---- overview -----------------------------------------------------------------------

async function renderLanding() {
  const L = state.landing;
  if (!L.stats.results) return renderEmpty();
  if (!state.sparks) state.sparks = await getJSON("views/sparks.json");
  const S = state.sparks;
  // sparks.json holds successful points only; a series that never succeeded
  // still gets a card (an empty chart and its failure) from series.json.
  const sparked = new Set(S.series.map((s) => `${s.suite}|${s.case}|${s.params}|${s.quantity}|${s.node}`));
  const never = state.series.filter((s) => !s.points && !sparked.has(`${s.suite}|${s.case}|${s.params}|${s.quantity}|${s.node}`))
    .map((s) => ({ ...s, orders: [], estimates: [], latest: null }));
  const all = groupSeries([...S.series, ...never], (g) => {
    const newest = g.list.filter((s) => s.latest != null).sort((a, b) => b.orders[b.orders.length - 1] - a.orders[a.orders.length - 1])[0];
    // Status, value and link all describe one node: the newest one's, or the failing one when none succeeded.
    const failing = g.list.find((s) => s.status && s.status !== "success" && (!newest || s.node === newest.node));
    return { newest, change: newest?.vs_prev ?? null, noise: newest?.noise ?? null, v: newest ? verdict(newest.vs_prev, g.direction) : "none",
      status: failing ? failing.status : "success", reason: failing?.reason };
  });
  const suites = [...new Set(all.map((g) => g.suite))].sort();
  const nodes = [...new Set(S.series.map((s) => s.node))].sort();
  const q = new URLSearchParams((location.hash.split("?")[1]) || "");
  const ctl = { suite: q.get("suite") || "all", sort: q.get("sort") || "change", text: q.get("f") || "", n: PAGE };
  const setHash = () => { const p = new URLSearchParams(); if (ctl.suite !== "all") p.set("suite", ctl.suite); if (ctl.sort !== "change") p.set("sort", ctl.sort); if (ctl.text) p.set("f", ctl.text); history.replaceState(null, "", "#/" + (p.toString() ? "?" + p : "")); };
  // Missing comparisons sort last; changes within noise rank below real ones of the same size.
  const size = (g) => (g.change == null ? -1 : Math.abs(g.change) * (withinNoise(g.change, g.noise) ? 0.5 : 1));
  const sorters = {
    change: (a, b) => size(b) - size(a),
    regressed: (a, b) => (b.v === "regressed") - (a.v === "regressed") || size(b) - size(a),
    improved: (a, b) => (b.v === "improved") - (a.v === "improved") || size(b) - size(a),
    failing: (a, b) => (b.status !== "success") - (a.status !== "success") || size(b) - size(a),
    noise: (a, b) => (b.noise ?? -1) - (a.noise ?? -1),
    name: (a, b) => a.suite.localeCompare(b.suite) || a.case.localeCompare(b.case) || a.params.localeCompare(b.params),
  };
  const grid = h("div", { class: "grid" });
  const status = h("span", { class: "note" });
  const more = h("button", { class: "ghost", onclick: () => { ctl.n += PAGE; draw(); } }, `show ${PAGE} more`);
  const draw = () => {
    const needle = ctl.text.trim().toLowerCase();
    const rows = all.filter((g) => (ctl.suite === "all" || g.suite === ctl.suite) && (!needle || `${g.suite} ${g.case} ${g.params}`.toLowerCase().includes(needle))).sort(sorters[ctl.sort] || sorters.change);
    destroyCharts();
    grid.replaceChildren();
    const shown = rows.slice(0, ctl.n), cards = [];
    for (const g of shown) { const [card, plot] = benchCard(g); grid.append(card); cards.push([g, plot]); }
    if (!shown.length) grid.append(h("p", { class: "empty" }, "no benchmark matches"));
    status.textContent = `${shown.length} of ${rows.length} benchmarks`;
    more.hidden = shown.length >= rows.length;
    drawCards(grid, cards, () => S.axis);
    setHash();
  };
  const select = (label, key, values, fmt = (x) => x) => h("label", {}, label, h("select", { onchange: (e) => { ctl[key] = e.target.value; ctl.n = PAGE; draw(); } }, values.map((v) => h("option", { value: v, selected: v === ctl[key] }, fmt(v)))));
  const filter = h("input", { type: "search", placeholder: "filter by name", value: ctl.text, "aria-label": "filter benchmarks", oninput: (e) => { ctl.text = e.target.value; ctl.n = PAGE; draw(); } });
  const st = L.stats;
  const main = h("main", {},
    problemsBanner(),
    ...L.latest_runs.map((run) => latestRun(run, L)),
    h("p", { class: "inventory note" },
      `${st.commits} commits measured · ${st.series} series · ${st.results.toLocaleString()} results · `,
      st.nodes.length === 1 ? `node ${st.nodes[0]}` : `nodes ${st.nodes.join(", ")}`, " · ", h("a", { href: "#/about" }, "how to read this page")),
    h("h2", { id: "all" }, "All benchmarks"),
    h("div", { class: "filters" },
      select("suite", "suite", ["all", ...suites], (v) => (v === "all" ? "all suites" : v)),
      select("sort", "sort", Object.keys(sorters), (v) => ({ change: "largest change", regressed: "regressed first", improved: "improved first", failing: "failing first", noise: "noisiest", name: "name" })[v]),
      filter, status, sharedLegend(nodes)),
    h("p", { class: "note" }, `Each chart shows the last ${S.axis.length} measured commits. The change compares the newest measurement with the one before it; muted changes are within twice the series' noise.`),
    grid,
    h("div", { class: "more" }, more),
    h("h2", {}, "Recent commits"),
    commitsTable(L.recent_commits, L.notable),
    h("div", { class: "cols" },
      h("section", { class: "card" }, h("h3", {}, "Noisiest series"),
        h("p", { class: "note" }, "Median change between consecutive measurements, over the last 21. A change on these needs more evidence."),
        table(["benchmark", "node", ">noise", ">points"], L.noisiest.map((s) => [h("a", { href: benchHref(s, { node: s.node }) }, caseLabel(s)), s.node, fmtAbsPct(s.noise), s.points]), { empty: "needs at least three measurements per series" })),
      h("section", { class: "card" }, h("h3", {}, "Suite wall time on the latest commit"),
        table(["suite", "node", ">cases", ">failed", ">time"], L.suite_time.map((s) => [h("a", { href: `#/s/${encodeURIComponent(s.suite)}` }, s.suite), s.node, s.cases, s.failed ? h("span", { class: "bad" }, s.failed) : "0", s.seconds < 120 ? `${s.seconds} s` : `${Math.round(s.seconds / 60)} min`])))),
    footer());
  draw();
  return main;
}

// The first thing on the page: what the newest run on a node found.
function latestRun(run, L) {
  const pct = `${Math.round(L.notable * 100)}%`;
  const recent = L.last_executed?.[run.node];
  const other = recent && recent.sha !== run.sha ? recent : null;
  const mine = (list) => list.filter((m) => m.node === run.node && m.sha === run.sha).slice(0, 5);
  const regressed = mine(L.movers.regressed), improved = mine(L.movers.improved);
  const failures = L.failures.filter((f) => f.node === run.node);
  const moverList = (title, list, tone) => h("div", { class: "movers" },
    h("h3", {}, title),
    list.length ? h("ol", {}, list.map((m) => h("li", {},
      h("a", { href: benchHref(m, { node: m.node }), class: "mono name", title: `${caseLabel(m)} (${m.suite})` }, caseLabel(m)),
      h("span", { class: "num from-to note" }, `${fmtValue(m.prev_estimate, m.unit)} → ${fmtValue(m.estimate, m.unit)}`),
      h("span", { class: "delta-cell" }, m.prev_is_parent ? null : gapTag(m), delta(m.rel_change, m.direction, { noise: m.noise, unit: m.unit })))))
      : h("p", { class: "note" }, !run.compared ? "no earlier measurement of these benchmarks to compare with"
        : tone === "bad" ? `nothing got worse by more than 0.5% (${run.compared} compared)` : `nothing got better by more than 0.5% (${run.compared} compared)`));
  const health = run.state !== "finished" ? "bad" : run.failed_results || run.suites_failed ? "warn" : "good";
  const upUrl = upstreamCommit(run.sha);
  const logUrl = runLog(run.sha, run.node, run.has_log);
  const metaParts = joinParts([
    run.committer_date ? `committed ${fmtDate(run.committer_date)}` : null,
    upUrl ? h("a", { href: upUrl, target: "_blank", rel: "noopener" }, "upstream commit") : null,
    logUrl ? h("a", { href: logUrl, target: "_blank", rel: "noopener" }, "run log") : null,
  ]);
  return h("section", { class: `hero ${health}` },
    h("div", { class: "hero-head" },
      h("div", {},
        h("div", { class: "eyebrow" }, `Newest commit measured on ${run.node} · run ${ago(run.finished_at || run.started_at) || fmtDate(run.committer_date)}`),
        h("h2", {}, h("a", { href: `#/c/${encodeURIComponent(run.sha)}`, class: "mono" }, run.short), run.subject ? ` ${run.subject}` : ""),
        metaParts.length ? h("div", { class: "note" }, ...metaParts) : null),
      h("a", { class: "button", href: `#/c/${encodeURIComponent(run.sha)}` }, "All changes in this commit →")),
    other ? h("div", { class: `banner ${other.state === "finished" && !other.failed_results ? "" : "bad"}` }, `Most recent run on ${run.node}: `,
      h("a", { href: `#/c/${encodeURIComponent(other.sha)}`, class: "mono" }, other.short), ` (an older commit, ${other.reason || "requested"}) finished ${ago(other.finished_at || other.started_at)}: `,
      other.state === "finished" ? `${other.results} results, ${other.failed_results} failed.` : h("b", {}, other.state)) : null,
    h("div", { class: "chips" },
      chip("results", run.results.toLocaleString()),
      chip("failed", run.failed_results, run.failed_results ? "bad" : "good"),
      chip(`regressed > ${pct}`, run.regressed, run.regressed ? "bad" : ""),
      chip(`improved > ${pct}`, run.improved, run.improved ? "good" : ""),
      chip("compared with an earlier run", run.compared),
      run.state !== "finished" ? chip("", run.state, "bad") : null),
    h("div", { class: "cols tight" }, moverList("Largest regressions", regressed, "bad"), moverList("Largest improvements", improved, "good")),
    failures.length ? h("details", { class: "failures", open: failures.length <= 6 },
      h("summary", {}, h("span", { class: "bad" }, `${failures.length} benchmark${failures.length > 1 ? "s" : ""} failed`), " on this run"),
      h("ul", {}, failures.map((f) => h("li", {}, h("a", { href: benchHref(f, { node: f.node }), class: "mono" }, caseLabel(f)), " ", h("span", { class: "note" }, shortSuite(f.suite)), " ",
        h("code", { class: "reason" }, f.reason || f.status), f.last_success_sha ? null : h("span", { class: "note" }, " · never succeeded")))))
      : null);
}

function commitsTable(list, notable) {
  const pct = `${Math.round(notable * 100)}%`;
  return table(["commit", "subject", "date", "node", ">results", ">failed", `>regressed > ${pct}`, `>improved > ${pct}`],
    list.map((c) => [
      h("a", { href: `#/c/${encodeURIComponent(c.sha)}`, class: "mono" }, c.short),
      h("span", {}, c.subject.length > 90 ? c.subject.slice(0, 88) + "…" : c.subject, " ", stateTag(c)),
      fmtDate(c.committer_date), c.node, c.results,
      c.failed_results ? h("span", { class: "bad" }, c.failed_results) : "0",
      c.compared ? (c.regressed ? h("span", { class: "delta regressed" }, c.regressed) : "0") : "–",
      c.compared ? (c.improved ? h("span", { class: "delta improved" }, c.improved) : "0") : "–"]),
    { empty: "no runs filed yet" });
}

function problemsBanner() {
  const n = state.manifest.counts.ingest_problems || 0;
  return n ? h("div", { class: "banner bad" }, `${n} result${n > 1 ? "s or run directories were" : " or run directory was"} rejected by the ingest and ${n > 1 ? "are" : "is"} missing from this page.`) : null;
}

// No results yet: say what state the project is in and what to do next.
function renderEmpty() {
  const L = state.landing, repo = state.project.repository, isRuns = state.manifest?.source === "runs";
  return h("main", {},
    problemsBanner(),
    h("section", { class: "hero" },
      h("div", { class: "eyebrow" }, "No results yet"),
      h("h2", {}, L.recent_commits.length ? "Runs are filed, but none produced a result" : "Waiting for the first run"),
      h("p", {}, L.recent_commits.length
        ? "The runs below have no successful measurements. Open a run to inspect its status and log."
        : isRuns
          ? "This page fills in when benchmark runs are filed under runs/. Run a benchmark and rebuild the site with bx view."
          : "This store has no benchmark measurements yet. Measure a target with bx bench, then run bx view:"),
      L.recent_commits.length ? null : h("pre", {}, isRuns
        ? "bx view --runs runs --out site"
        : "bx bench path/to/benchmarks.py\nbx view"),
      repo ? h("p", {}, h("a", { href: repo }, "Project repository")) : null),
    L.recent_commits.length ? [h("h2", {}, "Runs"), commitsTable(L.recent_commits, L.notable)] : null,
    footer());
}

// ---- suites -------------------------------------------------------------------------

function renderSuites() {
  const by = new Map();
  for (const s of state.series) {
    const e = by.get(s.suite) || { suite: s.suite, series: 0, cases: new Set(), nodes: new Set(), regressed: 0, improved: 0, failing: 0 };
    e.series++; e.cases.add(`${s.case}|${s.params}`); e.nodes.add(s.node);
    if (s.status && s.status !== "success") e.failing++;
    else if (s.vs_prev != null && Math.abs(s.vs_prev) > state.landing.notable) {
      const v = verdict(s.vs_prev, s.direction);
      if (v === "regressed") e.regressed++; else if (v === "improved") e.improved++;
    }
    by.set(s.suite, e);
  }
  const rows = [...by.values()].sort((a, b) => b.failing - a.failing || b.regressed - a.regressed || a.suite.localeCompare(b.suite));
  const pct = `${Math.round(state.landing.notable * 100)}%`;
  return h("main", {},
    h("nav", { class: "crumbs" }, h("a", { href: "#/" }, "overview"), " / suites"),
    h("h2", {}, "Suites"),
    h("p", { class: "lead" }, `${rows.length} benchmark suite${rows.length === 1 ? "" : "s"}. Regressed and improved count series whose latest change exceeds ${pct}; failing counts series whose latest attempt did not succeed.`),
    table(["suite", ">benchmarks", ">series", "nodes", ">failing", ">regressed", ">improved"], rows.map((e) => [
      h("a", { href: `#/s/${encodeURIComponent(e.suite)}` }, e.suite), e.cases.size, e.series, [...e.nodes].sort().join(", "),
      e.failing ? h("span", { class: "bad" }, e.failing) : "0",
      e.regressed ? h("span", { class: "delta regressed" }, e.regressed) : "0", e.improved ? h("span", { class: "delta improved" }, e.improved) : "0"])),
    footer());
}

async function loadHistory(suite) {
  let hist = state.histories.get(suite);
  if (!hist) { hist = await getJSON(`views/history/${encodeURIComponent(safeFile(suite))}.json`); state.histories.set(suite, hist); }
  return hist;
}

async function renderSuite(suite) {
  const rows = state.series.filter((s) => s.suite === suite);
  const nodes = [...new Set(rows.map((r) => r.node))].sort();
  const hist = await loadHistory(suite);
  const latestBy = new Map(rows.map((r) => [`${r.case}|${r.params}|${r.quantity}|${r.node}`, r]));
  const groups = groupSeries(Object.values(hist.series).map((s) => ({ ...s, suite })), (g) => {
    const mine = g.list.map((s) => latestBy.get(`${s.case}|${s.params}|${s.quantity}|${s.node}`)).filter(Boolean);
    const newest = mine.filter((r) => r.estimate != null).sort((a, b) => b.commit_order - a.commit_order)[0];
    const failing = mine.find((r) => r.status && r.status !== "success" && (!newest || r.node === newest.node));
    return { newest, change: newest?.vs_prev ?? null, status: failing ? failing.status : "success", reason: failing?.reason };
  }).sort((a, b) => a.case.localeCompare(b.case) || a.params.localeCompare(b.params));
  const grid = h("div", { class: "grid" });
  const status = h("span", { class: "note" });
  const drawSuite = (needle = "") => {
    destroyCharts();
    grid.replaceChildren();
    const shown = groups.filter((g) => !needle || caseLabel(g).toLowerCase().includes(needle));
    const cards = shown.map((g) => { const [card, plot] = benchCard(g, { showSuite: false }); grid.append(card); return [g, plot]; });
    status.textContent = `${shown.length} of ${groups.length} benchmarks`;
    drawCards(grid, cards, () => hist.commits);
  };
  const filter = h("input", { type: "search", placeholder: "filter by name", "aria-label": "filter benchmarks", oninput: (e) => drawSuite(e.target.value.trim().toLowerCase()) });
  const failing = groups.filter((g) => g.status !== "success").length;
  const main = h("main", {},
    h("nav", { class: "crumbs" }, h("a", { href: "#/" }, "overview"), " / ", h("a", { href: "#/suites" }, "suites"), " / ", suite),
    h("h2", {}, suite),
    h("p", { class: "lead" }, `${groups.length} benchmarks over ${hist.commits.length} measured commits`, failing ? [", ", h("span", { class: "bad" }, `${failing} failing`)] : null, `. The change compares each benchmark's newest measurement with the one before it.`),
    h("div", { class: "filters" }, filter, status, sharedLegend(nodes)),
    grid,
    h("details", { class: "table-view" }, h("summary", {}, "Table view"),
      table(["benchmark", "quantity", "node", "status", ">latest", ">vs previous", ">vs 10 back", ">noise", ">points"],
        rows.map((s) => [h("a", { href: benchHref(s, { node: s.node }) }, caseLabel(s)), s.quantity, s.node,
          s.status && s.status !== "success" ? failTag(s.reason, s.status) : "ok",
          fmtValue(s.estimate, s.unit), delta(s.vs_prev, s.direction, { noise: s.noise, unit: s.unit }), delta(s.vs_10, s.direction, { unit: s.unit }), fmtAbsPct(s.noise), s.points]))),
    footer());
  drawSuite();
  return main;
}

// ---- one benchmark ----------------------------------------------------------------

async function renderBench(suite, kase, query) {
  const params = query.get("params") || "";
  const quantity = query.get("q") || "wall-time";
  const nodeFilter = query.get("node") || "all";
  const hist = await loadHistory(suite);
  const all = Object.values(hist.series).filter((s) => s.case === kase);
  const variants = [...new Set(all.map((s) => s.params))];
  const quantities = [...new Set(all.map((s) => s.quantity))];
  const nodes = [...new Set(all.map((s) => s.node))].sort();
  const shown = all.filter((s) => s.params === params && s.quantity === quantity && (nodeFilter === "all" || s.node === nodeFilter));
  const latestRows = state.series.filter((s) => s.suite === suite && s.case === kase && s.params === params && s.quantity === quantity && (nodeFilter === "all" || s.node === nodeFilter));
  const kpi = latestRows.filter((r) => r.estimate != null).sort((a, b) => b.commit_order - a.commit_order)[0];
  const failing = latestRows.filter((r) => r.status && r.status !== "success");
  const unit = shown[0]?.unit || kpi?.unit || "";
  const direction = shown[0]?.direction || kpi?.direction || "";
  const opts = { unit };

  const setParam = (k, v) => { const q = new URLSearchParams(query); if (v == null || v === "" || v === "all" || (k === "q" && v === "wall-time")) q.delete(k); else q.set(k, v); location.hash = `#/b/${encodeURIComponent(suite)}/${encodeURIComponent(kase)}${q.toString() ? "?" + q : ""}`; };
  const select = (label, key, values, current, fmt = (x) => x) => h("label", {}, label, h("select", { onchange: (e) => setParam(key, e.target.value) }, values.map((v) => h("option", { value: v, selected: v === current }, fmt(v)))));
  const few = kpi && kpi.points < 3;
  const lastNs = shown.flatMap((x) => x.ns.filter((n, k) => x.estimates[k] != null));
  const reps = lastNs.length ? lastNs[lastNs.length - 1] : null;

  const main = h("main", {},
    h("nav", { class: "crumbs" }, h("a", { href: "#/" }, "overview"), " / ", h("a", { href: `#/s/${encodeURIComponent(suite)}` }, suite)),
    h("h2", { class: "mono title" }, kase, params ? ` ${params}` : ""),
    h("p", { class: "lead" }, `${quantity}, ${direction ? direction.replace(/-/g, " ") : "direction unknown"}`, kpi ? ` · ${kpi.node}` : ""),
    h("div", { class: "filters" },
      variants.length > 1 ? select("variant", "params", variants, params, (v) => v || "(default)") : null,
      quantities.length > 1 ? select("quantity", "q", quantities, quantity) : null,
      nodes.length > 1 ? select("node", "node", ["all", ...nodes], nodeFilter) : null),
    failing.map((f) => h("div", { class: "banner bad" }, `The latest attempt on ${f.node} (commit `, h("a", { href: `#/c/${encodeURIComponent(f.attempt_sha)}`, class: "mono" }, f.attempt_sha.slice(0, 12)), ") failed: ",
      h("code", {}, f.reason || f.status), f.points ? ". The values below are from the last successful measurement." : ". It has never succeeded.")),
    kpi ? h("div", { class: "tiles" },
      tile(`latest · ${kpi.node}`, fmtValue(kpi.estimate, unit), h("span", {}, "commit ", h("a", { href: `#/c/${encodeURIComponent(kpi.sha)}`, class: "mono" }, kpi.sha.slice(0, 12)))),
      tile("vs previous measurement", delta(kpi.vs_prev, direction, { ...opts, noise: kpi.noise })),
      tile("vs 10 measurements back", delta(kpi.vs_10, direction, opts), kpi.points <= 10 ? `needs 11+, has ${kpi.points}` : null),
      tile("vs 100 measurements back", delta(kpi.vs_100, direction, opts), kpi.points <= 100 ? `needs 101+, has ${kpi.points}` : null),
      tile("noise", few ? "–" : fmtAbsPct(kpi.noise), few ? "needs 3+ measurements" : kpi.noise_rank != null ? `quieter than ${Math.round((1 - kpi.noise_rank) * 100)}% of the suite` : null),
      tile("measured commits", kpi.points, reps != null ? `${reps} repetition${reps === 1 ? "" : "s"} each, median shown` : null)) : failing.length ? null : h("p", { class: "empty" }, "no successful measurement yet"),
    few ? h("p", { class: "note" }, `With ${kpi.points} measurement${kpi.points > 1 ? "s" : ""} there is no noise estimate yet, so a change here cannot be told apart from run-to-run variation.`) : null,
    h("h3", {}, "History"),
    h("p", { class: "note" }, "One point per measured commit, in commit order. Hover for details, click a point to open its commit, drag to zoom, double-click to reset. Failed attempts are marked under the axis."));
  const chartWrap = h("div", { class: "chart-wrap" });
  main.append(chartWrap);
  const moves = biggestMoves(shown, hist.commits);
  main.append(h("h3", {}, "Biggest moves in this series"),
    table(["commit", "subject", "node", "baseline", ">from", ">to", ">change"], moves.slice(0, 12).map((m) => [
      h("a", { href: `#/c/${encodeURIComponent(m.sha)}`, class: "mono" }, m.short), m.subject.length > 70 ? m.subject.slice(0, 68) + "…" : m.subject, m.node,
      h("span", {}, h("a", { href: `#/c/${encodeURIComponent(m.baseSha)}`, class: "mono" }, m.baseSha.slice(0, 7)),
        m.skipped > 0 ? h("span", { class: "tag gap", title: `${m.skipped} measured commit${m.skipped > 1 ? "s" : ""} in between had no successful result for this series, so this change spans ${m.skipped > 1 ? "them" : "it"}` }, `spans ${m.skipped + 1}`) : null),
      fmtValue(m.from, unit), fmtValue(m.to, unit), delta(m.rel, direction, { ...opts, noise: kpi?.noise })]), { empty: "needs two successful measurements" }),
    footer());
  requestAnimationFrame(() => drawHistory(chartWrap, shown, hist.commits, unit, kase));
  return main;
}

function biggestMoves(seriesList, axis) {
  const bySha = new Map(axis.map((c) => [c.commit_order, c]));
  const position = new Map(axis.map((c, i) => [c.commit_order, i]));
  const out = [];
  for (const s of seriesList) {
    let prev = null, prevOrder = null;
    for (let i = 0; i < s.orders.length; i++) {
      const b = s.estimates[i];
      if (b == null) continue;
      if (prev != null && prev > 0) {
        const c = bySha.get(s.orders[i]);
        const base = bySha.get(prevOrder);
        // skipped: the suite's measured commits between the baseline and this one
        out.push({ sha: c?.sha || "", short: c?.short || "", subject: c?.subject || "", node: s.node, from: prev, to: b, rel: b / prev - 1,
          baseSha: base?.sha || "", skipped: position.get(s.orders[i]) - position.get(prevOrder) - 1 });
      }
      prev = b; prevOrder = s.orders[i];
    }
  }
  return out.sort((x, y) => Math.abs(y.rel) - Math.abs(x.rel));
}

function drawHistory(wrap, seriesList, axis, unit, title) {
  wrap.replaceChildren();
  if (!seriesList.length || !axis.length) { wrap.append(h("div", { class: "empty" }, "no points to draw")); return; }
  const index = new Map(axis.map((c, i) => [c.commit_order, i]));
  const xs = axis.map((_, i) => i);
  const allValues = seriesList.flatMap((s) => s.estimates);
  const scale = pickScale(allValues, unit);
  const ys = seriesList.map((s) => { const col = new Array(xs.length).fill(null); s.orders.forEach((o, k) => { if (s.estimates[k] != null) col[index.get(o)] = s.estimates[k] / scale[1]; }); return col; });
  const fails = seriesList.map((s) => { const col = new Array(xs.length).fill(null); s.orders.forEach((o, k) => { if (s.statuses[k] && s.statuses[k] !== "success") col[index.get(o)] = s.reasons?.[k] || s.statuses[k]; }); return col; });
  const spread = seriesList.map((s) => { const col = new Array(xs.length).fill(null); s.orders.forEach((o, k) => { if (s.estimates[k] != null) col[index.get(o)] = { n: s.ns?.[k], min: s.mins?.[k], max: s.maxs?.[k] }; }); return col; });
  const nFailed = fails.flat().filter(Boolean).length;
  const totalPoints = allValues.filter((v) => v != null).length;
  const tableView = (open = false) => h("details", { class: "table-view", open }, h("summary", {}, "Table view"),
    table(["commit", "date", ...seriesList.map((s) => `>${s.node} (${scale[0]})`)], axis.map((c, i) => [h("a", { href: `#/c/${encodeURIComponent(c.sha)}`, class: "mono" }, c.short), fmtDate(c.date),
      ...ys.map((col, k) => (col[i] != null ? fmtNum(col[i]) : fails[k][i] ? failTag(fails[k][i]) : "–"))])));
  if (!totalPoints) {
    wrap.append(h("div", { class: "empty" }, `No successful measurement to draw: ${nFailed} failed attempt${nFailed === 1 ? "" : "s"} over ${axis.length} commits. The table below has the reason for each.`),
      h("details", { class: "table-view", open: true }, h("summary", {}, "Table view"),
        table(["commit", "date", ...seriesList.map((s) => `${s.node}`)], axis.map((c, i) => [h("a", { href: `#/c/${encodeURIComponent(c.sha)}`, class: "mono" }, c.short), fmtDate(c.date), ...fails.map((col) => (col[i] ? failTag(col[i], col[i]) : "–"))]))));
    return;
  }
  if (typeof uPlot === "undefined") {
    wrap.append(h("div", { class: "empty" }, "Chart library (uPlot) could not be loaded offline. Use the table below."), tableView(true));
    return;
  }
  const tip = h("div", { class: "tip" });
  const titleRow = h("div", { class: "chart-title" }, h("span", {}, `${title} · ${scale[0]}`), h("span", { class: "note" }, `${axis.length} commits`, nFailed ? [" · ", h("span", { class: "bad" }, `${nFailed} failed`)] : null));
  const el = h("div", { class: "chart" });
  wrap.append(titleRow, el, tip);
  const size = () => ({ width: Math.max(240, wrap.clientWidth - 16), height: window.innerWidth < 600 ? 220 : 300 });
  const failMarks = (u) => {  // a small mark under the plot at every failed attempt
    const ctx = u.ctx, y = u.bbox.top + u.bbox.height + 3 * devicePixelRatio;
    ctx.save(); ctx.fillStyle = cssVar("--critical");
    fails.forEach((col) => col.forEach((f, i) => { if (!f) return; const x = u.valToPos(i, "x", true); ctx.fillRect(x - 3 * devicePixelRatio, y, 6 * devicePixelRatio, 3 * devicePixelRatio); }));
    ctx.restore();
  };
  const opts = {
    ...size(),
    legend: { show: seriesList.length > 1, live: false },
    cursor: { points: { size: 10 }, drag: { x: true, y: false } },
    scales: { x: { time: false, range: [-0.5, xs.length - 0.5] }, y: { range: (u, min, max) => { const pad = (max - min) * 0.25 || max * 0.1 || 1; return [Math.max(0, min - pad), max + pad]; } } },
    axes: [
      { stroke: cssVar("--muted"), grid: { show: false }, ticks: { stroke: cssVar("--axis"), width: 1 }, font: "12px system-ui, sans-serif", space: 70, gap: 8,
        splits: (u, axisIdx, min, max, incr) => { const step = Math.max(1, Math.ceil(incr)); const out = []; for (let i = Math.max(0, Math.ceil(min)); i <= Math.min(xs.length - 1, Math.floor(max)); i += step) out.push(i); return out; },
        values: (u, vals) => vals.map((v) => axis[v] ? axis[v].short.slice(0, 7) : "") },
      { stroke: cssVar("--muted"), grid: { stroke: cssVar("--grid"), width: 1 }, ticks: { show: false }, font: "12px system-ui, sans-serif", size: 60,
        values: (u, vals) => vals.map((v) => fmtNum(v, 3)) },
    ],
    series: [{ label: "commit" }, ...seriesList.map((s) => ({
      label: s.node, stroke: nodeColor(s.node), width: 2, spanGaps: true,
      points: { show: totalPoints <= 80, size: 8, stroke: nodeColor(s.node), fill: cssVar("--surface"), width: 2 },
    }))],
    hooks: {
      draw: [failMarks],
      setCursor: [(u) => {
        const i = u.cursor.idx;
        if (i == null || !axis[i]) { tip.style.display = "none"; return; }
        const c = axis[i];
        const rows = seriesList.map((s, k) => {
          const v = ys[k][i];
          let prev = null;
          for (let j = i - 1; j >= 0; j--) if (ys[k][j] != null) { prev = ys[k][j]; break; }
          const rel = v != null && prev > 0 ? v / prev - 1 : null;
          return h("div", { class: "row" }, h("span", { class: "key", style: `border-color:${nodeColor(s.node)}` }),
            h("span", { class: "v num" }, v != null ? `${fmtNum(v)} ${scale[0]}` : fails[k][i] ? h("span", { class: "bad" }, `failed: ${fails[k][i]}`) : "not measured"),
            h("span", { class: "s num" }, rel == null ? "" : fmtPct(rel)), seriesList.length > 1 ? h("span", { class: "s" }, s.node) : null,
            spread[k][i] ? h("span", { class: "s spread" }, spread[k][i].n > 1
              ? `median of ${spread[k][i].n}, range ${fmtNum(spread[k][i].min / scale[1])}–${fmtNum(spread[k][i].max / scale[1])}`
              : "1 repetition: no spread") : null);
        });
        tip.replaceChildren(h("div", { class: "when" }, `${c.short} · ${fmtDate(c.date)}`), ...rows, h("div", { class: "subj" }, c.subject));
        tip.style.display = "block";
        const left = u.cursor.left, bbox = u.over.getBoundingClientRect(), wrapBox = wrap.getBoundingClientRect();
        const x = bbox.left - wrapBox.left + left + 14, maxX = wrap.clientWidth - tip.offsetWidth - 8;
        tip.style.left = `${Math.max(8, Math.min(x, maxX))}px`;
        tip.style.top = `${bbox.top - wrapBox.top + 10}px`;
      }],
    },
  };
  const chart = new uPlot(opts, [xs, ...ys], el);
  state.charts.push(chart);
  el.addEventListener("mouseleave", () => { tip.style.display = "none"; });
  el.addEventListener("click", () => { const i = chart.cursor.idx; if (i != null && axis[i] && chart.select.width === 0) location.hash = `#/c/${encodeURIComponent(axis[i].sha)}`; });
  wrap.append(tableView(false));
  const onResize = () => chart.setSize(size());
  window.addEventListener("resize", onResize);
  state.cleanup.push(() => window.removeEventListener("resize", onResize));
}

// ---- one commit ---------------------------------------------------------------------

async function renderCommit(sha) {
  let c = state.commits.get(sha);
  if (!c) { c = await getJSON(`views/commits/${encodeURIComponent(safeFile(sha))}.json`); state.commits.set(sha, c); }
  const filter = h("input", { type: "search", placeholder: "filter benchmarks", "aria-label": "filter benchmarks" });
  const minPct = h("select", {}, [0, 1, 2, 5, 10].map((p) => h("option", { value: p, selected: p === 0 }, p ? `≥ ${p}%` : "all changes")));
  const hideNoise = h("input", { type: "checkbox" });
  const body = h("div", {});
  const draw = () => {
    const needle = filter.value.trim().toLowerCase(), min = Number(minPct.value) / 100;
    const rows = c.changes.filter((ch) => Math.abs(ch.rel_change) >= min && !(hideNoise.checked && withinNoise(ch.rel_change, ch.noise))
      && (!needle || `${ch.suite} ${ch.case} ${ch.params} ${ch.node}`.toLowerCase().includes(needle)));
    body.replaceChildren(table(["benchmark", "suite", "node", ">baseline", ">before", ">after", ">change", ">noise"], rows.map((ch) => [
      h("a", { href: benchHref(ch, { node: ch.node }) }, caseLabel(ch)), shortSuite(ch.suite), ch.node,
      h("span", {}, gapTag(ch), " ", h("a", { href: `#/c/${encodeURIComponent(ch.prev_sha)}`, class: "mono" }, ch.prev_sha.slice(0, 7))),
      fmtValue(ch.prev_estimate, ch.unit), fmtValue(ch.estimate, ch.unit),
      delta(ch.rel_change, ch.direction, { noise: ch.noise, unit: ch.unit }), fmtAbsPct(ch.noise)]), { empty: "no changes match" }),
      h("p", { class: "note" }, `${rows.length} of ${c.changes.length} compared series`));
  };
  filter.addEventListener("input", draw); minPct.addEventListener("change", draw); hideNoise.addEventListener("change", draw);
  const parentMeasured = c.changes.some((ch) => ch.prev_is_parent);
  const upUrl = upstreamCommit(c.sha);
  const leadParts = joinParts([
    c.committer_date ? fmtDate(c.committer_date) : null,
    upUrl ? h("a", { href: upUrl, target: "_blank", rel: "noopener" }, "upstream commit") : null,
    c.parent ? h("span", {}, "parent ", h("a", { href: `#/c/${encodeURIComponent(c.parent)}`, class: "mono" }, c.parent.slice(0, 12))) : null,
  ]);
  const main = h("main", {},
    h("nav", { class: "crumbs" }, h("a", { href: "#/" }, "overview"), " / commit"),
    h("h2", {}, h("span", { class: "mono" }, c.short), c.subject ? ` ${c.subject}` : ""),
    leadParts.length ? h("p", { class: "lead" }, ...leadParts) : null,
    h("h3", {}, "Runs"),
    table(["node", "state", "reason", "started", "finished", ">suites ok", ">failed suites", ">results", ">failed results", ""], c.nodes.map((n) => {
      const filesUrl = runFiles(c.sha, n.node);
      const logUrl = runLog(c.sha, n.node, n.has_log);
      const links = joinParts([
        filesUrl ? h("a", { href: filesUrl, target: "_blank", rel: "noopener" }, "parquet") : null,
        logUrl ? h("a", { href: logUrl, target: "_blank", rel: "noopener" }, "log") : null,
      ]);
      return [
        n.node, n.state === "finished" ? n.state : h("span", { class: "bad" }, n.state), n.reason || "", fmtDate(n.started_at), fmtDate(n.finished_at),
        n.suites_ok, n.suites_failed ? h("span", { class: "bad" }, n.suites_failed) : "0", n.results,
        n.failed_results ? h("span", { class: "bad" }, n.failed_results) : "0",
        links.length ? h("span", {}, ...links) : ""];
    })),
    c.failures?.length ? [h("h3", {}, h("span", { class: "bad" }, `Failed results (${c.failures.length})`)),
      table(["benchmark", "suite", "node", "quantity", "reason"], c.failures.map((f) => [h("a", { href: benchHref(f, { node: f.node }) }, caseLabel(f)), shortSuite(f.suite), f.node, f.quantity, h("code", {}, f.reason || f.status)]))] : null,
    h("h3", {}, "Changes against the previous measured point"),
    h("p", { class: "note" }, parentMeasured || !c.changes.length
      ? "Each series is compared with its own previous successful measurement, named in the baseline column. \"not parent\" marks a baseline further back than the parent commit."
      : "This commit's parent was not measured, so every change here also covers the unmeasured commits in between."),
    h("div", { class: "filters" }, h("label", {}, "show", minPct), h("label", {}, hideNoise, "hide changes within noise"), filter),
    body, footer());
  draw();
  return main;
}

// ---- about: how to read the page -----------------------------------------------------

function renderAbout() {
  const P = state.project, repo = P.repository, pct = `${Math.round(state.landing.notable * 100)}%`;
  const upstream = (P.upstream?.url || "").replace(/\.git$/, "");
  const isRuns = state.manifest?.source === "runs";
  return h("main", { class: "prose" },
    h("nav", { class: "crumbs" }, h("a", { href: "#/" }, "overview"), " / about"),
    h("h2", {}, "About this page"),
    h("p", { class: "lead" }, P.description || "Benchmark results and history, built with benchx."),
    h("div", { class: "cols" },
      h("section", { class: "card" }, h("h3", {}, isRuns ? "How a commit gets measured" : "How this report is built"),
        isRuns
          ? h("ol", {},
              h("li", {}, "Commits from ", upstream ? h("a", { href: upstream }, `${upstream.replace(/^https:\/\/github.com\//, "")} ${P.upstream?.branch || ""}`.trim()) : "the upstream repository", " are built and benchmarked through benchx."),
              h("li", {}, "Each run is filed as Parquet under ", h("code", {}, "runs/<sha>/<node>/"), "."),
              h("li", {}, h("code", {}, "bx view"), " derives the flat tables and static JSON views this page reads. Nothing is stored in a database or server."))
          : h("ol", {},
              h("li", {}, "Benchmarks are executed with ", h("code", {}, "bx bench"), " or ", h("code", {}, "bx compare"), ", recording results in the benchx schema."),
              h("li", {}, h("code", {}, "bx view"), " reads the local store (", h("code", {}, "store.parquet"), ") or JSON result files and builds these static views."),
              h("li", {}, "The same static site can be viewed locally or published to static hosting without a backend."))),
      h("section", { class: "card" }, h("div", { class: "about-brand" }, wordmark()), h("h3", {}, "Built with benchx"),
        h("p", {}, "Every number shown on this page is derived directly from stored benchx measurement results."),
        h("p", {}, h("a", { class: "button", href: "https://github.com/benchx-dev/benchx" }, "benchx on GitHub →"),
          repo ? [" ", h("a", { href: repo }, "project repository")] : null))),
    h("h3", {}, "Reading the numbers"),
    h("dl", {},
      h("dt", {}, "Change"), h("dd", {}, "A benchmark's newest successful measurement divided by its previous successful one, minus one. ", delta(0.12, "lower-is-better", { unit: "s", noise: 0.01 }), " is worse, ", delta(-0.12, "lower-is-better", { unit: "s", noise: 0.01 }), " is better; for higher-is-better quantities such as throughput the colors flip. The arrow is the direction of the number; the color is good or bad. Under 0.5% is shown as flat (", delta(0.001, "lower-is-better"), ")."),
      h("dt", {}, "Noise"), h("dd", {}, "The series' usual commit-to-commit movement: the median change between consecutive measurements over its last 21. It is historical variability across commits, not repeatability at one commit, and it is the series' current value, also on older commit pages. It needs three measurements. A change smaller than twice the noise is drawn muted (", delta(0.03, "lower-is-better", { unit: "s", noise: 0.02 }), "), because changes that size are common for this series. A change with no noise estimate yet is underlined (", delta(0.12, "lower-is-better", { unit: "s", noise: null }), "): unconfirmed."),
      h("dt", {}, "Measured commits and repetitions"), h("dd", {}, "Each point is the median of the repetitions benchx ran on that commit. The history chart's tooltip shows how many there were and their range. With one repetition there is no spread to judge a point by."),
      h("dt", {}, "Not a verdict"), h("dd", {}, "None of this is a statistical test yet. benchx's comparator history mode will compare each commit with its ancestors using the measured repetitions; until then, treat a change as a lead to check, and re-measure it before acting on it."),
      h("dt", {}, "Baseline and \"not parent\""), h("dd", {}, "A change is against the previous measurement of the same series, which is the parent commit only if the parent was measured. Otherwise the change spans every unmeasured commit in between, and the commit page marks it ", h("span", { class: "tag gap" }, "not parent"), "."),
      h("dt", {}, "Counts"), h("dd", {}, `"Regressed" and "improved" in the summaries count changes larger than ${pct} in either direction, whatever the noise.`),
      h("dt", {}, "Failures"), h("dd", {}, "A benchmark whose latest attempt failed is marked on its card and listed under the latest run, with the reason benchx recorded. Its chart keeps the last successful values, and failed attempts are marked under the history chart's axis.")),
    footer());
}

function footer() {
  const m = state.manifest, repo = state.project.repository;
  const hasTables = m.tables && Object.keys(m.tables).length > 0;
  const t = (name) => h("a", { href: `${DATA}${m.tables[name]}`, download: true }, name);
  return h("footer", {},
    h("span", {}, `built ${fmtDate(m.built_at)} · ${m.counts.points.toLocaleString()} results`,
      hasTables ? [" · data: ", ...joinParts(["points", "latest", "changes", "commits"].filter((k) => m.tables[k]).map(t), ", "), " (Parquet)"] : null),
    h("span", { class: "made" }, "measured with ", h("a", { href: "https://github.com/benchx-dev/benchx", title: "benchx" }, wordmark(true)), repo ? [" · ", h("a", { href: repo }, "source")] : null));
}

// ---- shell: header, search, theme, router -------------------------------------------

function header() {
  const input = h("input", { type: "search", placeholder: "search a benchmark…  ( / )", "aria-label": "search a benchmark", autocomplete: "off" });
  const list = h("ul", { role: "listbox", hidden: true });
  let items = [], active = -1;
  const show = () => {
    const needle = input.value.trim().toLowerCase();
    list.replaceChildren(); active = -1;
    if (!needle) { list.hidden = true; return; }
    const seen = new Set();
    items = [];
    for (const s of state.series) {
      const key = `${s.suite}|${s.case}|${s.params}|${s.quantity}`;
      if (seen.has(key)) continue;
      if (`${s.case} ${s.params} ${s.suite}`.toLowerCase().includes(needle)) { seen.add(key); items.push(s); }
      if (items.length >= 14) break;
    }
    list.hidden = items.length === 0;
    items.forEach((s) => list.append(h("li", { role: "option", onclick: () => go(s) }, h("span", { class: "mono" }, caseLabel(s)), h("span", { class: "where" }, s.suite))));
  };
  const go = (s) => { input.value = ""; list.hidden = true; location.hash = benchHref(s); };
  input.addEventListener("input", show);
  input.addEventListener("keydown", (e) => {
    if (list.hidden) return;
    if (e.key === "ArrowDown") { active = Math.min(active + 1, items.length - 1); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); e.preventDefault(); }
    else if (e.key === "Enter") { if (items.length) go(items[Math.max(active, 0)]); return; }
    else if (e.key === "Escape") { list.hidden = true; return; }
    [...list.children].forEach((li, i) => li.setAttribute("aria-selected", i === active));
  });
  document.addEventListener("click", (e) => { if (!list.contains(e.target) && e.target !== input) list.hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "/" && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement?.tagName)) { e.preventDefault(); input.focus(); } });
  const label = () => `theme: ${document.documentElement.dataset.theme || "auto"}`;
  const theme = h("button", { class: "ghost", title: "cycle theme: auto / dark / light", onclick: (e) => { const cur = document.documentElement.dataset.theme || "auto"; applyTheme(cur === "auto" ? "dark" : cur === "dark" ? "light" : "auto"); e.currentTarget.textContent = label(); route(); } }, label());
  const nav = (href, text) => h("a", { href, "data-nav": href }, text);
  return h("header", { class: "top" },
    h("h1", {}, h("a", { href: "#/", class: "brand" }, h("span", { html: MARK }), state.project.title || "Benchmarks")),
    h("nav", {}, nav("#/", "overview"), nav("#/suites", "suites"), nav("#/about", "about")),
    h("div", { class: "search" }, input, list), theme);
}

function applyTheme(mode) {
  if (mode === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = mode;
  try { if (mode === "auto") localStorage.removeItem("theme"); else localStorage.setItem("theme", mode); } catch (e) { /* storage unavailable */ }
}

function destroyCharts() {
  for (const c of state.charts) c.destroy();
  state.charts = [];
}

async function route() {
  const root = document.getElementById("app");
  const hash = location.hash || "#/";
  const [path, qs] = hash.slice(1).split("?");
  const query = new URLSearchParams(qs || "");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  destroyCharts();
  for (const f of state.cleanup) f();
  state.cleanup = [];
  const section = parts[0] === "suites" || parts[0] === "s" ? "#/suites" : parts[0] === "about" ? "#/about" : parts[0] ? null : "#/";
  document.querySelectorAll("[data-nav]").forEach((a) => a.toggleAttribute("aria-current", a.dataset.nav === section));
  let view;
  try {
    if (parts[0] === "suites") view = renderSuites();
    else if (parts[0] === "about") view = renderAbout();
    else if (parts[0] === "s" && parts[1]) view = await renderSuite(parts[1]);
    else if (parts[0] === "b" && parts[2]) view = await renderBench(parts[1], parts.slice(2).join("/"), query);  // a case name may itself contain '/'
    else if (parts[0] === "c" && parts[1]) view = await renderCommit(parts[1]);
    else view = await renderLanding();
  } catch (e) {
    view = h("main", {}, h("h2", {}, "Could not load this view"), h("p", { class: "note" }, String(e.message || e)), h("p", {}, h("a", { href: "#/" }, "back to the overview")));
  }
  root.replaceChildren(view);
  window.scrollTo(0, 0);
}

async function init() {
  try { const t = localStorage.getItem("theme"); if (t) applyTheme(t); } catch (e) { /* ignore */ }
  const root = document.getElementById("app");
  try {
    state.manifest = await getJSON("manifest.json", { fresh: true });
    state.v = state.manifest.built_at;
    [state.project, state.landing, state.series] = await Promise.all([
      getJSON("project.json").catch(() => ({})), getJSON("views/landing.json"), getJSON("views/series.json")]);
  } catch (e) {
    root.replaceChildren(h("main", {}, h("section", { class: "hero" }, h("div", { class: "eyebrow" }, "No data"),
      h("h2", {}, "This page has not been built yet"),
      h("p", {}, "Run ", h("code", {}, "bx view --out site"), " to build the static dashboard, or ", h("code", {}, "bx view"), " to serve it locally."),
      h("p", { class: "note" }, String(e.message || e)))));
    return;
  }
  if (state.project.title) document.title = state.project.title;
  document.body.prepend(header());
  window.addEventListener("hashchange", route);
  await route();
}
init();
