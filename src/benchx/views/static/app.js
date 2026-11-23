// The benchx dashboard: a static, light-first SPA over the JSON views written by `bx view`.
// No query engine runs here; every number shown was computed ahead of time.
// Routes: #/ overview (no plots: top movers + runs history), #/benchmarks (sortable flat list),
// #/suites, #/s/<suite>, #/b/<suite>/<case>?params=&q=&node=, #/c/<sha>, #/about.

const DATA = "data/";
const SERIES_VARS = [
  "--series-1", "--series-2", "--series-3", "--series-4", "--series-5", "--series-6",
  "--series-7", "--series-8", "--series-9", "--series-10", "--series-11", "--series-12",
];
const SERIES_DASHES = [null, [6, 3], [2, 2]];
const MAX_AUTO_VARIANTS = 12;
const PAGE = 50;
const MARK = '<svg viewBox="0 0 64 64" aria-hidden="true" class="mark"><rect x="8" y="10" width="48" height="8" rx="2" fill="var(--brand)"/><path d="M18 24 L46 52 M46 24 L18 52" stroke="currentColor" stroke-width="7" stroke-linecap="round"/></svg>';
const MARK_SMALL = '<svg viewBox="0 0 64 64" aria-hidden="true" class="mark"><rect x="8" y="8" width="48" height="8" rx="2" fill="var(--brand)"/><path d="M18 26 L46 52 M46 26 L18 52" stroke="currentColor" stroke-width="8" stroke-linecap="round"/></svg>';
function wordmark(small) { return h("span", { class: "benchx" }, h("span", { html: small ? MARK_SMALL : MARK }), h("span", { class: "wm" }, "bench", h("b", {}, "x"))); }
const state = { v: "", project: null, manifest: null, landing: null, series: null, multiQty: new Set(), histories: new Map(), commits: new Map(), charts: [], cleanup: [], colorSlots: new Map(), routeSeq: 0 };

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

async function loadCommit(sha) {
  let c = state.commits.get(sha);
  if (!c) {
    c = await getJSON(`views/commits/${encodeURIComponent(safeFile(sha))}.json`);
    state.commits.set(sha, c);
  }
  return c;
}

const UNITS = {
  s: [["ns", 1e-9], ["µs", 1e-6], ["ms", 1e-3], ["s", 1]],
  B: [["B", 1], ["KiB", 1024], ["MiB", 1024 ** 2], ["GiB", 1024 ** 3]],
  "B/s": [["B/s", 1], ["KB/s", 1e3], ["MB/s", 1e6], ["GB/s", 1e9], ["TB/s", 1e12]],
  "By/s": [["B/s", 1], ["KB/s", 1e3], ["MB/s", 1e6], ["GB/s", 1e9], ["TB/s", 1e12]],
  "1/s": [["/s", 1], ["k/s", 1e3], ["M/s", 1e6], ["G/s", 1e9]],
  "i/s": [["/s", 1], ["k/s", 1e3], ["M/s", 1e6], ["G/s", 1e9]],
};
function pickScale(values, unit) {
  const table = UNITS[unit];
  const m = Math.max(...values.filter((v) => v != null && isFinite(v)).map(Math.abs), 0);
  if (!table) return [unit || "", 1];
  if (!m) return table[0];
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
  return `${fmtNum(v / factor)}${label ? " " + label : ""}`;
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
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  if (m < 48 * 60) return `${Math.round(m / 60)}h ago`;
  return `${Math.round(m / 1440)}d ago`;
}
function shortSuite(s) {
  let out = s.replace(/-benchmark$/g, "");
  const prefix = state.project?.name ? `${state.project.name}-` : "";
  if (prefix && out.startsWith(prefix)) out = out.slice(prefix.length);
  return out;
}
function safeFile(s) {
  const cleaned = String(s).replace(/[/\\\x00:*?"<>|]+/g, "_").replace(/\.\.+/g, "_");
  return cleaned && cleaned !== "." ? cleaned : "_";
}
function shortSha(sha, len = 10) {
  if (!sha) return "";
  if (sha.includes("-dirty-")) {
    const [base, tree] = sha.split("-dirty-", 2);
    return `${base.slice(0, 7)}+dirty-${tree.slice(0, 4)}`;
  }
  return sha.slice(0, len);
}
function isFailed(status) { return Boolean(status && !["success", "partial", "skipped"].includes(status)); }
function upstreamFileUrl(sha, relPath) {
  if (!sha || sha.includes("-dirty-") || !relPath || relPath.startsWith("/") || !relPath.endsWith(".py")) return null;
  const url = (state.project?.upstream?.url || "").replace(/\.git$/, "").replace(/\/$/, "");
  if (!/^https?:\/\/github\.com\//.test(url)) return null;
  return `${url}/blob/${encodeURIComponent(sha)}/${relPath.split("/").map(encodeURIComponent).join("/")}`;
}
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
  return hasLog === false ? null : `data/logs/${encodeURIComponent(safeFile(sha))}/${encodeURIComponent(safeFile(node))}.txt`;
}
function multiNode() { return (state.landing?.stats.nodes || []).length > 1; }

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
function changeCellClass(rel, direction, noise) {
  const v = verdict(rel, direction);
  if (v !== "regressed" && v !== "improved") return "";
  if (noise == null || withinNoise(rel, noise)) return "";
  return v === "regressed" ? "cell-bad" : "cell-good";
}
function delta(rel, direction, opts = {}) {
  const v = verdict(rel, direction);
  const real = v === "regressed" || v === "improved";
  const within = real && withinNoise(rel, opts.noise);
  const w = changeWords(direction, opts.unit);
  const glyph = v === "none" ? "" : v === "flat" ? "·" : rel > 0 ? "▲" : "▼";
  const word = { regressed: w.worse, improved: w.better, flat: "no change", none: "no comparison" }[v];
  let title = v === "none" ? word : `${fmtPct(rel, 2)}: ${word}`;
  const unknown = real && opts.noise === null;
  if (real && opts.noise != null) title += within ? ` · within 2× series noise (${fmtAbsPct(opts.noise)})` : ` · ${(Math.abs(rel) / opts.noise).toFixed(1)}× series noise (${fmtAbsPct(opts.noise)})`;
  else if (unknown) title += " · no noise estimate yet (<3 measurements)";
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
function formatParams(raw) {
  if (!raw) return "";
  if (raw.startsWith("{")) {
    try {
      const obj = JSON.parse(raw);
      if (obj && typeof obj === "object" && !Array.isArray(obj)) {
        return Object.entries(obj).map(([k, v]) => (k === "params" ? String(v) : `${k}=${v}`)).join(" · ");
      }
    } catch (_) { /* keep raw */ }
  }
  return raw;
}
function caseLabel(s) {
  const p = formatParams(s.params);
  return p ? `${s.case} (${p})` : s.case;
}
const SHORT_QTY = {
  "wall-time": "wall",
  "cpu-time": "cpu",
  "throughput-bytes": "B/s",
  "throughput-items": "items/s",
};
function shortQty(q) { return SHORT_QTY[q] || q || ""; }
function hasMultiQty(s) { return state.multiQty.has(`${s.suite}|${s.case}|${s.params || ""}`); }
function qtyTag(s, force = false) {
  if (!s.quantity || (!force && !hasMultiQty(s))) return null;
  return h("span", { class: "tag qty", title: s.quantity }, shortQty(s.quantity));
}
function benchLink(s, opts = {}) {
  const p = formatParams(s.params);
  return h("span", { class: "bench-name" },
    h("a", { href: benchHref(s, opts), class: "mono", title: `${caseLabel(s)} (${s.suite})` },
      s.case, p ? [" ", h("span", { class: "bench-params" }, p)] : null),
    qtyTag(s, opts.forceQty));
}
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function seriesColor(key) {
  if (!state.colorSlots.has(key)) state.colorSlots.set(key, state.colorSlots.size % SERIES_VARS.length);
  return cssVar(SERIES_VARS[state.colorSlots.get(key)]);
}
function failTag(reason, text = "failed") { return h("span", { class: "tag failed", title: reason || "" }, text); }
function gapTag(ch) {
  if (ch.prev_is_parent) return null;
  const title = `Baseline ${shortSha(ch.prev_sha)} is not the parent commit; unmeasured commits lie in between.`;
  return h("span", { class: "tag gap", title }, "not parent");
}
function stateTag(c) {
  if (c.state === "finished") return null;
  return h("span", { class: `tag ${c.state === "failed" ? "failed" : ""}` }, c.state === "failed" ? "failed" : c.state);
}
function sharedLegend(keys) {
  return keys.length > 1 ? h("span", { class: "legend" }, keys.map((n) => h("span", { class: "legend-key" }, h("span", { class: "key", style: `border-color:${seriesColor(n)}` }), n))) : null;
}

// Generic table builder supporting right alignment (">Label"), cell objects ({ node, cls }),
// optional sort state ({ key, dir, onSort, keys }), and optional 2-tier grouped headers.
function table(headers, rows, opts = {}) {
  const sort = opts.sort || null;
  const theadRows = [];
  if (opts.groups) {
    theadRows.push(h("tr", { class: "group-row" }, opts.groups.map((g) => h("th", { colspan: g.span }, g.label))));
  }
  theadRows.push(h("tr", {}, headers.map((raw, i) => {
    const right = raw.startsWith(">");
    const label = raw.replace(/^>/, "");
    const sep = opts.sepAt && opts.sepAt.includes(i) ? " col-sep" : "";
    const key = sort?.keys?.[i];
    if (key) {
      const active = sort.key === key;
      const ind = active ? (sort.dir === "asc" ? "▲" : "▼") : "";
      return h("th", { class: `${right ? "r " : ""}sortable${sep}`, onclick: () => sort.onSort(key) },
        label, ind ? h("span", { class: "sort-ind" }, ind) : null);
    }
    return h("th", { class: `${right ? "r" : ""}${sep}`.trim() }, label);
  })));
  const bodyRows = rows.length ? rows.map((row) => {
    const isRowObj = row && !Array.isArray(row) && "cells" in row;
    const cells = isRowObj ? row.cells : row;
    return h("tr", { class: isRowObj ? row.cls : null }, cells.map((cell, i) => {
      const right = headers[i].startsWith(">");
      const sep = opts.sepAt && opts.sepAt.includes(i) ? " col-sep" : "";
      const isObj = cell && typeof cell === "object" && !cell.nodeType && "v" in cell;
      const content = isObj ? cell.v : cell;
      const extra = isObj && cell.cls ? ` ${cell.cls}` : "";
      return h("td", { class: `${right ? "r num" : ""}${sep}${extra}`.trim() }, content);
    }));
  }) : [h("tr", {}, h("td", { colspan: headers.length, class: "empty" }, opts.empty || "nothing yet"))];
  return h("div", { class: "wrap" }, h("table", {}, h("thead", {}, ...theadRows), h("tbody", {}, ...bodyRows)));
}

function cmpVal(a, b, dir = "asc") {
  const mult = dir === "asc" ? 1 : -1;
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  if (typeof a === "string" && typeof b === "string") return mult * a.localeCompare(b);
  return mult * (a < b ? -1 : a > b ? 1 : 0);
}

function problemsBanner() {
  const n = state.manifest.counts.ingest_problems || 0;
  return n ? h("div", { class: "banner bad" }, `${n} result${n > 1 ? "s or run directories were" : " or run directory was"} rejected by ingest and omitted.`) : null;
}

// ---- overview (#/): plot-free, top movers + runs history ----------------------------

function renderLanding() {
  const L = state.landing;
  if (!L.stats.results) return renderEmpty();
  const nodes = L.stats.nodes || [];
  const q = new URLSearchParams((location.hash.split("?")[1]) || "");
  const hasNoise = [...L.movers.regressed, ...L.movers.improved].some((m) => m.noise != null);
  const ctl = {
    node: q.get("node") || "all",
    hideNoise: q.has("noisy") ? q.get("noisy") !== "1" : hasNoise,
    runFilter: "",
  };

  const moversHost = h("div", {});
  const failuresHost = h("div", {});
  const runsHost = h("div", {});

  const drawMovers = () => {
    const matchNode = (m) => ctl.node === "all" || m.node === ctl.node;
    const filterList = (list) => list
      .filter((m) => matchNode(m) && (!ctl.hideNoise || !withinNoise(m.rel_change, m.noise)))
      .slice(0, 10);
    const reg = filterList(L.movers.regressed);
    const imp = filterList(L.movers.improved);

    const moverPanel = (title, list, emptyMsg) => {
      const rows = list.map((m) => {
        const cls = changeCellClass(m.rel_change, m.direction, m.noise);
        const metaParts = [shortSuite(m.suite), ctl.node === "all" && multiNode() ? m.node : null].filter(Boolean);
        const nameCell = h("div", { class: "bench-cell" },
          benchLink(m, { node: m.node }),
          metaParts.length ? h("div", { class: "bench-meta" }, metaParts.join(" · ")) : null);
        const beforeAfter = h("span", { class: "num note" }, `${fmtValue(m.prev_estimate, m.unit)} → ${fmtValue(m.estimate, m.unit)}`);
        const dCell = h("span", { class: "delta-cell" }, m.prev_is_parent ? null : gapTag(m), delta(m.rel_change, m.direction, { noise: m.noise, unit: m.unit }));
        return [nameCell, beforeAfter, { v: dCell, cls }];
      });
      return h("section", { class: "panel" },
        h("div", { class: "panel-head" }, h("h3", {}, title), h("span", { class: "note" }, `${list.length} shown`)),
        table(["Benchmark", ">Before → After", ">Change"], rows, { empty: emptyMsg }));
    };

    const anyCompared = L.latest_runs.some((r) => (ctl.node === "all" || r.node === ctl.node) && r.compared > 0);
    const noReg = !anyCompared ? "needs at least 2 measured commits" : "no regressions above threshold";
    const noImp = !anyCompared ? "needs at least 2 measured commits" : "no improvements above threshold";
    moversHost.replaceChildren(h("div", { class: "cols" },
      moverPanel("Top regressions", reg, noReg),
      moverPanel("Top improvements", imp, noImp)));

    const fails = L.failures.filter(matchNode);
    const badRuns = L.latest_runs.filter((r) => matchNode(r) && r.state !== "finished");
    if (fails.length || badRuns.length) {
      failuresHost.replaceChildren(h("details", { class: "failures", open: fails.length > 0 && fails.length <= 6 },
        h("summary", {},
          fails.length ? h("span", { class: "bad" }, `${fails.length} failed benchmark${fails.length === 1 ? "" : "s"}`) : null,
          fails.length && badRuns.length ? " · " : null,
          badRuns.length ? h("span", { class: "bad" }, `${badRuns.length} incomplete run${badRuns.length === 1 ? "" : "s"}`) : null,
          " on latest commits"),
        badRuns.length ? h("p", { class: "note" }, ...joinParts(badRuns.map((r) => h("span", {}, h("a", { href: `#/c/${encodeURIComponent(r.sha)}`, class: "mono" }, r.short), ` (${r.node}: ${r.state})`)))) : null,
        fails.length ? h("ul", {}, fails.slice(0, 30).map((f) => h("li", {},
          h("a", { href: benchHref(f, { node: f.node }), class: "mono" }, caseLabel(f)), " ",
          h("span", { class: "note" }, shortSuite(f.suite)), " ",
          h("code", { class: "reason" }, f.reason || f.status)))) : null));
    } else {
      failuresHost.replaceChildren();
    }
  };

  const drawRuns = () => {
    const needle = ctl.runFilter.trim().toLowerCase();
    const list = L.recent_commits.filter((c) =>
      (ctl.node === "all" || c.node === ctl.node) &&
      (!needle || `${c.sha} ${c.subject} ${c.node} ${c.state}`.toLowerCase().includes(needle)));
    const pct = `${Math.round(L.notable * 100)}%`;
    let prevSha = null;
    const rows = list.map((c) => {
      const sameCommit = c.sha === prevSha;
      prevSha = c.sha;
      const logUrl = runLog(c.sha, c.node, c.has_log);
      const regCell = c.compared ? (c.regressed ? { v: h("span", { class: "delta regressed" }, c.regressed), cls: "cell-bad" } : "0") : "–";
      const impCell = c.compared ? (c.improved ? { v: h("span", { class: "delta improved" }, c.improved), cls: "cell-good" } : "0") : "–";
      const failCell = c.failed_results ? { v: h("span", { class: "bad" }, c.failed_results), cls: "cell-bad" } : "0";
      return {
        cls: sameCommit ? "same-commit" : null,
        cells: [
          { v: h("span", { title: fmtDate(c.finished_at || c.started_at || c.committer_date) }, ago(c.finished_at || c.started_at || c.committer_date) || fmtDate(c.committer_date)), cls: "nowrap" },
          { v: c.node, cls: "nowrap" },
          c.state === "finished" ? "ok" : stateTag(c),
          c.results.toLocaleString(),
          failCell,
          regCell,
          impCell,
          logUrl ? h("a", { href: logUrl, target: "_blank", rel: "noopener" }, "log") : "",
          { v: h("a", { href: `#/c/${encodeURIComponent(c.sha)}`, class: "mono" }, c.short), cls: "nowrap commit-col" },
          { v: h("span", {}, c.subject.length > 84 ? c.subject.slice(0, 82) + "…" : c.subject), cls: "commit-col" },
        ],
      };
    });
    runsHost.replaceChildren(table(
      ["Time", "Machine", "State", ">Results", ">Failed", `>Regressed >${pct}`, `>Improved >${pct}`, "Log", "Hash", "Message"],
      rows,
      {
        groups: [{ label: "Run", span: 8 }, { label: "Commit", span: 2 }],
        sepAt: [8],
        empty: "no matching runs",
      }));
  };

  const nodeSelect = nodes.length > 1 ? h("label", {}, "Machine",
    h("select", { onchange: (e) => { ctl.node = e.target.value; drawMovers(); drawRuns(); } },
      ["all", ...nodes].map((n) => h("option", { value: n, selected: n === ctl.node }, n === "all" ? "all machines" : n)))) : null;
  const noiseToggle = h("label", {},
    h("input", { type: "checkbox", checked: ctl.hideNoise, onchange: (e) => { ctl.hideNoise = e.target.checked; drawMovers(); } }),
    "hide within-noise");
  const runSearch = h("input", { type: "search", placeholder: "filter runs or commits…", "aria-label": "filter runs", oninput: (e) => { ctl.runFilter = e.target.value; drawRuns(); } });

  const st = L.stats;
  const main = h("main", {},
    problemsBanner(),
    h("div", { class: "status-bar" },
      h("span", {},
        `${st.commits} commit${st.commits === 1 ? "" : "s"} · ${st.series.toLocaleString()} series · ${st.results.toLocaleString()} results`,
        nodes.length === 1 ? ` · ${nodes[0]}` : ` · ${nodes.length} machines`,
        " · ", h("a", { href: "#/benchmarks" }, "browse all benchmarks →")),
      h("div", { class: "controls filters", style: "margin:0" }, nodeSelect, noiseToggle)),
    failuresHost,
    moversHost,
    h("div", { class: "section-head" },
      h("h2", {}, "Runs history"),
      h("div", { class: "filters", style: "margin:0" }, runSearch)),
    runsHost,
    (L.noisiest.length || L.suite_time.length) ? (() => {
      const nonZeroSuites = L.suite_time.filter((s) => s.seconds > 0);
      const shownSuites = (nonZeroSuites.length ? nonZeroSuites : L.suite_time).slice(0, 20);
      const suiteNote = nonZeroSuites.length
        ? `top ${shownSuites.length} of ${nonZeroSuites.length} timed suites`
        : `showing ${shownSuites.length} of ${L.suite_time.length} suites`;
      return h("details", { class: "fold" },
        h("summary", {}, "Noisiest series & suite wall time"),
        h("div", { class: "cols" },
          h("section", { class: "panel" },
            h("div", { class: "panel-head" }, h("h3", {}, "Noisiest series"), h("span", { class: "note" }, "median commit-to-commit |Δ|")),
            table(["Benchmark", "Machine", ">Noise", ">Pts"],
              L.noisiest.map((s) => [benchLink(s, { node: s.node }), { v: s.node, cls: "nowrap" }, fmtAbsPct(s.noise), s.points]),
              { empty: "needs at least 3 measurements per series" })),
          h("section", { class: "panel" },
            h("div", { class: "panel-head" }, h("h3", {}, "Suite wall time"), h("span", { class: "note" }, suiteNote)),
            table(["Suite", "Machine", ">Cases", ">Failed", ">Time"],
              shownSuites.map((s) => [h("a", { href: `#/s/${encodeURIComponent(s.suite)}` }, shortSuite(s.suite)), { v: s.node, cls: "nowrap" }, s.cases,
                s.failed ? h("span", { class: "bad" }, s.failed) : "0",
                s.seconds < 120 ? `${s.seconds}s` : `${Math.round(s.seconds / 60)}m`])))));
    })() : null,
    footer());

  drawMovers();
  drawRuns();
  return main;
}

function renderEmpty() {
  const L = state.landing, repo = state.project.repository, isRuns = state.manifest?.source === "runs";
  return h("main", {},
    problemsBanner(),
    h("section", { class: "empty-box" },
      h("h2", {}, L.recent_commits.length ? "Runs are filed, but none produced a result" : "Waiting for the first run"),
      h("p", { class: "lead" }, L.recent_commits.length
        ? "Open a run below to inspect its status and log."
        : isRuns
          ? "This page fills in once benchmark runs are recorded under runs/."
          : "Measure a target with bx bench, then run bx view:"),
      L.recent_commits.length ? null : h("pre", {}, isRuns ? "bx view --runs runs --out site" : "bx bench path/to/benchmarks.py\nbx view"),
      repo ? h("p", {}, h("a", { href: repo }, "Project repository")) : null),
    L.recent_commits.length ? [h("h2", {}, "Runs"), table(["Commit", "Subject", "Machine", "State", ">Results"],
      L.recent_commits.map((c) => [h("a", { href: `#/c/${encodeURIComponent(c.sha)}`, class: "mono" }, c.short), c.subject, c.node, stateTag(c) || c.state, c.results]))] : null,
    footer());
}

// ---- benchmarks (#/benchmarks): ASV summarylist-style flat sortable table -----------

function renderBenchmarks(query) {
  const all = state.series || [];
  const suites = [...new Set(all.map((s) => s.suite))].sort();
  const nodes = [...new Set(all.map((s) => s.node))].sort();
  const quantities = [...new Set(all.map((s) => s.quantity).filter(Boolean))].sort();
  const ctl = {
    suite: query.get("suite") || "all",
    node: query.get("node") || "all",
    qty: query.get("q") || "all",
    status: query.get("status") || "all",
    text: query.get("f") || "",
    hideNoise: query.get("hide_noise") === "1",
    sort: query.get("sort") || "change",
    dir: query.get("dir") || "desc",
    page: Math.max(0, Number(query.get("p") || 0) || 0),
  };

  const syncHash = () => {
    const p = new URLSearchParams();
    if (ctl.suite !== "all") p.set("suite", ctl.suite);
    if (ctl.node !== "all") p.set("node", ctl.node);
    if (ctl.qty !== "all") p.set("q", ctl.qty);
    if (ctl.status !== "all") p.set("status", ctl.status);
    if (ctl.text) p.set("f", ctl.text);
    if (ctl.hideNoise) p.set("hide_noise", "1");
    if (ctl.sort !== "change") p.set("sort", ctl.sort);
    if (ctl.dir !== "desc") p.set("dir", ctl.dir);
    if (ctl.page > 0) p.set("p", String(ctl.page));
    history.replaceState(null, "", "#/benchmarks" + (p.toString() ? "?" + p : ""));
  };

  const host = h("div", {});
  const pagerHost = h("div", {});
  const countEl = h("span", { class: "note" });

  const onSort = (key) => {
    if (ctl.sort === key) ctl.dir = ctl.dir === "asc" ? "desc" : "asc";
    else { ctl.sort = key; ctl.dir = key === "name" || key === "suite" || key === "node" ? "asc" : "desc"; }
    ctl.page = 0;
    draw();
  };

  const sortVal = (s, key) => {
    if (key === "name") return caseLabel(s);
    if (key === "suite") return s.suite;
    if (key === "node") return s.node;
    if (key === "value") return s.estimate;
    if (key === "change") return s.vs_prev == null ? null : Math.abs(s.vs_prev);
    if (key === "noise") return s.noise;
    if (key === "points") return s.points;
    return null;
  };

  const draw = () => {
    const needle = ctl.text.trim().toLowerCase();
    const filtered = all.filter((s) => {
      if (ctl.suite !== "all" && s.suite !== ctl.suite) return false;
      if (ctl.node !== "all" && s.node !== ctl.node) return false;
      if (ctl.qty !== "all" && s.quantity !== ctl.qty) return false;
      if (ctl.status === "failing" && !isFailed(s.status)) return false;
      if (ctl.status === "regressed" && verdict(s.vs_prev, s.direction) !== "regressed") return false;
      if (ctl.status === "improved" && verdict(s.vs_prev, s.direction) !== "improved") return false;
      if (ctl.hideNoise && s.vs_prev != null && withinNoise(s.vs_prev, s.noise)) return false;
      if (needle && !`${s.case} ${s.params} ${s.suite} ${s.node} ${s.quantity}`.toLowerCase().includes(needle)) return false;
      return true;
    }).sort((a, b) => cmpVal(sortVal(a, ctl.sort), sortVal(b, ctl.sort), ctl.dir) || caseLabel(a).localeCompare(caseLabel(b)));

    const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE));
    ctl.page = Math.min(ctl.page, totalPages - 1);
    const start = ctl.page * PAGE;
    const slice = filtered.slice(start, start + PAGE);
    countEl.textContent = filtered.length ? `Showing ${start + 1}–${start + slice.length} of ${filtered.length.toLocaleString()}` : "0 matches";

    const rows = slice.map((s) => {
      const cls = isFailed(s.status) ? "cell-bad" : changeCellClass(s.vs_prev, s.direction, s.noise);
      const changeEl = isFailed(s.status)
        ? failTag(s.reason, s.status)
        : delta(s.vs_prev, s.direction, { noise: s.noise, unit: s.unit });
      return [
        benchLink(s, { node: s.node }),
        { v: h("a", { href: `#/s/${encodeURIComponent(s.suite)}` }, shortSuite(s.suite)), cls: "nowrap" },
        { v: s.node, cls: "nowrap" },
        fmtValue(s.estimate, s.unit),
        { v: changeEl, cls },
        fmtAbsPct(s.noise),
        s.points,
      ];
    });

    host.replaceChildren(table(
      ["Benchmark", "Suite", "Machine", ">Value", ">Change", ">Noise", ">Points"],
      rows,
      {
        sort: { key: ctl.sort, dir: ctl.dir, onSort, keys: ["name", "suite", "node", "value", "change", "noise", "points"] },
        empty: "no benchmarks match the current filters",
      }));

    pagerHost.replaceChildren(filtered.length > PAGE ? h("div", { class: "pager" },
      h("span", { class: "note" }, `Page ${ctl.page + 1} of ${totalPages}`),
      h("div", { class: "btns" },
        h("button", { class: "ghost", disabled: ctl.page === 0, onclick: () => { ctl.page--; draw(); window.scrollTo(0, 0); } }, "← Prev"),
        h("button", { class: "ghost", disabled: ctl.page >= totalPages - 1, onclick: () => { ctl.page++; draw(); window.scrollTo(0, 0); } }, "Next →"))) : "");
    syncHash();
  };

  const select = (label, key, values, fmt = (x) => x) => h("label", {}, label,
    h("select", { onchange: (e) => { ctl[key] = e.target.value; ctl.page = 0; draw(); } },
      values.map((v) => h("option", { value: v, selected: v === ctl[key] }, fmt(v)))));

  const main = h("main", {},
    h("nav", { class: "crumbs" }, h("a", { href: "#/" }, "overview"), " / benchmarks"),
    h("h2", {}, "Benchmarks"),
    h("div", { class: "filters" },
      nodes.length > 1 ? select("Machine", "node", ["all", ...nodes], (v) => (v === "all" ? "all" : v)) : null,
      select("Suite", "suite", ["all", ...suites], (v) => (v === "all" ? "all suites" : shortSuite(v))),
      quantities.length > 1 ? select("Quantity", "qty", ["all", ...quantities], (v) => (v === "all" ? "all" : v)) : null,
      select("Status", "status", ["all", "regressed", "improved", "failing"]),
      h("input", { type: "search", placeholder: "filter by name…", value: ctl.text, "aria-label": "filter benchmarks", oninput: (e) => { ctl.text = e.target.value; ctl.page = 0; draw(); } }),
      h("label", {}, h("input", { type: "checkbox", checked: ctl.hideNoise, onchange: (e) => { ctl.hideNoise = e.target.checked; ctl.page = 0; draw(); } }), "hide within-noise"),
      countEl),
    host, pagerHost, footer());
  draw();
  return main;
}

// ---- suites (#/suites & #/s/<suite>) ------------------------------------------------

function renderSuites() {
  const by = new Map();
  const timeBySuite = new Map();
  for (const st of state.landing.suite_time || []) {
    timeBySuite.set(st.suite, (timeBySuite.get(st.suite) || 0) + st.seconds);
  }
  for (const s of state.series) {
    const e = by.get(s.suite) || { suite: s.suite, series: 0, cases: new Set(), nodes: new Set(), regressed: 0, improved: 0, failing: 0, seconds: timeBySuite.get(s.suite) || 0 };
    e.series++; e.cases.add(`${parseSeriesSpec(s).family}|${s.quantity}`); e.nodes.add(s.node);
    if (isFailed(s.status)) e.failing++;
    else if (s.vs_prev != null && Math.abs(s.vs_prev) > state.landing.notable) {
      const v = verdict(s.vs_prev, s.direction);
      if (v === "regressed") e.regressed++; else if (v === "improved") e.improved++;
    }
    by.set(s.suite, e);
  }
  const all = [...by.values()];
  const ctl = { text: "", sort: "suite", dir: "asc" };
  const host = h("div", {});
  const pct = `${Math.round(state.landing.notable * 100)}%`;

  const onSort = (key) => {
    if (ctl.sort === key) ctl.dir = ctl.dir === "asc" ? "desc" : "asc";
    else { ctl.sort = key; ctl.dir = key === "suite" ? "asc" : "desc"; }
    draw();
  };
  const valOf = (e, k) => k === "cases" ? e.cases.size : k === "nodes" ? e.nodes.size : e[k];

  const draw = () => {
    const needle = ctl.text.trim().toLowerCase();
    const rows = all
      .filter((e) => !needle || e.suite.toLowerCase().includes(needle))
      .sort((a, b) => cmpVal(valOf(a, ctl.sort), valOf(b, ctl.sort), ctl.dir) || a.suite.localeCompare(b.suite));
    host.replaceChildren(table(
      ["Suite", ">Benchmarks", ">Series", "Machines", ">Failing", `>Regressed >${pct}`, `>Improved >${pct}`, ">Time"],
      rows.map((e) => [
        h("a", { href: `#/s/${encodeURIComponent(e.suite)}`, class: "mono" }, e.suite),
        e.cases.size, e.series,
        h("div", { class: "machines-list" }, [...e.nodes].sort().map((n) => h("span", { class: "chip" }, n))),
        e.failing ? { v: h("span", { class: "bad" }, e.failing), cls: "cell-bad" } : "0",
        e.regressed ? { v: h("span", { class: "delta regressed" }, e.regressed), cls: "cell-bad" } : "0",
        e.improved ? { v: h("span", { class: "delta improved" }, e.improved), cls: "cell-good" } : "0",
        e.seconds ? (e.seconds < 120 ? `${e.seconds}s` : `${Math.round(e.seconds / 60)}m`) : "–",
      ]),
      { sort: { key: ctl.sort, dir: ctl.dir, onSort, keys: ["suite", "cases", "series", "nodes", "failing", "regressed", "improved", "seconds"] } }));
  };

  const main = h("main", {},
    h("nav", { class: "crumbs" }, h("a", { href: "#/" }, "overview"), " / suites"),
    h("h2", {}, `Suites (${all.length})`),
    h("div", { class: "filters" },
      h("input", { type: "search", placeholder: "filter suites…", "aria-label": "filter suites", oninput: (e) => { ctl.text = e.target.value; draw(); } })),
    host, footer());
  draw();
  return main;
}

async function loadHistory(suite) {
  let hist = state.histories.get(suite);
  if (!hist) {
    hist = await getJSON(`views/history/${encodeURIComponent(safeFile(suite))}.json`);
    state.histories.set(suite, hist);
  }
  return hist;
}

function splitCaseSlash(name) {
  const parts = [];
  let cur = "", inQuote = false, angle = 0;
  for (let i = 0; i < name.length; i++) {
    const ch = name[i];
    if (ch === '"' && name[i - 1] !== "\\") { inQuote = !inQuote; cur += ch; }
    else if (!inQuote && ch === "<") { angle++; cur += ch; }
    else if (!inQuote && ch === ">" && angle > 0) { angle--; cur += ch; }
    else if (!inQuote && angle === 0 && ch === "/") { parts.push(cur); cur = ""; }
    else cur += ch;
  }
  parts.push(cur);
  return parts;
}

function natCmp(a, b) {
  const sa = String(a), sb = String(b);
  const na = /^-?\d+(\.\d+)?$/.test(sa), nb = /^-?\d+(\.\d+)?$/.test(sb);
  if (na && nb) return Number(sa) - Number(sb);
  return sa.localeCompare(sb, undefined, { numeric: true });
}

function axisDisplayName(key) {
  const m = /^arg(\d+)$/.exec(key);
  return m ? `Param ${m[1]}` : key;
}

function parseSeriesSpec(s) {
  const rawParams = s.params || "";
  if (rawParams) {
    try {
      const obj = JSON.parse(rawParams);
      if (obj && typeof obj === "object" && !Array.isArray(obj)) {
        const entries = Object.entries(obj).map(([k, v]) => [k, String(v)]);
        return { family: s.case, map: Object.fromEntries(entries), keys: entries.map(([k]) => k), explicit: true };
      }
    } catch (_) { /* fall through */ }
    return { family: s.case, map: { param: rawParams }, keys: ["param"], explicit: true };
  }
  const kase = s.case || "";
  if (kase.includes("/")) {
    const parts = splitCaseSlash(kase);
    if (parts.length > 1) {
      const map = {}, keys = [];
      let pos = 0;
      for (const seg of parts.slice(1)) {
        let key, val;
        if (seg === "real_time" || seg === "manual_time" || seg === "process_time") {
          key = "timing"; val = seg;
        } else {
          const colon = seg.startsWith('"') ? -1 : seg.indexOf(":");
          if (colon > 0) {
            key = seg.slice(0, colon).trim();
            val = seg.slice(colon + 1).trim();
          } else {
            key = `arg${++pos}`;
            val = seg;
          }
        }
        let uniqKey = key;
        for (let n = 2; uniqKey in map; n++) uniqKey = `${key}_${n}`;
        map[uniqKey] = val;
        keys.push(uniqKey);
      }
      return { family: parts[0], map, keys, explicit: false };
    }
  }
  return { family: kase, map: {}, keys: [], explicit: false };
}

function seriesVariantId(s) { return `${s.case}\0${s.params || ""}`; }

function buildBenchFamily(suiteSeries, targetCase, targetParams) {
  const targetSpec = parseSeriesSpec({ case: targetCase, params: targetParams });
  let members;
  if (targetParams) {
    members = suiteSeries.filter((s) => s.case === targetCase);
  } else if (targetCase.includes("/")) {
    members = suiteSeries.filter((s) => !s.params && parseSeriesSpec(s).family === targetSpec.family);
  } else {
    const exact = suiteSeries.filter((s) => s.case === targetCase);
    if (exact.length) {
      members = exact;
    } else {
      const byFam = suiteSeries.filter((s) => !s.params && parseSeriesSpec(s).family === targetCase);
      members = byFam.length ? byFam : exact;
    }
  }

  const parsed = members.map((s) => ({ s, spec: parseSeriesSpec(s) }));
  const keyOrder = [];
  const keySeen = new Set();
  for (const { spec } of parsed) {
    for (const k of spec.keys) if (!keySeen.has(k)) { keySeen.add(k); keyOrder.push(k); }
  }
  const varyingAxes = [];
  const constantParams = [];
  for (const key of keyOrder) {
    const vals = [...new Set(parsed.map(({ spec }) => (key in spec.map ? spec.map[key] : "(none)")))].sort(natCmp);
    if (vals.length > 1) varyingAxes.push({ key, label: axisDisplayName(key), values: vals });
    else if (vals.length === 1) constantParams.push([key, vals[0]]);
  }

  const allPositional = varyingAxes.length > 0 && varyingAxes.every((ax) => /^arg\d+$/.test(ax.key));
  const labelForMember = (spec, s) => {
    if (!varyingAxes.length) return s.params || "(default)";
    if (varyingAxes.length === 1) {
      const k = varyingAxes[0].key;
      return k in spec.map ? spec.map[k] : "(none)";
    }
    if (allPositional) return varyingAxes.map((ax) => (ax.key in spec.map ? spec.map[ax.key] : "(none)")).join(" / ");
    return varyingAxes.map((ax) => {
      const v = ax.key in spec.map ? spec.map[ax.key] : "(none)";
      return /^arg\d+$/.test(ax.key) ? v : `${ax.key}=${v}`;
    }).join(", ");
  };

  parsed.sort((a, b) => {
    for (const ax of varyingAxes) {
      const va = ax.key in a.spec.map ? a.spec.map[ax.key] : "(none)";
      const vb = ax.key in b.spec.map ? b.spec.map[ax.key] : "(none)";
      const d = natCmp(va, vb);
      if (d !== 0) return d;
    }
    return a.s.node.localeCompare(b.s.node) || a.s.quantity.localeCompare(b.s.quantity);
  });

  const enriched = parsed.map(({ s, spec }) => ({
    ...s,
    _spec: spec,
    _vlabel: labelForMember(spec, s),
    _vid: seriesVariantId(s),
  }));
  const focused = enriched.find((s) => s.case === targetCase && (s.params || "") === (targetParams || "")) || enriched[0] || null;
  const familyName = varyingAxes.length > 0 ? targetSpec.family : (focused?.case || targetCase);
  return { familyName, members: enriched, varyingAxes, constantParams, focused };
}

function groupFamilySeries(list, extra = () => ({})) {
  const groups = new Map();
  for (const s of list) {
    const spec = parseSeriesSpec(s);
    const key = `${s.suite}|${spec.family}|${s.quantity}`;
    if (!groups.has(key)) {
      groups.set(key, { suite: s.suite, family: spec.family, case: s.case, params: s.params, quantity: s.quantity, unit: s.unit, direction: s.direction, list: [] });
    }
    groups.get(key).list.push({ ...s, _spec: spec });
  }
  return [...groups.values()].map((g) => {
    const vids = new Set(g.list.map(seriesVariantId));
    g.variantCount = vids.size;
    if (g.variantCount === 1) {
      g.displayName = caseLabel(g.list[0]);
      g.href = benchHref(g.list[0]);
    } else {
      g.displayName = g.family;
      const q = new URLSearchParams();
      if (g.quantity && g.quantity !== "wall-time") q.set("q", g.quantity);
      const qs = q.toString();
      g.href = `#/b/${encodeURIComponent(g.suite)}/${encodeURIComponent(g.family)}${qs ? "?" + qs : ""}`;
    }
    return { ...g, ...extra(g) };
  });
}

function benchCard(g) {
  const n = g.newest;
  const failed = isFailed(g.status);
  const plot = h("div", { class: "spark" });
  const sub = [
    g.variantCount > 1 ? (g.list.length > MAX_AUTO_VARIANTS ? `${MAX_AUTO_VARIANTS}/${g.variantCount} params` : `${g.variantCount} params`) : null,
    g.quantity !== "wall-time" ? shortQty(g.quantity) : null,
    n && multiNode() ? n.node : null,
  ].filter(Boolean).join(" · ");
  const points = n?.points ?? 0;
  const title = g.displayName || caseLabel(g);
  const card = h("a", { class: `mini${failed ? " failed" : ""}`, href: g.href || benchHref(g, { node: n?.node }) },
    h("div", { class: "mini-head" },
      h("span", { class: "mono name", title }, title),
      n ? delta(g.change, g.direction, { noise: n.noise, unit: g.unit }) : null),
    h("div", { class: "mini-value" }, h("span", { class: "num" }, n ? fmtValue(n.estimate ?? n.latest, g.unit) : "–"), sub && h("span", { class: "note" }, sub)),
    plot,
    h("div", { class: "mini-foot" },
      failed ? failTag(g.reason, "failed") : h("span", {}, `${points} pt${points === 1 ? "" : "s"}`),
      h("span", {}, n?.noise != null ? `noise ${fmtAbsPct(n.noise)}` : "")));
  return [card, plot];
}

function drawSpark(el, seriesList, axis) {
  const capped = seriesList.slice(0, MAX_AUTO_VARIANTS);
  const index = new Map(axis.map((c, i) => [c.commit_order, i]));
  const xs = axis.map((_, i) => i);
  const ys = capped.map((s) => { const col = new Array(xs.length).fill(null); s.orders.forEach((o, k) => { if (s.estimates[k] != null && index.has(o)) col[index.get(o)] = s.estimates[k]; }); return col; });
  const vals = ys.flat().filter((v) => v != null);
  if (!vals.length) { el.append(h("div", { class: "spark-empty" }, "no measurement")); return; }
  if (typeof uPlot === "undefined") { el.append(h("div", { class: "spark-empty" }, "offline")); return; }
  const lo = Math.min(...vals), hi = Math.max(...vals), mid = (lo + hi) / 2;
  const half = Math.max((hi - lo) * 0.6, Math.abs(mid) * 0.03, 1e-12);
  const width = Math.max(120, el.clientWidth || 220);
  const multiVariant = new Set(capped.map(seriesVariantId)).size > 1;
  const chart = new uPlot({
    width, height: 48, legend: { show: false }, cursor: { show: false },
    scales: { x: { time: false, range: [-0.5, xs.length - 0.5] }, y: { range: [mid - half, mid + half] } },
    axes: [{ show: false }, { show: false }],
    series: [{}, ...capped.map((s, k) => {
      const col = multiVariant ? cssVar(SERIES_VARS[k % SERIES_VARS.length]) : seriesColor(s.node);
      return { stroke: col, width: 1.5, spanGaps: true, points: { show: xs.length <= 12 && capped.length <= 4, size: 3.5, stroke: col, fill: cssVar("--page"), width: 1.5 } };
    })],
  }, [xs, ...ys], el);
  state.charts.push(chart);
}

async function renderSuite(suite, seq) {
  const rows = state.series.filter((s) => s.suite === suite);
  const nodes = [...new Set(rows.map((r) => r.node))].sort();
  const hist = await loadHistory(suite);
  if (seq != null && seq !== state.routeSeq) return null;
  const latestBy = new Map(rows.map((r) => [`${r.case}|${r.params}|${r.quantity}|${r.node}`, r]));
  const groups = groupFamilySeries(Object.values(hist.series).map((s) => ({ ...s, suite })), (g) => {
    const mine = g.list.map((s) => latestBy.get(`${s.case}|${s.params}|${s.quantity}|${s.node}`)).filter(Boolean);
    const measured = mine.filter((r) => r.estimate != null);
    const newest = measured.sort((a, b) => {
      const ca = a.vs_prev == null ? -1 : Math.abs(a.vs_prev);
      const cb = b.vs_prev == null ? -1 : Math.abs(b.vs_prev);
      return cb - ca || b.commit_order - a.commit_order;
    })[0];
    const failing = mine.find((r) => isFailed(r.status) && (!newest || r.node === newest.node));
    return { newest, change: newest?.vs_prev ?? null, status: failing ? failing.status : "success", reason: failing?.reason };
  }).sort((a, b) => a.displayName.localeCompare(b.displayName));

  const quantities = [...new Set(rows.map((r) => r.quantity).filter(Boolean))].sort();
  const ctl = { mode: "table", text: "", qty: "all", sort: "name", dir: "asc", page: 0 };
  const host = h("div", {});
  const pagerHost = h("div", {});
  const status = h("span", { class: "note" });

  const onSort = (key) => {
    if (ctl.sort === key) ctl.dir = ctl.dir === "asc" ? "desc" : "asc";
    else { ctl.sort = key; ctl.dir = key === "name" || key === "node" ? "asc" : "desc"; }
    ctl.page = 0;
    draw();
  };

  const draw = () => {
    destroyCharts();
    const needle = ctl.text.trim().toLowerCase();
    if (ctl.mode === "table") {
      const shown = rows
        .filter((s) => (ctl.qty === "all" || s.quantity === ctl.qty) && (!needle || `${s.case} ${s.params} ${s.node} ${s.quantity}`.toLowerCase().includes(needle)))
        .sort((a, b) => {
          const v = (x) => ctl.sort === "name" ? caseLabel(x) : ctl.sort === "value" ? x.estimate : ctl.sort === "change" ? (x.vs_prev == null ? null : Math.abs(x.vs_prev)) : ctl.sort === "noise" ? x.noise : ctl.sort === "points" ? x.points : x.node;
          return cmpVal(v(a), v(b), ctl.dir) || caseLabel(a).localeCompare(caseLabel(b));
        });
      const totalPages = Math.max(1, Math.ceil(shown.length / PAGE));
      ctl.page = Math.min(ctl.page, totalPages - 1);
      const start = ctl.page * PAGE;
      const slice = shown.slice(start, start + PAGE);
      status.textContent = shown.length ? `Showing ${start + 1}–${start + slice.length} of ${shown.length.toLocaleString()} series` : "0 series";
      host.replaceChildren(table(
        ["Benchmark", "Machine", ">Latest", ">vs previous", ">vs 10 back", ">Noise", ">Points"],
        slice.map((s) => {
          const cls = isFailed(s.status) ? "cell-bad" : changeCellClass(s.vs_prev, s.direction, s.noise);
          return [
            benchLink(s, { node: s.node }),
            { v: s.node, cls: "nowrap" },
            fmtValue(s.estimate, s.unit),
            { v: isFailed(s.status) ? failTag(s.reason, s.status) : delta(s.vs_prev, s.direction, { noise: s.noise, unit: s.unit }), cls },
            delta(s.vs_10, s.direction, { unit: s.unit }),
            fmtAbsPct(s.noise),
            s.points,
          ];
        }),
        { sort: { key: ctl.sort, dir: ctl.dir, onSort, keys: ["name", "node", "value", "change", null, "noise", "points"] } }));
      pagerHost.replaceChildren(shown.length > PAGE ? h("div", { class: "pager" },
        h("span", { class: "note" }, `Page ${ctl.page + 1} of ${totalPages}`),
        h("div", { class: "btns" },
          h("button", { class: "ghost", disabled: ctl.page === 0, onclick: () => { ctl.page--; draw(); window.scrollTo(0, 0); } }, "← Prev"),
          h("button", { class: "ghost", disabled: ctl.page >= totalPages - 1, onclick: () => { ctl.page++; draw(); window.scrollTo(0, 0); } }, "Next →"))) : "");
    } else {
      const shown = groups.filter((g) => (ctl.qty === "all" || g.quantity === ctl.qty) && (!needle || g.displayName.toLowerCase().includes(needle) || g.list.some((s) => caseLabel(s).toLowerCase().includes(needle))));
      status.textContent = `${shown.length} of ${groups.length} benchmarks`;
      pagerHost.replaceChildren();
      const grid = h("div", { class: "grid" });
      const cards = shown.map((g) => { const [card, plot] = benchCard(g); grid.append(card); return [g, plot]; });
      host.replaceChildren(grid);
      requestAnimationFrame(() => {
        if (seq != null && seq !== state.routeSeq) return;
        for (const [g, plot] of cards) drawSpark(plot, g.list, hist.commits);
      });
    }
  };

  const btnTable = h("button", { class: "active", onclick: () => { ctl.mode = "table"; btnTable.className = "active"; btnGrid.className = ""; draw(); } }, "Table");
  const btnGrid = h("button", { onclick: () => { ctl.mode = "grid"; btnGrid.className = "active"; btnTable.className = ""; draw(); } }, "Sparklines");
  const qtySelect = quantities.length > 1 ? h("label", {}, "Quantity",
    h("select", { onchange: (e) => { ctl.qty = e.target.value; ctl.page = 0; draw(); } },
      ["all", ...quantities].map((qv) => h("option", { value: qv, selected: qv === ctl.qty }, qv === "all" ? "all" : qv)))) : null;
  const failing = groups.filter((g) => isFailed(g.status)).length;
  const main = h("main", {},
    h("nav", { class: "crumbs" }, h("a", { href: "#/" }, "overview"), " / ", h("a", { href: "#/suites" }, "suites"), " / ", suite),
    h("h2", { class: "mono title" }, suite),
    h("p", { class: "lead" }, `${groups.length} benchmarks (${rows.length.toLocaleString()} series) across ${hist.commits.length} measured commits`, failing ? [", ", h("span", { class: "bad" }, `${failing} failing`)] : null),
    h("div", { class: "filters" },
      h("div", { class: "seg" }, btnTable, btnGrid),
      qtySelect,
      h("input", { type: "search", placeholder: "filter benchmarks…", "aria-label": "filter benchmarks", oninput: (e) => { ctl.text = e.target.value; ctl.page = 0; draw(); } }),
      status, sharedLegend(nodes)),
    host, pagerHost, footer());
  draw();
  return main;
}

// ---- benchmark detail (#/b/<suite>/<case>): Conbench summary + ASV plot controls ----

function exportCsv(seriesList, axis, suite, kase, unit) {
  const index = new Map(axis.map((c) => [c.commit_order, c]));
  const esc = (v) => { const s = v == null ? "" : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const lines = [["commit", "date", "machine", "benchmark", "params", "quantity", "unit", "median", "min", "max", "n", "status", "subject"].join(",")];
  for (const s of seriesList) {
    for (let i = 0; i < s.orders.length; i++) {
      const c = index.get(s.orders[i]) || {};
      lines.push([c.sha, c.date, s.node, s.case, s.params, s.quantity, unit, s.estimates[i], s.mins?.[i], s.maxs?.[i], s.ns?.[i], s.statuses?.[i], c.subject].map(esc).join(","));
    }
  }
  const blob = new Blob([lines.join("\n") + "\n"], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: `${safeFile(suite)}-${safeFile(kase)}.csv` });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function renderBench(suite, kase, query, seq) {
  const initParams = query.get("params") || "";
  const quantity = query.get("q") || "wall-time";
  const nodeFilter = query.get("node") || "all";
  const hist = await loadHistory(suite);
  if (seq != null && seq !== state.routeSeq) return null;

  const fam = buildBenchFamily(Object.values(hist.series), kase, initParams);
  const { familyName, members, varyingAxes, constantParams, focused } = fam;
  const quantities = [...new Set(members.map((s) => s.quantity))];
  const activeQty = quantities.includes(quantity) ? quantity : (quantities[0] || quantity);
  const nodes = [...new Set(members.map((s) => s.node))].sort();
  const totalVariants = new Set(members.filter((s) => s.quantity === activeQty).map((s) => s._vid)).size;

  // Default parameter selection: show all parameter variants together on the same graph
  // (up to MAX_AUTO_VARIANTS); for larger Cartesian grids, expand one varying parameter
  // around the clicked benchmark so the chart stays immediately readable.
  const selectedAxes = new Map();
  const valOf = (s, key) => (key in s._spec.map ? s._spec.map[key] : "(none)");
  const pinTo = (ref) => {
    for (const ax of varyingAxes) {
      const v = ref ? valOf(ref, ax.key) : ax.values[0];
      selectedAxes.set(ax.key, new Set([v]));
    }
  };
  const selectAllAxes = () => {
    for (const ax of varyingAxes) selectedAxes.set(ax.key, new Set(ax.values));
  };

  if (totalVariants <= MAX_AUTO_VARIANTS) {
    selectAllAxes();
  } else {
    pinTo(focused);
    // Prefer the last varying axis with <= MAX_AUTO_VARIANTS values (typically innermost param);
    // otherwise pick the smallest varying axis and show up to 8 values including the focused one.
    const goodAxis = [...varyingAxes].reverse().find((ax) => ax.values.length <= MAX_AUTO_VARIANTS);
    if (goodAxis) {
      selectedAxes.set(goodAxis.key, new Set(goodAxis.values));
    } else if (varyingAxes.length) {
      const smallest = [...varyingAxes].sort((a, b) => a.values.length - b.values.length)[0];
      const fv = focused ? valOf(focused, smallest.key) : smallest.values[0];
      const pick = new Set(smallest.values.slice(0, 8));
      pick.add(fv);
      selectedAxes.set(smallest.key, pick);
    }
  }

  let summaryVid = focused?._vid || null;
  const plotOpts = { logY: false, zeroY: false, dateX: false, showSpread: true, showNoise: true };

  const setParam = (k, v) => {
    const q = new URLSearchParams(query);
    if (v == null || v === "" || v === "all" || (k === "q" && v === "wall-time")) q.delete(k); else q.set(k, v);
    location.hash = `#/b/${encodeURIComponent(suite)}/${encodeURIComponent(kase)}${q.toString() ? "?" + q : ""}`;
  };
  const select = (label, key, values, current, fmt = (x) => x) => h("label", {}, label,
    h("select", { onchange: (e) => setParam(key, e.target.value) },
      values.map((v) => h("option", { value: v, selected: v === current }, fmt(v)))));

  const summaryHost = h("div", {});
  const paramsHost = h("div", {});
  const chartWrap = h("div", { class: "chart-wrap" });
  const historyTableHost = h("div", {});

  const latestIndex = new Map();
  for (const r of state.series) {
    if (r.suite === suite && r.quantity === activeQty) {
      latestIndex.set(`${r.case}\0${r.params || ""}\0${r.node}`, r);
    }
  }

  const getShown = () => members.filter((s) =>
    s.quantity === activeQty &&
    (nodeFilter === "all" || s.node === nodeFilter) &&
    varyingAxes.every((ax) => selectedAxes.get(ax.key)?.has(valOf(s, ax.key))));

  const redraw = () => {
    destroyCharts();
    for (const f of state.cleanup) f();
    state.cleanup = [];

    const shown = getShown();
    const shownVids = [...new Set(shown.map((s) => s._vid))];
    if (!shownVids.includes(summaryVid)) summaryVid = shownVids[0] || null;
    const summarySeries = shown.filter((s) => s._vid === summaryVid);
    const latestRows = summarySeries
      .map((s) => latestIndex.get(`${s.case}\0${s.params || ""}\0${s.node}`))
      .filter(Boolean);
    const kpi = latestRows.filter((r) => r.estimate != null).sort((a, b) => b.commit_order - a.commit_order)[0];
    const unit = shown[0]?.unit || kpi?.unit || members[0]?.unit || "";
    const direction = shown[0]?.direction || kpi?.direction || members[0]?.direction || "";
    const few = kpi && kpi.points < 3;
    const lastNs = summarySeries.flatMap((x) => (x.ns || []).filter((n, k) => x.estimates[k] != null));
    const reps = lastNs.length ? lastNs[lastNs.length - 1] : null;
    const multiVariantShown = shownVids.length > 1;
    const multiSeries = shown.length > 1;

    // Compact legend/table label using only the parameter axes that vary within the current plot.
    const activeVarying = varyingAxes.filter((ax) => new Set(shown.map((s) => valOf(s, ax.key))).size > 1);
    const activeAllPos = activeVarying.length > 0 && activeVarying.every((ax) => /^arg\d+$/.test(ax.key));
    const plotLabel = (s) => {
      if (!activeVarying.length) return s._vlabel;
      if (activeVarying.length === 1) return valOf(s, activeVarying[0].key);
      if (activeAllPos) return activeVarying.map((ax) => valOf(s, ax.key)).join(" / ");
      return activeVarying.map((ax) => (/^arg\d+$/.test(ax.key) ? valOf(s, ax.key) : `${ax.key}=${valOf(s, ax.key)}`)).join(", ");
    };
    const plotted = shown.map((s) => ({ ...s, _plabel: plotLabel(s) }));

    // Map single-axis values to chart colors when exactly one parameter axis varies in the plot.
    const swatchAxis = activeVarying.length === 1 && new Set(shown.map((s) => s.node)).size <= 1 ? activeVarying[0].key : null;
    const swatchColors = new Map();
    if (swatchAxis) {
      plotted.forEach((s, k) => {
        const v = valOf(s, swatchAxis);
        if (!swatchColors.has(v)) swatchColors.set(v, cssVar(SERIES_VARS[k % SERIES_VARS.length]));
      });
    }

    const fixedAxes = varyingAxes.filter((ax) => !activeVarying.includes(ax));
    const summaryRep = plotted.find((s) => s._vid === summaryVid) || plotted[0];
    const fixedSummaryText = multiVariantShown && fixedAxes.length && summaryRep
      ? fixedAxes.map((ax) => (/^arg\d+$/.test(ax.key) ? valOf(summaryRep, ax.key) : `${ax.key}=${valOf(summaryRep, ax.key)}`)).join(" / ")
      : null;

    const variantSelector = multiVariantShown
      ? h("select", {
          class: "summary-variant",
          "aria-label": "Summarized parameter variant",
          onchange: (e) => { summaryVid = e.target.value; redraw(); },
        }, shownVids.map((vid) => {
          const rep = plotted.find((s) => s._vid === vid);
          return h("option", { value: vid, selected: vid === summaryVid }, rep?._plabel || rep?._vlabel || vid);
        }))
      : null;

    summaryHost.replaceChildren(h("aside", { class: "summary-box" },
      h("h3", {}, "Summary"),
      variantSelector,
      fixedSummaryText ? h("p", { class: "summary-fixed mono", title: "Fixed parameter axes" }, fixedSummaryText) : null,
      kpi ? h("dl", { class: "kv" },
        h("dt", {}, "Latest"), h("dd", { class: "big num" }, fmtValue(kpi.estimate, unit)),
        h("dt", {}, "vs prev"), h("dd", {}, delta(kpi.vs_prev, direction, { unit, noise: kpi.noise })),
        h("dt", {}, "vs 10 back"), h("dd", {}, delta(kpi.vs_10, direction, { unit })),
        h("dt", {}, "vs 100 back"), h("dd", {}, delta(kpi.vs_100, direction, { unit })),
        h("dt", {}, "Noise"), h("dd", { class: "num" }, few ? "needs 3+" : fmtAbsPct(kpi.noise)),
        h("dt", {}, "Points"), h("dd", { class: "num" }, `${kpi.points}${reps > 1 ? ` (${reps} reps)` : ""}`),
        h("dt", {}, "Commit"), h("dd", {}, h("a", { href: `#/c/${encodeURIComponent(kpi.sha)}`, class: "mono" }, shortSha(kpi.sha, 8))),
        h("dt", {}, "Machine"), h("dd", {}, kpi.node),
        h("dt", {}, "Suite"), h("dd", {}, h("a", { href: `#/s/${encodeURIComponent(suite)}`, class: "mono" }, suite)),
      ) : h("p", { class: "note" }, "No successful measurement yet."),
      h("div", { class: "summary-actions" },
        h("button", { class: "ghost", onclick: () => exportCsv(shown, hist.commits, suite, familyName, unit) }, "Download CSV"),
        kpi ? h("a", { class: "button", href: `#/c/${encodeURIComponent(kpi.sha)}` }, "Open commit") : null,
        kpi && upstreamFileUrl(kpi.sha, suite) ? h("a", { class: "button", href: upstreamFileUrl(kpi.sha, suite), target: "_blank", rel: "noopener" }, "View suite") : null)));

    if (varyingAxes.length > 0) {
      const refForPin = shown.find((s) => s._vid === summaryVid) || focused || members[0];
      paramsHost.replaceChildren(h("aside", { class: "params-box" },
        h("div", { class: "params-head" },
          h("h3", {}, "Parameters"),
          h("span", { class: "params-actions" },
            h("button", { class: "link-btn", onclick: () => { selectAllAxes(); redraw(); } }, "all"),
            "·",
            h("button", { class: "link-btn", onclick: () => { pinTo(refForPin); redraw(); } }, "only 1"))),
        varyingAxes.map((ax) => {
          const sel = selectedAxes.get(ax.key);
          const refVal = refForPin ? valOf(refForPin, ax.key) : ax.values[0];
          return h("div", { class: "param-group" },
            h("div", { class: "param-group-head" },
              h("span", {}, ax.label),
              h("span", { class: "params-actions" },
                h("button", { class: "link-btn", onclick: () => { selectedAxes.set(ax.key, new Set(ax.values)); redraw(); } }, "all"),
                "·",
                h("button", { class: "link-btn", onclick: () => { selectedAxes.set(ax.key, new Set([sel.has(refVal) ? refVal : ax.values[0]])); redraw(); } }, "1"))),
            h("div", { class: "param-values" }, ax.values.map((val) => {
              const checked = sel.has(val);
              const sw = swatchAxis === ax.key ? swatchColors.get(val) : null;
              return h("label", { class: "param-opt" },
                h("input", {
                  type: "checkbox",
                  checked,
                  onchange: (e) => {
                    if (e.target.checked) sel.add(val);
                    else if (sel.size > 1) sel.delete(val);
                    else { e.target.checked = true; return; }
                    redraw();
                  },
                }),
                sw ? h("span", { class: "swatch", style: `border-color:${sw}` }) : null,
                h("span", { class: "mono" }, val));
            })));
        }),
        h("div", { class: "params-foot" },
          `Showing ${shownVids.length} of ${totalVariants} variants`,
          constantParams.length ? ` · ${constantParams.map(([k, v]) => (/^arg\d+$/.test(k) ? v : `${k}=${v}`)).join(", ")}` : "")));
    } else {
      paramsHost.replaceChildren();
    }

    requestAnimationFrame(() => {
      if (seq != null && seq !== state.routeSeq) return;
      drawHistory(chartWrap, plotted, hist.commits, unit, familyName, plotOpts, multiSeries ? null : kpi?.noise);
    });

    // Full clickable history table (newest first)
    const byOrder = new Map(hist.commits.map((c) => [c.commit_order, c]));
    const histRows = [];
    const scale = pickScale(plotted.flatMap((s) => s.estimates), unit);
    for (const s of plotted) {
      const sLatest = latestIndex.get(`${s.case}\0${s.params || ""}\0${s.node}`);
      let prev = null;
      for (let i = 0; i < s.orders.length; i++) {
        const c = byOrder.get(s.orders[i]) || {};
        const est = s.estimates[i];
        const rel = est != null && prev != null && prev > 0 ? est / prev - 1 : null;
        const mn = s.mins?.[i], mx = s.maxs?.[i], n = s.ns?.[i];
        const range = est != null && n > 1 && mn != null && mx != null ? `${fmtNum(mn / scale[1])}–${fmtNum(mx / scale[1])} (${n})` : n ? `n=${n}` : "–";
        histRows.push({
          order: s.orders[i],
          cells: [
            { v: h("a", { href: `#/c/${encodeURIComponent(c.sha || "")}`, class: "mono" }, c.short || ""), cls: "nowrap" },
            { v: fmtDate(c.date), cls: "nowrap" },
            (c.subject || "").length > 64 ? c.subject.slice(0, 62) + "…" : (c.subject || ""),
            { v: s.node, cls: "nowrap" },
            ...(multiVariantShown ? [{ v: h("span", { class: "mono" }, s._plabel), cls: "nowrap" }] : []),
            est != null ? fmtValue(est, unit, scale) : failTag(s.reasons?.[i], s.statuses?.[i] || "failed"),
            range,
            { v: delta(rel, direction, { unit, noise: sLatest?.noise }), cls: changeCellClass(rel, direction, sLatest?.noise) },
          ],
        });
        if (est != null) prev = est;
      }
    }
    histRows.sort((a, b) => b.order - a.order);
    const headers = ["Commit", "Date", "Subject", "Machine", ...(multiVariantShown ? ["Parameters"] : []), `>Median`, ">Min–Max (n)", ">vs prev"];
    let histPage = 0;
    const drawHistTable = () => {
      const totalPages = Math.max(1, Math.ceil(histRows.length / PAGE));
      histPage = Math.min(histPage, totalPages - 1);
      const start = histPage * PAGE;
      const slice = histRows.slice(start, start + PAGE);
      const pager = histRows.length > PAGE ? h("div", { class: "pager" },
        h("span", { class: "note" }, `Showing ${start + 1}–${start + slice.length} of ${histRows.length.toLocaleString()} · Page ${histPage + 1} of ${totalPages}`),
        h("div", { class: "btns" },
          h("button", { class: "ghost", disabled: histPage === 0, onclick: () => { histPage--; drawHistTable(); } }, "← Prev"),
          h("button", { class: "ghost", disabled: histPage >= totalPages - 1, onclick: () => { histPage++; drawHistTable(); } }, "Next →"))) : null;
      historyTableHost.replaceChildren(
        h("h3", {}, `Measurements (${histRows.length.toLocaleString()})`),
        table(headers, slice.map((r) => r.cells), { empty: "no measurements" }),
        pager);
    };
    drawHistTable();
  };

  const failing = members
    .map((s) => latestIndex.get(`${s.case}\0${s.params || ""}\0${s.node}`))
    .filter((r) => r && (nodeFilter === "all" || r.node === nodeFilter) && isFailed(r.status));
  const check = (label, key) => h("label", {},
    h("input", { type: "checkbox", checked: plotOpts[key], onchange: (e) => { plotOpts[key] = e.target.checked; redraw(); } }), label);

  const controlsBar = h("div", { class: "plot-controls" },
    check("Log Y", "logY"),
    check("Zero Y", "zeroY"),
    check("Date axis", "dateX"),
    check("Min–max", "showSpread"),
    check("±2× noise", "showNoise"),
    quantities.length > 1 ? select("Quantity", "q", quantities, activeQty) : null,
    nodes.length > 1 ? select("Machine", "node", ["all", ...nodes], nodeFilter) : null);

  const activeDir = members.find((s) => s.quantity === activeQty)?.direction || members[0]?.direction;
  const leadBits = [
    activeQty,
    activeDir ? activeDir.replace(/-/g, " ") : null,
    totalVariants > 1 ? `${totalVariants} parameter variants` : null,
  ].filter(Boolean).join(" · ");

  const main = h("main", {},
    h("nav", { class: "crumbs" }, h("a", { href: "#/" }, "overview"), " / ", h("a", { href: "#/suites" }, "suites"), " / ", h("a", { href: `#/s/${encodeURIComponent(suite)}` }, suite)),
    h("h2", { class: "mono title" }, familyName),
    h("p", { class: "lead" }, leadBits),
    failing.slice(0, 5).map((f) => h("div", { class: "banner bad" }, `Latest attempt on ${f.node} (`, h("a", { href: `#/c/${encodeURIComponent(f.attempt_sha)}`, class: "mono" }, shortSha(f.attempt_sha)), `) failed: `, h("code", {}, f.reason || f.status))),
    h("div", { class: "detail-grid" },
      h("div", { class: "detail-side" }, summaryHost, paramsHost),
      h("div", {}, controlsBar, chartWrap)),
    historyTableHost,
    footer());

  redraw();
  return main;
}

function drawHistory(wrap, seriesList, axis, unit, title, plotOpts, noise) {
  wrap.replaceChildren();
  if (!seriesList.length || !axis.length) { wrap.append(h("div", { class: "empty" }, "no points to draw")); return; }
  const index = new Map(axis.map((c, i) => [c.commit_order, i]));
  const useDate = Boolean(plotOpts.dateX && axis.every((c) => parseDate(c.date)));
  const xs = useDate ? axis.map((c) => Math.floor(parseDate(c.date).getTime() / 1000)) : axis.map((_, i) => i);
  const allValues = seriesList.flatMap((s) => s.estimates).filter((v) => v != null);
  const scale = pickScale(allValues, unit);
  const multiNodeInPlot = new Set(seriesList.map((s) => s.node)).size > 1;
  const multiVariantInPlot = new Set(seriesList.map(seriesVariantId)).size > 1;
  const labelFor = (s) => {
    const vlabel = s._plabel ?? s._vlabel ?? (s.params || "(default)");
    const parts = [multiNodeInPlot ? s.node : null, multiVariantInPlot ? vlabel : null].filter(Boolean);
    return parts.join(" · ") || s.node;
  };
  const strokeFor = (s, k) => (multiVariantInPlot ? cssVar(SERIES_VARS[k % SERIES_VARS.length]) : seriesColor(s.node));
  const dashFor = (k) => (multiVariantInPlot ? SERIES_DASHES[Math.floor(k / SERIES_VARS.length) % SERIES_DASHES.length] : null);
  const ys = seriesList.map((s) => { const col = new Array(xs.length).fill(null); s.orders.forEach((o, k) => { if (s.estimates[k] != null && index.has(o)) col[index.get(o)] = s.estimates[k] / scale[1]; }); return col; });
  const fails = seriesList.map((s) => { const col = new Array(xs.length).fill(null); s.orders.forEach((o, k) => { if (isFailed(s.statuses[k]) && index.has(o)) col[index.get(o)] = s.reasons?.[k] || s.statuses[k]; }); return col; });
  const spread = seriesList.map((s) => { const col = new Array(xs.length).fill(null); s.orders.forEach((o, k) => { if (s.estimates[k] != null && index.has(o)) col[index.get(o)] = { n: s.ns?.[k], min: s.mins?.[k], max: s.maxs?.[k] }; }); return col; });
  const nFailed = fails.flat().filter(Boolean).length;
  if (!allValues.length) {
    wrap.append(h("div", { class: "empty" }, `No successful measurements (${nFailed} failed attempts over ${axis.length} commits).`));
    return;
  }
  if (typeof uPlot === "undefined") {
    wrap.append(h("div", { class: "empty" }, "Chart library (uPlot) unavailable offline; see the table below."));
    return;
  }
  const canLog = plotOpts.logY && allValues.every((v) => v > 0);
  const tip = h("div", { class: "tip" });
  const titleRow = h("div", { class: "chart-title" },
    h("span", {}, `${title}${scale[0] ? ` (${scale[0]})` : ""}`),
    h("span", { class: "note" }, `${axis.length} commits`, seriesList.length > 1 ? ` · ${seriesList.length} series` : null, nFailed ? [" · ", h("span", { class: "bad" }, `${nFailed} failed`)] : null, " · drag to zoom, double-click to reset, click point to open commit"));
  const el = h("div", { class: "chart" });
  wrap.append(titleRow, el, tip);
  const size = () => ({ width: Math.max(240, wrap.clientWidth - 16), height: window.innerWidth < 600 ? 220 : 320 });

  // Draw optional ±2× noise band (single series with noise), min–max whiskers, and failure marks.
  const drawOverlays = (u) => {
    const ctx = u.ctx;
    ctx.save();
    if (plotOpts.showNoise && noise != null && seriesList.length === 1) {
      const vals = ys[0].filter((v) => v != null).sort((a, b) => a - b);
      if (vals.length) {
        const med = vals[Math.floor(vals.length / 2)];
        const hi = u.valToPos(med * (1 + 2 * noise), "y", true);
        const lo = u.valToPos(Math.max(canLog ? 1e-12 : 0, med * (1 - 2 * noise)), "y", true);
        ctx.fillStyle = "rgba(31, 119, 180, 0.08)";
        ctx.fillRect(u.bbox.left, Math.min(hi, lo), u.bbox.width, Math.abs(lo - hi));
      }
    }
    if (plotOpts.showSpread) {
      seriesList.forEach((s, k) => {
        ctx.strokeStyle = strokeFor(s, k);
        ctx.globalAlpha = 0.45;
        ctx.lineWidth = 1.5 * devicePixelRatio;
        for (let i = 0; i < xs.length; i++) {
          const sp = spread[k][i];
          if (!sp || sp.n <= 1 || sp.min == null || sp.max == null) continue;
          const x = u.valToPos(xs[i], "x", true);
          const y1 = u.valToPos(sp.min / scale[1], "y", true);
          const y2 = u.valToPos(sp.max / scale[1], "y", true);
          ctx.beginPath(); ctx.moveTo(x, y1); ctx.lineTo(x, y2); ctx.stroke();
        }
      });
      ctx.globalAlpha = 1;
    }
    const yFail = u.bbox.top + u.bbox.height + 3 * devicePixelRatio;
    ctx.fillStyle = cssVar("--critical");
    fails.forEach((col) => col.forEach((f, i) => {
      if (!f) return;
      const x = u.valToPos(xs[i], "x", true);
      ctx.fillRect(x - 3 * devicePixelRatio, yFail, 6 * devicePixelRatio, 3 * devicePixelRatio);
    }));
    ctx.restore();
  };

  const xScale = useDate
    ? { time: true }
    : { time: false, range: [-0.5, Math.max(0.5, xs.length - 0.5)] };
  const yScale = canLog
    ? { distr: 3 }
    : {
        range: (u, min, max) => {
          const pad = (max - min) * 0.2 || max * 0.1 || 1;
          return [plotOpts.zeroY ? 0 : Math.max(0, min - pad), max + pad];
        },
      };

  const axisInk = cssVar("--ink-2");
  const xAxis = useDate
    ? { stroke: axisInk, grid: { show: false }, ticks: { stroke: cssVar("--axis"), width: 1 }, font: "12px system-ui, sans-serif" }
    : {
        stroke: axisInk, grid: { show: false }, ticks: { stroke: cssVar("--axis"), width: 1 }, font: "12px system-ui, sans-serif", space: 72, gap: 6,
        splits: (u, axisIdx, min, max, incr) => { const step = Math.max(1, Math.ceil(incr)); const out = []; for (let i = Math.max(0, Math.ceil(min)); i <= Math.min(xs.length - 1, Math.floor(max)); i += step) out.push(i); return out; },
        values: (u, vals) => vals.map((v) => axis[v] ? (axis[v].short.includes("+dirty") ? axis[v].short.slice(0, 13) : axis[v].short.slice(0, 7)) : ""),
      };

  const showPts = axis.length <= 40;
  const opts = {
    ...size(),
    legend: { show: seriesList.length > 1, live: false },
    cursor: { points: { size: 7 }, drag: { x: true, y: false } },
    scales: { x: xScale, y: yScale },
    axes: [
      xAxis,
      { stroke: axisInk, grid: { stroke: cssVar("--grid"), width: 1 }, ticks: { show: false }, font: "12px system-ui, sans-serif", size: 64, values: (u, vals) => vals.map((v) => fmtNum(v, 3)) },
    ],
    series: [{ label: useDate ? "date" : "commit" }, ...seriesList.map((s, k) => {
      const lbl = labelFor(s);
      const col = strokeFor(s, k);
      const dash = dashFor(k);
      return {
        label: lbl, stroke: col, width: 1.75, spanGaps: true,
        ...(dash ? { dash } : {}),
        points: { show: showPts, size: seriesList.length > 6 ? 5 : 6, stroke: col, fill: cssVar("--page"), width: 1.5 },
      };
    })],
    hooks: {
      draw: [drawOverlays],
      setCursor: [(u) => {
        const i = u.cursor.idx;
        if (i == null || !axis[i]) { tip.style.display = "none"; return; }
        const c = axis[i];
        const showRowSpread = seriesList.length <= 4;
        const rows = seriesList.map((s, k) => {
          const v = ys[k][i];
          let prev = null;
          for (let j = i - 1; j >= 0; j--) if (ys[k][j] != null) { prev = ys[k][j]; break; }
          const rel = v != null && prev > 0 ? v / prev - 1 : null;
          const lbl = labelFor(s);
          const col = strokeFor(s, k);
          return h("div", { class: "row" }, h("span", { class: "key", style: `border-color:${col}` }),
            h("span", { class: "v num" }, v != null ? `${fmtNum(v)} ${scale[0]}` : fails[k][i] ? h("span", { class: "bad" }, `failed: ${fails[k][i]}`) : "–"),
            h("span", { class: "s num" }, rel == null ? "" : fmtPct(rel)), seriesList.length > 1 ? h("span", { class: "s mono" }, lbl) : null,
            showRowSpread && spread[k][i]?.n > 1 ? h("span", { class: "spread" }, `n=${spread[k][i].n}, ${fmtNum(spread[k][i].min / scale[1])}–${fmtNum(spread[k][i].max / scale[1])} ${scale[0]}`) : null);
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
  const onResize = () => chart.setSize(size());
  window.addEventListener("resize", onResize);
  state.cleanup.push(() => window.removeEventListener("resize", onResize));
}

// ---- commit detail (#/c/<sha>) ------------------------------------------------------

async function renderCommit(sha, seq) {
  const c = await loadCommit(sha);
  if (seq != null && seq !== state.routeSeq) return null;
  const ctl = { needle: "", min: 0, hideNoise: false, sort: "change", dir: "desc", page: 0 };
  const body = h("div", {});

  const onSort = (key) => {
    if (ctl.sort === key) ctl.dir = ctl.dir === "asc" ? "desc" : "asc";
    else { ctl.sort = key; ctl.dir = key === "name" || key === "suite" ? "asc" : "desc"; }
    ctl.page = 0;
    draw();
  };

  const draw = () => {
    const needle = ctl.needle.trim().toLowerCase();
    const rows = c.changes
      .filter((ch) => Math.abs(ch.rel_change) >= ctl.min && !(ctl.hideNoise && withinNoise(ch.rel_change, ch.noise))
        && (!needle || `${ch.suite} ${ch.case} ${ch.params} ${ch.node}`.toLowerCase().includes(needle)))
      .sort((a, b) => {
        const v = (x) => ctl.sort === "name" ? caseLabel(x) : ctl.sort === "suite" ? x.suite : ctl.sort === "before" ? x.prev_estimate : ctl.sort === "after" ? x.estimate : ctl.sort === "noise" ? x.noise : Math.abs(x.rel_change);
        return cmpVal(v(a), v(b), ctl.dir);
      });
    const totalPages = Math.max(1, Math.ceil(rows.length / PAGE));
    ctl.page = Math.min(ctl.page, totalPages - 1);
    const start = ctl.page * PAGE;
    const slice = rows.slice(start, start + PAGE);
    const pager = rows.length > PAGE ? h("div", { class: "pager" },
      h("span", { class: "note" }, `Showing ${start + 1}–${start + slice.length} of ${rows.length.toLocaleString()} (${c.changes.length.toLocaleString()} compared) · Page ${ctl.page + 1} of ${totalPages}`),
      h("div", { class: "btns" },
        h("button", { class: "ghost", disabled: ctl.page === 0, onclick: () => { ctl.page--; draw(); } }, "← Prev"),
        h("button", { class: "ghost", disabled: ctl.page >= totalPages - 1, onclick: () => { ctl.page++; draw(); } }, "Next →")))
      : h("p", { class: "note" }, `${rows.length.toLocaleString()} of ${c.changes.length.toLocaleString()} compared series`);
    body.replaceChildren(table(
      ["Benchmark", "Suite", "Machine", "Baseline", ">Before", ">After", ">Change", ">Noise"],
      slice.map((ch) => [
        benchLink(ch, { node: ch.node }),
        { v: shortSuite(ch.suite), cls: "nowrap" },
        { v: ch.node, cls: "nowrap" },
        { v: h("span", {}, gapTag(ch), " ", h("a", { href: `#/c/${encodeURIComponent(ch.prev_sha)}`, class: "mono" }, shortSha(ch.prev_sha, 7))), cls: "nowrap" },
        fmtValue(ch.prev_estimate, ch.unit),
        fmtValue(ch.estimate, ch.unit),
        { v: delta(ch.rel_change, ch.direction, { noise: ch.noise, unit: ch.unit }), cls: changeCellClass(ch.rel_change, ch.direction, ch.noise) },
        fmtAbsPct(ch.noise),
      ]),
      { sort: { key: ctl.sort, dir: ctl.dir, onSort, keys: ["name", "suite", null, null, "before", "after", "change", "noise"] }, empty: "no changes match" }),
      pager);
  };

  const upUrl = upstreamCommit(c.sha);
  const leadParts = joinParts([
    c.committer_date ? fmtDate(c.committer_date) : null,
    upUrl ? h("a", { href: upUrl, target: "_blank", rel: "noopener" }, "upstream commit") : null,
    c.parent ? h("span", {}, "parent ", h("a", { href: `#/c/${encodeURIComponent(c.parent)}`, class: "mono" }, shortSha(c.parent))) : null,
  ]);
  const main = h("main", {},
    h("nav", { class: "crumbs" }, h("a", { href: "#/" }, "overview"), " / commit"),
    h("h2", {}, h("span", { class: "mono" }, c.short), c.subject ? ` ${c.subject}` : ""),
    leadParts.length ? h("p", { class: "lead" }, ...leadParts) : null,
    h("h3", {}, "Runs"),
    table(["Machine", "State", "Reason", "Started", "Finished", ">Suites ok", ">Failed suites", ">Results", ">Failed", ""], c.nodes.map((n) => {
      const filesUrl = runFiles(c.sha, n.node);
      const logUrl = runLog(c.sha, n.node, n.has_log);
      const links = joinParts([
        filesUrl ? h("a", { href: filesUrl, target: "_blank", rel: "noopener" }, "parquet") : null,
        logUrl ? h("a", { href: logUrl, target: "_blank", rel: "noopener" }, "log") : null,
      ]);
      return [
        n.node, n.state === "finished" ? "ok" : h("span", { class: "bad" }, n.state), n.reason || "", fmtDate(n.started_at), fmtDate(n.finished_at),
        n.suites_ok, n.suites_failed ? h("span", { class: "bad" }, n.suites_failed) : "0", n.results,
        n.failed_results ? h("span", { class: "bad" }, n.failed_results) : "0",
        links.length ? h("span", {}, ...links) : "",
      ];
    })),
    c.failures?.length ? [h("h3", {}, h("span", { class: "bad" }, `Failed results (${c.failures.length})`)),
      table(["Benchmark", "Suite", "Machine", "Quantity", "Reason"], c.failures.map((f) => [
        h("a", { href: benchHref(f, { node: f.node }), class: "mono" }, caseLabel(f)), shortSuite(f.suite), f.node, f.quantity, h("code", {}, f.reason || f.status)]))] : null,
    h("h3", {}, "Changes against previous measured point"),
    h("div", { class: "filters" },
      h("label", {}, "Min |Δ|", h("select", { onchange: (e) => { ctl.min = Number(e.target.value) / 100; ctl.page = 0; draw(); } },
        [0, 1, 2, 5, 10].map((p) => h("option", { value: p }, p ? `≥ ${p}%` : "all")))),
      h("label", {}, h("input", { type: "checkbox", onchange: (e) => { ctl.hideNoise = e.target.checked; ctl.page = 0; draw(); } }), "hide within-noise"),
      h("input", { type: "search", placeholder: "filter benchmarks…", "aria-label": "filter benchmarks", oninput: (e) => { ctl.needle = e.target.value; ctl.page = 0; draw(); } })),
    body, footer());
  draw();
  return main;
}

// ---- about & footer -----------------------------------------------------------------

function renderAbout() {
  const P = state.project, repo = P.repository, pct = `${Math.round(state.landing.notable * 100)}%`;
  const upstream = (P.upstream?.url || "").replace(/\.git$/, "");
  const isRuns = state.manifest?.source === "runs";
  return h("main", { class: "prose" },
    h("nav", { class: "crumbs" }, h("a", { href: "#/" }, "overview"), " / about"),
    h("h2", {}, "About this dashboard"),
    h("p", { class: "lead" }, P.description || "Benchmark results and history, built with benchx."),
    h("div", { class: "cols" },
      h("section", { class: "panel" }, h("h3", {}, isRuns ? "How commits are measured" : "How this report is built"),
        isRuns
          ? h("ol", {},
              h("li", {}, "Commits from ", upstream ? h("a", { href: upstream }, `${upstream.replace(/^https:\/\/github.com\//, "")} ${P.upstream?.branch || ""}`.trim()) : "the upstream repository", " are built and measured with benchx."),
              h("li", {}, "Runs are stored as Parquet under ", h("code", {}, "runs/<sha>/<node>/"), "."),
              h("li", {}, h("code", {}, "bx view"), " builds the derived tables and static JSON files this page reads — no database or server."))
          : h("ol", {},
              h("li", {}, "Benchmarks are measured with ", h("code", {}, "bx bench"), " or ", h("code", {}, "bx compare"), "."),
              h("li", {}, h("code", {}, "bx view"), " reads the local store (", h("code", {}, "store.parquet"), " or ", h("code", {}, "store.jsonl"), ") or JSON results and emits this static site."))),
      h("section", { class: "panel" }, h("div", { class: "about-brand" }, wordmark()), h("h3", {}, "Built with benchx"),
        h("p", {}, "Every number on this site is computed directly from stored benchx measurement results."),
        h("p", {}, h("a", { class: "button", href: "https://github.com/benchx-dev/benchx" }, "benchx on GitHub →"),
          repo ? [" ", h("a", { class: "button", href: repo }, "Project repository")] : null))),
    h("h3", {}, "Reading the numbers"),
    h("dl", {},
      h("dt", {}, "Change & highlighting"), h("dd", {}, "Newest successful median divided by the previous successful one, minus one. Soft red/green cell shading appears only when a change exceeds twice the series noise; within-noise changes stay muted. Under 0.5% is shown as flat."),
      h("dt", {}, "Noise"), h("dd", {}, "Median absolute commit-to-commit change over the last 21 measurements (requires at least 3 points). Changes without a noise estimate yet are underlined as unconfirmed."),
      h("dt", {}, "Notable counts"), h("dd", {}, `"Regressed" and "improved" counts in the runs table tally series whose change exceeds ${pct}.`),
      h("dt", {}, "Not a statistical verdict"), h("dd", {}, "Treat highlighted regressions as leads to verify or re-measure.")),
    footer());
}

function footer() {
  const m = state.manifest, repo = state.project.repository;
  const hasTables = m.tables && Object.keys(m.tables).length > 0;
  const t = (name) => h("a", { href: `${DATA}${m.tables[name]}`, download: true }, name);
  return h("footer", {},
    h("span", {}, `built ${fmtDate(m.built_at)} · ${m.counts.points.toLocaleString()} results`,
      hasTables ? [" · Parquet: ", ...joinParts(["points", "latest", "changes", "commits"].filter((k) => m.tables[k]).map(t), ", ")] : null),
    h("span", { class: "made" }, "made with", h("a", { href: "https://github.com/benchx-dev/benchx" }, wordmark(true)),
      repo ? ["·", h("a", { href: repo }, "repository")] : null));
}

// ---- shell: header, search, theme (light default), router ---------------------------

function header() {
  const input = h("input", { type: "search", placeholder: "Search benchmarks…  ( / )", "aria-label": "search a benchmark", autocomplete: "off" });
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
    items.forEach((s) => {
      const p = formatParams(s.params);
      list.append(h("li", { role: "option", onclick: () => go(s) },
        h("span", { class: "search-main" },
          h("span", { class: "mono" }, s.case, p ? [" ", h("span", { class: "bench-params" }, p)] : null),
          qtyTag(s)),
        h("span", { class: "where" }, shortSuite(s.suite))));
    });
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

  const themeLabel = () => (document.documentElement.dataset.theme === "dark" ? "Light" : "Dark");
  const theme = h("button", {
    class: "ghost",
    title: "Toggle light / dark theme",
    onclick: (e) => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(next);
      e.currentTarget.textContent = themeLabel();
      route();
    },
  }, themeLabel());

  const nav = (href, text) => h("a", { href, "data-nav": href }, text);
  return h("header", { class: "top" },
    h("h1", {}, h("a", { href: "#/", class: "brand" }, h("span", { html: MARK }), state.project.title || "Benchmarks")),
    h("nav", {}, nav("#/", "Overview"), nav("#/benchmarks", "Benchmarks"), nav("#/suites", "Suites"), nav("#/about", "About")),
    h("div", { class: "search" }, input, list),
    theme);
}

function applyTheme(mode) {
  if (mode === "dark") document.documentElement.dataset.theme = "dark";
  else delete document.documentElement.dataset.theme;
  try { localStorage.setItem("theme", mode === "dark" ? "dark" : "light"); } catch (e) { /* storage unavailable */ }
}

function destroyCharts() {
  for (const c of state.charts) c.destroy();
  state.charts = [];
}

async function route() {
  const seq = ++state.routeSeq;
  const root = document.getElementById("app");
  const hash = location.hash || "#/";
  const [path, qs] = hash.slice(1).split("?");
  const query = new URLSearchParams(qs || "");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  destroyCharts();
  for (const f of state.cleanup) f();
  state.cleanup = [];
  const section = parts[0] === "benchmarks" ? "#/benchmarks"
    : parts[0] === "suites" || parts[0] === "s" ? "#/suites"
    : parts[0] === "about" ? "#/about"
    : parts[0] ? null : "#/";
  document.querySelectorAll("[data-nav]").forEach((a) => a.toggleAttribute("aria-current", a.dataset.nav === section));
  let view;
  try {
    if (parts[0] === "benchmarks") view = renderBenchmarks(query);
    else if (parts[0] === "suites") view = renderSuites();
    else if (parts[0] === "about") view = renderAbout();
    else if (parts[0] === "s" && parts[1]) view = await renderSuite(parts[1], seq);
    else if (parts[0] === "b" && parts[2]) view = await renderBench(parts[1], parts.slice(2).join("/"), query, seq);
    else if (parts[0] === "c" && parts[1]) view = await renderCommit(parts[1], seq);
    else view = renderLanding();
  } catch (e) {
    view = h("main", {}, h("h2", {}, "Could not load this view"), h("p", { class: "note" }, String(e.message || e)), h("p", {}, h("a", { href: "#/" }, "back to overview")));
  }
  if (seq !== state.routeSeq || !view) return;
  root.replaceChildren(view);
  window.scrollTo(0, 0);
}

async function init() {
  // Light mode by default; only enable dark if explicitly chosen previously.
  try { if (localStorage.getItem("theme") === "dark") applyTheme("dark"); } catch (e) { /* ignore */ }
  const root = document.getElementById("app");
  try {
    state.manifest = await getJSON("manifest.json", { fresh: true });
    state.v = state.manifest.built_at;
    [state.project, state.landing, state.series] = await Promise.all([
      getJSON("project.json").catch(() => ({})), getJSON("views/landing.json"), getJSON("views/series.json")]);
    const qByCase = new Map();
    for (const s of state.series) {
      const k = `${s.suite}|${s.case}|${s.params || ""}`;
      let qs = qByCase.get(k);
      if (!qs) { qs = new Set(); qByCase.set(k, qs); }
      if (s.quantity) qs.add(s.quantity);
    }
    state.multiQty = new Set([...qByCase].filter(([, qs]) => qs.size > 1).map(([k]) => k));
  } catch (e) {
    root.replaceChildren(h("main", {}, h("section", { class: "empty-box" },
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
