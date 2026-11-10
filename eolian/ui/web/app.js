/* Eolian Web UI 前端逻辑（vanilla JS，经 pywebview 桥接 Python） */
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  ready: false,
  busy: false,
  canCancel: false,
  championId: 0,
  currentSkinId: 0,
  selectedSkinId: 0,
  skins: [],
  catalogVersion: -1,
  lastCatalogVersion: -1,
  logCursor: 0,
  lastRenderSig: "",
  lastStatusText: "",
};

/* ---------------- 工具 ---------------- */

function setStatus(text, level) {
  if (text === state.lastStatusText) return;
  state.lastStatusText = text;
  const el = $("status-text");
  el.textContent = text;
  el.className = "status-text " + (level || "muted");
}

function setBusy(busy) {
  state.busy = busy;
  const btn = $("btn-swap");
  btn.textContent = busy ? "构建中…" : "开始换肤";
  updateSwapButton();
}

function updateSwapButton() {
  const skin = state.skins.find((s) => s.id === state.selectedSkinId);
  const btn = $("btn-swap");
  const usable =
    skin && !skin.is_base && skin.skin_num !== 0 && !state.busy && state.championId > 0;
  btn.disabled = !usable;
  const cancel = $("btn-cancel");
  if (cancel) cancel.classList.toggle("show", !!state.canCancel);
}

/* ---------------- 皮肤库渲染 ---------------- */

function renderSkins() {
  const grid = $("skin-grid");
  const query = $("search").value.trim().toLowerCase();
  const list = state.skins.filter(
    (s) => !query || s.name.toLowerCase().includes(query) || String(s.skin_num).includes(query)
  );

  // 数据没变化时跳过重绘（避免每 500ms 重建 DOM 导致闪烁）
  const sig = JSON.stringify([
    state.skins.map((s) => s.id),
    list.map((s) => s.id),
    state.currentSkinId,
    state.selectedSkinId,
    query,
  ]);
  if (sig === state.lastRenderSig) return;
  state.lastRenderSig = sig;

  grid.innerHTML = "";
  if (!list.length) {
    grid.innerHTML =
      '<div class="empty-state"><div class="empty-icon">✦</div><p>' +
      (state.championId > 0 ? "没有匹配的皮肤" : "进入英雄选择后，这里会加载该英雄的皮肤") +
      "</p></div>";
    return;
  }

  const frag = document.createDocumentFragment();
  for (const skin of list) {
    const card = document.createElement("div");
    card.className = "skin-card";
    if (skin.id === state.selectedSkinId) card.classList.add("selected");
    if (skin.id === state.currentSkinId) card.classList.add("current");
    card.dataset.skinId = skin.id;

    const chips = [];
    if (skin.id === state.currentSkinId) chips.push('<span class="chip current">当前</span>');
    if (skin.is_base) chips.push('<span class="chip base">基础</span>');
    else if (skin.id === state.currentSkinId) { /* 已有当前标记 */ }
    else chips.push('<span class="chip usable">可换</span>');

    card.innerHTML =
      '<div class="skin-card-head">' +
      '<span class="skin-name"></span>' +
      '<span class="skin-num">skin' + skin.skin_num + "</span>" +
      "</div>" +
      '<div class="skin-chips">' + chips.join("") + "</div>";
    card.querySelector(".skin-name").textContent = skin.name;

    card.addEventListener("click", () => selectSkin(skin.id));
    card.addEventListener("dblclick", () => {
      selectSkin(skin.id);
      doSwap();
    });
    frag.appendChild(card);
  }
  grid.appendChild(frag);
}

function selectSkin(skinId) {
  state.selectedSkinId = skinId;
  const skin = state.skins.find((s) => s.id === skinId);
  $("selected-name").textContent = skin ? skin.name : "未选择";
  $("selected-meta").textContent = skin
    ? "skin" + skin.skin_num + "  ·  ID " + skin.id + "  ·  " +
      (skin.is_base ? "基础皮肤" : "可替换皮肤") +
      (skin.id === state.currentSkinId ? "  ·  当前使用" : "")
    : "从皮肤库中选择一个皮肤";
  for (const el of document.querySelectorAll(".skin-card")) {
    el.classList.toggle("selected", Number(el.dataset.skinId) === skinId);
  }
  updateSwapButton();
}

/* ---------------- 日志 ---------------- */

function classifyLine(line) {
  if (line.includes("ERROR") || line.includes("失败")) return "l-error";
  if (line.includes("WARN") || line.includes("HINT")) return "l-warn";
  if (line.includes("构建完成") || line.includes("注入成功") || line.includes("已连接客户端"))
    return "l-ok";
  return "";
}

async function pullLogs() {
  if (!state.ready) return;
  try {
    const res = await window.pywebview.api.get_logs(state.logCursor);
    if (res && res.lines && res.lines.length) {
      const view = $("log-view");
      const frag = document.createDocumentFragment();
      for (const line of res.lines) {
        const span = document.createElement("span");
        span.className = classifyLine(line);
        span.textContent = line + "\n";
        frag.appendChild(span);
      }
      view.appendChild(frag);
      // 控制日志量：超过 2000 行时删掉最早的 800 行
      while (view.childNodes.length > 2000) {
        view.removeChild(view.firstChild);
      }
      view.scrollTop = view.scrollHeight;
      state.logCursor = res.next;
    }
  } catch (_) { /* 桥未就绪时忽略 */ }
}

/* ---------------- 轮询 ---------------- */

let pollTimer = null;
async function poll() {
  if (!state.ready) return;
  try {
    const snap = await window.pywebview.api.poll();
    if (!snap) return;

    // 连接与阶段
    const dot = $("conn-dot");
    dot.className = "dot " + (snap.connected ? "ok" : "idle");
    $("conn-text").textContent = snap.connected ? "客户端已连接" : "等待客户端";
    $("phase-text").textContent = "阶段：" + (snap.phase || "未知");

    // 英雄与皮肤状态
    const championChanged = snap.champion_id !== state.championId;
    state.championId = snap.champion_id;
    state.currentSkinId = snap.current_skin_id;

    if (snap.champion_id > 0) {
      $("champion-name").textContent = snap.champion_name || "…";
      $("skin-current").textContent = snap.current_skin_name || "…";
      $("skin-count").textContent = snap.skins_count + " 款可用皮肤";
      if (championChanged || snap.catalog_version !== state.catalogVersion) {
        state.catalogVersion = snap.catalog_version;
        try {
          const res = await window.pywebview.api.get_skins(snap.champion_id);
          state.skins = res.skins || [];
        } catch (_) { /* keep old */ }
        // 保持当前选择（若还存在）
        if (!state.skins.some((s) => s.id === state.selectedSkinId)) {
          const preferred =
            state.skins.find((s) => !s.is_base && s.id !== snap.current_skin_id) ||
            state.skins[0];
          state.selectedSkinId = preferred ? preferred.id : 0;
        }
        renderSkins();
        selectSkin(state.selectedSkinId);
      } else {
        renderSkins();
        selectSkin(state.selectedSkinId);
      }
    } else {
      $("champion-name").textContent = snap.in_champ_select ? "等待选择" : "未选择";
      $("skin-current").textContent = snap.in_champ_select ? "英雄尚未锁定" : "未进入选人阶段";
      $("skin-count").textContent = "0 款可用皮肤";
      if (state.skins.length) { state.skins = []; state.selectedSkinId = 0; renderSkins(); }
      $("selected-name").textContent = "未选择";
      $("selected-meta").textContent = snap.in_champ_select
        ? "英雄锁定后加载皮肤"
        : "从皮肤库中选择一个皮肤";
    }

    // 面板状态
    $("build-state").textContent = snap.build_text || "构建：等待操作";
    const injectEl = $("inject-state");
    injectEl.textContent = snap.inject_text || "注入：未启动";
    injectEl.className =
      "state-line" +
      (snap.inject_text && snap.inject_text.includes("成功") ? " ok"
        : snap.inject_text && snap.inject_text.includes("失败") ? " danger" : "");
    const omEl = $("overlay-mode");
    omEl.textContent = snap.overlay_mode || "本地覆盖：等待进入选人";
    omEl.className =
      "state-line" +
      (snap.overlay_mode && snap.overlay_mode.includes("可生效") ? " ok"
        : snap.overlay_mode && snap.overlay_mode.includes("改回基础") ? " danger" : " muted");

    // 忙碌与状态文本
    if (snap.busy !== state.busy) setBusy(snap.busy);
    const canCancel = !!snap.can_cancel;
    if (canCancel !== state.canCancel) {
      state.canCancel = canCancel;
      updateSwapButton();
    }
    if (snap.status_text !== undefined) setStatus(snap.status_text, snap.status_level);

    // 目录刷新按钮
    const catBtn = $("btn-catalog");
    if (snap.catalog_refreshing) {
      catBtn.disabled = true;
      catBtn.innerHTML = '<span class="btn-icon">⟳</span> 刷新中…';
    } else {
      catBtn.disabled = false;
      catBtn.innerHTML = '<span class="btn-icon">⟳</span> 刷新目录';
    }
  } catch (_) { /* 桥未就绪时忽略 */ }
}

/* ---------------- 交互 ---------------- */

async function doSwap() {
  if (state.busy || state.championId <= 0) return;
  const skin = state.skins.find((s) => s.id === state.selectedSkinId);
  if (!skin) { setStatus("请先在皮肤库中选择皮肤", "danger"); return; }
  try {
    const res = await window.pywebview.api.swap(skin.id);
    if (!res.ok) setStatus(res.message || "无法开始换肤", "danger");
  } catch (err) {
    setStatus("换肤调用失败：" + err, "danger");
  }
}

async function cancelSwap() {
  try {
    const res = await window.pywebview.api.cancel_swap();
    if (!res.ok) setStatus(res.message || "无法取消", "danger");
    else setStatus("正在取消本次构建…", "muted");
  } catch (err) {
    setStatus("取消失败：" + err, "danger");
  }
}

function toggleLogs(open) {
  const drawer = $("log-drawer");
  const backdrop = $("log-backdrop");
  const show = open !== undefined ? open : drawer.classList.contains("hidden");
  drawer.classList.toggle("hidden", !show);
  requestAnimationFrame(() => drawer.classList.toggle("open", show));
  backdrop.classList.toggle("hidden", !show);
  if (show) pullLogs();
}

function bindEvents() {
  $("btn-swap").addEventListener("click", doSwap);
  const cancelBtn = $("btn-cancel");
  if (cancelBtn) cancelBtn.addEventListener("click", cancelSwap);
  $("btn-catalog").addEventListener("click", async () => {
    try { await window.pywebview.api.refresh_catalog(); } catch (_) {}
  });
  $("btn-logs").addEventListener("click", () => toggleLogs());
  $("btn-log-close").addEventListener("click", () => toggleLogs(false));
  $("btn-log-clear").addEventListener("click", async () => {
    $("log-view").innerHTML = "";
    state.logCursor = await window.pywebview.api.clear_logs();
  });
  $("btn-log-folder").addEventListener("click", () => {
    window.pywebview.api.open_log_folder();
  });
  $("log-backdrop").addEventListener("click", () => toggleLogs(false));
  $("search").addEventListener("input", renderSkins);
  document.addEventListener("keydown", (e) => {
    if (e.key === "F12") { e.preventDefault(); toggleLogs(); }
    if (e.key === "Escape") toggleLogs(false);
  });
}

/* ---------------- 启动 ---------------- */

async function start() {
  bindEvents();
  try {
    const boot = await window.pywebview.api.bootstrap();
    state.catalogVersion = boot.catalog_version;
    setStatus(boot.status_text || "就绪", boot.status_level);
  } catch (err) {
    setStatus("初始化失败：" + err, "danger");
  }
  state.ready = true;
  pollTimer = setInterval(() => {
    poll();
    pullLogs();
  }, 500);
}

// pywebview 注入完成后启动
if (window.pywebview && window.pywebview.api) {
  start();
} else {
  window.addEventListener("pywebviewready", start);
}
