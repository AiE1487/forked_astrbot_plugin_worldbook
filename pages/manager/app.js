/* pages/manager/app.js — 世界书 WebUI 管理页逻辑 */

const bridge = window.AstrBotPluginPage;

const POSITION_LABELS = {
  default: "跟随全局",
  system_prompt: "System Prompt 末尾",
  user_input: "用户消息末尾",
};
const WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

const state = {
  entries: [],
  meta: null,
  editingName: null, // null=列表页；"__new__"=新建；其他=条目名（原名称，用于改名比对）
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

// ================= 示范场景 =================

// 触发描述：不再展示裸 .* 正则，给出可读话术
function triggerText(entry) {
  const kws = (entry.keywords || []).filter((k) => k !== ".*");
  if (kws.length) {
    return `当检测到「${kws.slice(0, 3).join("」「")}」时`;
  }
  if ((entry.keywords || []).includes(".*")) {
    const scopeAll = entry.scope || [];
    const ids = scopeAll.filter((s) => s !== "admin");
    if (scopeAll.includes("admin") && !ids.length) return "当管理员发言时";
    if (ids.length) return `当 ${ids.slice(0, 3).join("、")} 相关会话有消息时`;
    return "对所有消息可触发，";
  }
  if (entry.schedule_enabled) return "定时触发时";
  return "未配置触发方式，";
}

function exampleLine(entry) {
  const pos = POSITION_LABELS[resolvePosition(entry)] || "用户消息末尾";
  const text = triggerText(entry);
  // 话术末尾已带逗号的接「将注入到」，否则用「，将注入到」衔接
  const sep = /[,，]$/.test(text) ? "" : "，";
  let html = `${escapeHtml(text)}${sep}将注入到 <b>${pos}</b>`;
  if (entry.cooldown > 0) html += `，触发冷却 ${fmtDuration(entry.cooldown)}`;
  if (entry.schedule_enabled && entry.next_fires && entry.next_fires.length) {
    html += `<span class="fire">下一次执行日期：${escapeHtml(entry.next_fires[0])}${
      entry.schedule_text ? "（" + escapeHtml(entry.schedule_text) + "）" : ""
    }</span>`;
  }
  return html;
}

// ================= 列表渲染 =================

function currentFilters() {
  return {
    q: $("search").value.trim().toLowerCase(),
    enabled: $("filter-enabled").value,
    position: $("filter-position").value,
  };
}

function entryMatchesFilters(entry, f) {
  if (f.enabled === "on" && !entry.enabled) return false;
  if (f.enabled === "off" && entry.enabled) return false;
  if (f.position && (entry.inject_position || "default") !== f.position) return false;
  if (f.q) {
    const hay = [
      entry.name,
      ...(entry.keywords || []),
      ...(entry.scope || []),
      entry.content || "",
    ].join("\n").toLowerCase();
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
    const kws = (entry.keywords || []).filter((k) => k !== ".*").slice(0, 4).map((k) =>
      `<span class="kw-chip">${escapeHtml(k)}</span>`).join("");
    const moreKw = (entry.keywords || []).filter((k) => k !== ".*").length > 4
      ? `<span class="kw-chip">…</span>` : "";

    const scopeAll = entry.scope || [];
    const scopeChips = scopeAll.length
      ? `<div class="kw-chips"><span class="muted small">生效范围：</span>${
          scopeAll.slice(0, 4).map((s) => `<span class="kw-chip scope">${escapeHtml(s)}</span>`).join("")}${
          scopeAll.length > 4 ? `<span class="kw-chip">…共 ${scopeAll.length} 项</span>` : ""}</div>`
      : "";

    const runtime = entry.runtime || {};
    const live = runtime.active
      ? `<span class="badge live">生效中 · 剩${fmtDuration(runtime.remaining_time)} / 剩${
          runtime.remaining_times == null ? "∞" : runtime.remaining_times + "次"}</span>`
      : "";

    return `<div class="entry-card card" data-name="${escapeHtml(entry.name)}">
      <div class="entry-head">
        <span class="entry-name">${escapeHtml(entry.name)}</span>
        <span class="badge pos">${POSITION_LABELS[entry.inject_position || "default"]}</span>
        ${entry.enabled ? "" : '<span class="badge off">已禁用</span>'}
        ${live}
      </div>
      <div class="entry-meta">
        <span>优先级 ${entry.priority}</span>
        <span>时长 ${entry.duration > 0 ? fmtDuration(entry.duration) : "永久"}</span>
        <span>次数 ${entry.times > 0 ? entry.times + " 次" : "不限"}</span>
        <span>概率 ${Math.round((entry.probability || 1) * 100)}%</span>
        ${entry.schedule_text ? `<span>定时：${escapeHtml(entry.schedule_text)}</span>` : ""}
      </div>
      ${kws || moreKw ? `<div class="kw-chips">${kws}${moreKw}</div>` : ""}
      ${scopeChips}
      <div class="example-line">${exampleLine(entry)}</div>
      <div class="entry-actions">
        <button class="btn" data-act="edit">编辑</button>
        <button class="btn" data-act="toggle">${entry.enabled ? "禁用" : "启用"}</button>
        <button class="btn danger" data-act="delete">删除</button>
      </div>
    </div>`;
  }).join("");

  list.querySelectorAll(".entry-card").forEach((card) => {
    const name = card.getAttribute("data-name");
    const entry = state.entries.find((e) => e.name === name);
    card.querySelector('[data-act="edit"]').onclick = () => openModal(name);
    card.querySelector('[data-act="toggle"]').onclick = () => toggleEntry(entry);
    card.querySelector('[data-act="delete"]').onclick = () => {
      openConfirm(
        `确定删除条目「${name}」？此操作不可恢复。`,
        async () => {
          await apiPost("entries/delete", { name });
          toast("已删除");
          await loadEntries();
        },
      );
    };
  });
}

// ================= 确认弹窗（沙盒 iframe 禁用 window.confirm，必须用页面内弹窗） =================

let confirmAction = null;

function openConfirm(text, onOk, okLabel) {
  $("confirm-text").textContent = text;
  $("confirm-ok").textContent = okLabel || "确认";
  confirmAction = onOk;
  $("confirm-mask").classList.remove("hidden");
}

function closeConfirm() {
  $("confirm-mask").classList.add("hidden");
  confirmAction = null;
}

async function toggleEntry(entry) {
  try {
    await apiPost("entries/save", {
      name: entry.name,
      fields: { enabled: !entry.enabled },
    });
    toast(entry.enabled ? "已禁用" : "已启用");
    await loadEntries();
  } catch (e) {
    toast("操作失败: " + e.message, true);
  }
}

// ================= 编辑弹窗 =================

function scheduleToggleOn() {
  return $("f-schedule-enabled").checked;
}

function currentSpan() {
  const el = document.querySelector('input[name="trigger_span"]:checked');
  return el ? el.value : "moment";
}

function currentScheduleFromForm() {
  if (!scheduleToggleOn()) {
    return {
      mode: "none",
      times: [],
      weekdays: [],
      start_date: "",
      end_date: "",
      day_filter: "all",
      all_day: false,
      time_start: "",
      time_end: "",
    };
  }
  return {
    mode: $("f-mode").value,
    times: [...document.querySelectorAll("#times-list input")].map((i) => i.value),
    weekdays: [...document.querySelectorAll("#weekday-boxes input:checked")]
      .map((i) => Number(i.value)),
    start_date: $("f-start-date").value || "",
    end_date: $("f-end-date").value || "",
    day_filter: (document.querySelector('input[name="day_filter"]:checked') || {}).value || "all",
    all_day: currentSpan() === "all_day",
    time_start: currentSpan() === "range" ? ($("f-range-start").value || "") : "",
    time_end: currentSpan() === "range" ? ($("f-range-end").value || "") : "",
  };
}

function refreshSchedulePanels() {
  const on = scheduleToggleOn();
  $("schedule-config").classList.toggle("hidden", !on);
  $("schedule-off-hint").classList.toggle("hidden", on);
  if (!on) return;

  const mode = $("f-mode").value;
  const structural = mode === "daily" || mode === "weekly";
  const span = currentSpan();
  $("mode-span").classList.toggle("hidden", !structural);
  $("mode-times").classList.toggle("hidden", !(structural && span === "moment"));
  $("mode-allday").classList.toggle("hidden", !(structural && span === "all_day"));
  $("mode-range").classList.toggle("hidden", !(structural && span === "range"));
  $("mode-weekdays").classList.toggle("hidden", mode !== "weekly");
}

function setSpan(span) {
  const el = document.querySelector(`input[name="trigger_span"][value="${span}"]`);
  if (el) el.checked = true;
  refreshSchedulePanels();
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

function fillScheduleForm(schedule) {
  const s = schedule || {};
  const enabled = !!s.mode && s.mode !== "none";
  $("f-schedule-enabled").checked = enabled;
  $("f-mode").value = s.mode === "weekly" ? "weekly" : "daily";
  $("times-list").innerHTML = "";
  (s.times && s.times.length ? s.times : ["09:00"]).forEach(addTimeInput);
  renderWeekdayBoxes(s.weekdays);
  $("f-start-date").value = s.start_date || "";
  $("f-end-date").value = s.end_date || "";
  const filter = s.day_filter || "all";
  document.querySelectorAll('input[name="day_filter"]').forEach((r) => {
    r.checked = r.value === filter;
  });
  if (enabled) {
    setSpan(s.all_day ? "all_day" : (s.time_start && s.time_end ? "range" : "moment"));
    $("f-range-start").value = s.time_start || "09:00";
    $("f-range-end").value = s.time_end || "22:00";
  }
  $("schedule-describe").textContent = "";
  $("preview-fires").innerHTML = "";
  refreshSchedulePanels();
}

function openModal(name) {
  const isNew = name === null || name === undefined;
  state.editingName = isNew ? "__new__" : name;
  const entry = isNew ? null : state.entries.find((e) => e.name === name);

  $("modal-title").textContent = isNew ? "新建条目" : `编辑条目：${name}`;
  $("f-name").value = isNew ? "" : name;
  $("f-name").disabled = false; // 名称允许修改（保存时自动重命名）
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

  fillScheduleForm(entry ? entry.schedule : null);
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
  const fake = {
    keywords: kws,
    scope: $("f-scope").value.split("\n").map((s) => s.trim()).filter(Boolean),
    schedule_enabled: schedule.mode !== "none",
    cooldown: Number($("f-cooldown").value) || 0,
    next_fires: [],
    schedule_text: "",
  };
  const text = triggerText(fake);
  const sep = /[,，]$/.test(text) ? "" : "，";
  $("f-example").innerHTML =
    `${escapeHtml(text)}${sep}将注入到 <b>${POSITION_LABELS[pos] || pos}</b>`;
}

async function previewSchedule() {
  const schedule = currentScheduleFromForm();
  $("schedule-describe").textContent = "计算中…";
  $("preview-fires").innerHTML = "";
  try {
    const data = await apiPost("schedule/preview", { schedule, count: 5 });
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
  if (name.length > 20) {
    toast("条目名称过长（不超过 20 字符）", true);
    return;
  }
  const payload = { name, fields: collectForm() };
  const isEditing = state.editingName !== "__new__";
  if (isEditing) {
    payload.original_name = state.editingName;
    if (name !== state.editingName) {
      openConfirm(`确定将条目「${state.editingName}」重命名为「${name}」？`, async () => {
        await submitSave(payload);
      }, "重命名并保存");
      return;
    }
  }
  await submitSave(payload);
}

async function submitSave(payload) {
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

// ================= 导入 / 导出 =================

function exportStamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

async function exportLorebook() {
  const filename = `worldbook_${exportStamp()}.json`;
  try {
    await bridge.download("export", {}, filename);
    toast("已开始下载 " + filename);
  } catch (e) {
    toast("导出失败: " + e.message, true);
  }
}

// 待导入的文件（选择后暂存，等用户在策略弹窗里选择）
let pendingImportFile = null;

function pickImportFile() {
  $("import-file").click();
}

function openImportDialog(file) {
  pendingImportFile = file;
  $("import-text").textContent =
    `已选择「${file.name}」。文件中与现有条目同名的条目，要如何处理？` +
    `（跳过＝保留现有配置；覆盖＝用文件内容替换现有条目）`;
  $("import-mask").classList.remove("hidden");
}

function closeImportDialog() {
  $("import-mask").classList.add("hidden");
  pendingImportFile = null;
}

async function doImport(overwrite) {
  const file = pendingImportFile;
  if (!file) return;
  closeImportDialog();
  try {
    const endpoint = overwrite ? "import/overwrite" : "import";
    const data = await bridge.upload(endpoint, file);
    const parts = [`已导入 ${(data.imported || []).length} 条`];
    if ((data.skipped || []).length) parts.push(`跳过同名 ${data.skipped.length} 条`);
    if ((data.invalid || []).length) parts.push(`无效 ${data.invalid.length} 条`);
    toast(parts.join("，"));
    if ((data.invalid || []).length) {
      console.warn("[worldbook] 导入时跳过的无效条目:", data.invalid);
    }
    await loadEntries();
  } catch (e) {
    toast("导入失败: " + e.message, true);
  }
}

// ================= 事件绑定 & 启动 =================

function bindEvents() {
  ["search", "filter-enabled", "filter-position"].forEach((id) => {
    $(id).addEventListener("input", renderList);
    $(id).addEventListener("change", renderList);
  });
  $("btn-refresh").onclick = loadEntries;
  $("btn-new").onclick = () => openModal(null);
  $("btn-export").onclick = exportLorebook;
  $("btn-import").onclick = pickImportFile;
  $("import-file").onchange = () => {
    const file = $("import-file").files[0];
    // 立即重置 value，保证同一文件可再次选择
    $("import-file").value = "";
    if (file) openImportDialog(file);
  };
  $("import-cancel").onclick = closeImportDialog;
  $("import-skip").onclick = () => doImport(false);
  $("import-overwrite").onclick = () => doImport(true);
  $("import-mask").addEventListener("click", (e) => {
    if (e.target === $("import-mask")) closeImportDialog();
  });
  $("modal-close").onclick = closeModal;
  $("modal-mask").addEventListener("click", (e) => {
    if (e.target === $("modal-mask")) closeModal();
  });
  $("btn-save").onclick = saveEntry;
  $("save-global").onclick = saveGlobal;

  // 确认弹窗
  $("confirm-cancel").onclick = closeConfirm;
  $("confirm-ok").onclick = async () => {
    const action = confirmAction;
    closeConfirm();
    if (action) {
      try {
        await action();
      } catch (e) {
        toast("操作失败: " + e.message, true);
      }
    }
  };
  $("confirm-mask").addEventListener("click", (e) => {
    if (e.target === $("confirm-mask")) closeConfirm();
  });

  $("f-schedule-enabled").onchange = () => {
    refreshSchedulePanels();
    updateExampleLine();
  };
  $("f-mode").onchange = refreshSchedulePanels;
  document.querySelectorAll('input[name="trigger_span"]').forEach((r) => {
    r.addEventListener("change", refreshSchedulePanels);
  });
  $("btn-add-time").onclick = () => addTimeInput();
  $("btn-preview").onclick = previewSchedule;
  ["f-keywords", "f-position", "f-scope", "f-cooldown"].forEach((id) => {
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
  // 导入导出依赖宿主 bridge 的文件通道（旧版 AstrBot 可能缺失）
  if (typeof bridge.upload !== "function" || typeof bridge.download !== "function") {
    $("btn-import").disabled = true;
    $("btn-export").disabled = true;
    $("btn-import").title = "当前 AstrBot 版本不支持页面文件上传，请升级宿主";
    $("btn-export").title = "当前 AstrBot 版本不支持页面文件下载，请升级宿主";
  }
  try {
    await bridge.ready();
  } catch (e) {
    toast("桥接初始化失败: " + e.message, true);
  }
  await loadMeta();
  await loadEntries();
}

init();
