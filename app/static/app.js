/* 设备测量数据分布汇总前端 —— 原生 JS，无第三方依赖。
 * 所有来自 API 的字符串只用 textContent 插入 DOM，绝不拼 innerHTML。
 */
"use strict";

const state = {
  groups: [],
  records: [],
  versions: [],
  summary: null,
  recompute: null,
  selected: { version: null, groupId: "", granularity: "all", period: "" },
  theme: localStorage.getItem("pd-theme") || "light",
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(body.error || `HTTP ${resp.status}`);
  }
  return body;
}

function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.classList.add("show");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.remove("show"), 3800);
}

function fmt(value, digits = 3) {
  if (value === null || value === undefined) return "—";
  if (typeof value !== "number") return String(value);
  if (!Number.isFinite(value)) return String(value);
  if (Number.isInteger(value)) return value.toLocaleString("en-US");
  return value.toLocaleString("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

function applyTheme() {
  document.documentElement.dataset.theme = state.theme;
  $("theme-toggle").textContent = state.theme === "dark" ? "切换浅色" : "切换深色";
  localStorage.setItem("pd-theme", state.theme);
}

/* ---------------- 数据加载 ---------------- */

async function loadGroups() {
  state.groups = await api("/api/groups");
  const select = $("filter-group");
  select.textContent = "";
  select.append(new Option("全部设备组", ""));
  for (const g of state.groups) select.append(new Option(g.name, g.group_id));

  const datalist = $("group-options");
  datalist.textContent = "";
  for (const g of state.groups) {
    const opt = document.createElement("option");
    opt.value = g.name;
    datalist.append(opt);
  }
}

async function loadRecords() {
  state.records = await api("/api/records?include_inactive=1");
  renderRecordsTable();
}

async function loadVersions() {
  state.versions = await api("/api/summaries");
  const select = $("filter-version");
  const previous = select.value;
  select.textContent = "";
  if (state.versions.length === 0) {
    select.append(new Option("尚无版本", ""));
  } else {
    for (const v of state.versions) {
      select.append(new Option(`v${v.version} · ${v.event_type}`, v.version));
    }
    const want = previous || String(state.versions[state.versions.length - 1].version);
    if ([...select.options].some((o) => o.value === want)) select.value = want;
  }
  state.selected.version = select.value ? Number(select.value) : null;
  renderVersionsTable();
}

async function loadSummary() {
  if (!state.selected.version) {
    state.summary = null;
    renderSummary();
    return;
  }
  state.summary = await api(`/api/summary?version=${state.selected.version}`);
  renderSummary();
}

async function refreshAll() {
  await Promise.all([loadGroups(), loadVersions()]);
  await Promise.all([loadRecords(), loadSummary()]);
  populatePeriods();
}

/* ---------------- 过滤器联动 ---------------- */

function currentRows() {
  if (!state.summary) return [];
  const rows = state.summary.details.granularities[state.selected.granularity] || [];
  return rows.filter((r) => {
    if (state.selected.groupId && r.group_id !== Number(state.selected.groupId)) return false;
    if (state.selected.period && r.period !== state.selected.period) return false;
    return true;
  });
}

function populatePeriods() {
  const select = $("filter-period");
  const previous = select.value;
  select.textContent = "";
  select.append(new Option("全部周期", ""));
  if (!state.summary) return;
  const rows = state.summary.details.granularities[state.selected.granularity] || [];
  const periods = [...new Set(rows.map((r) => r.period))].sort();
  for (const p of periods) select.append(new Option(p, p));
  if ([...select.options].some((o) => o.value === previous)) select.value = previous;
}

/* ---------------- 汇总渲染 ---------------- */

function renderSummary() {
  populatePeriods();
  const rows = currentRows();
  const schemeHint = $("scheme-hint");
  const note = $("quantile-note");
  note.textContent = "";

  if (!state.summary || rows.length === 0) {
    schemeHint.textContent = "";
    $("tiles").textContent = "";
    $("chart-container").textContent = "";
    $("bin-table").querySelector("tbody").textContent = "";
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = state.versions.length === 0
      ? "尚无任何汇总：请在上方新增第一条测量记录"
      : "当前过滤条件下没有分组/周期数据（空分组会以 count=0 显式列出，请放宽周期过滤）";
    $("chart-container").append(empty);
    return;
  }

  const edges = state.summary.details.edges;
  schemeHint.textContent =
    `桶方案 v${state.summary.details.scheme_version}：边界 [${edges.join(", ")}]，左闭右开，桶外双侧单独计数`;

  // 聚合当前过滤命中的所有行
  const agg = aggregateRows(rows);
  renderTiles(agg, rows.length);
  renderHistogram(agg, state.summary.details);
  renderBinTable(agg);

  const notes = [...new Set(rows.map((r) => r.stats.quantile_note).filter(Boolean))];
  if (notes.length) {
    const notice = document.createElement("div");
    notice.className = "notice";
    notice.textContent = notes.join(" ｜ ");
    note.append(notice);
  }
}

function aggregateRows(rows) {
  const first = rows[0].stats;
  const bins = first.bins.map((b, i) => ({
    index: i,
    label: b.label,
    edge_lo: b.edge_lo,
    edge_hi: b.edge_hi,
    count: 0,
  }));
  let below = 0;
  let above = 0;
  let count = 0;
  let sum = 0;
  let min = null;
  let max = null;
  const qMap = new Map();

  for (const row of rows) {
    const s = row.stats;
    count += s.count;
    if (s.mean !== null) sum += s.mean * s.count;
    if (s.min !== null) min = min === null ? s.min : Math.min(min, s.min);
    if (s.max !== null) max = max === null ? s.max : Math.max(max, s.max);
    below += s.below_count;
    above += s.above_count;
    s.bins.forEach((b, i) => { bins[i].count += b.count; });
    for (const q of s.quantiles) {
      if (!qMap.has(q.p)) qMap.set(q.p, []);
      qMap.get(q.p).push(q);
    }
  }

  // 合并分位：全部精确才标精确；出现近似/桶外则如实展示最差口径
  const quantiles = [...qMap.entries()].sort((a, b) => a[0] - b[0]).map(([p, list]) => {
    const methods = new Set(list.map((q) => q.method));
    if (count === 0 || methods.has("empty")) {
      return { p, method: "empty", value: null, max_abs_error: null, side: null };
    }
    if (methods.has("unbounded")) {
      const sides = [...new Set(list.filter((q) => q.method === "unbounded").map((q) => q.side))];
      return { p, method: "unbounded", value: null, max_abs_error: null, side: sides.join("/") };
    }
    const values = list.map((q) => q.value).filter((v) => v !== null);
    const errors = list.map((q) => q.max_abs_error).filter((v) => v !== null);
    const allExact = methods.size === 1 && methods.has("exact");
    return {
      p,
      method: allExact ? "exact" : "interpolated",
      value: values.reduce((a, b) => a + b, 0) / values.length,
      max_abs_error: allExact ? 0 : Math.max(...errors),
      side: null,
    };
  });

  return {
    count,
    mean: count ? sum / count : null,
    min,
    max,
    below,
    above,
    bins,
    quantiles,
  };
}

function renderTiles(agg, groupCount) {
  const tiles = $("tiles");
  tiles.textContent = "";
  const defs = [
    { label: `有效记录数（${groupCount} 个分组行）`, value: fmt(agg.count), sub: agg.count === 0 ? "空分组：分位数未定义" : "" },
    { label: "均值", value: agg.mean === null ? "—" : fmt(agg.mean), sub: "" },
    { label: "最小值 / 最大值", value: agg.count ? `${fmt(agg.min)} / ${fmt(agg.max)}` : "—", sub: "" },
    { label: "桶外下界 / 上界", value: `${fmt(agg.below)} / ${fmt(agg.above)}`, sub: "不并入任何桶，单独计数" },
  ];
  for (const q of agg.quantiles) {
    const pct = `P${Math.round(q.p * 100)}`;
    let value;
    let sub;
    let cls = "";
    if (q.method === "empty") {
      value = "—";
      sub = "无数据";
      cls = "empty";
    } else if (q.method === "unbounded") {
      value = "桶外";
      sub = `目标分位落在桶外${q.side === "below" ? "下界" : q.side === "above" ? "上界" : ""}，无有限误差界`;
      cls = "empty";
    } else if (q.method === "exact") {
      value = fmt(q.value);
      sub = `${pct} · type-7 精确值，误差 0`;
    } else {
      value = `≈ ${fmt(q.value)}`;
      sub = `${pct} · 桶插值近似，最坏绝对误差 ≤ ${fmt(q.max_abs_error)}`;
      cls = "approx";
    }
    defs.push({ label: pct, value, sub, cls });
  }
  for (const d of defs) {
    const tile = document.createElement("div");
    tile.className = "tile";
    const label = document.createElement("div");
    label.className = "label";
    label.textContent = d.label;
    const value = document.createElement("div");
    value.className = `value ${d.cls || ""}`;
    value.textContent = d.value;
    tile.append(label, value);
    if (d.sub) {
      const sub = document.createElement("div");
      sub.className = "sub";
      sub.textContent = d.sub;
      tile.append(sub);
    }
    tiles.append(tile);
  }
}

function renderHistogram(agg, details) {
  const container = $("chart-container");
  container.textContent = "";
  const svgNS = "http://www.w3.org/2000/svg";
  const width = Math.max(640, agg.bins.length * 78 + 120);
  const height = 320;
  const margin = { top: 24, right: 24, bottom: 64, left: 56 };
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;

  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("class", "histogram");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "直方图：含桶外下界与桶外上界");

  // 展示序列：桶外下界 + 各桶 + 桶外上界
  const segments = [
    { key: "below", label: `桶外 < ${fmt(details.edges[0])}`, count: agg.below, out: true },
    ...agg.bins.map((b) => ({ key: `bin-${b.index}`, label: b.label, count: b.count, out: false, bin: b })),
    { key: "above", label: `桶外 >= ${fmt(details.edges[details.edges.length - 1])}`, count: agg.above, out: true },
  ];
  const maxCount = Math.max(1, ...segments.map((s) => s.count));
  const slot = plotW / segments.length;
  const barWidth = Math.min(24, slot - 6);

  // 网格线与 Y 轴刻度（整洁数字）
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const value = Math.round((maxCount / ticks) * i);
    const y = margin.top + plotH - (value / maxCount) * plotH;
    const line = document.createElementNS(svgNS, "line");
    line.setAttribute("class", "gridline");
    line.setAttribute("x1", margin.left);
    line.setAttribute("x2", margin.left + plotW);
    line.setAttribute("y1", y);
    line.setAttribute("y2", y);
    svg.append(line);
    const text = document.createElementNS(svgNS, "text");
    text.setAttribute("class", "axis-text");
    text.setAttribute("x", margin.left - 8);
    text.setAttribute("y", y + 4);
    text.setAttribute("text-anchor", "end");
    text.textContent = fmt(value);
    svg.append(text);
  }

  // 基线
  const baseline = document.createElementNS(svgNS, "line");
  baseline.setAttribute("class", "baseline-line");
  baseline.setAttribute("x1", margin.left);
  baseline.setAttribute("x2", margin.left + plotW);
  baseline.setAttribute("y", margin.top + plotH);
  baseline.setAttribute("y2", margin.top + plotH);
  svg.append(baseline);

  segments.forEach((seg, i) => {
    const x = margin.left + i * slot + (slot - barWidth) / 2;
    const h = (seg.count / maxCount) * plotH;
    const y = margin.top + plotH - h;
    const rect = document.createElementNS(svgNS, "rect");
    rect.setAttribute("class", seg.count === 0 ? "bar bar-zero" : `bar${seg.out ? " out-of-range" : ""}`);
    rect.setAttribute("x", x);
    rect.setAttribute("y", seg.count === 0 ? margin.top + plotH - 1 : y);
    rect.setAttribute("width", barWidth);
    rect.setAttribute("height", Math.max(seg.count === 0 ? 1 : h, 2));
    rect.setAttribute("rx", 4);
    rect.setAttribute("ry", 4);
    rect.setAttribute("tabindex", "0");
    rect.setAttribute("role", "button");
    rect.dataset.key = seg.key;
    rect.dataset.label = seg.label;
    rect.dataset.count = String(seg.count);

    rect.addEventListener("pointermove", (ev) => showBarTooltip(ev, seg, agg.count));
    rect.addEventListener("focus", () => showBarTooltipFixed(seg, agg.count, rect));
    rect.addEventListener("pointerleave", hideTooltip);
    rect.addEventListener("blur", hideTooltip);
    rect.addEventListener("click", () => openBinRecords(seg));
    rect.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        openBinRecords(seg);
      }
    });
    svg.append(rect);

    // X 轴标签（旋转避免重叠）
    const text = document.createElementNS(svgNS, "text");
    text.setAttribute("class", "axis-text");
    text.setAttribute("x", x + barWidth / 2);
    text.setAttribute("y", margin.top + plotH + 16);
    text.setAttribute("text-anchor", "end");
    text.setAttribute("transform", `rotate(-35 ${x + barWidth / 2} ${margin.top + plotH + 16})`);
    text.textContent = seg.out ? seg.label : seg.label;
    svg.append(text);
  });

  // 轴标题
  const yTitle = document.createElementNS(svgNS, "text");
  yTitle.setAttribute("class", "axis-text");
  yTitle.setAttribute("x", 14);
  yTitle.setAttribute("y", margin.top + plotH / 2);
  yTitle.setAttribute("transform", `rotate(-90 14 ${margin.top + plotH / 2})`);
  yTitle.setAttribute("text-anchor", "middle");
  yTitle.textContent = "记录数";
  svg.append(yTitle);

  container.append(svg);
}

function showBarTooltip(ev, seg, total) {
  const tip = $("tooltip");
  fillTooltip(tip, seg, total);
  tip.style.display = "block";
  tip.style.left = `${Math.min(ev.clientX + 14, window.innerWidth - 280)}px`;
  tip.style.top = `${Math.min(ev.clientY + 14, window.innerHeight - 120)}px`;
}

function showBarTooltipFixed(seg, total, rect) {
  const tip = $("tooltip");
  fillTooltip(tip, seg, total);
  tip.style.display = "block";
  const box = rect.getBoundingClientRect();
  tip.style.left = `${Math.min(box.left, window.innerWidth - 280)}px`;
  tip.style.top = `${Math.max(box.top - 90, 8)}px`;
}

function fillTooltip(tip, seg, total) {
  tip.textContent = "";
  const swatch = document.createElement("span");
  swatch.className = "tt-swatch";
  swatch.style.background = seg.out
    ? "var(--series-2)"
    : seg.count === 0 ? "var(--gridline)" : "var(--series-1)";
  const label = document.createElement("div");
  label.className = "tt-label";
  label.append(swatch, document.createTextNode(seg.label));
  const value = document.createElement("div");
  value.className = "tt-value";
  const pct = total ? ((seg.count / total) * 100).toFixed(1) : "0.0";
  value.textContent = `${seg.count} 条（${pct}%）`;
  const hint = document.createElement("div");
  hint.className = "tt-label";
  hint.textContent = seg.count === 0 ? "空桶" : "点击查看桶内原始记录";
  tip.append(label, value, hint);
}

function hideTooltip() {
  $("tooltip").style.display = "none";
}

function renderBinTable(agg) {
  const tbody = $("bin-table").querySelector("tbody");
  tbody.textContent = "";
  const total = agg.count || 1;
  const segments = [
    { label: `桶外下界（< ${fmt(agg.bins[0].edge_lo)}）`, count: agg.below, out: true },
    ...agg.bins.map((b) => ({ label: b.label, count: b.count, out: false, bin: b })),
    { label: `桶外上界（>= ${fmt(agg.bins[agg.bins.length - 1].edge_hi)}）`, count: agg.above, out: true },
  ];
  for (const seg of segments) {
    const tr = document.createElement("tr");
    if (seg.count > 0) tr.className = "clickable";
    const tdLabel = document.createElement("td");
    tdLabel.textContent = seg.label;
    const tdCount = document.createElement("td");
    tdCount.className = "num mono";
    tdCount.textContent = fmt(seg.count);
    const tdPct = document.createElement("td");
    tdPct.className = "num mono";
    tdPct.textContent = `${((seg.count / total) * 100).toFixed(1)}%`;
    tr.append(tdLabel, tdCount, tdPct);
    if (seg.count > 0) {
      tr.addEventListener("click", () => openBinRecords(seg));
    }
    tbody.append(tr);
  }
}

/* ---------------- 桶内原始记录抽屉 ---------------- */

function openBinRecords(seg) {
  const rows = currentRows();
  const groupIds = new Set(rows.map((r) => r.group_id));
  const matching = state.records.filter((r) => {
    if (r.status !== "active") return false;
    if (!groupIds.has(r.group_id)) return false;
    if (state.selected.granularity !== "all") {
      // 按当前粒度的周期过滤
      const key = periodKey(r.measured_at, state.selected.granularity);
      if (state.selected.period && key !== state.selected.period) return false;
    }
    return valueInSegment(r.value, seg);
  });

  openDrawer(`桶内原始记录：${seg.label}（${matching.length} 条）`, (body) => {
    if (matching.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "该区间在当前过滤条件下没有有效记录";
      body.append(empty);
      return;
    }
    const table = buildRecordTable(matching, { showGroup: true, actions: false });
    body.append(table);
  });
}

function valueInSegment(value, seg) {
  if (seg.key === "below") {
    return value < state.summary.details.edges[0];
  }
  if (seg.key === "above") {
    return value >= state.summary.details.edges[state.summary.details.edges.length - 1];
  }
  const bin = seg.bin;
  return value >= bin.edge_lo && value < bin.edge_hi;
}

function periodKey(measuredAt, granularity) {
  const dt = new Date(measuredAt);
  const pad = (n) => String(n).padStart(2, "0");
  if (granularity === "day") {
    return `${dt.getUTCFullYear()}-${pad(dt.getUTCMonth() + 1)}-${pad(dt.getUTCDate())}`;
  }
  if (granularity === "month") {
    return `${dt.getUTCFullYear()}-${pad(dt.getUTCMonth() + 1)}`;
  }
  if (granularity === "week") {
    // ISO 周
    const date = new Date(Date.UTC(dt.getUTCFullYear(), dt.getUTCMonth(), dt.getUTCDate()));
    const dayNum = (date.getUTCDay() + 6) % 7;
    date.setUTCDate(date.getUTCDate() - dayNum + 3);
    const firstThursday = new Date(Date.UTC(date.getUTCFullYear(), 0, 4));
    const diff = date - firstThursday;
    const week = 1 + Math.round(diff / 86400000 / 7);
    return `${date.getUTCFullYear()}-W${pad(week)}`;
  }
  return "all";
}

/* ---------------- 原始记录表 + 修订操作 ---------------- */

function groupName(groupId) {
  const g = state.groups.find((x) => x.group_id === groupId);
  return g ? g.name : `#${groupId}`;
}

function renderRecordsTable() {
  const tbody = $("records-table").querySelector("tbody");
  tbody.textContent = "";
  $("record-count-hint").textContent = `共 ${state.records.length} 条（含已更正/已撤销）`;
  const visible = state.records.slice().reverse();
  if (visible.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.className = "empty-state";
    td.textContent = "暂无记录";
    tr.append(td);
    tbody.append(tr);
    return;
  }
  for (const record of visible) {
    const tr = document.createElement("tr");
    const tdId = document.createElement("td");
    tdId.className = "num mono";
    tdId.textContent = record.record_id;
    const tdGroup = document.createElement("td");
    tdGroup.textContent = groupName(record.group_id);
    const tdValue = document.createElement("td");
    tdValue.className = "num mono";
    tdValue.textContent = fmt(record.value);
    const tdTime = document.createElement("td");
    tdTime.className = "mono";
    tdTime.textContent = record.measured_at;
    const tdStatus = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = `pill ${record.status}`;
    const statusText = { active: "有效", superseded: "已更正", revoked: "已撤销" };
    pill.textContent = statusText[record.status] || record.status;
    tdStatus.append(pill);
    const tdNote = document.createElement("td");
    tdNote.textContent = record.note || "—";
    const tdActions = document.createElement("td");
    if (record.status === "active") {
      const btnCorrect = document.createElement("button");
      btnCorrect.textContent = "更正";
      btnCorrect.className = "ghost";
      btnCorrect.addEventListener("click", () => openCorrectForm(record));
      const btnRevoke = document.createElement("button");
      btnRevoke.textContent = "撤销";
      btnRevoke.className = "ghost";
      btnRevoke.addEventListener("click", () => doRevoke(record));
      tdActions.append(btnCorrect, btnRevoke);
    } else {
      tdActions.className = "muted";
      tdActions.textContent = "不可再修订";
    }
    tr.append(tdId, tdGroup, tdValue, tdTime, tdStatus, tdNote, tdActions);
    tbody.append(tr);
  }
}

function buildRecordTable(records, options = {}) {
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  const headers = ["#", ...(options.showGroup ? ["设备组"] : []), "测量值", "测量时间", "状态"];
  for (const h of headers) {
    const th = document.createElement("th");
    th.textContent = h;
    headRow.append(th);
  }
  thead.append(headRow);
  const tb = document.createElement("tbody");
  for (const r of records) {
    const tr = document.createElement("tr");
    const cells = [
      String(r.record_id),
      ...(options.showGroup ? [groupName(r.group_id)] : []),
      fmt(r.value),
      r.measured_at,
      { active: "有效", superseded: "已更正", revoked: "已撤销" }[r.status],
    ];
    for (const c of cells) {
      const td = document.createElement("td");
      td.textContent = c;
      tr.append(td);
    }
    tb.append(tr);
  }
  table.append(thead, tb);
  return table;
}

async function doInsert() {
  const payload = {
    group_name: $("f-group").value.trim(),
    value: $("f-value").value.trim(),
    measured_at: $("f-time").value.trim(),
    reason: $("f-reason").value.trim(),
    fail_before_commit: $("f-fail").checked,
  };
  if (!payload.group_name || !payload.value || !payload.measured_at || !payload.reason) {
    toast("设备组、测量值、测量时间与修订原因均为必填", true);
    return;
  }
  try {
    const result = await api("/api/records", { method: "POST", body: JSON.stringify(payload) });
    toast(`已新增记录 #${result.record_id}，发布汇总 v${result.version}`);
    $("f-reason").value = "";
    await refreshAll();
  } catch (err) {
    toast(`新增失败，事务已回滚：${err.message}`, true);
  }
}

function openCorrectForm(record) {
  openDrawer(`更正记录 #${record.record_id}`, (body) => {
    const form = document.createElement("div");
    form.className = "form-grid";
    const fields = [
      ["新测量值", "c-value", String(record.value)],
      ["测量时间", "c-time", record.measured_at],
      ["更正原因（必填）", "c-reason", ""],
    ];
    for (const [label, id, value] of fields) {
      const wrap = document.createElement("div");
      wrap.className = "field wide";
      const lab = document.createElement("label");
      lab.textContent = label;
      lab.setAttribute("for", id);
      const input = document.createElement("input");
      input.id = id;
      input.value = value;
      wrap.append(lab, input);
      form.append(wrap);
    }
    const failWrap = document.createElement("div");
    failWrap.className = "field wide checkbox-field";
    const failInput = document.createElement("input");
    failInput.type = "checkbox";
    failInput.id = "c-fail";
    const failLabel = document.createElement("label");
    failLabel.textContent = "故障注入：提交前主动失败";
    failLabel.setAttribute("for", "c-fail");
    failWrap.append(failInput, failLabel);
    form.append(failWrap);

    const btnRow = document.createElement("div");
    btnRow.className = "field wide row-actions";
    const btn = document.createElement("button");
    btn.className = "primary";
    btn.textContent = "提交更正（旧记录标记为已更正，发布新版本）";
    btn.addEventListener("click", async () => {
      try {
        const result = await api("/api/correct", {
          method: "POST",
          body: JSON.stringify({
            record_id: record.record_id,
            new_value: $("c-value").value.trim(),
            measured_at: $("c-time").value.trim(),
            reason: $("c-reason").value.trim(),
            fail_before_commit: $("c-fail").checked,
          }),
        });
        closeDrawer();
        toast(`已更正：新记录 #${result.record_id}，发布汇总 v${result.version}`);
        await refreshAll();
      } catch (err) {
        toast(`更正失败，事务已回滚：${err.message}`, true);
      }
    });
    btnRow.append(btn);
    form.append(btnRow);
    body.append(form);
  });
}

async function doRevoke(record) {
  const reason = window.prompt(`撤销记录 #${record.record_id} 的原因（必填）：`);
  if (reason === null) return;
  if (!reason.trim()) {
    toast("撤销必须填写原因", true);
    return;
  }
  try {
    const result = await api("/api/revoke", {
      method: "POST",
      body: JSON.stringify({ record_id: record.record_id, reason: reason.trim() }),
    });
    toast(`已撤销记录 #${record.record_id}，旧贡献已移除，发布汇总 v${result.version}`);
    await refreshAll();
  } catch (err) {
    toast(`撤销失败，事务已回滚：${err.message}`, true);
  }
}

/* ---------------- 版本表 / 比较 / 重算 ---------------- */

function renderVersionsTable() {
  const tbody = $("versions-table").querySelector("tbody");
  tbody.textContent = "";
  if (state.versions.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "empty-state";
    td.textContent = "尚无汇总版本";
    tr.append(td);
    tbody.append(tr);
    return;
  }
  for (const v of [...state.versions].reverse()) {
    const tr = document.createElement("tr");
    const tdV = document.createElement("td");
    tdV.className = "num mono";
    tdV.textContent = `v${v.version}`;
    const tdEvent = document.createElement("td");
    const eventText = { insert: "新增", correct: "更正", revoke: "撤销" };
    tdEvent.textContent = eventText[v.event_type] || v.event_type;
    const tdCount = document.createElement("td");
    tdCount.className = "num mono";
    tdCount.textContent = fmt(v.active_count);
    const tdTime = document.createElement("td");
    tdTime.className = "mono";
    tdTime.textContent = v.created_at;
    const tdActions = document.createElement("td");
    const btnView = document.createElement("button");
    btnView.className = "ghost";
    btnView.textContent = "查看";
    btnView.addEventListener("click", () => {
      $("filter-version").value = String(v.version);
      state.selected.version = v.version;
      loadSummary();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    const btnDiff = document.createElement("button");
    btnDiff.className = "ghost";
    btnDiff.textContent = "与上一版比较";
    btnDiff.disabled = v.version === 1;
    btnDiff.addEventListener("click", () => openCompare(v.version - 1, v.version));
    tdActions.append(btnView, btnDiff);
    tr.append(tdV, tdEvent, tdCount, tdTime, tdActions);
    tbody.append(tr);
  }
}

async function openCompare(a, b) {
  const diff = await api(`/api/compare?a=${a}&b=${b}`);
  openDrawer(`版本比较：v${a} → v${b}`, (body) => {
    const meta = document.createElement("div");
    meta.className = "notice";
    meta.textContent =
      `v${a}（${diff.event_a}，${diff.created_at_a}） → ` +
      `v${b}（${diff.event_b}，${diff.created_at_b}）；仅展示“全部时段”粒度的计数与分位差异`;
    body.append(meta);
    const table = document.createElement("table");
    const thead = document.createElement("thead");
    const head = document.createElement("tr");
    for (const h of ["设备组", "粒度", "周期", "计数 v" + a, "计数 v" + b, "变化", "P50 v" + a, "P50 v" + b]) {
      const th = document.createElement("th");
      th.textContent = h;
      head.append(th);
    }
    thead.append(head);
    const tb = document.createElement("tbody");
    for (const row of diff.rows) {
      const tr = document.createElement("tr");
      const delta = row.count_delta;
      const q50a = row.quantiles_a ? row.quantiles_a.find((q) => q.p === 0.5) : null;
      const q50b = row.quantiles_b ? row.quantiles_b.find((q) => q.p === 0.5) : null;
      const vals = [
        row.group_name,
        row.granularity,
        row.period,
        row.count_a === null ? "—" : fmt(row.count_a),
        row.count_b === null ? "—" : fmt(row.count_b),
        delta === 0 ? "0" : (delta > 0 ? `+${delta}` : String(delta)),
        q50a && q50a.value !== null ? fmt(q50a.value) : "—",
        q50b && q50b.value !== null ? fmt(q50b.value) : "—",
      ];
      vals.forEach((text, idx) => {
        const td = document.createElement("td");
        td.textContent = text;
        if (idx === 5 && delta !== 0) td.className = delta > 0 ? "delta-up mono" : "delta-down mono";
        tr.append(td);
      });
      tb.append(tr);
    }
    table.append(thead, tb);
    body.append(table);
  });
}

async function runRecompute() {
  if (!state.selected.version) {
    toast("尚无汇总版本可比对", true);
    return;
  }
  const banner = $("recompute-banner");
  banner.textContent = "";
  try {
    const [fresh, verify] = await Promise.all([
      api("/api/recompute"),
      api(`/api/verify?version=${state.selected.version}`),
    ]);
    state.recompute = fresh;
    const notice = document.createElement("div");
    notice.className = verify.matches_full_recompute ? "notice good" : "notice warn";
    const storedCount = state.summary.details.active_count;
    notice.textContent = verify.matches_full_recompute
      ? `全量重算与存储的 v${state.selected.version} 完全一致（有效记录 ${fmt(storedCount)} 条），` +
        "证明撤销/更正的旧贡献确已从结果中移除，而非在不可撤回的摘要上假装删除。"
      : `发现不一致：${verify.mismatches.join("；")}`;
    banner.append(notice);
    toast(verify.matches_full_recompute ? "重算校验通过：逐字段一致" : "重算发现不一致", !verify.matches_full_recompute);
  } catch (err) {
    toast(`重算失败：${err.message}`, true);
  }
}

/* ---------------- 抽屉 ---------------- */

function openDrawer(title, fill) {
  $("drawer-title").textContent = title;
  const body = $("drawer-body");
  body.textContent = "";
  fill(body);
  $("drawer").classList.add("open");
  $("drawer-backdrop").classList.add("open");
}

function closeDrawer() {
  $("drawer").classList.remove("open");
  $("drawer-backdrop").classList.remove("open");
}

/* ---------------- 事件绑定与启动 ---------------- */

function bindEvents() {
  $("theme-toggle").addEventListener("click", () => {
    state.theme = state.theme === "dark" ? "light" : "dark";
    applyTheme();
  });
  $("filter-version").addEventListener("change", async (ev) => {
    state.selected.version = ev.target.value ? Number(ev.target.value) : null;
    await loadSummary();
  });
  $("filter-group").addEventListener("change", (ev) => {
    state.selected.groupId = ev.target.value;
    renderSummary();
  });
  $("filter-granularity").addEventListener("change", (ev) => {
    state.selected.granularity = ev.target.value;
    state.selected.period = "";
    renderSummary();
  });
  $("filter-period").addEventListener("change", (ev) => {
    state.selected.period = ev.target.value;
    renderSummary();
  });
  $("btn-insert").addEventListener("click", doInsert);
  $("btn-recompute").addEventListener("click", runRecompute);
  $("btn-verify").addEventListener("click", runRecompute);
  $("btn-compare").addEventListener("click", () => {
    if (state.versions.length < 2) {
      toast("至少需要两个版本才能比较", true);
      return;
    }
    const latest = state.versions[state.versions.length - 1].version;
    openCompare(latest - 1, latest);
  });
  $("drawer-close").addEventListener("click", closeDrawer);
  $("drawer-backdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") closeDrawer();
  });
}

(async function init() {
  applyTheme();
  bindEvents();
  try {
    await refreshAll();
  } catch (err) {
    toast(`初始化失败：${err.message}`, true);
  }
})();
