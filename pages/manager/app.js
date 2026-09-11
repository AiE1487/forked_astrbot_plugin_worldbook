/* pages/manager/app.js — 世界书 WebUI 管理页逻辑 */

const bridge = window.AstrBotPluginPage;

const TEMPLATE_LABELS = {
  default: "通用",
  common: "常用",
  resident: "常驻",
  chance: "随机",
  schedule: "日程",
  group: "群聊",
  user: "用户",
};
const POSITION_LABELS = {
  default: "跟随全局",
  system_prompt: "System Prompt 末尾",
  user_input: "用户消息末尾",
};
const WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

const state = {
  entries: [],
  meta: null,
  editingName: null, // null=列表页；"__new__"=新建；其他=条目名
};

// ================= 工具 =================

function $(id) { return document.getElementById(id); }

function toast(msg, isError) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add("hidden"), 2600);
}

async function apiGet(endpoint, params) {
  return bridge.apiGet(endpoint, params);
}

async function apiPost(endpoint, body) {
  return bridge.apiPost(endpoint, body || {});
}

function fmtDuration(sec) {
  if (sec == null || sec === Infinity) return "∞";
  sec = Math.round(Number(sec));
  if (sec <= 0) return "0秒";
  if (sec < 60) return sec + "秒";
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return m + "分" + (s ? s + "秒" : "");
  const h = Math.floor(m / 60);
  if (h < 24) return h + "小时" + (m % 60 ? (m % 60) + "分" : "");
  const d = Math.floor(h / 24);
  return d + "天" + (h % 24 ? (h % 24) + "小时" : "");
}

function escapeHtml(text) {
  return String(text == null ? "" : text)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function resolvePosition(entry) {
  const pos = entry.inject_position || "default";
  if (pos !== "default") return pos;
  const gp = (state.meta && state.meta.global && state.meta.global.inject_position) || "user_input";
  return gp;
}

// ================= 数据加载 =================

async function loadMeta() {
  try {
    state.meta = await apiGet("meta");
  } catch (e) {
    toast("加载元信息失败: " + e.message, true);
    return;
  }
  const g = (state.meta && state.meta.global) || {};
  $("global-position").value = g.inject_position || "user_input";
  const cover = state.meta ? state.meta.holiday_coverage_year : null;
  $("meta-line").textContent =
    `版本 ${state.meta ? state.meta.version : "?"} · 条目 ${state.entries.length} 条` +
    (cover ? ` · 节假日离线数据覆盖至 ${cover} 年` : " · 节假日离线数据不可用（将在线查询）");
  $("holiday-hint").textContent = cover
    ? `节假日/工作日过滤依赖中国法定节假日数据（含调休），离线数据覆盖至 ${cover} 年，更早之后的日期自动在线查询。`
    : "未检测到离线节假日数据包，日期类型过滤将在线查询或按自然周估算。";
}

async function loadEntries() {
  try {
    const data = await apiGet("entries");
    state.entries = (data && data.entries) || [];
  } catch (e) {
    toast("加载条目失败: " + e.message, true);
    state.entries = [];
  }
  await loadMeta();
  renderList();
}

// ================= 列表渲染 =================

function currentFilters() {
  return {
    q: $("search").value.trim().toLowerCase(),
    template: $("filter-template").value,
    enabled: $("filter-enabled").value,
    position: $("filter-position").value,
  };
}

function entryMatchesFilters(entry, f) {
  if (f.template && entry.template !== f.template) return false;
  if (f.enabled === "on" && !entry.enabled) return false;
  if (f.enabled === "off" && entry.enabled) return false;
  if (f.position && (entry.inject_position || "default") !== f.position) return false;
  if (f.q) {
    const hay = [entry.name, ...(entry.keywords || []), entry.content || ""]
      .join("\n").toLowerCase();
    if (!hay.includes(f.q)) return false;
  }
  return true;
}

function renderList() {
  const list = $("entry-list");
  const f = currentFilters();
  const items = state.entries.filter((e) => entryMatchesFilters(e, f));

  if (!items.length) {
    list.innerHTML = '<div class="empty-tip">没有符合条件的条目，点击右上角「＋ 新建条目」开始创建</div>';
    return;
  }

  list.innerHTML = items.map((entry) => {
    const pos = resolvePosition(entry);
    const kws = (entry.keywords || []).slice(0, 4).map((k) =>
      `<span class="kw-chip">${escapeHtml(k)}</span>`).join("");
    const moreKw = (entry.keywords || []).length > 4
      ? `<span class="kw-chip">…共 ${(entry.keywords || []).length} 条</span>` : "";

    // 示范场景
    const triggerPart = (entry.keywords || []).length
      ? `当检测到「${(entry.keywords || []).slice(0, 3).join("」「")}」时`
      : (entry.schedule_enabled ? "定时触发时" : "未配置触发方式");
    const example =
      `${escapeHtml(triggerPart)}，将注入到 <b>${POSITION_LABELS[pos] || pos}</b>` +
      (entry.cooldown > 0 ? `，触发冷却 ${fmtDuration(entry.cooldown)}` : "");
    const fireLine = entry.schedule_enabled
      ? `<span class="fire">下一次执行日期：${(entry.next_fires && entry.next_fires[0]) || "—"}${
          entry.next_fires && entry.next_fires.length > 1
            ? `（之后还有 ${entry.next_fires.length - 1} 次）` : ""}</span>`
      : "";

    const runtime = entry.runtime || {};
    const live = runtime.active
      ? `<span class="badge live">生效中 · 剩${fmtDuration(runtime.remaining_time)} / 剩${
          runtime.remaining_times == null ? "∞" : runtime.remaining_times + "次"}</span>`
      : "";

    const scopeCount = (entry.scope || []).length;
    return `<div class="entry-card card" data-name="${escapeHtml(entry.name)}">
      <div class="entry-head">
        <span class="entry-name">${escapeHtml(entry.name)}</span>
        <span class="badge">${TEMPLATE_LABELS[entry.template] || entry.template}</span>
        <span class="badge pos">${POSITION_LABELS[entry.inject_position || "default"]}</span>
        ${entry.enabled ? "" : '<span class="badge off">已禁用</span>'}
        ${live}
      </div>
      <div class="entry-meta">
        <span>优先级 ${entry.priority}</span>
        <span>时长 ${entry.duration > 0 ? fmtDuration(entry.duration) : "永久"}</span>
        <span>次数 ${entry.times > 0 ? entry.times + " 次" : "不限"}</span>
        <span>概率 ${Math.round((entry.probability || 1) * 100)}%</span>
        ${scopeCount ? `<span>范围 ${scopeCount} 项</span>` : ""}
        ${entry.schedule_text ? `<span>定时：${escapeHtml(entry.schedule_text)}</span>` : ""}
      </div>
      ${kws || moreKw ? `<div class="kw-chips">${kws}${moreKw}</div>` : ""}
      <div class="example-line">${example}${fireLine}</div>
      <div class="entry-actions">
        <button class="btn" data-act="edit">编辑</button>
        <button class="btn danger" data-act="delete">删除</button>
      </div>
    </div>`;
  }).join("");

  list.querySelectorAll(".entry-card").forEach((card) => {
    const name = card.getAttribute("data-name");
    card.querySelector('[data-act="edit"]').onclick = () => openModal(name);
    card.querySelector('[data-act="delete"]').onclick = () => deleteEntry(name);
  });
}

// ================= 编辑弹窗 =================

function currentScheduleFromForm() {
  return {
    mode: $("f-mode").value,
    times: [...document.querySelectorAll("#times-list input")].map((i) => i.value),
    weekdays: [...document.querySelectorAll("#weekday-boxes input:checked")]
      .map((i) => Number(i.value)),
    start_date: $("f-start-date").value || "",
    end_date: $("f-end-date").value || "",
    day_filter: (document.querySelector('input[name="day_filter"]:checked') || {}).value || "all",
  };
}

function refreshSchedulePanels() {
  const mode = $("f-mode").value;
  $("mode-times").classList.toggle("hidden", !(mode === "daily" || mode === "weekly"));
  $("mode-weekdays").classList.toggle("hidden", mode !== "weekly");
  $("mode-cron").classList.toggle("hidden", mode !== "cron");
}

function addTimeInput(value) {
  const wrap = document.createElement("div");
  wrap.className = "time-item";
  const input = document.createElement("input");
  input.type = "time";
  input.value = value || "09:00";
  const del = document.createElement("button");
  del.className = "btn small";
  del.textContent = "✕";
  del.onclick = () => wrap.remove();
  wrap.append(input, del);
  $("times-list").appendChild(wrap);
}

function renderWeekdayBoxes(selected) {
  const box = $("weekday-boxes");
  box.innerHTML = "";
  WEEKDAY_LABELS.forEach((label, idx) => {
    const value = idx + 1;
    const lab = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = String(value);
    input.checked = (selected || []).includes(value);
    const sync = () => lab.classList.toggle("on", input.checked);
    input.onchange = sync;
    sync();
    lab.append(input, document.createTextNode(label));
    box.appendChild(lab);
  });
}

function fillScheduleForm(schedule, cron) {
  const s = schedule || {};
  $("f-mode").value = s.mode || "none";
  $("times-list").innerHTML = "";
  (s.times && s.times.length ? s.times : ["09:00"]).forEach(addTimeInput);
  renderWeekdayBoxes(s.weekdays);
  $("f-start-date").value = s.start_date || "";
  $("f-end-date").value = s.end_date || "";
  const filter = s.day_filter || "all";
  document.querySelectorAll('input[name="day_filter"]').forEach((r) => {
    r.checked = r.value === filter;
  });
  $("f-cron").value = cron || "";
  $("schedule-describe").textContent = "";
  $("preview-fires").innerHTML = "";
  refreshSchedulePanels();
}

function openModal(name) {
  state.editingName = name || "__new__";
  const isNew = name === null || name === undefined;
  const entry = isNew ? null : state.entries.find((e) => e.name === name);

  $("modal-title").textContent = isNew ? "新建条目" : `编辑条目：${name}`;
  $("f-name").value = isNew ? "" : name;
  $("f-name").disabled = !isNew;
  $("f-template").value = entry ? entry.template || "default" : "default";
  $("f-template").disabled = !isNew;
  $("f-enabled").checked = entry ? entry.enabled !== false : true;
  $("f-priority").value = entry ? entry.priority : 50;
  $("f-probability").value = entry ? entry.probability : 1;
  $("f-duration").value = entry ? entry.duration : 180;
  $("f-times").value = entry ? entry.times : 5;
  $("f-cooldown").value = entry ? entry.cooldown || 0 : 0;
  $("f-position").value = (entry && entry.inject_position) || "default";
  $("f-keywords").value = entry ? (entry.keywords || []).join("\n") : "";
  $("f-scope").value = entry ? (entry.scope || []).join("\n") : "";
  $("f-content").value = entry ? entry.content || "" : "";
  $("save-msg").textContent = "";

  fillScheduleForm(entry ? entry.schedule : null, entry ? entry.cron : "");
  updateExampleLine();
  $("modal-mask").classList.remove("hidden");
}

function closeModal() {
  $("modal-mask").classList.add("hidden");
  state.editingName = null;
}

function updateExampleLine() {
  const pos = $("f-position").value === "default"
    ? (state.meta && state.meta.global && state.meta.global.inject_position) || "user_input"
    : $("f-position").value;
  const kws = $("f-keywords").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const schedule = currentScheduleFromForm();
  const trigger = kws.length
    ? `当检测到「${kws.slice(0, 3).join("」「")}」时`
    : (schedule.mode !== "none" ? "定时触发时" : "未配置触发方式");
  $("f-example").innerHTML =
    `${escapeHtml(trigger)}，将注入到 <b>${POSITION_LABELS[pos] || pos}</b>`;
}

async function previewSchedule() {
  const schedule = currentScheduleFromForm();
  const cron = $("f-cron").value.trim();
  $("schedule-describe").textContent = "计算中…";
  $("preview-fires").innerHTML = "";
  try {
    const data = await apiPost("schedule/preview", { schedule, cron, count: 5 });
    $("schedule-describe").textContent = data.describe || "（未配置定时）";
    $("preview-fires").innerHTML = (data.fires || []).map((f) => `<li>${escapeHtml(f)}</li>`).join("");
    if (!(data.fires || []).length) {
      $("preview-fires").innerHTML = "<li>未来 400 天内没有满足条件的触发时刻</li>";
    }
  } catch (e) {
    $("schedule-describe").textContent = "";
    toast("预览失败: " + e.message, true);
  }
}

function collectForm() {
  const keywords = $("f-keywords").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const scope = $("f-scope").value.split("\n").map((s) => s.trim()).filter(Boolean);
  return {
    enabled: $("f-enabled").checked,
    priority: Number($("f-priority").value) || 0,
    scope,
    keywords,
    probability: Number($("f-probability").value),
    cron: $("f-cron").value.trim(),
    duration: Math.max(0, Number($("f-duration").value) || 0),
    times: Math.max(0, Number($("f-times").value) || 0),
    content: $("f-content").value,
    inject_position: $("f-position").value,
    cooldown: Math.max(0, Number($("f-cooldown").value) || 0),
    schedule: currentScheduleFromForm(),
  };
}

async function saveEntry() {
  const name = $("f-name").value.trim();
  if (!name) { toast("条目名称不能为空", true); return; }
  if (state.editingName === "__new__" && name.length > 20) {
    toast("条目名称过长", true);
    return;
  }
  const payload = { name, fields: collectForm() };
  if (state.editingName === "__new__") {
    payload.template = $("f-template").value;
  }
  try {
    await apiPost("entries/save", payload);
    toast("已保存");
    closeModal();
    await loadEntries();
  } catch (e) {
    $("save-msg").textContent = e.message;
    toast("保存失败: " + e.message, true);
  }
}

async function deleteEntry(name) {
  if (!confirm(`确定删除条目「${name}」？此操作不可恢复。`)) return;
  try {
    await apiPost("entries/delete", { name });
    toast("已删除");
    await loadEntries();
  } catch (e) {
    toast("删除失败: " + e.message, true);
  }
}

async function saveGlobal() {
  try {
    await apiPost("config/save", {
      global: { inject_position: $("global-position").value },
    });
    toast("全局配置已保存");
    await loadMeta();
    renderList();
  } catch (e) {
    toast("保存失败: " + e.message, true);
  }
}

// ================= 事件绑定 & 启动 =================

function bindEvents() {
  ["search", "filter-template", "filter-enabled", "filter-position"].forEach((id) => {
    $(id).addEventListener("input", renderList);
    $(id).addEventListener("change", renderList);
  });
  $("btn-refresh").onclick = loadEntries;
  $("btn-new").onclick = () => openModal(null);
  $("modal-close").onclick = closeModal;
  $("modal-mask").addEventListener("click", (e) => {
    if (e.target === $("modal-mask")) closeModal();
  });
  $("btn-save").onclick = saveEntry;
  $("save-global").onclick = saveGlobal;

  $("f-mode").onchange = refreshSchedulePanels;
  $("btn-add-time").onclick = () => addTimeInput();
  $("btn-preview").onclick = previewSchedule;
  ["f-keywords", "f-position"].forEach((id) => {
    $(id).addEventListener("input", updateExampleLine);
    $(id).addEventListener("change", updateExampleLine);
  });
}

async function init() {
  if (!bridge || typeof bridge.ready !== "function") {
    $("entry-list").innerHTML =
      '<div class="empty-tip">未检测到 AstrBot 插件页面桥接环境，请在 AstrBot WebUI（≥ v4.24.1）的插件详情页中打开本页面。</div>';
    return;
  }
  bindEvents();
  try {
    await bridge.ready();
  } catch (e) {
    toast("桥接初始化失败: " + e.message, true);
  }
  await loadMeta();
  await loadEntries();
}

init();
